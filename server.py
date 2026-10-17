"""
NEXUS AGENT SERVER v3.1 (Shopify + Gemini + PostZen + YouTube research)
====================================================================
Kya karta hai:
  1. /research  -> aap niche bolo (jaise "mobile accessories") -> Gemini (free key)
                   5 sellable product ideas banata hai (title, description, price, SEO)
  2. /add       -> APPROVE kiya hua product khud Shopify store me add kar deta hai
  3. /products  -> store ke latest 10 products ki list (verify karne ke liye)

Token ki tension mat lo: server khud client-credentials se token leta hai
(24h expire hota hai -> khud refresh ho jata hai). Purana legacy token ho to wo bhi chalta hai.

Chalane ka tarika (laptop pe):
  pip install -r requirements.txt
  set env vars (README-HINDI.md dekho)
  uvicorn server:app --host 0.0.0.0 --port 8000
Phir browser me: http://localhost:8000/health
"""
import os, re, json, time, uuid
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title='NEXUS Shopify Agent')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

# ---- config (env vars) ----
STORE        = os.getenv('SHOPIFY_STORE', '').strip()          # mystore.myshopify.com
CLIENT_ID    = ''.join(ch for ch in os.getenv('SHOPIFY_CLIENT_ID', '') if 32 <= ord(ch) < 127).strip()      # Dev Dashboard se
CLIENT_SECRET= ''.join(ch for ch in os.getenv('SHOPIFY_CLIENT_SECRET', '') if 32 <= ord(ch) < 127).strip()  # Dev Dashboard se
LEGACY_TOKEN = ''.join(ch for ch in os.getenv('SHOPIFY_TOKEN', '') if 32 <= ord(ch) < 127).strip()          # purana permanent token (optional)
GEMINI_KEY   = ''.join(ch for ch in os.getenv('GEMINI_KEY', '') if 32 <= ord(ch) < 127).strip()
API_VER      = '2025-07'

_tok = {'v': '', 'exp': 0}

async def get_token() -> str:
    if LEGACY_TOKEN:
        return LEGACY_TOKEN
    if not (STORE and CLIENT_ID and CLIENT_SECRET):
        raise HTTPException(400, 'SHOPIFY_STORE + SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET env set karo (ya SHOPIFY_TOKEN)')
    if _tok['v'] and time.time() < _tok['exp'] - 3600:
        return _tok['v']
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f'https://{STORE}/admin/oauth/access_token',
                         json={'grant_type': 'client_credentials',
                               'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET})
    if r.status_code != 200:
        raise HTTPException(502, f'Shopify token fail ({r.status_code}): {r.text[:200]}')
    d = r.json()
    _tok['v'] = d['access_token']
    _tok['exp'] = time.time() + int(d.get('expires_in', 86400))
    return _tok['v']

# ---------- health ----------
@app.get('/health')
async def health():
    return {'ok': True, 'store': STORE or '(not set)', 'gemini': bool(GEMINI_KEY),
            'shopify_auth': 'legacy' if LEGACY_TOKEN else ('client-credentials' if CLIENT_ID else 'not-set')}

# ---------- research (Gemini) ----------
class Niche(BaseModel):
    niche: str

@app.post('/research')
async def research(n: Niche):
    if not GEMINI_KEY:
        raise HTTPException(400, 'GEMINI_KEY env set nahi hai (aapki free Gemini key server ko do, browser ko nahi)')
    prompt = (
        "You are an expert e-commerce product researcher for an Indian Shopify dropshipping store.\n"
        f"Niche: {n.niche}\n"
        "Suggest exactly 5 winning products. Reply ONLY with a JSON array, no markdown:\n"
        '[{"title":"product name","description":"3-4 line selling HTML description with benefits","price":499,'
        '"seo_title":"<=60 chars","why":"1 line why it can sell in India"}]'
    )
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = None
            for model in ('gemini-flash-latest', 'gemini-2.5-flash', 'gemini-2.0-flash'):
                r = await c.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                    headers={'x-goog-api-key': GEMINI_KEY},
                    json={'contents': [{'parts': [{'text': prompt}]}],
                          'generationConfig': {'temperature': 0.8, 'maxOutputTokens': 4096}})
                if r.status_code != 404:
                    break
        if r.status_code == 404:
            async with httpx.AsyncClient(timeout=45) as c2:
                r2 = await c2.post(
                    'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent',
                    headers={'x-goog-api-key': GEMINI_KEY},
                    json={'contents': [{'parts': [{'text': prompt}]}],
                          'generationConfig': {'temperature': 0.8, 'maxOutputTokens': 4096}})
            r = r2
        if r.status_code == 429:
            raise HTTPException(429, 'Gemini free limit hit - thodi der baad try karo')
        r.raise_for_status()
        txt = ''.join(p.get('text', '') for p in r.json()['candidates'][0]['content']['parts'])
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f'Gemini error {e.response.status_code}: {e.response.text[:220]}')
    m = re.search(r'\[[\s\S]*\]', txt)
    if not m:
        raise HTTPException(500, 'Gemini response samajh nahi aaya: ' + txt[:150])
    try:
        ideas = json.loads(m.group(0))
    except Exception:
        raise HTTPException(500, 'JSON parse fail: ' + txt[:150])
    return {'ideas': ideas[:5]}

# ---------- add to Shopify ----------
class Prod(BaseModel):
    title: str
    description: str = ''
    price: float = 299
    status: str = 'DRAFT'   # DRAFT = safe (store pe live nahi hoga), ACTIVE = turant live

@app.post('/add')
async def add(p: Prod):
    tok = await get_token()
    q = ('mutation Set($input: ProductSetInput!) { productSet(synchronous: true, input: $input) '
         '{ product { id title onlineStoreUrl } userErrors { field message } } }')
    variables = {'input': {
        'title': p.title,
        'descriptionHtml': p.description or p.title,
        'status': p.status,
        'productOptions': [{'name': 'Title', 'values': [{'name': 'Default Title'}]}],
        'variants': [{'optionValues': [{'optionName': 'Title', 'name': 'Default Title'}],
                      'price': f'{p.price:.2f}'}],
    }}
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post(f'https://{STORE}/admin/api/{API_VER}/graphql.json',
                         headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                         json={'query': q, 'variables': variables})
    d = r.json()
    if d.get('errors'):
        raise HTTPException(502, json.dumps(d['errors'])[:300])
    ps = (d.get('data') or {}).get('productSet') or {}
    if ps.get('userErrors'):
        raise HTTPException(400, json.dumps(ps['userErrors'])[:300])
    return {'ok': True, 'product': ps.get('product')}

# ---------- list products ----------
@app.get('/products')
async def products():
    tok = await get_token()
    q = '{ products(first: 25) { nodes { title status onlineStoreUrl } } }'
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f'https://{STORE}/admin/api/{API_VER}/graphql.json',
                         headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                         json={'query': q})
        d = r.json()
    if d.get('errors'):
        raise HTTPException(502, json.dumps(d['errors'])[:300])
    return {'products': (d.get('data') or {}).get('products', {}).get('nodes', [])}

# ---------- PostZen (social auto-post: Instagram, YouTube, +9) ----------
PZ_KEY   = ''.join(ch for ch in os.getenv('POSTZEN_KEY', '') if 32 <= ord(ch) < 127).strip()
PZ_BASE  = 'https://api.postzen.dev/v1'
_pz_prof = {'id': '', 'name': ''}

def _pz_h(extra=None):
    h2 = {'Authorization': 'Bearer ' + PZ_KEY, 'Content-Type': 'application/json'}
    if extra:
        h2.update(extra)
    return h2

