"""WisOrbit AI Video Agent - web app. Run: uvicorn app:app --host 0.0.0.0 --port 7860
Jobs go into the Supabase queue (table public.jobs); workers (worker.py) render them.
WORKER_IN_WEB=1 (default) also runs one worker inside this web process; set 0 when Oracle/other workers run."""
import os, re, math, threading, traceback
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import accounts

app = FastAPI(title='WisOrbit AI Video Agent')
WORKER_IN_WEB = os.environ.get('WORKER_IN_WEB', '1').lower() in ('1', 'true', 'yes')
WEB_WORKERS = int(os.environ.get('WEB_WORKERS', '1'))
# seconds of render time per minute of video per language, used until real job history exists
DEFAULT_SEC_PER_MIN = float(os.environ.get('RENDER_SEC_PER_MIN', '900'))
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

class Req(BaseModel):
    topic: str; minutes: float = 1; style: str = 'educational'; langs: str = 'hi,en'

def auth(authorization):
    if not accounts.configured(): raise HTTPException(503, 'लॉगिन अभी सेट नहीं है / Login is not configured on the server yet.')
    tok = (authorization or '').removeprefix('Bearer ').strip()
    u = accounts.get_user(tok)
    if not u: raise HTTPException(401, 'कृपया लॉगिन करें / Please log in first.')
    return u

@app.exception_handler(Exception)
def oops(req, exc):
    traceback.print_exc()
    return JSONResponse({'detail': 'सर्वर में दिक्कत, थोड़ी देर बाद कोशिश करें / Server error, please try again shortly.'}, status_code=500)

@app.on_event('startup')
def startup():
    if not accounts.server_configured(): return
    threading.Thread(target=accounts.cleanup, daemon=True).start()
    if WORKER_IN_WEB:
        import worker; worker.start_in_background(WEB_WORKERS)

@app.on_event('shutdown')
def shutdown():
    if WORKER_IN_WEB:   # a job in progress is put back in line by the worker thread (JobAborted) or by the reaper
        import worker; worker.stop_all()

@app.get('/api/config')
def config():
    return {'supabase_url': accounts.URL, 'publishable_key': accounts.PUB, 'google_enabled': accounts.GOOGLE_ENABLED,
            'login_enabled': accounts.configured()}

@app.get('/api/health')
def health():
    out = {'ok': True, 'worker_in_web': WORKER_IN_WEB}
    try: out.update(accounts.queue_stats())
    except Exception as e: out.update(ok=False, error=str(e)[:100])
    return out

@app.get('/api/me')
def me(authorization: str | None = Header(None)):
    u = auth(authorization); d = accounts.usage(u); d['active_job'] = accounts.active_job(u['id']); return d

@app.get('/api/videos')
def videos(authorization: str | None = Header(None)):
    return accounts.history(auth(authorization)['id'])

@app.post('/api/generate')
def gen(r: Req, authorization: str | None = Header(None)):
    user = auth(authorization)
    r.topic = r.topic.strip()[:500]
    if not r.topic: raise HTTPException(400, 'विषय लिखिए / Please enter a topic.')
    r.minutes = round(min(max(r.minutes, 0.5), 10) * 2) / 2
    if r.langs not in ('hi,en', 'hi', 'en'): r.langs = 'hi,en'
    if r.style not in ('educational', 'cinematic', 'realistic', 'youtube'): r.style = 'educational'
    ok, msg, p = accounts.check_quota(user, r.minutes)
    if not ok: raise HTTPException(429, msg)
    try: job = accounts.enqueue(user, p, r.topic, r.minutes, r.style, r.langs)
    except ValueError as e: raise HTTPException(429, str(e))
    if msg == 'credit': accounts.spend_credit(p)
    return {'id': job['id'], 'video_id': job['video_id']}

def _work(job): return float(job.get('minutes') or 1) * (2 if ',' in (job.get('langs') or '') else 1)

@app.get('/api/job/{jid}')
def job_status(jid: str, authorization: str | None = Header(None)):
    user = auth(authorization)
    if not UUID.match(jid): raise HTTPException(404, 'not found')
    j = accounts.get_job(jid, user['id'])
    if not j: raise HTTPException(404, 'not found')
    out = {'id': j['id'], 'status': j['status'], 'step': j.get('step') or '', 'progress': j.get('progress') or 0,
           'topic': j['topic'], 'minutes': float(j['minutes']), 'langs': j['langs'], 'error': j.get('error')}
    if j['status'] in ('queued', 'running'):
        info = accounts.queue_info(jid)
        spm = float(info.get('sec_per_min') or DEFAULT_SEC_PER_MIN)
        workers = int(info.get('workers') or 0)
        mine = _work(j) * spm
        if j['status'] == 'queued':
            pos = int(info.get('position') or 1)
            wait = (float(info.get('ahead_work') or 0) + float(info.get('running_left') or 0)) * spm / max(1, workers)
            out.update(position=pos, workers=workers, wait_min=math.ceil(wait / 60), done_in_min=math.ceil((wait + mine) / 60))
        else:
            left = mine * (100 - out['progress']) / 100
            out.update(position=0, workers=workers, done_in_min=max(1, math.ceil(left / 60)))
    if j['status'] == 'done':
        out['files'] = [{'lang': 'hi' if p.endswith('_hi.mp4') else 'en', 'url': accounts.sign(p)}
                        for p in (j.get('files') or '').split(',') if p]
    return out

@app.post('/api/job/{jid}/cancel')
def job_cancel(jid: str, authorization: str | None = Header(None)):
    user = auth(authorization)
    if not UUID.match(jid): raise HTTPException(404, 'not found')
    if not accounts.cancel_job(jid, user['id']):
        raise HTTPException(409, 'अब रद्द नहीं हो सकता (बनना शुरू हो गया) / Cannot cancel now — rendering has started.')
    return {'ok': True}

app.mount('/', StaticFiles(directory='static', html=True), name='static')
