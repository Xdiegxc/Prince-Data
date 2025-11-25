import os
import aiohttp
import asyncio
import json
import re
import time

# ==========================================
# 1. CONFIGURACIÓN Y SECRETOS
# ==========================================
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")

# MÁSCARA DE IDENTIDAD (CRUCIAL: DEBE COINCIDIR CON LA APP ROKU)
USER_AGENT = "IPTVSmartersPro"

SOURCES = [
    { "type": "xtream", "alias": "LatinaPro_VIP", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
    { "type": "m3u", "alias": "M3U_Publica", "url": "https://www.m3u.cl/lista/MX.m3u" },
    { "type": "m3u", "alias": "GitHub_FreeTV", "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8" }
]

# ==========================================
# 2. FILTROS Y LÓGICA (IQ 200)
# ==========================================

# BLOQUEO DE BASURA
GLOBAL_BLOCKLIST = r"(?i)\b(spain|espana|españa|colombia|peru|perú|argentina|chile|ecuador|venezuela|bolivia|uruguay|paraguay|brasil|brazil|portugal|french|italian|arab|korea|hindi|bengali|turkish|televicentro|tve|antena 3|telecinco|rtve|xxx|adult|porn)\b"

# CATEGORÍAS
REGEX_SPORTS = r"(?i)\b(espn|fox|sport|deporte|tudn|dazn|nba|nfl|mlb|ufc|wwe|f1|gp|futbol|soccer|liga|match|gol|win|directv sports|claro sports|fighting|racing|tennis|golf|bein)\b"
REGEX_MUSIC = r"(?i)\b(mtv|vh1|telehit|banda|musica|music|radio|fm|pop|rock|viva|beat|exa|concert|recital|deezer|spotify|tidal|k-pop|ritmoson|cmtv|htv|vevo)\b"
REGEX_GENERAL = r"(?i)\b(mexico|mx|usa|us|estados unidos|latino|lat|latam|tv abierta|cine|fhd|hevc|4k|azteca|televisa|estrellas|canal 5|imagen|multimedios|milenio|foro tv|noticias|news|telemundo|univision|hbo|tnt|space|universal|sony|warner|discovery|history|a&e|axn)\b"

# DETECTOR DE ESTRENOS (2024/2025)
REGEX_PREMIERE = r"(?i)\b(2024|2025)\b"

M3U_REGEX = r'#EXTINF:.*?(?:tvg-logo="(.*?)")?.*?(?:group-title="(.*?)")?,(.*?)\n(http.*)'

ACTIONS = { "LIVE": "get_live_streams", "MOVIES": "get_vod_streams", "SERIES": "get_series" }

# ==========================================
# 3. MOTOR DE PROCESAMIENTO
# ==========================================

def clean_rating(value):
    """Sanitiza el rating para que la UI de estrellas no falle"""
    if not value: return 0.0
    try:
        val_str = str(value)
        if "/" in val_str: val_str = val_str.split('/')[0]
        val_str = re.sub(r"[^0-9.]", "", val_str)
        if val_str == "": return 0.0
        r = float(val_str)
        return r if r <= 10 else 10.0 # Tope de 10
    except: return 0.0

async def fetch_xtream(session, server, action):
    if not server['host']: return [] 
    url = f"{server['host']}/player_api.php?username={server['user']}&password={server['pass']}&action={action}"
    print(f"[>] {server['alias']}: Solicitando {action}...")
    try:
        async with session.get(url, timeout=45) as response:
            if response.status == 200: return await response.json()
    except: pass
    return []

async def fetch_and_parse_m3u(session, source):
    print(f"[>] {source['alias']}: Descargando M3U...")
    try:
        async with session.get(source['url'], timeout=30) as response:
            if response.status == 200:
                content = await response.text()
                matches = re.findall(M3U_REGEX, content, re.MULTILINE)
                parsed = []
                for logo, group, name, url in matches:
                    parsed.append({
                        "name": name.strip(), "stream_icon": logo, "url": url.strip(),
                        "category_name": group, "stream_id": "m3u_" + str(hash(url.strip()))
                    })
                return parsed
    except: pass
    return []

async def check_health(session, url):
    """
    Sincronización Crítica V92:
    Usamos el MISMO User-Agent que usa Roku.
    Si Roku puede verlo, Python debe marcarlo como True.
    """
    try:
        headers = {"User-Agent": USER_AGENT}
        async with session.head(url, headers=headers, timeout=2.5) as response:
            return response.status == 200
    except: return False

def categorize(name):
    """Logica de Cubetas Exclusivas"""
    if re.search(GLOBAL_BLOCKLIST, name): return None
    if re.search(REGEX_SPORTS, name): return "SPORTS" # Prioridad 1
    if re.search(REGEX_MUSIC, name): return "MUSIC"   # Prioridad 2
    if re.search(REGEX_GENERAL, name): return "LIVE_TV" # Prioridad 3
    return None

async def process_source(session, source, playlist):
    if source['type'] == 'xtream':
        # 1. LIVE TV (.TS Iron Stream)
        raw_live = await fetch_xtream(session, source, ACTIONS["LIVE"])
        tasks = []
        for item in raw_live:
            name = item.get('name', '')
            cat = categorize(name)
            if cat:
                sid = item.get('stream_id')
                url = f"{source['host']}/live/{source['user']}/{source['pass']}/{sid}.ts"
                obj = {
                    "title": f"[{source['alias']}] {name}", "id": str(sid), "url": url,
                    "hdPosterUrl": item.get('stream_icon'), "group": cat
                }
                tasks.append((obj, check_health(session, url), cat))
        
        if tasks:
            results = await asyncio.gather(*[t[1] for t in tasks])
            for (obj, online, cat) in zip([t[0] for t in tasks], results, [t[2] for t in tasks]):
                if online: playlist[cat.lower()].append(obj)

        # 2. MOVIES (VOD)
        raw_vod = await fetch_xtream(session, source, ACTIONS["MOVIES"])
        for item in raw_vod:
            name = item.get('name', '')
            if not re.search(GLOBAL_BLOCKLIST, name):
                sid = item.get('stream_id')
                ext = item.get('container_extension', 'mp4')
                url = f"{source['host']}/movie/{source['user']}/{source['pass']}/{sid}.{ext}"
                rating = clean_rating(item.get('rating'))
                
                obj = {
                    "title": name, "id": str(sid), "url": url,
                    "hdPosterUrl": item.get('stream_icon'), "rating": rating, "group": "MOVIE"
                }
                playlist["movies"].append(obj)
                if re.search(REGEX_PREMIERE, name): playlist["premieres"].append(obj)

        # 3. SERIES (Solo Metadata)
        raw_series = await fetch_xtream(session, source, ACTIONS["SERIES"])
        for item in raw_series:
            name = item.get('name', '')
            if not re.search(GLOBAL_BLOCKLIST, name):
                rating = clean_rating(item.get('rating'))
                obj = {
                    "title": name, "id": str(item.get('series_id')),
                    "hdPosterUrl": item.get('cover'), "rating": rating, "group": "SERIES"
                }
                playlist["series"].append(obj)
                if re.search(REGEX_PREMIERE, name): playlist["premieres"].append(obj)

    elif source['type'] == 'm3u':
        items = await fetch_and_parse_m3u(session, source)
        tasks = []
        for item in items:
            cat = categorize(item['name'])
            if cat:
                obj = {
                    "title": f"[{source['alias']}] {item['name']}", "id": item['stream_id'],
                    "url": item['url'], "hdPosterUrl": item['stream_icon'], "group": cat
                }
                tasks.append((obj, check_health(session, item['url']), cat))
        if tasks:
            results = await asyncio.gather(*[t[1] for t in tasks])
            for (obj, online, cat) in zip([t[0] for t in tasks], results, [t[2] for t in tasks]):
                if online: playlist[cat.lower()].append(obj)

async def main():
    t0 = time.time()
    # Estructura V92 Exacta
    playlist = {
        "meta": { "updated": time.ctime(), "version": "v92_quantum" },
        "live_tv": [], "sports": [], "music": [], 
        "movies": [], "series": [], "premieres": []
    }
    
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[process_source(session, src, playlist) for src in SOURCES])

    with open('playlist.json', 'w', encoding='utf-8') as f: json.dump(playlist, f, indent=4)

    print(f"\n--- REPORTE V92 ---")
    print(f"Tiempo: {time.time() - t0:.2f}s")
    print(f"TV: {len(playlist['live_tv'])} | Deportes: {len(playlist['sports'])} | Musica: {len(playlist['music'])}")
    print(f"Pelis: {len(playlist['movies'])} | Series: {len(playlist['series'])} | Estrenos: {len(playlist['premieres'])}")

if __name__ == "__main__":
    asyncio.run(main())