async def _pz_profile() -> str:
    if _pz_prof['id']:
        return _pz_prof['id']
    if not PZ_KEY:
        raise HTTPException(400, 'POSTZEN_KEY env set nahi hai (Render dashboard me daalo)')
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(PZ_BASE + '/profiles', headers=_pz_h())
        if r.status_code in (401, 403):
            raise HTTPException(502, 'POSTZEN_KEY galat/expired (' + str(r.status_code) + ') - Render > Environment me SAHI key daalo')
        if r.status_code >= 400:
            raise HTTPException(502, 'PostZen profiles fail (' + str(r.status_code) + '): ' + r.text[:160])
        try:
            ps = r.json().get('profiles', [])
        except Exception:
            raise HTTPException(502, 'PostZen response alag hai: ' + r.text[:120])
        if ps:
            _pz_prof['id'] = ps[0]['_id']; _pz_prof['name'] = ps[0].get('name', '')
        else:
            r2 = await c.post(PZ_BASE + '/profiles', headers=_pz_h(),
                              json={'name': 'NEXUS', 'description': 'NEXUS Agent auto-posting'})
            if r2.status_code in (401, 403):
                raise HTTPException(502, 'POSTZEN_KEY galat/expired (' + str(r2.status_code) + ') - Render > Environment me SAHI key daalo')
            if r2.status_code >= 400:
                raise HTTPException(502, 'PostZen profile ban nahi payi (' + str(r2.status_code) + '): ' + r2.text[:160])
            p = r2.json().get('profile') or {}
            _pz_prof['id'] = p.get('_id', ''); _pz_prof['name'] = p.get('name', 'NEXUS')
    if not _pz_prof['id']:
        raise HTTPException(502, 'PostZen profile nahi ban payi')
    return _pz_prof['id']

_PZ_SHA = {'new': '9028de76', 'old': 'a0c9085f'}

@app.get('/pz/dbg')
async def pz_dbg():
    import hashlib
    sha8 = hashlib.sha256(PZ_KEY.encode()).hexdigest()[:8] if PZ_KEY else '(empty)'
    return {'len': len(PZ_KEY), 'first6': PZ_KEY[:6], 'last4': PZ_KEY[-4:], 'sha8': sha8,
            'isNew': sha8 == _PZ_SHA['new'], 'isOld': sha8 == _PZ_SHA['old']}

@app.get('/pz/accounts')
async def pz_accounts():
    pid = await _pz_profile()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(PZ_BASE + '/accounts', headers=_pz_h(), params={'profileId': pid})
        if r.status_code >= 400:
            raise HTTPException(502, 'PostZen accounts fail (' + str(r.status_code) + '): ' + r.text[:160])
    return {'accounts': r.json().get('accounts', [])}

@app.get('/pz/connect/{platform}')
async def pz_connect(platform: str):
    pid = await _pz_profile()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(PZ_BASE + '/connect/' + platform, headers=_pz_h(),
                        params={'profileId': pid, 'redirectUrl': 'https://app.postzen.dev/connected'})
        if r.status_code >= 400:
            raise HTTPException(502, 'PostZen connect fail: ' + r.text[:200])
        d = r.json()
    return {'authUrl': d.get('authUrl'), 'note': 'Link 10 minute me expire - TURANT kholo'}

class SocialPost(BaseModel):
    content: str
    platform: str = 'instagram'
    imageUrl: str = ''        # product ki public image URL (shopify se)
    mediaUrl: str = ''        # koi bhi public image/video URL (100MB tak)
    mediaItems: list = []     # ya pehle se upload ki hui items [{url, title?}]
    postType: str = ''        # instagram: feed | story | reel | carousel
    firstComment: str = ''
    title: str = ''           # youtube video title fallback
    tags: list = []
    draft: bool = False       # True = sirf save, publish nahi
    scheduledFor: str = ''    # ISO UTC time (future) - ya publishNow
    extraSettings: dict = {}  # platform-specific passthrough

@app.post('/post')
async def pz_post(b: SocialPost):
    pid = await _pz_profile()
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(PZ_BASE + '/accounts', headers=_pz_h(),
                        params={'profileId': pid, 'platform': b.platform, 'status': 'connected'})
        r.raise_for_status()
        accs = r.json().get('accounts', [])
        if not accs:
            raise HTTPException(400, b.platform + ' connected nahi hai - pehle /pz/connect/' + b.platform + ' se jodo')
        tgt = {'platform': b.platform, 'accountId': accs[0]['_id']}
        st = {}
        if b.postType: st['postType'] = b.postType
        if b.firstComment: st['firstComment'] = b.firstComment
        if b.extraSettings: st.update(b.extraSettings)
        if st: tgt['settings'] = st
        body = {'content': b.content, 'platforms': [tgt]}
        media = ([{'url': b.imageUrl}] if (not b.mediaUrl and not b.mediaItems and b.imageUrl)
                 else ([{'url': b.mediaUrl}] if b.mediaUrl else b.mediaItems))
        if media: body['mediaItems'] = media
        if b.title: body['title'] = b.title
        if b.tags: body['tags'] = b.tags
        if b.draft: body['isDraft'] = True
        elif b.scheduledFor: body['scheduledFor'] = b.scheduledFor
        else: body['publishNow'] = True
        r2 = await c.post(PZ_BASE + '/posts', headers=_pz_h({'x-request-id': str(uuid.uuid4())}), json=body)
        if r2.status_code >= 400:
            raise HTTPException(502, 'PostZen post fail (%d): %s' % (r2.status_code, r2.text[:250]))
        return {'ok': True, 'post': r2.json().get('post')}

# ---------- YouTube trending research (Data API v3 - sirf SEARCH, upload nahi) ----------
YT_KEY = ''.join(ch for ch in os.getenv('YT_API_KEY', '') if 32 <= ord(ch) < 127).strip()

@app.get('/yt/trending')
async def yt_trending(q: str = 'mobile accessories', maxr: int = 5):
    if not YT_KEY:
        raise HTTPException(400, 'YT_API_KEY env set nahi hai (Google Cloud free key)')
    import urllib.parse
    params = urllib.parse.urlencode({'part': 'snippet', 'q': q, 'type': 'video',
        'order': 'viewCount', 'publishedAfter': '2026-07-01T00:00:00Z', 'maxResults': max(min(maxr,10),1)})
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get('https://youtube.googleapis.com/youtube/v3/search?' + params,
                        headers={'Accept': 'application/json', 'x-goog-api-key': YT_KEY})
        if r.status_code >= 400:
            raise HTTPException(502, 'YouTube API fail (%d): %s' % (r.status_code, r.text[:200]))
        d = r.json()
    out = [{'title': i['snippet']['title'],
            'channel': i['snippet']['channelTitle'],
            'link': 'https://youtube.com/watch?v=' + i['id']['videoId'],
            'published': i['snippet']['publishedAt'][:10]} for i in d.get('items', [])]
    return {'query': q, 'videos': out}


# ---------- VIDEO AGENT (free: Gemini images + gTTS voice + ffmpeg render) ----------
import asyncio, subprocess, threading, base64
from pathlib import Path
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS as _gTTS
import imageio_ffmpeg
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
HF_TOKEN = ''.join(ch for ch in os.getenv('HF_TOKEN', '') if 32 <= ord(ch) < 127).strip()
MEDIA_DIR = Path('/tmp/nexus_media'); MEDIA_DIR.mkdir(parents=True, exist_ok=True)
VIDS = {}
_BUNDLED_FONT = Path(__file__).parent / 'fonts' / 'DejaVuSans-Bold.ttf'
_IMG_MODELS = ('gemini-2.5-flash-image', 'gemini-3.1-flash-image-preview', 'gemini-3.1-flash-image', 'gemini-3-pro-image-preview', 'gemini-3-pro-image', 'gemini-3.1-flash-lite-image')

