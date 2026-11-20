import os,urllib.parse,base64,aiohttp,asyncio
_u=urllib.parse.urlsplit(os.environ.get('HTTPS_PROXY',''))
if _u.hostname:
    _user=urllib.parse.unquote(_u.username or ''); _pw=urllib.parse.unquote(_u.password or '')
    _host=f"http://{_u.hostname}:{_u.port}"
    _hdr={'Proxy-Authorization':'Basic '+base64.b64encode(f'{_user}:{_pw}'.encode()).decode()}
    _oi=aiohttp.ClientSession.__init__
    def _ni(self,*a,**k): k['trust_env']=False; _oi(self,*a,**k)
    aiohttp.ClientSession.__init__=_ni
    _ow=aiohttp.ClientSession._ws_connect
    async def _nw(self,url,**k):
        k['proxy']=_host; k['proxy_headers']=_hdr; return await _ow(self,url,**k)
    aiohttp.ClientSession._ws_connect=_nw
    _orq=aiohttp.ClientSession._request
    async def _nr(self,m,url,**k):
        k['proxy']=_host; k['proxy_headers']=_hdr; return await _orq(self,m,url,**k)
    aiohttp.ClientSession._request=_nr
import edge_tts
def say(text,voice,out,rate='+0%'):
    async def m(): await edge_tts.Communicate(text,voice,rate=rate).save(out)
    asyncio.run(m())
if __name__=='__main__':
    say('नमस्ते, यह एक परीक्षण है।','hi-IN-MadhurNeural','/tmp/t.mp3'); print(os.path.getsize('/tmp/t.mp3'))
