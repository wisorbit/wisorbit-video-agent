"""WisOrbit AI Video Agent - web app. Run: uvicorn app:app --host 0.0.0.0 --port 7860"""
import os,uuid,threading,traceback,time
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import agent
app=FastAPI(title='WisOrbit AI Video Agent')
JOBS={}; LOCK=threading.Semaphore(1)   # one render at a time on free hardware
class Req(BaseModel):
    topic:str; minutes:float=1; style:str='educational'; langs:str='hi,en'
def worker(jid,r):
    j=JOBS[jid]
    def cb(msg,p): j.update(step=msg,progress=round(p*100))
    try:
        j['step']='waiting in queue'
        with LOCK:
            files=agent.run_job(r.topic,min(max(r.minutes,0.5),10),r.style,r.langs,out=f'jobs/{jid}',cb=cb)
        j.update(status='done',files=[f'/api/file/{jid}/{os.path.basename(f)}' for f in files])
    except Exception as e:
        j.update(status='error',step=str(e)[:200]); traceback.print_exc()
@app.post('/api/generate')
def gen(r:Req):
    jid=uuid.uuid4().hex[:10]; JOBS[jid]={'status':'running','step':'starting','progress':0,'files':[],'topic':r.topic,'t':time.time()}
    threading.Thread(target=worker,args=(jid,r),daemon=True).start(); return {'id':jid}
@app.get('/api/status/{jid}')
def status(jid:str): return JOBS.get(jid,{'status':'unknown'})
@app.get('/api/file/{jid}/{name}')
def file(jid:str,name:str): return FileResponse(os.path.join('jobs',jid,os.path.basename(name)),media_type='video/mp4',filename=name)
app.mount('/',StaticFiles(directory='static',html=True),name='static')
