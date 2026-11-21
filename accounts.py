"""Supabase accounts: auth check, profiles, quota, video history, storage, cleanup.
Env: SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY (never commit values)."""
import os, time, json, datetime as dt, requests
import billing

URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
PUB = os.environ.get('SUPABASE_PUBLISHABLE_KEY', '')
SEC = os.environ.get('SUPABASE_SECRET_KEY', '')
GOOGLE_ENABLED = os.environ.get('GOOGLE_AUTH_ENABLED', '0').lower() in ('1', 'true', 'yes')
BUCKET = 'videos'
KEEP_DAYS = 7            # storage files older than this are deleted (free 1 GB)
SIGNED_URL_TTL = 3600    # seconds
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))  # daily quota resets at IST midnight

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

REST = f'{URL}/rest/v1'

def _rest(method, path, **kw):
    r = requests.request(method, f'{REST}/{path}', headers=_admin(kw.pop('headers', None)), timeout=20, **kw)
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

# ---------------- plans + entitlement (Phase 3) ----------------
PLANS_TTL = 60          # seconds; price/limit edits in Supabase show up within this time
_plans = {'at': 0.0, 'data': None, 'ready': False}
UPGRADE = ' प्लान देखें / See plans: /plans'

def plans(force=False):
    """All plans by id from table public.plans (cached 60 s). Until the Phase 3 SQL is run the table is
    missing: then billing.DEFAULT_PLANS are used and db_ready() is False (payments stay off)."""
    if force or not _plans['data'] or time.time() - _plans['at'] > PLANS_TTL:
        try:
            rows = _rest('GET', 'plans?select=*&order=sort.asc,id.asc')
            _plans.update(data=billing.index(rows or billing.DEFAULT_PLANS), ready=True)
        except Exception as e:
            if not _plans['ready']: _plans['data'] = billing.index(billing.DEFAULT_PLANS)
            print('plans: using', 'cached' if _plans['ready'] else 'built-in defaults', '-', str(e)[:120], flush=True)
        _plans['at'] = time.time()
    return _plans['data']

def db_ready(): plans(); return _plans['ready']
def payments_enabled(): return billing.keys_set() and db_ready()

def _q(v): return requests.utils.quote(str(v), safe='')
def _f(x): return f'{billing.num(x):g}'

def _cas_profile(old, new):
    """Update the entitlement fields only if they are still what we read (no lost updates)."""
    q = f'profiles?id=eq.{old["id"]}&plan=eq.{_q(old.get("plan") or "free")}' \
        f'&minutes_left=eq.{_f(old.get("minutes_left"))}&bonus_minutes=eq.{_f(old.get("bonus_minutes"))}'
    q += f'&plan_expires_at=eq.{_q(old["plan_expires_at"])}' if old.get('plan_expires_at') else '&plan_expires_at=is.null'
    rows = _rest('PATCH', q, json=new, headers={'Prefer': 'return=representation'})
    return bool(rows)

def entitlement(user):
    """(profile with current entitlement, plan row). Expired pass -> free; yearly pass refills monthly."""
    p = profile(user); P = plans()
    plan, s, changed = billing.refresh(p, P)
    if changed and _plans['ready']:
        try: _cas_profile(p, s)
        except Exception as e: print('entitlement refresh failed', str(e)[:120], flush=True)
    return {**p, **s}, plan

