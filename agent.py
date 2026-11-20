"""WisOrbit Video Agent v0.2: topic -> research -> storyboard -> self-check -> render (engine.py).
Brains (fallback chain): Pollinations text (free, no key) -> Gemini API (if GEMINI_API_KEY set)."""
import json,os,re,sys,time,requests,argparse
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import engine
log=lambda *a: print('[agent]',*a,flush=True)
UA={'User-Agent':'WisOrbitVideoAgent/0.4 (https://video.wisorbit.com; contact via https://wisorbit.com)'}   # Wikimedia rate-limits generic agents (429)

def llm_pollinations(prompt):
    for k in range(5):
        r=requests.post('https://text.pollinations.ai/openai',json={'model':'openai','messages':[{'role':'user','content':prompt}]},timeout=180)
        if r.status_code in (402,429,503): log('brain busy, waiting'); time.sleep(16); continue
        break
    r.raise_for_status(); return r.json()['choices'][0]['message']['content']
def llm_gemini(prompt):
    k=os.environ.get('GEMINI_API_KEY'); 
    if not k: raise RuntimeError('no key')
    r=requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={k}',json={'contents':[{'parts':[{'text':prompt}]}]},timeout=180)
    r.raise_for_status(); return r.json()['candidates'][0]['content']['parts'][0]['text']
BRAINS=[('gemini',llm_gemini),('pollinations-text',llm_pollinations)]   # best free brain first
def think(prompt,want_json=True):
    for name,fn in BRAINS:
        if name=='gemini' and not os.environ.get('GEMINI_API_KEY'): continue
        for a in range(3):
            try:
                out=fn(prompt)
                if not want_json: return out
                m=re.search(r'\{.*\}',out,re.S); return json.loads(m.group(0))
            except Exception as e: log('brain fail',name,str(e)[:100]); time.sleep(8)
    raise RuntimeError('all brains failed')

def research(topic):
    notes=[]
    for lang in ['en']:
        try:
            s=requests.get(f'https://{lang}.wikipedia.org/w/api.php',params={'action':'query','list':'search','srsearch':topic,'format':'json','srlimit':1},headers=UA,timeout=30).json()
            title=s['query']['search'][0]['title']
            ex=requests.get(f'https://{lang}.wikipedia.org/w/api.php',params={'action':'query','prop':'extracts','explaintext':1,'titles':title,'format':'json','exchars':6000},headers=UA,timeout=30).json()
            notes.append(f'[{lang}.wikipedia: {title}]\n'+list(ex['query']['pages'].values())[0].get('extract',''))
        except Exception as e: log('research fail',lang,e)
    return '\n\n'.join(notes)

STYLE={'educational':'clear teacher-like narration, simple words, facts and examples',
 'cinematic':'dramatic storytelling narration, emotional build-up, film-like shots',
 'realistic':'documentary narration, grounded real-world visuals',
 'youtube':'hook in first 5 seconds, energetic, ends with call to subscribe'}
# English words per second of narration (measured with the en-IN neural voice incl. numbers/dates ~2.2-2.6).
# Hindi narration of the same line runs ~15-20% longer, so plan fewer words when Hindi is made.
WPS_EN={'en':2.3,'hi':2.0}
def budget(minutes,langs='hi,en'):
    """Scene count and words per scene so the narration fits the requested length (+-10%)."""
    target=minutes*60; intro,outro=engine.bookends(target); speech=target-intro-outro
    wps=WPS_EN['hi'] if 'hi' in langs else WPS_EN['en']
    n=max(2,round(speech/8.5)); words=max(12,round((speech/n-engine.PAD)*wps))
    return target,n,words
