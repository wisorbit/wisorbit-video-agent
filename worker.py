"""WisOrbit render worker: takes the oldest queued job from Supabase (table public.jobs), renders it, uploads it.

Run on its own (Oracle VM, any server):   python worker.py
or inside Docker:                         docker run -e WORKER_ONLY=1 --env-file .env wisorbit-video-agent
The web app (app.py) also starts one in-process worker unless WORKER_IN_WEB=0.

Env: SUPABASE_URL, SUPABASE_SECRET_KEY (required); GEMINI_API_KEY, HF_TOKEN (optional, better quality);
     WORKER_NAME (optional label), WORKER_POLL_SEC (default 5), JOB_STALE_SEC (default 300).
One worker = one video at a time. Run more workers as more processes/containers (engine globals are per process)."""
import os, socket, time, uuid, shutil, threading, traceback, signal
import accounts, agent

POLL = float(os.environ.get('WORKER_POLL_SEC', '5'))
BEAT = 30                                            # seconds between heartbeats
STALE = int(os.environ.get('JOB_STALE_SEC', '300'))  # a running job without heartbeat this long is requeued
STOP = threading.Event()
_busy = threading.Lock()                             # engine/agent keep per-process state: one render per process

def log(*a): print('[worker]', *a, flush=True)

class Worker:
    def __init__(self, name=None):
        host = socket.gethostname()
        self.host = os.environ.get('WORKER_NAME') or host
        self.id = f"{name or self.host}-{os.getpid()}-{uuid.uuid4().hex[:4]}"

    def run(self):
        if not accounts.server_configured():
            log('SUPABASE_URL / SUPABASE_SECRET_KEY missing - worker not started'); return
        while not STOP.is_set():
            try: accounts.worker_register(self.id, self.host); break
            except Exception as e: log('register failed, retrying', e); STOP.wait(15)
        log('started', self.id)
        last_beat = last_reap = last_clean = 0
        try:
            while not STOP.is_set():
                now = time.time(); job = None
                try:
                    if now - last_reap > 60: accounts.reap_stale(STALE); last_reap = now
                    if now - last_clean > 6 * 3600:
                        accounts.cleanup(); accounts.prune_workers(); last_clean = now
                    job = accounts.claim_job(self.id)
                except Exception as e:
                    log('queue error', str(e)[:200]); STOP.wait(POLL * 3); continue
                if job:
                    with _busy: self.process(job)
                    last_beat = time.time(); continue
                if now - last_beat > BEAT:
                    try: accounts.worker_beat(self.id, None)
                    except Exception as e: log('beat failed', str(e)[:100])
                    last_beat = now
                STOP.wait(POLL)
        finally:
            accounts.worker_remove(self.id); log('stopped', self.id)

    def process(self, job):
        jid, vid, uid = job['id'], job.get('video_id'), job['user_id']
        log('job', jid, job.get('topic', '')[:60], f"{job.get('minutes')} min", job.get('langs'), 'try', job.get('attempts'))
        out = os.path.join('jobs', jid)
        st = {'step': 'starting', 'progress': 0, 'sent': None}
        done = threading.Event()

        def push(force=False):
            cur = (st['step'], st['progress'])
            if force or cur != st['sent']:
                accounts.job_update(jid, step=st['step'][:200], progress=st['progress'], heartbeat_at=accounts._now())
                st['sent'] = cur
        def beat():                       # progress every 5 s, heartbeat at least every 30 s (long ffmpeg steps)
            last = time.time()
            while not done.wait(5):
                try:
                    force = time.time() - last > BEAT
                    push(force)
                    if force: accounts.worker_beat(self.id, jid); last = time.time()
                except Exception as e: log('progress update failed', str(e)[:100])
        def cb(msg, p): st['step'] = msg; st['progress'] = max(0, min(99, round(p * 100)))

        t = threading.Thread(target=beat, daemon=True); t.start()
        try:
            if vid: accounts.update_video(vid, status='running')
            files = agent.run_job(job['topic'], float(job['minutes']), job['style'], job['langs'], out=out, cb=cb)
            st['step'] = 'saving to your account'; st['progress'] = 99
            paths = [accounts.upload(uid, vid or jid, f) for f in files]
            done.set(); t.join(2)
            if vid: accounts.update_video(vid, status='done', storage_path=','.join(paths))
            accounts.job_update(jid, status='done', step='done', progress=100, files=','.join(paths),
                                finished_at=accounts._now(), heartbeat_at=accounts._now())
            accounts.worker_done(self.id)
            log('job done', jid)
        except BaseException as e:          # SystemExit on SIGTERM -> put the job back in line
            done.set(); t.join(2)
            stopping = isinstance(e, (SystemExit, KeyboardInterrupt))
            if not stopping: traceback.print_exc()
            try:
                if stopping:
                    accounts.job_update(jid, status='queued', step='requeued', worker_id=None, progress=0)
                    if vid: accounts.update_video(vid, status='queued')
                else:
                    accounts.job_update(jid, status='error', step='failed', error=str(e)[:500], finished_at=accounts._now())
                    if vid: accounts.update_video(vid, status='error')
            except Exception as e2: log('could not record job result', e2)
            if stopping: raise
        finally:
            shutil.rmtree(out, ignore_errors=True)

def start_in_background(n=1):
    """Used by the web app (WORKER_IN_WEB=1). Returns the threads."""
    ths = []
    for i in range(max(1, n)):
        w = Worker(name=f"web-{socket.gethostname()}")
        th = threading.Thread(target=w.run, daemon=True, name=f'worker-{i}'); th.start(); ths.append(th)
    return ths

def _sigterm(*_): STOP.set(); raise SystemExit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _sigterm)
    try: Worker().run()
    except (SystemExit, KeyboardInterrupt): STOP.set()
