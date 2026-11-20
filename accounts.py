"""Supabase accounts: auth check, profiles, quota, video history, storage, cleanup.
Env: SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY (never commit values)."""
import os, time, datetime as dt, requests

URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
PUB = os.environ.get('SUPABASE_PUBLISHABLE_KEY', '')
SEC = os.environ.get('SUPABASE_SECRET_KEY', '')
GOOGLE_ENABLED = os.environ.get('GOOGLE_AUTH_ENABLED', '0').lower() in ('1', 'true', 'yes')
BUCKET = 'videos'
KEEP_DAYS = 7            # storage files older than this are deleted (free 1 GB)
SIGNED_URL_TTL = 3600    # seconds
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))  # daily quota resets at IST midnight

# Plan limits. 'credits' plans spend 1 credit per video once the daily allowance is used.
# 'priority': queue order (higher first). Paid plans are not sold yet; the column is ready.
PLANS = {
    'free':    {'daily': 2,  'max_minutes': 1,  'use_credits': False, 'priority': 0},
    'pro':     {'daily': 10, 'max_minutes': 5,  'use_credits': True,  'priority': 10},
    'premium': {'daily': 30, 'max_minutes': 10, 'use_credits': True,  'priority': 20},
}

def configured(): return bool(URL and PUB and SEC)
def server_configured(): return bool(URL and SEC)      # enough for a render worker
def _now(): return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

def _admin(extra=None):
    h = {'apikey': SEC, 'User-Agent': 'wisorbit-video-agent/1.0'}
    if SEC.startswith('eyJ'): h['Authorization'] = 'Bearer ' + SEC   # legacy service_role JWT
    if extra: h.update(extra)
    return h

def get_user(token):
    """Verify a Supabase access token; returns {'id','email'} or None."""
    if not (token and URL): return None
    try:
        r = requests.get(f'{URL}/auth/v1/user', headers={'apikey': PUB, 'Authorization': 'Bearer ' + token}, timeout=10)
        if r.status_code == 200:
            u = r.json(); return {'id': u['id'], 'email': u.get('email')}
    except Exception as e:
        print('auth check failed', e)
    return None

class DBError(RuntimeError):
    def __init__(self, msg, status=0, code=''): super().__init__(msg); self.status = status; self.code = code

def _rest(method, path, **kw):
    r = requests.request(method, f'{URL}/rest/v1/{path}', headers=_admin(kw.pop('headers', None)), timeout=20, **kw)
    if r.status_code >= 300:
        code = ''
        try: code = r.json().get('code', '')
        except Exception: pass
        raise DBError(f'db {method} {path.split("?")[0]} {r.status_code}: {r.text[:200]}', r.status_code, code)
    return r.json() if r.text else None

def rpc(name, **args): return _rest('POST', f'rpc/{name}', json=args)

def profile(user):
    rows = _rest('GET', f'profiles?id=eq.{user["id"]}&select=*')
    if rows: return rows[0]
    rows = _rest('POST', 'profiles?on_conflict=id', json={'id': user['id'], 'email': user.get('email')},
                 headers={'Prefer': 'return=representation,resolution=merge-duplicates'})
    return rows[0]