class VideoReq(BaseModel):
    prompt: str
    scenes: int = 6
    texts: list = []      # har scene ka text overlay (optional)
    voice: str = 'hi'     # hi | en | none
    seconds: float = 0    # total length; 0 = auto (scenes x 4.5s)

async def _gen_image_gemini(prompt: str) -> bytes:
    last = ''
    async with httpx.AsyncClient(timeout=120) as c:
        for model in _IMG_MODELS:
            try:
                r = await c.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                    headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_KEY},
                    json={'contents': [{'parts': [{'text': prompt}]}],
                          'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}})
                if r.status_code >= 400:
                    last = f'{model} {r.status_code}: ' + r.text[:140]
                    continue
                parts = r.json().get('candidates', [{}])[0].get('content', {}).get('parts', [])
                for p in parts:
                    if p.get('inlineData', {}).get('data'):
                        return base64.b64decode(p['inlineData']['data'])
                last = model + ': image part nahi mila'
            except Exception as e:
                last = model + ': ' + str(e)[:120]
    raise RuntimeError(last or 'gemini img fail')

async def _gen_image_hf(prompt: str) -> bytes:
    url = 'https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell'
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, headers={'Authorization': 'Bearer ' + HF_TOKEN}, json={'inputs': prompt})
        if r.status_code == 503:
            await asyncio.sleep(20)
            r = await c.post(url, headers={'Authorization': 'Bearer ' + HF_TOKEN}, json={'inputs': prompt})
        if r.status_code >= 400:
            raise RuntimeError(f'hf {r.status_code}: ' + r.text[:140])
        return r.content

def _local_scene(prompt: str, seed: int, out: Path):
    import math
    seed2 = seed * 97 + sum(ord(c) for c in prompt) % 1000
    im = Image.new('RGB', (1080, 1920), (6, 12, 30))
    d = ImageDraw.Draw(im, 'RGBA')
    cx, cy = 540 + (seed2 % 200) - 100, 800 + (seed2 % 300) - 150
    for rr in range(340, 8, -6):
        hue = (seed2 + rr) % 360
        col = (int(120 + 100 * math.sin(math.radians(hue))), int(90 + 80 * math.sin(math.radians(hue + 90))), 40 + (rr % 90), 60)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=col, width=3)
    for i in range(140):
        import random as _r
        _r.seed(seed2 + i)
        x, y, r = _r.randint(0, 1080), _r.randint(0, 1920), _r.choice([1, 1, 2, 2, 3])
        a = _r.randint(90, 220)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(200, 220, 255, a))
    d.text((60, 60), 'NEXUS', fill=(255, 215, 94, 200))
    im.save(out, quality=92)

async def _gen_image(prompt: str, seed: int, out: Path):
    last = ''
    for attempt in range(2):
        try:
            data = await _gen_image_gemini(prompt + f' (variation {attempt})')
            out.write_bytes(data); return 'gemini'
        except Exception as e:
            last = str(e)
        if HF_TOKEN:
            try:
                data = await _gen_image_hf(prompt)
                out.write_bytes(data); return 'hf'
            except Exception as e:
                last = str(e)
    _local_scene(prompt, seed, out)
    return 'local-fallback'

def _bake(img_path: Path, text: str):
    im = Image.open(img_path).convert('RGB')
    tw, th = 720, 1280
    r = max(tw / im.width, th / im.height)
    im = im.resize((round(im.width * r), round(im.height * r)))
    x = (im.width - tw) // 2; y = (im.height - th) // 2
    im = im.crop((x, y, x + tw, y + th))
    d = ImageDraw.Draw(im, 'RGBA')
    for gy in range(920, 1280, 8):
        a = int(120 * (gy - 920) / 360)
        d.rectangle([0, gy, tw, gy + 8], fill=(0, 0, 20, a))
    if text:
        sz = 46
        f = (ImageFont.truetype(str(_BUNDLED_FONT), sz) if _BUNDLED_FONT.exists() else ImageFont.load_default())
        while _BUNDLED_FONT.exists() and d.textlength(text, font=f) > 650 and sz > 24:
            sz -= 4; f = ImageFont.truetype(str(_BUNDLED_FONT), sz)
        w = d.textlength(text, font=f)
        d.text(((tw - w) / 2 + 2, 995), text, font=f, fill=(0, 0, 0, 200))
        d.text(((tw - w) / 2, 993), text, font=f, fill=(255, 255, 255, 255))
    im.save(img_path, quality=92)