def plan(topic,minutes,style,facts,cb=lambda *x:None,langs='hi,en'):
    target,n,words=budget(minutes,langs)
    chunks=[]; done=[]
    per=8
    for start in range(0,n,per):
        k=min(per,n-start)
        p=f"""You are a video scriptwriter for WisOrbit, an Indian education brand. Topic: "{topic}". Style: {STYLE[style]}.
Total video is {round(target)} seconds with exactly {n} scenes (~{round(target/n)} seconds each). Write scenes {start+1} to {start+k}. {'Start with a strong hook.' if start==0 else 'Continue smoothly from previous scenes: '+' | '.join(d['en'] for d in done[-2:])}
{'End with a short conclusion and "Keep learning with WisOrbit".' if start+k>=n else ''}
Use ONLY facts from these research notes; never invent numbers or dates:
{facts[:7000]}
Return JSON only: {{"scenes":[{{"en":"English narration only (no labels like Hook or Scene), {words-2}-{words+2} words, simple common words (no rare words like crenellated)","visual":"real if the scene shows a specific real object/person/place/event that has real photos, else ai","wiki_query":"2-5 word Wikimedia Commons search for a real photo","prompt":"detailed photorealistic image prompt, no text, no people unless needed","motion":"in|out|left|right"}}]}}"""
        js=think(p); sc=js['scenes'][:k]; done+=sc; log('planned',len(done),'/',n); cb(f'script {len(done)}/{n} scenes',len(done)/n)
    return done
NUM=re.compile(r'\d[\d,.]*')
def unsupported(text,facts):
    f=facts.replace(',','')
    return [n for n in NUM.findall(text) if n.strip('.,').replace(',','') not in f]
def verify(topic,scenes,facts,cb=lambda *x:None):
    good=[]
    for i,s in enumerate(scenes):
        for k in range(3):
            bad=unsupported(s['en'],facts)
            if not bad: break
            log('unsupported numbers',bad,'-> rewriting scene',i+1)
            r=think(f'Rewrite this narration for a video on "{topic}" so that every number and date is exactly as in the notes, or remove the number. Keep about {len(s["en"].split())} words. JSON only: {{"en":"..."}}\nNOTES:\n{facts[:6000]}\nLINE: {s["en"]}')
            s['en']=r['en']
        if not unsupported(s['en'],facts): good.append(s)
        else: log('dropped unverifiable scene',i+1)
    return good
# ---------- Hindi translation with a safety check ----------
# The free text brain sometimes invents words (Taj test: "formal gardens" became "होरी के बाग़").
# Every Hindi line is back-translated to English and compared with the English line; a line that lost
# meaning, gained a new name, lost a number or is not in Devanagari is replaced by Google's translation.
def gtx(text,sl,tl):
    """Free Google Translate web endpoint (no key)."""
    for k in range(3):
        try:
            r=requests.get('https://translate.googleapis.com/translate_a/single',params={'client':'gtx','sl':sl,'tl':tl,'dt':'t','q':text},headers=UA,timeout=20)
            r.raise_for_status(); return ''.join(x[0] for x in r.json()[0] if x and x[0]).strip()
        except Exception as e: log('gtx fail',str(e)[:80]); time.sleep(2+3*k)
    return None
DEVA_DIGITS=str.maketrans('०१२३४५६७८९','0123456789')
STOP=set('the a an and or of to in on at for by with from is are was were be been this that these those it its as into over than then also which who whom whose their there here has have had not but so such very more most can will would about after before during while'.split())
def _stems(t): return {w.lower()[:5] for w in re.findall(r"[A-Za-z]+",t) if len(w)>2 and w.lower() not in STOP}
def hindi_problems(en,hi,facts=''):
    """Reasons why a Hindi line looks wrong (empty list = looks fine)."""
    why=[]
    if not hi or not hi.strip(): return ['empty']
    letters=[c for c in hi if c.isalpha()]
    deva=sum('\u0900'<=c<='\u097f' for c in letters)
    if not letters or deva/len(letters)<0.85: why.append('not Devanagari')
    nums_en={n.strip('.,').replace(',','') for n in NUM.findall(en)}
    nums_hi={n.strip('.,').replace(',','') for n in NUM.findall(hi.translate(DEVA_DIGITS))}
    if nums_en-nums_hi: why.append(f'numbers missing {sorted(nums_en-nums_hi)}')
    ratio=len(hi.split())/max(1,len(en.split()))
    if ratio<0.6 or ratio>2.3: why.append(f'length ratio {ratio:.1f}')
    back=gtx(hi,'hi','en')
    if back:
        a,b=_stems(en),_stems(back)
        sim=len(a&b)/max(1,len(a))
        if sim<0.4: why.append(f'meaning drift {sim:.2f} ({back[:80]})')
        src=(en+' '+facts).lower()
        toks=re.findall(r"[A-Za-z]+",back)
        new=[w for j,w in enumerate(toks) if j>0 and w[0].isupper() and len(w)>2 and w.lower() not in STOP
             and not re.search(r'\b'+re.escape(w.lower()[:5]),src)]
        if new: why.append(f'invented names {new} ({back[:80]})')
    return why
