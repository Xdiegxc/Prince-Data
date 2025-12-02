import os
import aiohttp
import asyncio
import json
import re
import time
import logging
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Dict, Any

# ==========================================
# 1. CONFIGURACIÓN Y LOGGING
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("CerebroV98.5_HybridSoft")

# Credenciales
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")
USER_AGENT = "IPTVSmartersPro/98.5"

# --- SEGURIDAD ROKU ---
# Límite de items para evitar el Error &h23 (Timeout)
SAFETY_LIMIT = 2500 

HTTP_TIMEOUT = 30
MAX_CONCURRENT_CHECKS = 100

# ==========================================
# 2. FUENTES DE DATOS
# ==========================================

# Tu fuente API original
XTREAM_SOURCES = [
    { "type": "xtream", "alias": "LatinaPro_API", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# Tus nuevas fuentes M3U
M3U_SOURCES = [
    {"alias": "Mirror_77", "url": "http://77.237.238.21:2082/get.php?username=VicTorC&password=Victo423&type=m3u_plus"},
    {"alias": "Latina_M3U", "url": "http://tvappapk@latinapro.net:25461/get.php?username=lazaroperez&password=perez3&type=m3u_plus"},
    {"alias": "UK_Server", "url": "http://ip96.uk:8080/get.php?username=H645668DH&password=7565848DHY&type=m3u_plus"},
    {"alias": "VocoTV", "url": "http://vocotv.live/get.php?username=Sanchez01&password=Sanchez01&type=m3u_plus&output=ts"},
    {"alias": "TV14S", "url": "http://tv14s.xyz:8080/get.php?username=71700855&password=71700855&type=m3u_plus"},
    {"alias": "ClubTV_Lista1", "url": "http://clubtv.link/20nv/lista1.m3u"},
    {"alias": "ClubTV_Geo", "url": "http://clubtv.link/20nv/geomex.m3u"},
    {"alias": "Pluto_MX", "url": "https://i.mjh.nz/PlutoTV/mx.m3u8"},
    {"alias": "Pastebin_Mix", "url": "https://pastebin.com/raw/CgA3a8Yp"},
    {"alias": "ClubTV_Pelis", "url": "http://clubtv.link/24no/peliculas.m3u"},
]

# ==========================================
# 3. FILTROS (MODERADOS)
# ==========================================

# Bloqueamos lo obvio que no sirve en México
REGEX_BLOCK = r"(?i)\b(xxx|adult|porn|brazil|brasil|portugal|uk|usa|french|italy|germany|turk|arab|hindi|korea|ru|russia)\b"

# Priorizamos esto
REGEX_MX = r"(?i)\b(mx|mex|mexico|méxico|latam|latino|spanish|español|azteca|televisa|imagen|canal 5|estrellas)\b"

# Categorías
REGEX_MOVIES = r"(?i)\b(movie|pelicula|film|vod|cinema|estreno|accion|terror|drama)\b"
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
# 5. LÓGICA DE PROCESAMIENTO
# ==========================================

class ContentFilter:
    @staticmethod
    def should_keep(title: str, group: str) -> bool:
        combined = f"{title} {group}".lower()
        
        # 1. Regla de Oro: Si está en la lista negra, adiós.
        if re.search(REGEX_BLOCK, combined):
            return False
            
        # 2. Regla de Plata: Si dice MX/Latino, entra seguro.
        if re.search(REGEX_MX, combined):
            return True
            
        # 3. Regla de Bronce (Suave): Si no dice nada prohibido, permitimos pasar 
        # (esto arregla el que borrara contenido bueno que no decía explícitamente "MX")
        return True

    @staticmethod
    def classify(title: str, group: str, url: str) -> str:
        combined = f"{title} {group}".lower()
        url_lower = url.lower()
        
        # Detección técnica primero (Extensión de archivo)
        if url_lower.endswith(('.mp4', '.mkv', '.avi')):
            return "movies"
            
        # Detección semántica
        if re.search(REGEX_MOVIES, combined): return "movies"
        if re.search(REGEX_KIDS, combined): return "kids"
        
        return "live_tv" # Default para .ts/.m3u8

    @staticmethod
    def clean_title(title: str) -> str:
        # Limpieza estética simple
        return re.sub(r'(MX:|LAT:|\||\[.*?\])', '', title).strip()

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
    # Verificación rápida (HEAD)
    async with semaphore:
        try:
            async with session.head(url, timeout=5, allow_redirects=True) as r:
                return r.status < 400
        except: return False

# ==========================================
# 7. PROCESADORES (XTREAM + M3U)
# ==========================================

async def process_xtream(session, src, playlist, semaphore):
    """Procesa API Xtream (Tu código original mejorado)"""
    url = f"{src['host']}/player_api.php?username={src['user']}&password={src['pass']}&action=get_live_streams"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    tasks = []
    for item in data:
        name = item.get('name', '')
        if ContentFilter.should_keep(name, "Live"):
            url_play = f"{src['host']}/live/{src['user']}/{src['pass']}/{item.get('stream_id')}.ts"
            obj = StreamItem(
                title=ContentFilter.clean_title(name),
                url=url_play,
                hdPosterUrl=item.get('stream_icon', ''),
                group=ContentFilter.classify(name, "Live", url_play),
                contentId=str(item.get('stream_id')),
                source=src['alias']
            )
            tasks.append((obj, check_link(session, url_play, semaphore)))
            
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        for ((obj, _), ok) in zip(tasks, results):
            if ok: playlist[obj.group].append(obj.to_dict())

async def process_m3u(session, src, playlist, semaphore):
    """Procesa Listas M3U (Nuevo requerimiento)"""
    data = await fetch_text(session, src['url'])
    if not data: return
    
    logger.info(f"Procesando M3U: {src['alias']}")
    # Regex para extraer info de #EXTINF
    pattern = re.compile(r'#EXTINF:(?P<dur>[-0-9]+)(?:.*?)group-title="(?P<grp>.*?)".*?,(?P<title>.*?)[\r\n]+(?P<url>http[^\s]+)', re.DOTALL)
    
    matches = pattern.finditer(data)
    tasks = []
    
    for m in matches:
        title = m.group('title').strip()
        grp = m.group('grp').strip()
        url = m.group('url').strip()
        
        if ContentFilter.should_keep(title, grp):
            obj = StreamItem(
                title=ContentFilter.clean_title(title),
                url=url,
                hdPosterUrl="", # M3U plano no suele tener imagenes fiables
                group=ContentFilter.classify(title, grp, url),
                contentId=str(abs(hash(url))),
                source=src['alias']
            )
            tasks.append((obj, check_link(session, url, semaphore)))
            
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        count = 0
        for ((obj, _), ok) in zip(tasks, results):
            if ok:
                playlist[obj.group].append(obj.to_dict())
                count += 1
        logger.info(f"[{src['alias']}] Agregados: {count}")

# ==========================================
# 8. MAIN Y SINCRONIZACIÓN
# ==========================================

async def main():
    start = time.time()
    
    # ESTRUCTURA PLANA (Para evitar el crash de Roku)
    playlist = {
        "live_tv": [],
        "movies": [],
        "series": [], # Roku las leerá como películas largas, pero funcionarán
        "kids": []
    }
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    connector = aiohttp.TCPConnector(limit=100, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        
        # 1. Cargar Xtream (Si hay credenciales)
        if XT_HOST:
            for s in XTREAM_SOURCES: tasks.append(process_xtream(session, s, playlist, semaphore))
            
        # 2. Cargar M3Us
        for m in M3U_SOURCES: tasks.append(process_m3u(session, m, playlist, semaphore))
        
        await asyncio.gather(*tasks)

    # DEDUPLICACIÓN Y CORTE DE SEGURIDAD
    for key in playlist:
        # Eliminar duplicados por nombre
        unique = {x['title']: x for x in playlist[key]}.values()
        sorted_list = sorted(list(unique), key=lambda x: x['title'])
        
        # Límite de seguridad para Roku (Evita Timeout)
        if len(sorted_list) > SAFETY_LIMIT:
            logger.warning(f"⚠️ Recortando categoría '{key}' a {SAFETY_LIMIT} items por seguridad.")
            sorted_list = sorted_list[:SAFETY_LIMIT]
            
        playlist[key] = sorted_list

    # Guardar JSON
    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)

    # Git Sync (Con rebase para evitar errores)
    try:
        subprocess.run(["git", "config", "--global", "user.name", "PrinceBot"], check=False)
        subprocess.run(["git", "config", "--global", "user.email", "bot@princetv.com"], check=False)
        subprocess.run(["git", "add", "playlist.json"], check=True)
        subprocess.run(["git", "commit", "-m", "Auto-Update: Hybrid Soft"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        subprocess.run(["git", "push", "origin", "main"], check=True)
    except Exception as e:
        logger.error(f"Git Error: {e}")

    logger.info(f"Finalizado en {time.time() - start:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())









