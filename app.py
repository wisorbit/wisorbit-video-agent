"""WisOrbit AI Video Agent - web app. Run: uvicorn app:app --host 0.0.0.0 --port 7860
Jobs go into the Supabase queue (table public.jobs); workers (worker.py) render them.
WORKER_IN_WEB=1 (default) also runs one worker inside this web process; set 0 when Oracle/other workers run."""
import os, re, math, json, threading, traceback
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import accounts, billing

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
    ok, msg, p, ctx = accounts.check_quota(user, r.minutes, r.style, r.langs)
    if not ok: raise HTTPException(429, msg)
    spent = (0, 0)
    if ctx['cost'] > 0:
        spent = accounts.spend(user['id'], ctx['cost'])
        if not spent: raise HTTPException(429, 'मिनट कम हैं / Not enough minutes.' + accounts.UPGRADE)
    try: job = accounts.enqueue(user, p, r.topic, r.minutes, r.style, r.langs, ctx, spent)
    except BaseException as e:
        if ctx['cost'] > 0: accounts.give_back(user['id'], *spent)
        if isinstance(e, ValueError): raise HTTPException(429, str(e))
        raise
    return {'id': job['id'], 'video_id': job['video_id'], 'cost_minutes': ctx['cost']}

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

# ---------------- plans + Razorpay payments (Phase 3) ----------------
@app.get('/api/plans')
def plans_list():
    P = accounts.plans() if accounts.server_configured() else billing.index(billing.DEFAULT_PLANS)
    rows = sorted((p for p in P.values() if p['active']), key=lambda p: (p['sort'], p['id']))
    on = accounts.server_configured() and accounts.payments_enabled()
    return {'plans': [billing.public(p) for p in rows], 'payments_enabled': on, 'key_id': billing.KEY_ID if on else ''}

class BuyReq(BaseModel):
    plan: str

COMING_SOON = 'पेमेंट जल्द आ रहा है / Payments coming soon.'

@app.post('/api/pay/order')
def pay_order(b: BuyReq, authorization: str | None = Header(None)):
    user = auth(authorization)
    if not accounts.payments_enabled(): raise HTTPException(503, COMING_SOON)
    plan = accounts.plans().get(b.plan)
    if not plan or not plan['active'] or plan['kind'] == 'free' or plan['price_paise'] <= 0:
        raise HTTPException(400, 'यह प्लान उपलब्ध नहीं / This plan is not available.')
    order = accounts.order_new(user, plan)            # amount comes from the DB, never from the browser
    try:
        rz = billing.create_order(plan['price_paise'], order['id'], {'user_id': user['id'], 'plan': plan['id'], 'order': order['id']})
    except Exception as e:
        accounts.order_set(order['id'], status='failed', error=str(e)[:300])
        raise HTTPException(502, 'पेमेंट शुरू नहीं हो पाया, थोड़ी देर बाद कोशिश करें / Could not start the payment, please try again shortly.')
    accounts.order_set(order['id'], razorpay_order_id=rz['id'])
    return {'key_id': billing.KEY_ID, 'order_id': rz['id'], 'amount': plan['price_paise'], 'currency': 'INR',
            'name': 'WisOrbit AI Video Agent', 'description': f"{plan['name_en']} / {plan['name_hi']}",
            'email': user.get('email') or ''}

class VerifyReq(BaseModel):
    razorpay_order_id: str; razorpay_payment_id: str; razorpay_signature: str

@app.post('/api/pay/verify')
def pay_verify(v: VerifyReq, authorization: str | None = Header(None)):
    user = auth(authorization)
    if not accounts.payments_enabled(): raise HTTPException(503, COMING_SOON)
    if not billing.verify_payment(v.razorpay_order_id, v.razorpay_payment_id, v.razorpay_signature):
        raise HTTPException(400, 'पेमेंट की जाँच नहीं हो पाई / Payment could not be verified.')
    order = accounts.order_by_rzp(v.razorpay_order_id)
    if not order or order['user_id'] != user['id']: raise HTTPException(404, 'order not found')
    res = accounts.activate(order, v.razorpay_payment_id, 'verify')
    if res not in ('ok', 'already'):
        print('activation problem', order['id'], res, flush=True)
        raise HTTPException(500, 'पेमेंट मिला, प्लान चालू करने में दिक्कत — हम जल्द ठीक करेंगे / Payment received; activating the plan failed — we will fix it shortly.')
    out = accounts.usage(user); out['result'] = res; return out

def handle_webhook(body: bytes, signature: str):
    """Returns (http_status, json). Duplicate deliveries are safe: activation happens once."""
    if not billing.WEBHOOK_SECRET: return 503, {'ok': False, 'detail': 'webhook secret not set'}
    if not billing.verify_webhook(body, signature): return 400, {'ok': False, 'detail': 'bad signature'}
    if not accounts.db_ready(): return 503, {'ok': False, 'detail': 'plans tables not set up yet'}   # Razorpay retries
    try: ev = json.loads(body)
    except Exception: return 400, {'ok': False, 'detail': 'bad json'}
    info = billing.parse_webhook(ev)
    if not info: return 200, {'ok': True, 'ignored': ev.get('event')}
    order = accounts.order_by_rzp(info['order_id'])
    if not order: return 200, {'ok': True, 'ignored': 'unknown order'}
    if info['amount'] != int(order['amount_paise']) or info['currency'] != 'INR':
        accounts.order_set(order['id'], error=f"amount mismatch {info['amount']} {info['currency']}")
        return 200, {'ok': True, 'ignored': 'amount mismatch'}
    res = accounts.activate(order, info['payment_id'], 'webhook')
    if res not in ('ok', 'already'): return 500, {'ok': False, 'result': res}   # Razorpay retries later
    return 200, {'ok': True, 'result': res}

@app.post('/api/razorpay/webhook')
async def razorpay_webhook(request: Request):
    body = await request.body()
    code, out = await run_in_threadpool(handle_webhook, body, request.headers.get('x-razorpay-signature', ''))
    return JSONResponse(out, status_code=code)

# simple pages (Razorpay website requirements) without the .html in the address
PAGES = {'plans': 'plans.html', 'terms': 'terms.html', 'privacy': 'privacy.html', 'refund': 'refund.html',
         'shipping': 'shipping.html', 'contact': 'contact.html'}

def _page_route(fname):
    return lambda: FileResponse(os.path.join('static', fname), media_type='text/html')
for _slug, _file in PAGES.items():
    app.add_api_route(f'/{_slug}', _page_route(_file), methods=['GET'], include_in_schema=False)

app.mount('/', StaticFiles(directory='static', html=True), name='static')
