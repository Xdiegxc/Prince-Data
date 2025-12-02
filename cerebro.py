import os
import aiohttp
import asyncio
import json
import re
import time
import logging
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

# ==========================================
# 1. CONFIGURACIÓN Y LOGGING
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("CerebroV100.1_Hotfix")

# Credenciales y Configuración
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")
USER_AGENT = "IPTVSmartersPro/100.0" 

# Tiempos más agresivos para descartar basura rápido
HTTP_TIMEOUT = 20 
MAX_CONCURRENT_CHECKS = 150

# ==========================================
# 2. DEFINICIÓN DE FUENTES
# ==========================================

XTREAM_SOURCES = [
    { "type": "xtream", "alias": "LatinaPro_Main", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# Tus listas M3U
M3U_SOURCES = [
    {"alias": "Mirror_77", "url": "http://77.237.238.21:2082/get.php?username=VicTorC&password=Victo423&type=m3u_plus"},
    {"alias": "Latina_Direct", "url": "http://tvappapk@latinapro.net:25461/get.php?username=lazaroperez&password=perez3&type=m3u_plus"},
    {"alias": "UK_Server", "url": "http://ip96.uk:8080/get.php?username=H645668DH&password=7565848DHY&type=m3u_plus"},
    {"alias": "VocoTV", "url": "http://vocotv.live/get.php?username=Sanchez01&password=Sanchez01&type=m3u_plus&output=ts"},
    {"alias": "TV14S", "url": "http://tv14s.xyz:8080/get.php?username=71700855&password=71700855&type=m3u_plus"},
    {"alias": "ClubTV_L1", "url": "http://clubtv.link/20nv/lista1.m3u"},
    {"alias": "ClubTV_Geo", "url": "http://clubtv.link/20nv/geomex.m3u"},
    {"alias": "Pluto_MX", "url": "https://i.mjh.nz/PlutoTV/mx.m3u8"},
    {"alias": "Pastebin_Mix", "url": "https://pastebin.com/raw/CgA3a8Yp"},
    {"alias": "ClubTV_Mov", "url": "http://clubtv.link/24no/peliculas.m3u"},
]

# ==========================================
# 3. FILTROS "ZERO TRUST" (ESTRICTOS)
# ==========================================

REGEX_MX_STRICT = r"(?i)\b(mx|mex|mexico|méxico|latam|latino|latin|spanish|español)\b"
REGEX_MX_CHANNELS = r"(?i)\b(azteca|televisa|las estrellas|canal 5|imagen|adn 40|foro tv|milenio|multimedios|once|canal 22|tdn|tudn|afizzionados|univision|telemundo|fox sports|espn|hbo|cinemax|tnt|space|cinecanal|golden|edge)\b"
REGEX_BLOCK = r"(?i)\b(xxx|adult|porn|brazil|brasil|portugal|uk|usa|french|italy|germany|turk|arab|hindi|korea|ru|russia)\b"

REGEX_MOVIES = r"(?i)\b(movie|pelicula|film|vod|cinema|estreno|accion|terror|drama|comedia)\b"
REGEX_SERIES = r"(?i)\b(serie|capitulo|temp|season|s0|e0)\b"
REGEX_KIDS = r"(?i)\b(kids|infantil|cartoon|disney|nick|discovery k|jr)\b"

# ==========================================
# 4. MODELO DE DATOS
# ==========================================

@dataclass
class StreamItem:
    title: str
    url: str         
    hdPosterUrl: str 
    group: str       
    contentId: str   
    quality: str = "SD"
    source: str = ""
    
    def to_dict(self):
        return {
            "title": self.title,
            "url": self.url,
            "hdPosterUrl": self.hdPosterUrl,
            "quality": self.quality,
            "contentId": self.contentId
        }

# ==========================================
# 5. LÓGICA DE NEGOCIO
# ==========================================

class TrafficController:
    @staticmethod
    def is_strictly_mexican(title: str, group: str) -> bool:
        combined = f"{title} {group}".lower()
        if re.search(REGEX_BLOCK, combined): return False
        if re.search(REGEX_MX_CHANNELS, combined): return True
        if re.search(REGEX_MX_STRICT, combined): return True
        if (re.search(REGEX_MOVIES, combined) or re.search(REGEX_SERIES, combined)) and "english" not in combined:
            return True
        return False

    @staticmethod
    def classify_type(title: str, group: str, url: str) -> str:
        combined = f"{title} {group}".lower()
        url_lower = url.lower()
        
        is_vod_file = url_lower.endswith(('.mp4', '.mkv', '.avi'))
        
        if re.search(REGEX_SERIES, combined): return "series"
        if re.search(REGEX_MOVIES, combined): return "movies"
        if re.search(REGEX_KIDS, combined): return "kids"
        if is_vod_file: return "movies"
            
        return "live_tv"

    @staticmethod
    def clean_title(title: str) -> str:
        t = re.sub(r'^\d+\s*[-|]\s*', '', title)
        t = re.sub(r'(MX:|LAT:|\||\[.*?\]|\(.*?\))', '', t)
        return t.strip().title()

# ==========================================
# 6. NETWORKING
# ==========================================

async def fetch_text(session, url):
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as r:
            if r.status == 200:
                content = await r.read()
                try: return content.decode('utf-8')
                except: return content.decode('latin-1', errors='ignore')
    except: return None

async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as r:
            if r.status == 200: return await r.json()
    except: return None

async def check_link(session, url, semaphore):
    async with semaphore:
        try:
            async with session.head(url, timeout=5, allow_redirects=True) as r:
                return r.status < 400
        except: return False

# ==========================================
# 7. PROCESADORES (CORREGIDOS)
# ==========================================

async def process_m3u(session, src, playlist, semaphore):
    data = await fetch_text(session, src['url'])
    if not data: return
    
    logger.info(f"Analizando M3U: {src['alias']}")
    pattern = re.compile(r'#EXTINF:(?P<dur>[-0-9]+)(?:.*?)group-title="(?P<grp>.*?)".*?,(?P<title>.*?)[\r\n]+(?P<url>http[^\s]+)', re.DOTALL)
    
    matches = pattern.finditer(data)
    tasks = []
    
    for m in matches:
        raw_title = m.group('title').strip()
        raw_group = m.group('grp').strip()
        url = m.group('url').strip()
        
        if not TrafficController.is_strictly_mexican(raw_title, raw_group):
            continue
            
        category = TrafficController.classify_type(raw_title, raw_group, url)
        clean_name = TrafficController.clean_title(raw_title)
        
        item = StreamItem(
            title=clean_name,
            url=url,
            hdPosterUrl="",
            group=category,
            contentId=str(abs(hash(url))),
            source=src['alias']
        )
        # Guardamos TUPLA (item, corutina)
        tasks.append((item, check_link(session, url, semaphore)))

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        valid_count = 0
        # CORRECCIÓN AQUÍ: Desempaquetamos ((item, _), is_ok)
        for ((item_obj, _), is_ok) in zip(tasks, results):
            if is_ok:
                playlist[item_obj.group].append(item_obj.to_dict())
                valid_count += 1
        logger.info(f"[{src['alias']}] Agregados: {valid_count}")

async def process_xtream(session, src, playlist, semaphore):
    url = f"{src['host']}/player_api.php?username={src['user']}&password={src['pass']}&action=get_live_streams"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    tasks = []
    for x in data:
        name = x.get('name', '')
        if TrafficController.is_strictly_mexican(name, "Live"):
            play_url = f"{src['host']}/live/{src['user']}/{src['pass']}/{x.get('stream_id')}.ts"
            cat = "kids" if re.search(REGEX_KIDS, name, re.I) else "live_tv"
            
            item = StreamItem(
                title=TrafficController.clean_title(name),
                url=play_url,
                hdPosterUrl=x.get('stream_icon', ''),
                group=cat,
                contentId=str(x.get('stream_id')),
                source=src['alias']
            )
            tasks.append((item, check_link(session, play_url, semaphore)))
            
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        # CORRECCIÓN AQUÍ TAMBIÉN
        for ((item_obj, _), is_ok) in zip(tasks, results):
            if is_ok: playlist[item_obj.group].append(item_obj.to_dict())

# ==========================================
# 8. MAIN
# ==========================================

async def main():
    start = time.time()
    
    playlist = {
        "live_tv": [],
        "movies": [],
        "series": [], 
        "kids": [],
        "generated_at": time.ctime()
    }
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    connector = aiohttp.TCPConnector(limit=100, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        
        if XT_HOST:
            for s in XTREAM_SOURCES:
                tasks.append(process_xtream(session, s, playlist, semaphore))
        
        for m in M3U_SOURCES:
            tasks.append(process_m3u(session, m, playlist, semaphore))
            
        await asyncio.gather(*tasks)
        
    # Deduplicación
    for key in ["live_tv", "movies", "series", "kids"]:
        seen = set()
        unique = []
        for item in playlist[key]:
            ident = item['title']
            if ident not in seen:
                unique.append(item)
                seen.add(ident)
        playlist[key] = unique
        playlist[key].sort(key=lambda x: x['title'])

    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)
        
    try:
        subprocess.run(["git", "add", "playlist.json"], check=True)
        subprocess.run(["git", "commit", "-m", "Fix: Tuple Unpacking"], check=False)
        subprocess.run(["git", "push", "origin", "main"], capture_output=True)
    except Exception as e:
        logger.error(f"Git error: {e}")

    logger.info(f"Done in {time.time() - start:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())







