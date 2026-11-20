"""WisOrbit AI Video Agent - web app. Run: uvicorn app:app --host 0.0.0.0 --port 7860"""
import os,uuid,threading,traceback,time,shutil
from fastapi import FastAPI,Header,HTTPException
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import agent,accounts
app=FastAPI(title='WisOrbit AI Video Agent')
JOBS={}; LOCK=threading.Semaphore(1)   # one render at a time on free hardware
class Req(BaseModel):
    topic:str; minutes:float=1; style:str='educational'; langs:str='hi,en'

def auth(authorization):
    if not accounts.configured(): raise HTTPException(503,'लॉगिन अभी सेट नहीं है / Login is not configured on the server yet.')
    tok=(authorization or '').removeprefix('Bearer ').strip()
    u=accounts.get_user(tok)
    if not u: raise HTTPException(401,'कृपया लॉगिन करें / Please log in first.')
    return u

def clean_local(max_age=6*3600):
    if not os.path.isdir('jobs'): return
    for d in os.listdir('jobs'):
        p=os.path.join('jobs',d)
        if time.time()-os.path.getmtime(p)>max_age: shutil.rmtree(p,ignore_errors=True)

def worker(jid,r,user,vid):
    j=JOBS[jid]
    def cb(msg,p): j.update(step=msg,progress=round(p*100))
    try:
        j['step']='waiting in queue'
        with LOCK:
            accounts.update_video(vid,status='running')
            files=agent.run_job(r.topic,r.minutes,r.style,r.langs,out=f'jobs/{jid}',cb=cb)
        j.update(status='saving',step='saving to your account',progress=99)
        paths=[]
        try:
            for f in files: paths.append(accounts.upload(user['id'],vid,f))
            accounts.update_video(vid,status='done',storage_path=','.join(paths))
        except Exception as e:
            traceback.print_exc(); accounts.update_video(vid,status='done',storage_path=','.join(paths) or None)
        j.update(status='done',progress=100,step='done',files=[f'/api/file/{jid}/{os.path.basename(f)}' for f in files])
    except Exception as e:
        j.update(status='error',step=str(e)[:200]); traceback.print_exc()
        try: accounts.update_video(vid,status='error')
        except Exception: pass
    finally:
        accounts.cleanup(); clean_local()

@app.exception_handler(Exception)
def oops(req,exc):
    traceback.print_exc()
    return JSONResponse({'detail':'सर्वर में दिक्कत, थोड़ी देर बाद कोशिश करें / Server error, please try again shortly.'},status_code=500)

@app.on_event('startup')
def startup():
    accounts.fail_stale(); threading.Thread(target=accounts.cleanup,daemon=True).start()

@app.get('/api/config')
def config():
    return {'supabase_url':accounts.URL,'publishable_key':accounts.PUB,'google_enabled':accounts.GOOGLE_ENABLED,
            'login_enabled':accounts.configured()}

@app.get('/api/me')
def me(authorization:str|None=Header(None)):
    return accounts.usage(auth(authorization))

@app.get('/api/videos')
def videos(authorization:str|None=Header(None)):
    return accounts.history(auth(authorization)['id'])

@app.post('/api/generate')
def gen(r:Req,authorization:str|None=Header(None)):
    user=auth(authorization)
    r.topic=r.topic.strip()[:500]
    if not r.topic: raise HTTPException(400,'विषय लिखिए / Please enter a topic.')
    r.minutes=min(max(r.minutes,0.5),10)
    if r.langs not in ('hi,en','hi','en'): r.langs='hi,en'
    ok,msg,p=accounts.check_quota(user,r.minutes)
    if not ok: raise HTTPException(429,msg)
    if msg=='credit': accounts.spend_credit(p)
    vid=accounts.new_video(user['id'],r.topic,r.langs,r.style,r.minutes)
    jid=uuid.uuid4().hex[:10]; JOBS[jid]={'status':'running','step':'starting','progress':0,'files':[],'topic':r.topic,'t':time.time(),'video_id':vid}
    threading.Thread(target=worker,args=(jid,r,user,vid),daemon=True).start(); return {'id':jid,'video_id':vid}
@app.get('/api/status/{jid}')
def status(jid:str): return JOBS.get(jid,{'status':'unknown'})
@app.get('/api/file/{jid}/{name}')
def file(jid:str,name:str):
    p=os.path.join('jobs',os.path.basename(jid),os.path.basename(name))
    if not os.path.isfile(p): raise HTTPException(404,'file not found (server restarted?) — see Meri Videos')
    return FileResponse(p,media_type='video/mp4',filename=name)
app.mount('/',StaticFiles(directory='static',html=True),name='static')
