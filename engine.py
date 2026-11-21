"""WisOrbit AI Video Engine v0.1 - free providers with automatic fallback.
Pipeline: storyboard -> per-scene voice (TTS chain) -> measure duration -> visual (image chain)
-> motion clip cut to exact voice length -> subtitles (ASS) -> concat -> final MP4."""
import re,json,os,subprocess,sys,time,urllib.parse,requests,random
sys.path.insert(0,os.path.dirname(__file__))
W,H,FPS=1280,720,25
def set_resolution(h=720):
    """720 (free) or 1080 (paid plans). Subtitles keep a 1280x720 layout and are scaled by libass."""
    global W,H
    W,H=(1920,1080) if int(h or 720)>=1080 else (1280,720)
WIKI_UA={'User-Agent':'WisOrbitVideoAgent/0.4 (https://video.wisorbit.com; contact via https://wisorbit.com)'}   # Wikimedia rate-limits generic agents (429)
VOICES={'hi':['hi-IN-MadhurNeural','hi-IN-SwaraNeural'],'en':['en-IN-PrabhatNeural','en-IN-NeerjaNeural']}
log=lambda *a: print('[engine]',*a,flush=True)
def run(cmd): subprocess.run(cmd,check=True,capture_output=True)
def dur(f): return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',f]).decode())

# ---------- TTS providers (fallback chain) ----------
def tts_edge(text,lang,out):
    import tts_edge; tts_edge.say(text,VOICES[lang][0],out)
def tts_gtts(text,lang,out):
    from gtts import gTTS; gTTS(text,lang=lang,tld='co.in' if lang=='en' else 'com').save(out)
TTS_CHAIN=[('edge-neural',tts_edge),('google-gtts',tts_gtts)]
def speak(text,lang,out):
    for name,fn in TTS_CHAIN:
        for attempt in range(2):
            try:
                fn(text,lang,out)
                if os.path.getsize(out)>1000: log('voice',name); return name
            except Exception as e: log('voice fail',name,str(e)[:80]); time.sleep(2)
    raise RuntimeError('all TTS failed')

# ---------- Visual providers (fallback chain) ----------
def img_pollinations(prompt,out,model='flux'):
    url='https://image.pollinations.ai/prompt/'+urllib.parse.quote(prompt)+f'?width={W}&height={H}&nologo=true&model={model}&seed={random.randint(1,99999)}'
    for k in range(4):
        r=requests.get(url,timeout=120)
        if r.status_code in (402,429): log('pollinations busy, waiting'); time.sleep(20); continue
        break
    r.raise_for_status()
    if 'image' not in r.headers.get('content-type',''): raise RuntimeError('not image')
    open(out,'wb').write(r.content)
def img_hf(prompt,out):  # Hugging Face free inference, no watermark (needs free HF token)
    h={'Content-Type':'application/json'}
    if os.environ.get('HF_TOKEN'): h['Authorization']='Bearer '+os.environ['HF_TOKEN']
    r=requests.post('https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell',headers=h,json={'inputs':prompt,'parameters':{'width':1280,'height':720}},timeout=180)
    r.raise_for_status()
    if 'image' not in r.headers.get('content-type',''): raise RuntimeError(r.text[:100])
    open(out,'wb').write(r.content)