def _worker(vid: str, b: VideoReq):
    v = VIDS[vid]
    try:
        work = MEDIA_DIR / vid; work.mkdir(parents=True, exist_ok=True)
        n = max(2, min(8, b.scenes))
        style = ', cinematic lighting, rich detail, vertical 9:16 composition, no text'
        for i in range(n):
            v['stage'] = f'image {i+1}/{n}'
            img = work / f'sc{i}.jpg'
            eng = asyncio.run(_gen_image(b.prompt + style, i * 137 + 11, img))
            v['engine'] = eng
            _bake(img, b.texts[i] if i < len(b.texts) else '')
            time.sleep(1.2)
        v['stage'] = 'voice'
        vo = ''
        if b.voice in ('hi', 'en'):
            try:
                vo = str(work / 'vo.mp3')
                _gTTS(text=b.prompt[:450], lang=b.voice).save(vo)
            except Exception:
                vo = ''
        v['stage'] = 'render'
        d = max(45, int(30 * ((b.seconds or n * 4.5) / n)))
        total = round(n * d / 30, 2)
        parts = []
        for i in range(n):
            if i % 2 == 0:
                zp = f"zoompan=z='min(1+0.0035*on,1.5)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={d}:s=1080x1920:fps=30"
            else:
                zp = f"zoompan=z='if(eq(on,0),1.5,max(zoom-0.0037,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={d}:s=1080x1920:fps=30"
            parts.append(f'[{i}:v]{zp},setsar=1[v{i}]')
        fc = ';'.join(parts) + ';' + ''.join(f'[v{i}]' for i in range(n)) + f'concat=n={n}:v=1:a=0,format=yuv420p[vc]'
        cmd = [FFMPEG_BIN, '-y']
        for i in range(n): cmd += ['-i', str(work / f'sc{i}.jpg')]
        out = MEDIA_DIR / f'{vid}.mp4'
        if vo:
            fc += f';[{n}:a]adelay=600|600,apad[aud]'
            cmd += ['-i', vo, '-filter_complex', fc, '-map', '[vc]', '-map', '[aud]']
        else:
            cmd += ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100']
            fc += ';anullsrc=channel_layout=stereo:sample_rate=44100[aud]'
            cmd += ['-filter_complex', fc, '-map', '[vc]', '-map', '[aud]']
        cmd += ['-t', str(total), '-r', '30', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '27',
                '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out)]
        rr = subprocess.run(cmd, capture_output=True, text=True)
        if rr.returncode != 0 or not out.exists():
            raise RuntimeError('ffmpeg: ' + rr.stderr[-220:])
        for i in range(n):
            try: (work / f'sc{i}.jpg').unlink()
            except Exception: pass
        v.update(status='done', stage='ready', url=f'/media/{vid}.mp4')
        _vids_save()
    except Exception as e:
        v.update(status='error', error=str(e)[:280])

@app.post('/video')
async def video_make(b: VideoReq):
    if not b.prompt.strip(): raise HTTPException(400, 'prompt khaali hai')
    vid = uuid.uuid4().hex[:12]
    VIDS[vid] = {'status': 'working', 'stage': 'queued', 'url': '', 'error': ''}
    threading.Thread(target=_worker, args=(vid, b), daemon=True).start()
    return {'ok': True, 'id': vid, 'check': '/video/' + vid}

@app.get('/videos')
async def videos_list():
    out = []
    for vid, v in VIDS.items():
        out.append({'id': vid, 'status': v.get('status'), 'stage': v.get('stage'), 'url': v.get('url'), 'site': v.get('site'), 'url_in': v.get('url')})
    out.sort(key=lambda x: x['id'], reverse=True)
    return {'ok': True, 'count': len(out), 'videos': out[-25:]}

@app.get('/video/{vid}')
async def video_status(vid: str):
    v = VIDS.get(vid)
    if not v: raise HTTPException(404, 'id nahi mili')
    return v

@app.get('/media/{fname}')
async def media_serve(fname: str):
    if '/' in fname or '..' in fname: raise HTTPException(400, 'nope')
    p = MEDIA_DIR / fname
    if not p.exists(): raise HTTPException(404, 'file gayab')
    return FileResponse(p, media_type='video/mp4')


# ---------- AUTO-BRAIN: topic research + prompt likhna, sab khud ----------
class AutoReq(BaseModel):
    topic: str = ''       # khaali chhodo = agent KHUD trending topic chunega
    niche: str = 'viral AI tools, tech hacks aur future technology — Indian audience'
    scenes: int = 5
    voice: str = 'hi'

async def _plan_with_gemini(b: AutoReq) -> dict:
    ask = ('You are a viral Instagram reel director for an Indian audience. '
        + (f'Topic: "{b.topic}". ' if b.topic.strip() else f'Pick ONE fresh, scroll-stopping trending topic in this niche: {b.niche}. ')
        + f'Design a {b.scenes}-scene vertical 9:16 reel. '
        + 'Reply ONLY with raw JSON, no markdown fences:\n'
        + '{"topic":"short topic name","voice_script":"35-45 word punchy Hindi voiceover script",'
        + '"scenes":[{"prompt":"detailed ENGLISH visual prompt for an AI image generator, vertical composition",'
        + '"text":"max 5 word punchy overlay text"}]}')
    try:
        txt, _m = await _ai(ask, max_tok=3000, temp=0.9)
    except Exception as e:
        raise RuntimeError('gemini plan sab fail - ' + str(e)[:160])
    m = re.search(r'\{[\s\S]*\}', txt)
    if not m:
        raise RuntimeError('plan JSON nahi mila: ' + txt[:120])
    return json.loads(m.group(0))

def _plan_local(b: AutoReq) -> dict:
    topic = b.topic.strip() or 'AI agents ne kaam sambhal liya — reels se trading tak'
    p = [
        'futuristic glowing AI orb in dark starfield, golden particles',
        'holographic screens showing charts and code in dark room',
        'robotic hand assembling digital puzzle pieces, neon light',
        'smartphone with glowing app icons floating around it',
        'golden sunrise over futuristic city skyline, hopeful mood',
    ]
    t = ['NEXUS AI', 'SAB AUTO', '19 AGENTS', 'TUM BAS DEKHO', 'FUTURE AA GAYA']
    n = max(3, min(8, b.scenes))
    return {'topic': topic[:90],
            'voice_script': 'Nexus ke agents ne kaam sambhal liya. Soch, plan, banana, post karna - sab automatic. Tum bas dekho aur muskurao!',
            'scenes': [{'prompt': p[i % 5], 'text': t[i % 5]} for i in range(n)]}

def _worker_auto(vid: str, b: AutoReq, plan: dict):
    v = VIDS[vid]
    try:
        work = MEDIA_DIR / vid; work.mkdir(parents=True, exist_ok=True)
        scenes = [x for x in plan.get('scenes', []) if isinstance(x, dict)][:8]
        if not scenes: raise RuntimeError('plan me scenes nahi')
        n = len(scenes)
        v['plan'] = {'topic': str(plan.get('topic', ''))[:80], 'scenes': n}
        for i, sc in enumerate(scenes):
            v['stage'] = f'image {i+1}/{n}'
            img = work / f'sc{i}.jpg'
            eng = asyncio.run(_gen_image(str(sc.get('prompt', 'cinematic abstract technology'))[:600]
                                         + ', cinematic lighting, rich detail, vertical 9:16 composition, no text',
                                         i * 137 + 11, img))
            v['engine'] = eng
            _bake(img, str(sc.get('text', ''))[:40])
            time.sleep(1.0)
        v['stage'] = 'voice'
        vo = ''
        script = str(plan.get('voice_script', ''))[:450]
        if b.voice in ('hi', 'en') and script:
            try:
                vo = str(work / 'vo.mp3'); _gTTS(text=script, lang=b.voice).save(vo)
            except Exception:
                vo = ''
        v['stage'] = 'render'
        d = 112; total = round(n * d / 24, 2)
        parts = []
        for i in range(n):
            if i % 2 == 0:
                zp = f"zoompan=z='min(1+0.0035*on,1.5)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={d}:s=720x1280:fps=24"
            else:
                zp = f"zoompan=z='if(eq(on,0),1.5,max(zoom-0.0037,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={d}:s=720x1280:fps=24"
            parts.append(f'[{i}:v]{zp},setsar=1[v{i}]')
        fc = ';'.join(parts) + ';' + ''.join(f'[v{i}]' for i in range(n)) + f'concat=n={n}:v=1:a=0,format=yuv420p[vc]'
        cmd = [FFMPEG_BIN, '-y']
        for i in range(n): cmd += ['-i', str(work / f'sc{i}.jpg')]
        out = MEDIA_DIR / f'{vid}.mp4'
        if vo:
            fc += f';[{n}:a]adelay=600|600,apad[aud]'
            cmd += ['-i', vo, '-filter_complex', fc, '-map', '[vc]', '-map', '[aud]']
        else:
            fc += ';anullsrc=channel_layout=stereo:sample_rate=44100[aud]'
            cmd += ['-filter_complex', fc, '-map', '[vc]', '-map', '[aud]']
        cmd += ['-t', str(total), '-r', '30', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '27',
                '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out)]
        rr = subprocess.run(cmd, capture_output=True, text=True)
        if rr.returncode != 0 or not out.exists():
            raise RuntimeError('ffmpeg: ' + rr.stderr[-220:])
        for i in range(n):
            try: (work / f'sc{i}.jpg').unlink()
            except Exception: pass
        v.update(status='done', stage='ready', url=f'/media/{vid}.mp4', topic=str(plan.get('topic', ''))[:80])
        _vids_save()
    except Exception as e:
        v.update(status='error', error=str(e)[:280])

@app.post('/video/auto')
async def video_auto(b: AutoReq):
    try:
        plan = await _plan_with_gemini(b)
        planner = 'gemini'
    except Exception as e:
        plan = _plan_local(b)
        planner = 'local-fallback (' + str(e)[:90] + ')'
    vid = uuid.uuid4().hex[:12]
    VIDS[vid] = {'status': 'working', 'stage': 'queued', 'url': '', 'error': '',
                 'plan': {'topic': str(plan.get('topic', ''))[:80], 'scenes': len(plan.get('scenes', []))}}
    threading.Thread(target=_worker_auto, args=(vid, b, plan), daemon=True).start()
    return {'ok': True, 'id': vid, 'topic': str(plan.get('topic', ''))[:80], 'planner': planner, 'check': '/video/' + vid}


# ---------- FOOTAGE MODE: user ka video -> auto-edit reel ----------
from fastapi import UploadFile, File

@app.post('/footage')
async def footage_upload(file: UploadFile = File(...), voice: str = 'hi', texts: str = ''):
    vid = uuid.uuid4().hex[:12]
    work = MEDIA_DIR / vid; work.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or 'v.mp4').suffix or '.mp4'
    raw = work / ('raw' + suffix)
    data = await file.read()
    if len(data) > 90 * 1024 * 1024:
        raise HTTPException(400, 'video 90MB se chhoti honi chahiye')
    raw.write_bytes(data)
    VIDS[vid] = {'status': 'working', 'stage': 'queued', 'url': '', 'error': ''}
    tlist = [t.strip() for t in (texts or '').split('|') if t.strip()]
    threading.Thread(target=_worker_footage, args=(vid, raw, voice, tlist), daemon=True).start()
    return {'ok': True, 'id': vid, 'check': '/video/' + vid}

def _worker_footage(vid: str, raw: Path, voice: str, texts: list):
    v = VIDS[vid]
    try:
        work = raw.parent
        v['stage'] = 'scan'
        # 1) smart cut: footage ko 4 segments me split (har ek ko halka trim + crop 9:16 + boost)
        n = 4
        segs = []
        for i in range(n):
            seg = work / f'seg{i}.mp4'
            ss = i * 2.2
            rr = subprocess.run([FFMPEG_BIN, '-y', '-ss', str(ss), '-i', str(raw), '-t', '2.2',
                '-vf', "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,eq=brightness=0.02:saturation=1.12,setsar=1",
                '-an', '-r', '24', '-threads', '2', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26', str(seg)],
                capture_output=True, text=True)
            if rr.returncode == 0 and seg.exists() and seg.stat().st_size > 10000:
                segs.append(seg)
        if not segs:
            raise RuntimeError('footage scan fail (video kharab ya format odd hai)')
        n = len(segs)
        v['stage'] = 'text'
        for i, seg in enumerate(segs):
            if i < len(texts) and texts[i]:
                img_work = work / f'fr{i}.png'
                subprocess.run([FFMPEG_BIN, '-y', '-ss', '0.5', '-i', str(seg), '-frames:v', '1', str(img_work)],
                               capture_output=True, text=True)
                if img_work.exists():
                    _bake(img_work, texts[i][:40])
                    subprocess.run([FFMPEG_BIN, '-y', '-loop', '1', '-i', str(img_work), '-t', '0.9',
                        '-r', '24', '-threads', '2', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26', '-pix_fmt', 'yuv420p',
                        str(work / f'cap{i}.mp4')], capture_output=True, text=True)
                    if (work / f'cap{i}.mp4').exists():
                        segs.insert(i + 1, work / f'cap{i}.mp4')
        n = len(segs)
        v['stage'] = 'voice'
        vo = ''
        if voice in ('hi', 'en'):
            try:
                script = 'Ye reel aapke AI agent ne banayi hai. Footage diya, editing khud ki. Nexus sab sambhal leta hai!'
                vo = str(work / 'vo.mp3'); _gTTS(text=script, lang=voice).save(vo)
            except Exception:
                vo = ''
        v['stage'] = 'render'
        parts = []
        for i, seg in enumerate(segs):
            try:
                probe = subprocess.run([FFMPEG_BIN, '-i', str(seg)], capture_output=True, text=True)
                dur = 2.2
                for line in probe.stderr.split('\n'):
                    if 'Duration:' in line:
                        hh, mm, ss2 = line.split('Duration:')[1].split(',')[0].strip().split(':')
                        dur = float(hh) * 3600 + float(mm) * 60 + float(ss2)
                        break
            except Exception:
                dur = 2.2
            parts.append((seg, round(dur, 2)))
        # build filter: har seg ko apni duration ke saath concat
        inputs = []
        for seg, _ in parts: inputs += ['-i', str(seg)]
        fc = ''.join(f'[{i}:v]setsar=1[v{i}];' for i in range(len(parts)))
        fc += ''.join(f'[v{i}]' for i in range(len(parts))) + f'concat=n={len(parts)}:v=1:a=0,format=yuv420p[vc]'
        cmd = [FFMPEG_BIN, '-y'] + inputs
        out = MEDIA_DIR / f'{vid}.mp4'
        if vo:
            fc += ';[len(parts):a]adelay=500|500,apad[aud]'
            fc = fc.replace(f'[len(parts):a]', f'[{len(parts)}:a]')
            cmd += ['-i', vo, '-filter_complex', fc, '-map', '[vc]', '-map', '[aud]']
        else:
            cmd += ['-filter_complex', fc + ';anullsrc=channel_layout=stereo:sample_rate=44100[aud]', '-map', '[vc]', '-map', '[aud]']
        total = round(sum(d for _, d in parts), 2)
        cmd += ['-t', str(total), '-r', '24', '-threads', '2', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26',
                '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out)]
        rr = subprocess.run(cmd, capture_output=True, text=True)
        if rr.returncode != 0 or not out.exists():
            raise RuntimeError('render: ' + rr.stderr[-200:])
        try:
            raw.unlink()
            for seg in segs:
                try: seg.unlink()
                except Exception: pass
        except Exception: pass
        v.update(status='done', stage='ready', url=f'/media/{vid}.mp4')
        _vids_save()
    except Exception as e:
        v.update(status='error', error=str(e)[:280])


# ---------- TUTOR-BRAIN: meri soch, NEXUS ke andar ----------
BRAIN_MD = ''
_bp = Path(__file__).parent / 'BRAIN.md'
try: BRAIN_MD = _bp.read_text(encoding='utf-8')
except Exception: BRAIN_MD = ''

class AskReq(BaseModel):
    q: str

@app.post('/brain/ask')
async def brain_ask(b: AskReq):
    if not BRAIN_MD:
        raise HTTPException(500, 'BRAIN.md load nahi hua')
    sys = ('Tum NEXUS-JARVIS ho — Malik (Sir) ka personal AI, Iron-Man ke JARVIS jaisa: shant, witty, loyal. '
           'Neeche tumhara BRAIN document hai — uske rules aur data HAMESHA follow karo. '
           'Malik ko "Sir" bolo. Hinglish me, MAX 2 crisp lines — pehle seedha jawab, phir zaroorat ho to 1 chhota detail. '
           'Market/trade sawal = sirf data-based clear verdict (LONG/WAIT/SHORT + reason), hype nahi. '
           'Bina matlab ka lamba gyan kabhi nahi.\n\n=== BRAIN ===\n' + BRAIN_MD + '\n=== END BRAIN ===')
    try:
        ans, model = await _ai(sys + '\n\nMalik puchta hai: ' + b.q[:800], max_tok=2200, temp=0.8)
        return {'ok': True, 'model': model, 'answer': ans[:900]}
    except Exception as e:
        raise HTTPException(502, 'Gemini sab models fail - ' + str(e)[:200])

@app.get('/brain/status')
async def brain_status():
    return {'loaded': bool(BRAIN_MD), 'chars': len(BRAIN_MD)}

@app.get('/models')
async def models_list():
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.get('https://generativelanguage.googleapis.com/v1beta/models',
                        headers={'x-goog-api-key': GEMINI_KEY})
        if r.status_code >= 400:
            raise HTTPException(502, 'models ' + str(r.status_code) + ': ' + r.text[:200])
        names = [m.get('name', '').replace('models/', '') for m in r.json().get('models', [])]
        return {'count': len(names), 'models': names}

@app.get('/imgtest')
async def imgtest():
    errs = []
    async with httpx.AsyncClient(timeout=120) as c:
        for model in _IMG_MODELS:
            try:
                r = await c.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                    headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_KEY},
                    json={'contents': [{'parts': [{'text': 'a glowing neon robot DJ performing at a Mumbai street food stall at night, cinematic, vibrant'}]}],
                          'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}})
                if r.status_code >= 400:
                    errs.append(f'{model} {r.status_code}: ' + r.text[:120])
                    continue
                parts = r.json().get('candidates', [{}])[0].get('content', {}).get('parts', [])
                for p in parts:
                    if p.get('inlineData', {}).get('data'):
                        return {'ok': True, 'model': model, 'bytes': len(base64.b64decode(p['inlineData']['data'])), 'errors': errs}
                errs.append(model + ': image part nahi mila')
            except Exception as e:
                errs.append(model + ': ' + str(e)[:120])
    raise HTTPException(502, 'koi image model nahi chala | ' + ' || '.join(errs))


# ============================ XRAY — REA-lite (website ka x-ray) ============================
import secrets as _secrets
XRAYS = {}
_FRAME_SIGNS = {
    'React': ['react', '__NEXT_DATA__', 'data-reactroot'],
    'Next.js': ['next', '__NEXT_DATA__', '/_next/'],
    'Vue': ['vue', 'data-v-'],
    'Nuxt': ['__NUXT__', '/_nuxt/'],
    'Svelte': ['svelte'],
    'Angular': ['ng-version', 'zone.js'],
    'jQuery': ['jquery'],
    'Bootstrap': ['bootstrap'],
    'Tailwind': ['tailwind'],
    'WordPress': ['wp-content', 'wp-includes'],
    'Shopify': ['cdn.shopify.com', 'Shopify.theme'],
    'Wix': ['wixstatic', 'wix.com'],
    'Webflow': ['webflow'],
    'Three.js': ['three.min.js', 'three.module', 'THREE.'],
    'GSAP': ['gsap', 'ScrollTrigger'],
    'Font Awesome': ['font-awesome', 'fontawesome'],
}

def _xray_signals(url: str, html: str) -> dict:
    import re as _re
    import html as _html
    h = html[:500000]
    low = h.lower()
    title = ''
    m = _re.search(r'<title[^>]*>(.*?)</title>', h, _re.S | _re.I)
    if m:
        title = _html.unescape(m.group(1)).strip()[:120]
    desc = ''
    m = _re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', h, _re.S | _re.I)
    if m:
        desc = _html.unescape(m.group(1)).strip()[:220]
    heads = [_html.unescape(_re.sub(r'<[^>]+>', '', x.group(2))).strip()[:90] for x in list(_re.finditer(r'<h([1-3])[^>]*>(.*?)</h\1>', h, _re.S | _re.I))[:14]]
    scripts = list(dict.fromkeys(_re.findall(r'<script[^>]+src=["\'](https?://[^/"\'\s]+)', h, _re.I)))[:15]
    styles = list(dict.fromkeys(_re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\'](https?://[^/"\'\s]+)', h, _re.I)))[:12]
    fonts = list(dict.fromkeys(_re.findall(r'font-family:\s*([^;\}]{3,40})', low)))[:8]
    colors = list(dict.fromkeys(_re.findall(r'#[0-9a-f]{6}\b', low)))[:18]
    imgs = len(_re.findall(r'<img', h, _re.I))
    links = len(_re.findall(r'<a\s', h, _re.I))
    iframes = len(_re.findall(r'<iframe', h, _re.I))
    found = [k for k, signs in _FRAME_SIGNS.items() if any(s.lower() in low for s in signs)]
    api_hints = list(dict.fromkeys(_re.findall(r'["\'](/api/[a-z0-9_\-/\.]{2,50})["\']', low)))[:10]
    return {'url': url, 'title': title, 'desc': desc, 'kb': round(len(h) / 1024), 'heads': heads, 'scripts': scripts,
            'styles': styles, 'fonts': fonts, 'colors': colors, 'imgs': imgs, 'links': links, 'iframes': iframes,
            'stack': found, 'api_hints': api_hints}

def _signals_text(s: dict) -> str:
    L = []
    L.append('URL: ' + s['url'])
    L.append('TITLE: ' + (s['title'] or '?'))
    if s['desc']:
        L.append('DESC: ' + s['desc'])
    L.append('SIZE: ~' + str(s['kb']) + 'KB html | imgs:' + str(s['imgs']) + ' links:' + str(s['links']) + ' iframes:' + str(s['iframes']))
    L.append('DETECTED STACK: ' + (', '.join(s['stack']) if s['stack'] else 'kuch nahi pakda (custom/static)'))
    if s['scripts']:
        L.append('SCRIPT DOMAINS: ' + ', '.join(s['scripts']))
    if s['styles']:
        L.append('CSS FILES: ' + ', '.join(s['styles'][:6]))
    if s['fonts']:
        L.append('FONTS: ' + ', '.join(s['fonts']))
    if s['colors']:
        L.append('COLORS: ' + ' '.join(s['colors']))
    if s['heads']:
        L.append('HEADINGS: ' + ' | '.join(s['heads']))
    if s['api_hints']:
        L.append('API HINTS: ' + ', '.join(s['api_hints']))
    return '\n'.join(L)

async def _xray_report(sig_text: str) -> str:
    ask = ('Tu senior web-forensics expert hai. Neeche ek website ke raw signals hain (server-side scan se mile).\n'
           'Hinglish TEARDOWN REPORT de, exactly in sections me (plain text, ** bold ** chalega):\n'
           '1) TECH STACK (har cheez confidence ke sath - pakka vs andaza)\n'
           '2) DESIGN SYSTEM (colors/fonts/layout ka use)\n'
           '3) PAGE STRUCTURE (kya kahan hai)\n'
           '4) WOW-FACTOR (is site ka khaas kya hai)\n'
           '5) APNA ORIGINAL VERSION - blueprint (kaise apna version banaye, 5-7 concrete steps)\n'
           '6) 3 VIRAL REEL-LINES is site ke bare me (Hinglish, catchy)\n'
           'Total 3000 chars se kam. Signals:\n\n' + sig_text)
    return (await _ai(ask, max_tok=4000, temp=0.7))[0][:3200]

async def _xray_worker(xid: str, url: str):
    st = XRAYS[xid]
    try:
        st['stage'] = 'fetching'
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                         'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                         'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8'}) as c:
            r = await c.get(url)
            if r.status_code >= 400:
                st.update(status='error', error=f'site ne mana kar diya HTTP {r.status_code} (kuch sites bot-block karti hain - doosri URL try karo)')
                return
            html = r.text
        st['stage'] = 'signals'
        sig = _xray_signals(url, html)
        st['site'] = sig.get('title') or url
        st['signals'] = sig
        st['stage'] = 'report'
        rep = await _xray_report(_signals_text(sig))
        st.update(status='done', stage='ready', report=rep)
    except Exception as e:
        st.update(status='error', error=str(e)[:220])

class XrayReq(BaseModel):
    url: str

@app.post('/xray')
async def xray_start(x: XrayReq):
    url = x.url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    xid = _secrets.token_hex(5)
    XRAYS[xid] = {'status': 'working', 'stage': 'queue', 'url': url, 'error': ''}
    asyncio.create_task(_xray_worker(xid, url))
    return {'ok': True, 'id': xid, 'check': f'/xray/{xid}'}

@app.get('/xray/{xid}')
async def xray_status(xid: str):
    st = XRAYS.get(xid)
    if not st:
        raise HTTPException(404, 'xray nahi mila (restart hua to gaya)')
    out = dict(st)
    out.pop('signals', None)
    return out

async def _xray_build_worker(xid: str):
    st = XRAYS[xid]
    try:
        st['stage'] = 'building'
        bp = ('Ye ek website ka teardown hai. Iske base par ek ORIGINAL single-file HTML page bana (copy NAHI - apna original version, '
              'apna branding/naming). Modern dark theme, responsive, working hover/click interactions, <= 650 lines, sirf HTML code (ek hi file, inline css/js). '
              'Pehla character < ho. Teardown:\n\n' + st.get('report', '') + '\n\nSignals:\n' + _signals_text(st.get('signals', {})))
        try:
            txt, _m = await _ai(bp, max_tok=32768, temp=0.8, think='medium')
        except Exception as e:
            raise RuntimeError('gemini build fail ' + str(e)[:140])
        txt = txt.strip()
        if txt.startswith('```'):
            txt = txt.split('\n', 1)[1].rsplit('```', 1)[0]
        st['build_html'] = txt
        st.update(stage='build-ready', build_url=f'/xray/{xid}/build')
        return
    except Exception as e:
        st['build_err'] = str(e)[:200]
        st['stage'] = 'build-fail'

@app.post('/xray/{xid}/build')
async def xray_build(xid: str):
    st = XRAYS.get(xid)
    if not st:
        raise HTTPException(404, 'xray nahi mila')
    if st['status'] != 'done':
        raise HTTPException(400, 'pehle report ready hone do')
    if 'build_html' in st:
        return {'ok': True, 'build_url': st['build_url']}
    st['stage'] = 'building'
    asyncio.create_task(_xray_build_worker(xid))
    return {'ok': True, 'check': f'/xray/{xid}'}

@app.get('/xray/{xid}/build')
async def xray_build_view(xid: str):
    from fastapi import Response
    st = XRAYS.get(xid)
    if not st or 'build_html' not in st:
        raise HTTPException(404, 'build nahi mila')
    return Response(content=st['build_html'], media_type='text/html')

# ============================ TRADE-BRAIN (indicators + paper-bots + self-learning) ============================
TB_PAIRS = [('BTCUSDT', 'BTC'), ('ETHUSDT', 'ETH'), ('SOLUSDT', 'SOL'), ('DOGEUSDT', 'DOGE'), ('ADAUSDT', 'ADA'), ('XRPUSDT', 'XRP')]
TB_STYLES = ['trend', 'momentum', 'meanrev']
TB = {'bots': {}, 'journal': [], 'lessons': [], 'last_scan': 0}
for _i, (_sym, _nm) in enumerate(TB_PAIRS):
    TB['bots'][_sym] = {'name': 'Bot-' + _nm, 'style': TB_STYLES[_i % 3], 'pos': None, 'wins': 0, 'losses': 0, 'pnl': 0.0}

async def _tb_klines(sym: str):
    last = ''
    async with httpx.AsyncClient(timeout=15) as c:
        for url, make in (
            ('https://data-api.binance.vision/api/v3/klines?symbol=' + sym + '&interval=15m&limit=200',
             lambda k: [(float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in k]),
            ('https://api.kraken.com/0/public/OHLC?pair=' + sym.replace('USDT', 'USD') + '&interval=15',
             lambda k: [(float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in list(k.values())[-1][-200:]]),
            ('https://api.exchange.coinbase.com/products/' + sym.replace('USDT', '-USD') + '/candles?granularity=900',
             lambda k: [(float(x[5]), float(x[3]), float(x[2]), float(x[1]), float(x[4])) for x in sorted(k)]),
        ):
            try:
                r = await c.get(url)
                if r.status_code >= 400:
                    last = url.split('/')[2] + ' ' + str(r.status_code)
                    continue
                rows = make(r.json())
                if len(rows) >= 60:
                    return rows
                last = url.split('/')[2] + ' rows ' + str(len(rows))
            except Exception as e:
                last = url.split('/')[2] + ': ' + str(e)[:60]
                continue
    raise RuntimeError('klines sab fail - ' + last)

def _tb_ema(vals, n):
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e

def _tb_rsi(vals, n=14):
    if len(vals) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)

def _tb_atr(rows, n=14):
    trs = []
    for i in range(-n, 0):
        h, l, pc = rows[i][2], rows[i][3], rows[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)

def _tb_signal(sym, rows):
    closes = [r[4] for r in rows]
    px = closes[-1]
    e12, e26, e20 = _tb_ema(closes, 12), _tb_ema(closes, 26), _tb_ema(closes, 20)
    rsi = _tb_rsi(closes)
    atr = _tb_atr(rows)
    trend = 1 if (e12 > e26 and px > e20) else (-1 if (e12 < e26 and px < e20) else 0)
    if rsi <= 32:
        mom = 1
    elif rsi >= 68:
        mom = -1
    else:
        mom = 0
    hi20 = max(r[2] for r in rows[-20:])
    lo20 = min(r[3] for r in rows[-20:])
    brk = 1 if px > hi20 * 0.999 else (-1 if px < lo20 * 1.001 else 0)
    score = trend + mom + brk
    label = 'LONG' if score >= 2 else ('SHORT' if score <= -2 else 'WAIT')
    return {'px': round(px, 6), 'rsi': round(rsi, 1), 'ema_trend': 'UP' if trend > 0 else ('DOWN' if trend < 0 else 'FLAT'),
            'atr_pct': round(atr / px * 100, 2), 'score': score, 'signal': label,
            'sl': round(px - 1.6 * atr, 6) if label == 'LONG' else (round(px + 1.6 * atr, 6) if label == 'SHORT' else None),
            'tp': round(px + 2.4 * atr, 6) if label == 'LONG' else (round(px - 2.4 * atr, 6) if label == 'SHORT' else None)}

def _tb_manage(sym, sig):
    b = TB['bots'][sym]
    pos = b['pos']
    px = sig['px']
    if pos:
        hit_sl = (px <= pos['sl']) if pos['side'] == 'LONG' else (px >= pos['sl'])
        hit_tp = (px >= pos['tp']) if pos['side'] == 'LONG' else (px <= pos['tp'])
        flip = (pos['side'] == 'LONG' and sig['signal'] == 'SHORT') or (pos['side'] == 'SHORT' and sig['signal'] == 'LONG')
        if hit_sl or hit_tp or flip:
            pnl = (px - pos['entry']) / pos['entry'] * 100 * (1 if pos['side'] == 'LONG' else -1)
            reason = 'TP hit' if hit_tp else ('SL hit' if hit_sl else 'signal-flip')
            b['pnl'] = round(b['pnl'] + pnl, 2)
            if pnl >= 0:
                b['wins'] += 1
            else:
                b['losses'] += 1
            TB['journal'].append({'t': int(time.time()), 'sym': sym, 'side': pos['side'], 'entry': pos['entry'], 'exit': px,
                                  'pnl': round(pnl, 2), 'reason': reason, 'style': b['style']})
            TB['journal'] = TB['journal'][-60:]
            b['pos'] = None
    if not pos and sig['signal'] in ('LONG', 'SHORT'):
        b['pos'] = {'side': sig['signal'], 'entry': sig['px'], 'sl': sig['sl'], 'tp': sig['tp'], 'opened': int(time.time())}
    return b

@app.get('/trade/brain')
async def trade_brain():
    out = {'ok': True, 'pairs': {}, 'bots': {}}
    for sym, nm in TB_PAIRS:
        try:
            rows = await _tb_klines(sym)
            sig = _tb_signal(sym, rows)
            bot = _tb_manage(sym, sig)
            out['pairs'][nm] = sig
            out['bots'][sym] = {'name': bot['name'], 'style': bot['style'], 'wins': bot['wins'], 'losses': bot['losses'],
                                'pnl': bot['pnl'], 'pos': bot['pos']}
        except Exception as e:
            out['pairs'][nm] = {'error': str(e)[:100]}
    TB['last_scan'] = int(time.time())
    return out

@app.post('/trade/brain/learn')
async def trade_brain_learn():
    if not TB['journal']:
        return {'ok': True, 'lessons': [], 'note': 'journal khali - SCAN chalao, trades band hone par seekhega'}
    lines = []
    for j in TB['journal'][-14:]:
        lines.append(str(j['sym']) + ' ' + j['side'] + ' (' + j['style'] + '): entry ' + str(j['entry']) + ' exit ' + str(j['exit']) + ' P/L ' + str(j['pnl']) + '% (' + j['reason'] + ')')
    ask = ('Tu ek institutional trading-coach hai. Neeche paper-trading journal hai (hypothetical, real paisa nahi). '
           'Losses/wins ka pattern dhundo aur 3-5 CONCRETE Hinglish rules banao jo bots agla scan me follow karein '
           '(jaise: RSI<30 par sirf tab LONG jab EMA trend UP ho). Har rule ek line, number karke. Journal: | ')
    try:
        txt, _m = await _ai(ask + ' || '.join(lines), max_tok=2200, temp=0.6)
    except Exception as e:
        raise HTTPException(502, 'gemini learn fail - ' + str(e)[:160])
    txt = txt[:1500]
    TB['lessons'] = [{'t': int(time.time()), 'rules': txt}]
    return {'ok': True, 'lessons': TB['lessons']}

# ============================ MCP SERVER (/mcp — Digen/Gemini connectors ke liye) ============================
from fastapi import Request
from fastapi.responses import JSONResponse

MCP_TOOLS = [
    {'name': 'brain_ask', 'description': 'NEXUS brain se Hinglish jawab (guide, ideas, how-to)',
     'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string', 'description': 'sawaal Hinglish me'}}, 'required': ['q']}},
    {'name': 'scan_market', 'description': '6 crypto pairs ka live EMA/RSI/ATR paper-signals + bots ka P/L status',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'footage_from_url', 'description': 'kisi direct video-URL (mp4) se NEXUS reel banao: 9:16 crop + Hindi VO + caps. ~60 sec me /media/URL par ready hota hai',
     'inputSchema': {'type': 'object', 'properties': {'url': {'type': 'string', 'description': 'public mp4 URL'}, 'texts': {'type': 'string', 'description': 'optional captions | se alag'}, 'voice': {'type': 'string', 'enum': ['hi', 'en', 'none']}}, 'required': ['url']}},
]