def safe_hindi(en,hi,facts=''):
    bad=hindi_problems(en,hi,facts)
    if not bad: return hi
    alt=gtx(en,'en','hi')
    log('Hindi line replaced:',bad,'|',hi[:60],'->',(alt or '')[:60])
    return alt or hi
def translate_hi(scenes,facts=''):
    lines=[s['en'] for s in scenes]
    hi=None
    try:
        r=think('Translate each English narration line into natural, simple spoken Hindi (Devanagari) for an Indian education video. '
                'Keep all numbers and dates exactly. Spell names correctly in Hindi (e.g. Chandrayaan = चंद्रयान, ISRO = इसरो, Taj Mahal = ताज महल). '
                'Never invent words or names: if unsure of a word, use a simple common Hindi word or the English word written in Devanagari. '
                'JSON only: {"hi":["...", ...]} with exactly the same number of lines.\n'+json.dumps(lines,ensure_ascii=False))
        hi=r.get('hi')
        if not isinstance(hi,list) or len(hi)!=len(lines): log('translation count mismatch, using per-line fallback'); hi=None
    except Exception as e: log('brain translation failed',str(e)[:80])
    for k,s in enumerate(scenes):
        cand=hi[k] if hi else (gtx(s['en'],'en','hi') or '')
        s['hi']=safe_hindi(s['en'],cand,facts)
    return scenes
def check(topic,scenes,facts):
    p=f"""Fact-check these narration lines for a video on "{topic}" against the notes. Fix any wrong fact; keep length; keep the same keys. Return JSON only: {{"scenes":[same objects, corrected]}}.
NOTES:\n{facts[:6000]}\nSCENES:\n{json.dumps(scenes,ensure_ascii=False)}"""
    try:
        js=think(p); 
        if len(js['scenes'])==len(scenes): return js['scenes']
    except Exception as e: log('check skipped',e)
    return scenes

def run_job(topic,minutes=1,style='educational',langs='hi,en',out=None,cb=lambda *x:None):
    slug=re.sub(r'\W+','_',topic)[:40]; out=out or f'projects/{slug}'; os.makedirs(out,exist_ok=True)
    sbp=os.path.join(out,'storyboard.json')
    cb('researching topic',0.02); facts=research(topic); open(os.path.join(out,'research.txt'),'w').write(facts)
    cb('writing script',0.05); sc=plan(topic,minutes,style,facts,lambda m,p: cb(m,0.05+0.15*p),langs)
    cb('fact-checking',0.17); sc=check(topic,sc,facts); sc=verify(topic,sc,facts)
    if 'hi' in langs: cb('translating to Hindi',0.22); sc=translate_hi(sc,facts)
    tt=think(f'Give a short catchy video title for "{topic}" in English and Hindi. JSON only: {{"en":"...","hi":"..."}}')
    if 'hi' in langs: tt['hi']=safe_hindi(tt['en'],tt.get('hi',''),facts+' '+topic)
    target,_,_=budget(minutes,langs)
    json.dump({'title_en':tt['en'],'title_hi':tt['hi'],'target_sec':target,'scenes':sc},open(sbp,'w'),ensure_ascii=False,indent=1)
    files=[]; L=langs.split(',')
    for j,lang in enumerate(L):
        engine.CREDITS.clear(); engine.USED.clear()
        files.append(engine.build(sbp,lang,out,lambda m,p: cb(m,0.25+0.75*(j+p)/len(L)),target=target))
    cb('done',1.0); return files
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('topic'); ap.add_argument('--minutes',type=float,default=1)
    ap.add_argument('--style',default='educational',choices=list(STYLE)); ap.add_argument('--langs',default='hi,en'); ap.add_argument('--out',default=None)
    a=ap.parse_args(); print(run_job(a.topic,a.minutes,a.style,a.langs,a.out,lambda m,p: log(f'{round(p*100)}%',m)))
if __name__=='__main__': main()