def img_pollinations_turbo(prompt,out): img_pollinations(prompt,out,'turbo')
def img_wikimedia(prompt,out,query=None):  # real, freely licensed photos (e.g. ISRO GODL images)
    q=query or ' '.join(prompt.split()[:6])
    r=requests.get('https://commons.wikimedia.org/w/api.php',params={'action':'query','generator':'search','gsrsearch':'filetype:bitmap '+q,'gsrnamespace':6,'gsrlimit':15,'prop':'imageinfo','iiprop':'url|size','iiurlwidth':1600,'format':'json'},headers=WIKI_UA,timeout=30).json()
    pages=sorted(r.get('query',{}).get('pages',{}).values(),key=lambda p:p.get('index',9))
    pages=sorted(pages,key=lambda p:-(p['imageinfo'][0].get('width',0)>=p['imageinfo'][0].get('height',1)))
    toks=[t.lower() for t in re.findall(r'\w+',q) if len(t)>3]
    for p in pages:
        ii=p['imageinfo'][0]; tl=p['title'].lower()
        if toks and (toks[0] not in tl or sum(t in tl for t in toks)<max(1,len(toks)//2)): continue   # relevance: subject + most keywords in title
        if p['title'] in USED: continue                                 # no repeated photos
        if ii.get('width',0)>=800 and not p['title'].lower().endswith(('.svg','.gif','.tif','.tiff')):
            data=requests.get(ii['thumburl'],headers=WIKI_UA,timeout=60).content
            if len(data)<40000: continue
            open(out,'wb').write(data); USED.add(p['title']); CREDITS.append(p['title'].replace('File:','')+' (Wikimedia Commons)'); return
    raise RuntimeError('no wikimedia match')
CREDITS=[]
USED=set()
def img_card(prompt,out):  # last-resort offline title card
    run(['ffmpeg','-y','-f','lavfi','-i',f'gradients=s={W}x{H}:c0=0x0b1030:c1=0x3a1c71:d=1','-frames:v','1',out])
ALLOW_WATERMARK=os.environ.get('ALLOW_WATERMARK')=='1'   # default: watermark-free sources only
IMG_CHAIN=[('huggingface-flux',img_hf),('wikimedia-commons',img_wikimedia)]+([('pollinations-flux',img_pollinations),('pollinations-turbo',img_pollinations_turbo)] if ALLOW_WATERMARK else [])+[('offline-card',img_card)]
def picture(prompt,out,real=False,query=None):
    chain=list(IMG_CHAIN)
    if real and query: chain=[('wikimedia-commons',lambda p,o: img_wikimedia(p,o,query))]+chain
    for name,fn in chain:
        for attempt in range(2):
            try:
                fn(prompt,out)
                if os.path.getsize(out)>20000 or name=='offline-card': log('image',name); return name
            except Exception as e: log('image fail',name,str(e)[:80]); time.sleep(3)
    raise RuntimeError('all image providers failed')

# ---------- motion + subtitles ----------
def motion_clip(img,d,motion,out):
    n=int(d*FPS)+1
    z={'in':"min(1+0.0009*on,1.25)",'out':"max(1.25-0.0009*on,1)"}.get(motion,"1.15")
    x={'left':f"(iw-iw/zoom)*(1-on/{n})",'right':f"(iw-iw/zoom)*on/{n}"}.get(motion,"iw/2-(iw/zoom/2)")
    pre=(f"split[b][f];[b]scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},boxblur=30:2,eq=brightness=-0.15[bg];"
         f"[f]scale={W*2}:{H*2}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,")
    vf=(pre+f"zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)':d={n}:s={W}x{H}:fps={FPS},"
        f"fade=t=in:st=0:d=0.4,fade=t=out:st={max(d-0.4,0):.2f}:d=0.4,format=yuv420p")
    run(['ffmpeg','-y','-loop','1','-i',img,'-filter_complex',vf,'-t',f'{d:.3f}','-r',str(FPS),'-c:v','libx264','-preset','veryfast',out])
# ---------- length control: requested length +-10% ----------
PAD=0.45            # pause after each spoken scene (s)
MAX_TEMPO=1.18      # fastest natural-sounding speed-up of the voice
def bookends(target):
    """(intro, outro) seconds; short videos get shorter title/credit cards."""
    return (2.0,2.5) if target and target<=90 else (3.0,4.0)
def fit_plan(raw,target,intro,outro):
    """raw = spoken seconds per scene. Returns (kept indexes, tempo, extra pad per scene, extra outro).
    Aims for target +-6% so the final file lands inside +-10%."""
    lo,hi=target*0.94,target*1.06
    keep=list(range(len(raw)))
    def total(ix,tempo,extra=0): return intro+outro+sum(raw[i]/tempo+PAD+extra for i in ix)
    def tempo_for(ix):
        budget=target-intro-outro-PAD*len(ix); need=sum(raw[i] for i in ix)/max(budget,1)
        return min(MAX_TEMPO,max(1.0,need))
    tempo=tempo_for(keep)
    while total(keep,tempo)>hi and len(keep)>2:
        mids=keep[1:-1] or keep[:-1]            # keep the hook (first) and the conclusion (last)
        best=min(mids,key=lambda i: abs(total([k for k in keep if k!=i],tempo_for([k for k in keep if k!=i]))-target))
        keep.remove(best); tempo=tempo_for(keep)
    if total(keep,tempo)>hi: tempo=min(1.3,max(tempo,sum(raw[i] for i in keep)/max(target-intro-outro-PAD*len(keep),1)))
    extra=outro_extra=0.0
    t=total(keep,tempo)
    if t<lo:                                    # too short: slightly longer pauses, then a longer end card
        extra=min(1.0,(target-t)/len(keep)); t=total(keep,tempo,extra)
        if t<lo: outro_extra=min(4.0,target-t)
    return keep,tempo,extra,outro_extra

def ts(t): h=int(t//3600); m=int(t%3600//60); s=t%60; return f"{h}:{m:02d}:{s:05.2f}"
def write_ass(lines,path,lang):
    font='Noto Sans Devanagari' if lang=='hi' else 'Noto Sans'
    hdr=f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},40,&H00FFFFFF,&H00FFFFFF,&H64200A0A,&H64200A0A,1,0,0,0,100,100,0,0,3,10,0,2,110,110,46,1
Style: Brand,Noto Sans,24,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,1,0,1,1,1,9,30,30,24,1
Style: Small,{font},28,&H00E0E0E0,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,1,5,80,80,40,1
Style: Title,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,2,5,60,60,40,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path,'w') as f:
        f.write(hdr)
        for st,en,style,txt in lines: f.write(f"Dialogue: 0,{ts(st)},{ts(en)},{style},,0,0,0,,{txt}\n")

def build(sb_path,lang,outdir,cb=None,target=None):
    cb=cb or (lambda *a: None)
    outdir=os.path.abspath(outdir); sb=json.load(open(sb_path)); os.makedirs(outdir,exist_ok=True); work=os.path.join(outdir,'tmp_'+lang); os.makedirs(work,exist_ok=True)
    target=target or sb.get('target_sec')
    intro_d,outro_d=bookends(target)
    scenes=sb['scenes']
    # 1) voices first, so the length can be fitted before any video is rendered
    raw=[]; vps=[]
    for i,s in enumerate(scenes,1):
        mp3=os.path.join(work,f'v{i:02d}.mp3'); vps.append(speak(s[lang],lang,mp3)); raw.append(dur(mp3))
        cb(f'voice {i}/{len(scenes)} ({lang})',0.25*i/len(scenes))
    keep,tempo,extra,outro_extra=fit_plan(raw,target,intro_d,outro_d) if target else (list(range(len(scenes))),1.0,0.0,0.0)
    outro_d+=outro_extra
    log(f'length fit {lang}: target {target}s, voice {sum(raw):.1f}s, scenes {len(keep)}/{len(scenes)}, tempo {tempo:.2f}, extra pause {extra:.2f}s')
    report=[]; clips=[]; audios=[]; subs=[]; t=0.0
    title=sb['title_'+lang]; intro_img=os.path.join(work,'intro.png'); img_card('',intro_img)
    intro=os.path.join(work,'c00.mp4'); motion_clip(intro_img,intro_d,'in',intro); clips.append(intro)
    sil=os.path.join(work,'a00.wav'); run(['ffmpeg','-y','-f','lavfi','-i','anullsrc=r=24000:cl=mono','-t',f'{intro_d}','-c:a','pcm_s16le',sil]); audios.append(sil)
    subs.append((0.3,intro_d-0.2,'Title',title+r'\N{\fs30}WisOrbit')); t=intro_d
    shared_img=os.path.join(outdir,'images'); os.makedirs(shared_img,exist_ok=True)
    for n,k in enumerate(keep,1):
        i=k+1; s=scenes[k]; mp3=os.path.join(work,f'v{i:02d}.mp3')
        wav=os.path.join(work,f'a{i:02d}.wav'); af=(f'atempo={tempo:.3f},' if tempo>1.001 else '')+f'apad=pad_dur={PAD+extra:.2f}'
        run(['ffmpeg','-y','-i',mp3,'-af',af,'-ar','24000','-ac','1','-c:a','pcm_s16le',wav])
        d=dur(wav)
        img=os.path.join(shared_img,f's{i:02d}.jpg')
        ip='cached' if os.path.exists(img) and os.path.getsize(img)>20000 else picture(s['prompt'],img,real=s.get('visual')=='real',query=s.get('wiki_query'))   # same visuals for both languages
        clip=os.path.join(work,f'c{i:02d}.mp4'); motion_clip(img,d,s.get('motion','in'),clip)
        clips.append(clip); audios.append(wav); subs.append((t+0.1,t+d-0.3,'Sub',s[lang])); t+=d
        cb(f'scene {n}/{len(keep)} ready ({lang})',0.25+0.7*n/len(keep))
        report.append({'scene':i,'seconds':round(d,2),'voice':vps[k],'image':ip})
    # outro credits card 4s
    oimg=os.path.join(work,'outro.png'); img_card('',oimg); oc=os.path.join(work,'c99.mp4'); motion_clip(oimg,outro_d,'none',oc); clips.append(oc)
    osil=os.path.join(work,'a99.wav'); run(['ffmpeg','-y','-f','lavfi','-i','anullsrc=r=24000:cl=mono','-t',f'{outro_d:.2f}','-c:a','pcm_s16le',osil]); audios.append(osil)
    cred=sorted(set(CREDITS))[:6]
    ctext=('सीखते रहिए' if lang=='hi' else 'Keep learning')+r'\N{\fs22}'+(r'\N'.join(['Photos: '+c for c in cred]) if cred else 'Visuals: AI generated')
    subs.append((t+0.2,t+outro_d-0.2,'Small',ctext)); t+=outro_d
    cb(f'final edit ({lang})',0.97)
    # concat video and audio, then subtitles + gentle music bed
    lst=os.path.join(work,'v.txt'); open(lst,'w').write(''.join(f"file '{c}'\n" for c in clips))
    alst=os.path.join(work,'a.txt'); open(alst,'w').write(''.join(f"file '{a}'\n" for a in audios))
    vcat=os.path.join(work,'v.mp4'); run(['ffmpeg','-y','-f','concat','-safe','0','-i',lst,'-c','copy',vcat])
    acat=os.path.join(work,'a.wav'); run(['ffmpeg','-y','-f','concat','-safe','0','-i',alst,'-c','copy',acat])
    total=dur(acat)
    subs.append((intro_d,total,'Brand',r'{\c&H00D7FF&}●{\c&HFFFFFF&} WisOrbit'))
    ass=os.path.join(work,'subs.ass'); write_ass(subs,ass,lang)
    final=os.path.join(outdir,re.sub(r'\W+','_',sb['title_en'])[:40]+f'_{lang}.mp4')
    music=f"sine=f=110:d={total},volume=0.05[m1];sine=f=164.8:d={total},volume=0.035[m2];[m1][m2]amix=2,afade=t=in:d=2,afade=t=out:st={total-3}:d=3[mb]"
    run(['ffmpeg','-y','-i',vcat,'-i',acat,'-filter_complex',f"[0:v]ass={ass},drawbox=x=0:y=ih-6:w='iw*t/{total:.2f}':h=6:color=0xFFD700@0.9:t=fill[v];{music};[1:a][mb]amix=2:duration=first:normalize=0[a]",
         '-map','[v]','-map','[a]','-c:v','libx264','-preset','medium','-crf','21','-c:a','aac','-b:a','160k','-shortest',final])
    fd=dur(final); report.append({'final_seconds':round(fd,2),'target_seconds':target,'tempo':round(tempo,3)})
    json.dump(report,open(os.path.join(outdir,f'report_{lang}.json'),'w'),indent=1)
    log('DONE',final,round(fd,1),'s','(target',target,'s)'); return final
if __name__=='__main__':
    for lang in sys.argv[2:] or ['hi','en']: build(sys.argv[1],lang,'out')