async def _mcp_tool_call(name: str, args: dict):
    if name == 'brain_ask':
        d = await brain_ask(AskReq(q=str(args.get('q', ''))[:300]))
        return d.get('answer', 'jawab nahi mila')
    if name == 'scan_market':
        d = await trade_brain()
        L = []
        for nm in ('BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'XRP'):
            s = d.get('pairs', {}).get(nm, {})
            if 'error' in s:
                L.append(nm + ': err ' + s['error'][:40])
            else:
                L.append(str(nm) + ' $' + str(s.get('px')) + ' RSI ' + str(s.get('rsi')) + ' ' + str(s.get('signal')))
        for k, b in d.get('bots', {}).items():
            pos = (b.get('pos') or {}).get('side', 'flat')
            L.append(str(b.get('name')) + ' P/L ' + str(b.get('pnl')) + '% ' + str(pos))
        return '\n'.join(L)
    if name == 'footage_from_url':
        u = str(args.get('url', '')).strip()
        if not u.startswith('http'):
            raise ValueError('url chahiye')
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers={'User-Agent': 'Mozilla/5.0'}) as c:
            r = await c.get(u)
            if r.status_code >= 400:
                raise ValueError('download fail HTTP ' + str(r.status_code))
            data = r.content
        if len(data) > 90 * 1024 * 1024:
            raise ValueError('video 90MB se chhoti honi chahiye')
        vid = uuid.uuid4().hex[:12]
        work = MEDIA_DIR / vid
        work.mkdir(parents=True, exist_ok=True)
        raw = work / 'raw.mp4'
        raw.write_bytes(data)
        VIDS[vid] = {'status': 'working', 'stage': 'queued', 'url': '', 'error': ''}
        tlist = [t.strip() for t in str(args.get('texts', '') or '').split('|') if t.strip()]
        voice = str(args.get('voice', 'hi'))
        threading.Thread(target=_worker_footage, args=(vid, raw, voice, tlist), daemon=True).start()
        return 'reel bann rahi hai (~60 sec). Status: GET /video/' + vid + ' | final: /media/' + vid + '.mp4'
    raise ValueError('unknown tool: ' + name)