def _today_start():
    now = dt.datetime.now(IST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def used_today(uid):
    rows = _rest('GET', f'videos?user_id=eq.{uid}&created_at=gte.{_today_start()}&status=not.in.(error,cancelled)&select=id')
    return len(rows or [])

def _effective(plan, P, bonus):
    """Limits shown in the UI: the pass, or free (+ pack limits when pack minutes are left)."""
    if plan['id'] != 'free': return plan['max_video_minutes'], plan['styles'], plan['resolution']
    free, pack = P['free'], P.get('pack')
    if bonus > 0 and pack:
        st = None if (not free['styles'] or not pack['styles']) else sorted(set(free['styles']) | set(pack['styles']))
        return max(free['max_video_minutes'], pack['max_video_minutes']), st, pack['resolution']
    return free['max_video_minutes'], free['styles'], free['resolution']

def usage(user):
    p, plan = entitlement(user); P = plans(); paid = plan['id'] != 'free'
    bonus = billing.num(p.get('bonus_minutes'))
    mx, styles, res = _effective(plan, P, bonus)
    return {'email': user.get('email'), 'plan': plan['id'], 'plan_name_hi': plan['name_hi'], 'plan_name_en': plan['name_en'],
            'plan_expires_at': p.get('plan_expires_at') if paid else None,
            'minutes_left': billing.num(p.get('minutes_left')) if paid else 0, 'bonus_minutes': bonus,
            'period_resets_at': p.get('period_resets_at') if paid else None,
            'daily_limit': P['free']['daily_videos'], 'used_today': used_today(user['id']),
            'max_minutes': mx, 'styles': styles, 'resolution': res,
            'both_langs_one_charge': plan['both_langs_one_charge'], 'payments_enabled': payments_enabled()}

def check_quota(user, minutes, style='educational', langs='hi,en'):
    """Returns (ok, message, profile_row, ctx). ctx: mode free|minutes, plan, priority, resolution, cost."""
    p, plan = entitlement(user); P = plans(); free = P['free']
    running = _rest('GET', f'videos?user_id=eq.{user["id"]}&status=in.(queued,running)&select=id')
    if running:
        return False, 'आपका एक वीडियो पहले से बन रहा है, उसके पूरा होने तक रुकिए। / One of your videos is already being made — please wait for it to finish.', p, None
    paid = plan['id'] != 'free'
    left = billing.num(p.get('minutes_left')) if paid else 0.0
    bonus = billing.num(p.get('bonus_minutes'))
    fits_free = minutes <= free['max_video_minutes'] + 1e-9 and billing.style_ok(free, style)
    def daily_ok():
        return free['daily_videos'] is None or used_today(user['id']) < free['daily_videos']
    if paid:
        lim, mode = plan, 'minutes'
        if left + bonus + 1e-9 < billing.job_cost(plan, minutes, langs) and fits_free and daily_ok():
            lim, mode = free, 'free'                 # out of minutes: today's free videos still work
    elif fits_free and daily_ok():
        lim, mode = free, 'free'
    elif bonus > 0 and P.get('pack'):
        lim, mode = P['pack'], 'minutes'             # pack minutes, pack limits
    elif not fits_free:
        lim, mode = free, 'free'
    else:
        return False, (f'आज की मुफ़्त सीमा ({free["daily_videos"]} वीडियो/दिन) पूरी हो गई। कल फिर आइए या प्लान लीजिए। / '
                       f'Daily free limit reached ({free["daily_videos"]} videos/day). Come back tomorrow or get a plan.' + UPGRADE), p, None
    name = f'{lim["name_hi"]} / {lim["name_en"]}'
    if minutes > lim['max_video_minutes'] + 1e-9:
        return False, (f'आपके प्लान ({name}) में अधिकतम {lim["max_video_minutes"]:g} मिनट का वीडियो बन सकता है। / '
                       f'Your plan ({lim["name_en"]}) allows videos up to {lim["max_video_minutes"]:g} min.' + UPGRADE), p, None
    if not billing.style_ok(lim, style):
        return False, (f'"{style}" स्टाइल आपके प्लान ({name}) में नहीं है। / The "{style}" style is not in your plan ({lim["name_en"]}).'
                       + UPGRADE), p, None
    cost = 0.0
    if mode == 'minutes':
        cost = billing.job_cost(lim, minutes, langs)
        if left + bonus + 1e-9 < cost:
            return False, (f'मिनट कम हैं: बचे {left + bonus:g}, इस वीडियो के लिए चाहिए {cost:g} (हर भाषा अलग गिनी जाती है)। / '
                           f'Not enough minutes: {left + bonus:g} left, this video needs {cost:g} (each language counts).' + UPGRADE), p, None
    return True, '', p, {'mode': mode, 'plan': lim['id'], 'priority': plan['priority'] if paid else lim['priority'],
                         'resolution': lim['resolution'], 'cost': cost}

def spend(uid, cost):
    """Take `cost` minutes (period minutes first, then pack minutes). Returns (from_plan, from_bonus) or None."""
    for _ in range(5):
        rows = _rest('GET', f'profiles?id=eq.{uid}&select=minutes_left,bonus_minutes')
        if not rows: return None
        left, bonus = billing.num(rows[0].get('minutes_left')), billing.num(rows[0].get('bonus_minutes'))
        sp = billing.split_spend(left, bonus, cost)
        if not sp: return None
        got = _rest('PATCH', f'profiles?id=eq.{uid}&minutes_left=eq.{_f(left)}&bonus_minutes=eq.{_f(bonus)}',
                    json={'minutes_left': billing.num(left - sp[0]), 'bonus_minutes': billing.num(bonus - sp[1])},
                    headers={'Prefer': 'return=representation'})
        if got: return sp
        time.sleep(0.2)
    return None

def give_back(uid, from_plan, from_bonus):
    """Return minutes (failed/cancelled video). If the pass has ended meanwhile, they go to pack minutes."""
    from_plan, from_bonus = billing.num(from_plan), billing.num(from_bonus)
    if from_plan <= 0 and from_bonus <= 0: return True
    for _ in range(5):
        rows = _rest('GET', f'profiles?id=eq.{uid}&select=plan,minutes_left,bonus_minutes')
        if not rows: return False
        r = rows[0]; left, bonus = billing.num(r.get('minutes_left')), billing.num(r.get('bonus_minutes'))
        if (r.get('plan') or 'free') == 'free': new = {'minutes_left': left, 'bonus_minutes': billing.num(bonus + from_plan + from_bonus)}
        else: new = {'minutes_left': billing.num(left + from_plan), 'bonus_minutes': billing.num(bonus + from_bonus)}
        got = _rest('PATCH', f'profiles?id=eq.{uid}&minutes_left=eq.{_f(left)}&bonus_minutes=eq.{_f(bonus)}',
                    json=new, headers={'Prefer': 'return=representation'})
        if got: return True
        time.sleep(0.2)
    return False

def refund_job(job):
    """Give a failed/cancelled job's minutes back, once (jobs.refunded flips false -> true first)."""
    try:
        if not db_ready() or billing.num(job.get('cost_minutes')) <= 0: return False
        rows = _rest('PATCH', f'jobs?id=eq.{job["id"]}&refunded=eq.false', json={'refunded': True},
                     headers={'Prefer': 'return=representation'})
        if not rows: return False
        j = rows[0]
        return give_back(j['user_id'], j.get('from_plan'), j.get('from_bonus'))
    except Exception as e:
        print('refund failed', job.get('id'), str(e)[:150], flush=True); return False

def refund_failed():
    """Safety net (worker loop): refund every failed/cancelled job that still holds minutes."""
    if not db_ready(): return 0
    rows = _rest('GET', 'jobs?status=in.(error,cancelled)&refunded=eq.false&cost_minutes=gt.0&select=*&limit=50') or []
    return sum(1 for j in rows if refund_job(j))

# ---------------- orders (Razorpay) ----------------
def order_new(user, plan):
    rows = _rest('POST', 'orders', json={'user_id': user['id'], 'email': user.get('email'), 'plan_id': plan['id'],
                 'amount_paise': plan['price_paise'], 'currency': 'INR', 'status': 'created'},
                 headers={'Prefer': 'return=representation'})
    return rows[0]

def order_set(oid, **fields): _rest('PATCH', f'orders?id=eq.{oid}', json=fields)

def order_by_rzp(rzp_order_id):
    rows = _rest('GET', f'orders?razorpay_order_id=eq.{_q(rzp_order_id)}&select=*')
    return rows[0] if rows else None

def activate(order, payment_id, via):
    """Give the bought plan for a paid order. Idempotent: returns 'ok' the first time, 'already' after."""
    P = plans(); buy = P.get(order['plan_id']) or billing.index(billing.DEFAULT_PLANS).get(order['plan_id'])
    if not buy: return 'no_plan'
    for _ in range(5):
        rows = _rest('GET', f'profiles?id=eq.{order["user_id"]}&select=*')
        p = rows[0] if rows else profile({'id': order['user_id'], 'email': order.get('email')})
        expect = {k: p.get(k) for k in ('plan', 'plan_expires_at', 'minutes_left', 'bonus_minutes')}
        new = billing.apply_purchase(p, buy, P)
        res = rpc('commit_activation', p_order=order['id'], p_payment=payment_id, p_via=via, p_expect=expect, p_new=new)
        if res != 'retry': return res
        time.sleep(0.2)
    return 'retry'

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

def enqueue(user, p, topic, minutes, style, langs, ctx=None, spent=(0, 0)):
    """Create the history row + queued job. Returns the job row. The DB allows one active job per user."""
    ctx = ctx or {'plan': 'free', 'priority': 0, 'resolution': 720, 'cost': 0}
    vid = new_video(user['id'], topic, langs, style, minutes)
    row = {'user_id': user['id'], 'video_id': vid, 'topic': topic[:500], 'minutes': minutes, 'style': style,
           'langs': langs, 'plan': ctx['plan'], 'priority': ctx['priority'], 'step': 'queued'}
    if db_ready():
        row.update(resolution=ctx['resolution'], cost_minutes=ctx['cost'], from_plan=spent[0], from_bonus=spent[1])
    try:
        rows = _rest('POST', 'jobs', json=row, headers={'Prefer': 'return=representation'})
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
    if rows: refund_job(rows[0])
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