def _today_start():
    now = dt.datetime.now(IST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def used_today(uid):
    rows = _rest('GET', f'videos?user_id=eq.{uid}&created_at=gte.{_today_start()}&status=not.in.(error,cancelled)&select=id')
    return len(rows or [])

def usage(user):
    p = profile(user); plan = PLANS.get(p.get('plan') or 'free', PLANS['free'])
    return {'email': user.get('email'), 'plan': p.get('plan') or 'free', 'credits': p.get('credits') or 0,
            'daily_limit': plan['daily'], 'used_today': used_today(user['id']), 'max_minutes': plan['max_minutes']}

def check_quota(user, minutes):
    """Returns (ok, message, profile_row)."""
    p = profile(user); name = p.get('plan') or 'free'; plan = PLANS.get(name, PLANS['free'])
    if minutes > plan['max_minutes'] + 1e-9:
        return False, (f'आपके {name} प्लान में अधिकतम {plan["max_minutes"]} मिनट का वीडियो बन सकता है। / '
                       f'Your {name} plan allows videos up to {plan["max_minutes"]} min.'), p
    running = _rest('GET', f'videos?user_id=eq.{user["id"]}&status=in.(queued,running)&select=id')
    if running:
        return False, 'आपका एक वीडियो पहले से बन रहा है, उसके पूरा होने तक रुकिए। / One of your videos is already being made — please wait for it to finish.', p
    used = used_today(user['id'])
    if used >= plan['daily']:
        if plan['use_credits'] and (p.get('credits') or 0) > 0: return True, 'credit', p
        return False, (f'आज की मुफ़्त सीमा ({plan["daily"]} वीडियो/दिन) पूरी हो गई। कल फिर आइए। / '
                       f'Daily limit reached ({plan["daily"]} videos/day on the {name} plan). Please come back tomorrow.'), p
    return True, '', p

def spend_credit(p):
    _rest('PATCH', f'profiles?id=eq.{p["id"]}', json={'credits': max(0, (p.get('credits') or 0) - 1)})

def new_video(uid, topic, lang, style, minutes):
    rows = _rest('POST', 'videos', json={'user_id': uid, 'topic': topic[:500], 'lang': lang, 'style': style,
                 'duration_sec': int(minutes * 60), 'status': 'queued'}, headers={'Prefer': 'return=representation'})
    return rows[0]['id']

def update_video(vid, **fields): _rest('PATCH', f'videos?id=eq.{vid}', json=fields)

def upload(uid, vid, local, tries=3):
    path = f'{uid}/{vid}_{os.path.basename(local)}'
    for k in range(tries):
        try:
            with open(local, 'rb') as f:
                r = requests.post(f'{URL}/storage/v1/object/{BUCKET}/{path}', data=f, timeout=600,
                                  headers=_admin({'Content-Type': 'video/mp4', 'x-upsert': 'true'}))
            if r.status_code < 300: return path
            err = f'storage upload {r.status_code}: {r.text[:200]}'
        except requests.RequestException as e: err = f'storage upload: {e}'
        print(err, flush=True); time.sleep(5 * (k + 1))
    raise RuntimeError(err)

def sign(path):
    r = requests.post(f'{URL}/storage/v1/object/sign/{BUCKET}/{path}', json={'expiresIn': SIGNED_URL_TTL},
                      headers=_admin(), timeout=20)
    if r.status_code >= 300: return None
    u = r.json().get('signedURL') or r.json().get('signedUrl')
    return (URL + '/storage/v1' + u) if u and u.startswith('/') else u

def history(uid, limit=30):
    rows = _rest('GET', f'videos?user_id=eq.{uid}&select=*&order=created_at.desc&limit={limit}') or []
    for v in rows:
        v['files'] = []
        for p in [x for x in (v.get('storage_path') or '').split(',') if x]:
            lang = 'hi' if p.endswith('_hi.mp4') else 'en' if p.endswith('_en.mp4') else ''
            v['files'].append({'lang': lang, 'url': sign(p), 'name': p.split('/')[-1]})
    return rows

def cleanup():
    """Delete stored files older than KEEP_DAYS and mark rows expired; mark stale running jobs as error."""
    if not configured(): return
    try:
        cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        old = _rest('GET', f'videos?created_at=lt.{cut}&storage_path=not.is.null&select=id,storage_path') or []
        for v in old:
            paths = [x for x in v['storage_path'].split(',') if x]
            requests.delete(f'{URL}/storage/v1/object/{BUCKET}', json={'prefixes': paths}, headers=_admin(), timeout=30)
            update_video(v['id'], storage_path=None, status='expired')
        if old: print(f'cleanup: expired {len(old)} videos')
    except Exception as e:
        print('cleanup failed', e)

# ---------------- render job queue (table public.jobs) ----------------
ONE_ACTIVE_MSG = ('आपका एक वीडियो पहले से लाइन में है या बन रहा है, उसके पूरा होने तक रुकिए। / '
                  'One of your videos is already in line or being made — please wait for it to finish.')

def enqueue(user, p, topic, minutes, style, langs):
    """Create the history row + queued job. Returns the job row. The DB allows one active job per user."""
    plan = p.get('plan') or 'free'
    vid = new_video(user['id'], topic, langs, style, minutes)
    try:
        rows = _rest('POST', 'jobs', json={'user_id': user['id'], 'video_id': vid, 'topic': topic[:500], 'minutes': minutes,
                     'style': style, 'langs': langs, 'plan': plan, 'priority': PLANS.get(plan, PLANS['free'])['priority'],
                     'step': 'queued'}, headers={'Prefer': 'return=representation'})
    except DBError as e:
        _rest('DELETE', f'videos?id=eq.{vid}')
        if e.code == '23505': raise ValueError(ONE_ACTIVE_MSG)
        raise
    return rows[0]

def get_job(jid, uid=None):
    q = f'jobs?id=eq.{jid}&select=*' + (f'&user_id=eq.{uid}' if uid else '')
    rows = _rest('GET', q); return rows[0] if rows else None

def active_job(uid):
    rows = _rest('GET', f'jobs?user_id=eq.{uid}&status=in.(queued,running)&select=id&limit=1')
    return rows[0]['id'] if rows else None

def queue_info(jid): return rpc('job_queue_info', p_job=jid) or {}

def queue_stats():
    q = _rest('GET', 'jobs?status=eq.queued&select=id') or []
    r = _rest('GET', 'jobs?status=eq.running&select=id') or []
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
    w = _rest('GET', f'workers?last_seen=gt.{cut}&select=id,host,current_job,jobs_done') or []
    return {'queued': len(q), 'running': len(r), 'workers': len(w)}

def job_update(jid, **fields): _rest('PATCH', f'jobs?id=eq.{jid}', json=fields)

def cancel_job(jid, uid):
    rows = _rest('PATCH', f'jobs?id=eq.{jid}&user_id=eq.{uid}&status=eq.queued',
                 json={'status': 'cancelled', 'step': 'cancelled', 'finished_at': _now()}, headers={'Prefer': 'return=representation'})
    if rows and rows[0].get('video_id'): update_video(rows[0]['video_id'], status='cancelled')
    return bool(rows)

def claim_job(worker_id):
    rows = rpc('claim_job', p_worker=worker_id); return rows[0] if rows else None

def reap_stale(timeout_sec=300):
    n = rpc('reap_stale_jobs', p_timeout_sec=timeout_sec)
    if n: print(f'queue: requeued/failed {n} stale jobs', flush=True)
    return n

def worker_register(wid, host):
    _rest('POST', 'workers?on_conflict=id', json={'id': wid, 'host': host, 'last_seen': _now(), 'current_job': None},
          headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})

def worker_beat(wid, job=None):
    _rest('PATCH', f'workers?id=eq.{wid}', json={'last_seen': _now(), 'current_job': job})

def worker_done(wid):
    rows = _rest('GET', f'workers?id=eq.{wid}&select=jobs_done') or [{}]
    _rest('PATCH', f'workers?id=eq.{wid}', json={'jobs_done': (rows[0].get('jobs_done') or 0) + 1, 'current_job': None, 'last_seen': _now()})

def worker_remove(wid):
    try: _rest('DELETE', f'workers?id=eq.{wid}')
    except Exception: pass

def prune_workers(days=1):
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try: _rest('DELETE', f'workers?last_seen=lt.{cut}')
    except Exception as e: print('prune_workers failed', e)