@app.post('/mcp')
async def mcp_endpoint(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({'jsonrpc': '2.0', 'error': {'code': -32700, 'message': 'parse error'}, 'id': None})
    if isinstance(body, list):
        body = body[0] if body else {}
    method = body.get('method', '')
    mid = body.get('id')
    def ok(result):
        return JSONResponse({'jsonrpc': '2.0', 'id': mid, 'result': result})
    if method == 'initialize':
        return ok({'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                   'serverInfo': {'name': 'nexus-mcp', 'version': '1.0.0'}})
    if method == 'notifications/initialized':
        return JSONResponse({}, status_code=202)
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': MCP_TOOLS})
    if method == 'tools/call':
        params = body.get('params', {}) or {}
        name = params.get('name', '')
        args = params.get('arguments', {}) or {}
        try:
            txt = await _mcp_tool_call(name, args)
            return ok({'content': [{'type': 'text', 'text': str(txt)[:3000]}]})
        except Exception as e:
            return ok({'content': [{'type': 'text', 'text': 'ERROR: ' + str(e)[:200]}], 'isError': True})
    return JSONResponse({'jsonrpc': '2.0', 'error': {'code': -32601, 'message': 'method nahi mila: ' + method}, 'id': mid})

@app.get('/mcp')
async def mcp_info():
    return {'ok': True, 'server': 'nexus-mcp', 'tools': [t['name'] for t in MCP_TOOLS],
            'note': 'Streamable-HTTP MCP — Digen/Gemini connectors me URL daalo'}


# ============================ AI-CORE (gemini-4-argon first chain) ============================
_AI_CHAIN = (('gemini-4-argon', {'thinkingConfig': {'thinkingLevel': 'low'}}),
             ('gemini-flash-latest', {'thinkingConfig': {'thinkingBudget': 0}}),
             ('gemini-2.5-flash', {'thinkingConfig': {'thinkingBudget': 0}}))

async def _ai(ask: str, max_tok: int = 2200, temp: float = 0.8, think: str = 'low'):
    last = ''
    async with httpx.AsyncClient(timeout=180) as c:
        for model, tc in _AI_CHAIN:
            tc_use = {'thinkingConfig': {'thinkingLevel': think}} if model == 'gemini-4-argon' else tc
            try:
                r = await c.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                    headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_KEY},
                    json={'contents': [{'role': 'user', 'parts': [{'text': ask}]}],
                          'generationConfig': {'temperature': temp, 'maxOutputTokens': max_tok, **tc_use}})
                if r.status_code < 400:
                    return r.json()['candidates'][0]['content']['parts'][0]['text'], model
                last = f'{model} {r.status_code}: ' + r.text[:120]
            except Exception as e:
                last = model + ': ' + str(e)[:120]
    raise RuntimeError(last)
