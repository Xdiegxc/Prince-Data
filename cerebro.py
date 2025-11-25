import os
import aiohttp
import asyncio
import json
import re
import time

# --- LEER SECRETOS DEL ENTORNO (NO HARDCODED) ---
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")

SOURCES = [
    { "type": "xtream", "alias": "LatinaPro", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
    { "type": "m3u", "alias": "M3U_MX", "url": "https://www.m3u.cl/lista/MX.m3u" },
    { "type": "m3u", "alias": "GitHub_Free", "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8" }
]

# --- FILTROS (MISMOS DE V8) ---
GLOBAL_BLOCKLIST = r"(?i)\b(spain|espana|españa|colombia|peru|perú|argentina|chile|ecuador|venezuela|bolivia|uruguay|paraguay|brasil|brazil|portugal|french|italian|arab|korea|hindi|bengali|turkish|televicentro|tve|antena 3|telecinco)\b"
REGEX_SPORTS = r"(?i)\b(espn|fox|sport|deporte|tudn|dazn|nba|nfl|mlb|ufc|wwe|f1|gp|futbol|soccer|liga|match|gol|win|directv sports|claro sports|fighting|racing|tennis|golf)\b"
REGEX_MUSIC = r"(?i)\b(mtv|vh1|telehit|banda|musica|music|radio|fm|pop|rock|viva|beat|exa|concert|recital|deezer|spotify|tidal|k-pop|ritmoson|cmtv|htv|vevo|xxx|adult|porn)\b"
REGEX_GENERAL = r"(?i)\b(mexico|mx|usa|us|estados unidos|latino|lat|latam|tv abierta|cine|fhd|hevc|4k|azteca|televisa|estrellas|canal 5|imagen|multimedios|milenio|foro tv|noticias|news|telemundo|univision|hbo|tnt|space|universal|sony|warner)\b"
M3U_REGEX = r'#EXTINF:.*?(?:tvg-logo="(.*?)")?.*?(?:group-title="(.*?)")?,(.*?)\n(http.*)'
ACTIONS = { "LIVE": "get_live_streams", "MOVIES": "get_vod_streams", "SERIES": "get_series" }

async def fetch_xtream(session, server, action):
    if not server['host']: return [] # Seguridad si falla ENV
    url = f"{server['host']}/player_api.php?username={server['user']}&password={server['pass']}&action={action}"
    try:
        async with session.get(url, timeout=45) as r:
            if r.status == 200: return await r.json()
    except: pass
    return []

async def fetch_m3u(session, src):
    try:
        async with session.get(src['url'], timeout=30) as r:
            if r.status == 200:
                txt = await r.text()
                return [{"name":n.strip(),"stream_icon":l,"url":u.strip(),"category_name":g,"stream_id":"m3u_"+str(hash(u))} for l,g,n,u in re.findall(M3U_REGEX, txt, re.MULTILINE)]
    except: pass
    return []

async def check(session, url):
    try:
        async with session.head(url, timeout=2) as r: return r.status == 200
    except: return False

def categorize(name):
    if re.search(GLOBAL_BLOCKLIST, name): return None
    if re.search(REGEX_SPORTS, name): return "SPORTS"
    if re.search(REGEX_MUSIC, name): return "MUSIC"
    if re.search(REGEX_GENERAL, name): return "LIVE_TV"
    return None

async def process(session, src, pl):
    if src['type'] == 'xtream':
        # LIVE
        raw = await fetch_xtream(session, src, ACTIONS["LIVE"])
        tasks = []
        for i in raw:
            cat = categorize(i.get('name',''))
            if cat:
                url = f"{src['host']}/live/{src['user']}/{src['pass']}/{i['stream_id']}.ts"
                item = {"title":f"[{src['alias']}] {i['name']}","id":str(i['stream_id']),"url":url,"hdPosterUrl":i.get('stream_icon'),"group":cat}
                tasks.append((item, check(session, url), cat))
        if tasks:
            res = await asyncio.gather(*[t[1] for t in tasks])
            for (it, ok, cat) in zip([t[0] for t in tasks], res, [t[2] for t in tasks]):
                if ok: pl[cat.lower()].append(it)
        
        # VOD
        raw = await fetch_xtream(session, src, ACTIONS["MOVIES"])
        for i in raw:
            if not re.search(GLOBAL_BLOCKLIST, i.get('name','')):
                ext = i.get('container_extension','mp4')
                url = f"{src['host']}/movie/{src['user']}/{src['pass']}/{i['stream_id']}.{ext}"
                pl["movies"].append({"title":i['name'],"id":str(i['stream_id']),"url":url,"hdPosterUrl":i.get('stream_icon'),"group":"MOVIE"})
        
        # SERIES
        raw = await fetch_xtream(session, src, ACTIONS["SERIES"])
        for i in raw:
            if not re.search(GLOBAL_BLOCKLIST, i.get('name','')):
                 pl["series"].append({"title":i['name'],"id":str(i['series_id']),"hdPosterUrl":i.get('cover'),"group":"SERIES"})

    elif src['type'] == 'm3u':
        items = await fetch_m3u(session, src)
        tasks = []
        for i in items:
            cat = categorize(i['name'])
            if cat:
                item = {"title":f"[{src['alias']}] {i['name']}","id":i['stream_id'],"url":i['url'],"hdPosterUrl":i['stream_icon'],"group":cat}
                tasks.append((item, check(session, i['url']), cat))
        if tasks:
            res = await asyncio.gather(*[t[1] for t in tasks])
            for (it, ok, cat) in zip([t[0] for t in tasks], res, [t[2] for t in tasks]):
                if ok: pl[cat.lower()].append(it)

async def main():
    pl = {"live_tv":[],"sports":[],"music":[],"movies":[],"series":[],"meta":{"updated":time.ctime()}}
    async with aiohttp.ClientSession() as s:
        await asyncio.gather(*[process(s, src, pl) for src in SOURCES])
    
    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(pl, f)

if __name__ == "__main__":
    asyncio.run(main())