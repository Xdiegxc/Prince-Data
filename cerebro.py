import os
import aiohttp
import asyncio
import json
import re
import time
import logging
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Set, Pattern

# ==========================================
# 1. CONFIGURACIÓN Y ARQUITECTURA
# ==========================================

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CerebroV99.2")

# Credenciales
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")
USER_AGENT = "IPTVSmartersPro/98.2"

# Configuración de Rendimiento
MAX_CONCURRENT_CHECKS = 100
HTTP_TIMEOUT = 30
MAX_RETRIES = 2

# --- INYECCIÓN MANUAL (ACTUALIZADO) ---
MANUAL_OVERRIDES = [
    {
        "type": "manual_stream",
        "title": "Fox Sports Premium",  # Nombre actualizado
        "contentId": "FoxSportsPremium.mx",
        "group": "sports",
        "url": "https://live20.bozztv.com/akamaissh101/ssh101/foxsports/playlist.m3u8",
        "hdPosterUrl": "https://i.imgur.com/3lOZWeD.png",  # Logo actualizado
        "quality": "HD"
    }
]

SOURCES = [
    { "type": "xtream", "alias": "LatinaPro_MX", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# ==========================================
# 2. MOTORES DE FILTRADO (PRE-COMPILADO)
# ==========================================

RE_FLAGS = re.IGNORECASE

PATTERNS = {
    "MX_STRICT": re.compile(r"\b(mx|mex|mexico|méxico|latam|latino|spanish|español|audio latino)\b", RE_FLAGS),
    "MX_CHANNELS": re.compile(r"\b(azteca|televisa|estrellas|canal 5|imagen|adn 40|foro tv|milenio|multimedios|once|canal 22|tdn|tudn|afizzionados)\b", RE_FLAGS),
    "PREMIUM_LATAM": re.compile(r"\b(hbo|max|star|disney|espn|fox|f1|gol|win|vix|cnn|axn|warner|tnt|space|universal)\b", RE_FLAGS),
    "HARD_BLOCK": re.compile(r"\b(usa|uk|canada|adult|xxx|porn|hindi|arab|turk|korea|french|german|italian|brasil|brazil|portugal|pt)\b", RE_FLAGS),
    "SPORTS_BLOCK": re.compile(r"\b(brasil|brazil|portugal|pt)\b", RE_FLAGS),
    "SPORTS_KEYWORDS": re.compile(r"(deporte|sport|espn|fox|ufc|nfl|f1|liga|chivas|beisbol|tenis|racing)", RE_FLAGS),
    "KIDS": re.compile(r"\b(kids|infantil|cartoon|nick|disney|discovery kids|paka paka|boing|clantv|cbeebies|zaz|toons|baby|junior)\b", RE_FLAGS),
    "DOCS": re.compile(r"\b(discovery|history|nat geo|national geographic|documental|docu|a\&e|misterio|science|viajes|travel|animal planet|investigation)\b", RE_FLAGS),
    "MUSIC": re.compile(r"(music|mtv|vh1|radio|concert)", RE_FLAGS),
    "SPAIN_ALLOW": re.compile(r"\b(spain|españa)\b", RE_FLAGS),
    "4K": re.compile(r"\b(4k|uhd|2160p)\b", RE_FLAGS),
    "FHD": re.compile(r"\b(fhd|1080p|hevc)\b", RE_FLAGS),
    "HD": re.compile(r"\b(hd|720p)\b", RE_FLAGS),
    "PREMIERE_YEAR": re.compile(r"(2024|2025)", RE_FLAGS)
}

# ==========================================
# 3. MODELADO DE DATOS
# ==========================================

@dataclass
class StreamItem:
    title: str
    contentId: str
    group: str
    url: str
    hdPosterUrl: str = ""
    rating: float = 0.0
    plot: str = ""
    genre: str = ""
    releaseDate: str = ""
    quality: str = "SD"
    source_alias: str = ""
    series_id: str = ""
    api_url: str = ""
    
    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v}

# ==========================================
# 4. LÓGICA DE NEGOCIO (FILTROS)
# ==========================================

class ContentFilter:
    @staticmethod
    def is_mexico_focused(name: str) -> bool:
        if PATTERNS["HARD_BLOCK"].search(name): return False
        return (bool(PATTERNS["MX_STRICT"].search(name)) or 
                bool(PATTERNS["MX_CHANNELS"].search(name)) or 
                bool(PATTERNS["PREMIUM_LATAM"].search(name)))

    @staticmethod
    def is_sports_focused(name: str) -> bool:
        if PATTERNS["SPORTS_BLOCK"].search(name): return False
        if not PATTERNS["SPORTS_KEYWORDS"].search(name): return False
        
        is_relevant = (bool(PATTERNS["MX_STRICT"].search(name)) or 
                       bool(PATTERNS["SPAIN_ALLOW"].search(name)) or 
                       bool(PATTERNS["PREMIUM_LATAM"].search(name)))
        return is_relevant

    @staticmethod
    def detect_quality(name: str) -> str:
        if PATTERNS["4K"].search(name): return "4K"
        if PATTERNS["FHD"].search(name): return "FHD"
        if PATTERNS["HD"].search(name): return "HD"
        return "SD"

    @staticmethod
    def categorize_live(name: str) -> str:
        if PATTERNS["KIDS"].search(name): return "kids"
        if PATTERNS["DOCS"].search(name): return "docs"
        if ContentFilter.is_sports_focused(name): return "sports"
        if PATTERNS["MUSIC"].search(name): return "music"
        return "live_tv"

    @staticmethod
    def is_premiere(item: Dict, name: str) -> bool:
        if PATTERNS["PREMIERE_YEAR"].search(name): return True
        r_date = str(item.get('releasedate') or item.get('releaseDate') or item.get('year', ''))
        return bool(PATTERNS["PREMIERE_YEAR"].search(r_date))

    @staticmethod
    def clean_rating(value: Any) -> float:
        if not value: return 0.0
        try:
            val_str = str(value).split('/')[0]
            r = float(''.join(c for c in val_str if c.isdigit() or c == '.'))
            return min(r, 10.0)
        except: return 0.0

# ==========================================
# 5. NETWORKING (AIOHTTP OPTIMIZED)
# ==========================================

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status in (401, 403):
                    logger.error(f"AUTH ERROR: {url}")
                    return None
        except Exception as e:
            wait = 2 ** attempt
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"Failed {url}: {e}")
            await asyncio.sleep(wait)
    return None

async def check_stream_health(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> bool:
    async with semaphore:
        try:
            async with session.head(url, timeout=5, allow_redirects=True) as response:
                return response.status < 400
        except:
            return False

# ==========================================
# 6. PROCESADORES (XTREAM + MANUAL)
# ==========================================

async def process_manual_streams(session, playlist_container, semaphore):
    if not MANUAL_OVERRIDES: return
    
    logger.info(f"[Manual] Inyectando {len(MANUAL_OVERRIDES)} canales estáticos...")
    tasks = []
    
    for item in MANUAL_OVERRIDES:
        stream_obj = StreamItem(
            title=item['title'],
            contentId=item['contentId'],
            group=item['group'],
            url=item['url'],
            hdPosterUrl=item.get('hdPosterUrl', ''),
            quality=item.get('quality', 'SD'),
            source_alias="Static_Manual"
        )
        tasks.append((stream_obj, check_stream_health(session, item['url'], semaphore)))
    
    results = await asyncio.gather(*[t[1] for t in tasks])
    
    valid_count = 0
    for (stream, is_valid) in zip(tasks, results):
        if is_valid:
            playlist_container[stream[0].group].append(stream[0].to_dict())
            valid_count += 1
        else:
            logger.warning(f"[Manual] Stream caído: {stream[0].title}")
            
    logger.info(f"[Manual] Agregados exitosamente: {valid_count}")

async def process_xtream_live(session, source, playlist_container, semaphore):
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_live_streams"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): return

    tasks = []
    logger.info(f"[{source['alias']}] Analizando {len(data)} canales LIVE...")

    for item in data:
        name = item.get('name', '')
        
        is_mx = ContentFilter.is_mexico_focused(name)
        is_sports = ContentFilter.is_sports_focused(name)
        
        if is_mx or is_sports:
            cat_key = ContentFilter.categorize_live(name)
            if cat_key == "sports" and not is_sports: continue

            stream_id = item.get('stream_id')
            play_url = f"{source['host']}/live/{source['user']}/{source['pass']}/{stream_id}.ts"
            
            stream_obj = StreamItem(
                title=name,
                contentId=str(stream_id),
                group=cat_key,
                url=play_url,
                hdPosterUrl=item.get('stream_icon'),
                quality=ContentFilter.detect_quality(name),
                source_alias=source['alias']
            )
            tasks.append((stream_obj, check_stream_health(session, play_url, semaphore)))

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        valid_streams = [t[0].to_dict() for t, ok in zip(tasks, results) if ok]
        
        for s in valid_streams:
            playlist_container[s['group']].append(s)

    logger.info(f"[{source['alias']}] Canales LIVE funcionales: {len(valid_streams) if tasks else 0}")

async def process_xtream_vod(session, source, playlist_container, type_action="get_vod_streams"):
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={type_action}"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Analizando VOD ({type_action})...")
    
    for item in data:
        name = item.get('name', '')
        if PATTERNS["HARD_BLOCK"].search(name): continue

        stream_id = item.get('stream_id')
        ext = item.get('container_extension', 'mp4')
        
        obj = StreamItem(
            title=name,
            contentId=str(stream_id),
            group="movies",
            url=f"{source['host']}/movie/{source['user']}/{source['pass']}/{stream_id}.{ext}",
            hdPosterUrl=item.get('stream_icon'),
            rating=ContentFilter.clean_rating(item.get('rating')),
            plot=item.get('plot', ''),
            genre=item.get('genre', ''),
            releaseDate=item.get('releasedate') or item.get('releaseDate'),
            quality=ContentFilter.detect_quality(name),
            source_alias=source['alias']
        ).to_dict()

        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(obj)
        
        playlist_container["movies"].append(obj)

async def process_xtream_series(session, source, playlist_container):
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Analizando Series...")
    
    for item in data:
        name = item.get('name', '')
        if PATTERNS["HARD_BLOCK"].search(name): continue

        series_id = str(item.get('series_id'))
        api_url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series_info&series_id={series_id}"

        obj = StreamItem(
            title=name,
            contentId=series_id,
            group="series",
            url=api_url,
            hdPosterUrl=item.get('cover'),
            rating=ContentFilter.clean_rating(item.get('rating')),
            plot=item.get('plot', ''),
            genre=item.get('genre', ''),
            releaseDate=item.get('releaseDate'),
            source_alias=source['alias'],
            series_id=series_id,
            api_url=api_url
        ).to_dict()

        playlist_container["series"].append(obj)
        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(obj)

# ==========================================
# 7. ORQUESTADOR Y UTILIDADES
# ==========================================

def deduplicate_and_prioritize(playlist: Dict[str, Any]):
    logger.info("Normalizando y deduplicando playlist...")
    quality_rank = {"4K": 4, "FHD": 3, "HD": 2, "SD": 1}

    for category in playlist:
        if category == "meta": continue
        
        playlist[category].sort(key=lambda x: (
            x['title'], 
            -quality_rank.get(x.get('quality', 'SD'), 1),
            not bool(x.get('hdPosterUrl'))
        ))

        seen = set()
        unique = []
        
        for item in playlist[category]:
            norm_title = re.sub(r'[^a-z0-9]', '', item['title'].lower())
            
            if norm_title not in seen:
                unique.append(item)
                seen.add(norm_title)
        
        playlist[category] = unique
        logger.info(f"   └── {category}: {len(unique)} items finales.")

def push_to_github(filename: str):
    logger.info("--- GIT AUTOMATION ---")
    try:
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if not status.stdout.strip():
            logger.info("⚡ No hay cambios detectados. Git push omitido.")
            return

        subprocess.run(["git", "add", filename], check=True)
        commit_msg = f"Auto-Update: {time.strftime('%Y-%m-%d %H:%M')} | +FoxSportsPremium"
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)
        
        result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            logger.info("✅ GitHub actualizado correctamente.")
        else:
            logger.error(f"❌ Git Push falló: {result.stderr}")
            
    except Exception as e:
        logger.error(f"❌ Error en Git: {e}")

async def main():
    start_time = time.time()
    
    playlist = {
        "meta": { 
            "generated_at": time.ctime(), 
            "version": "v99.2_Manual_Update", 
            "notes": "Includes Updated FoxSports"
        },
        "premieres": [], "live_tv": [], "sports": [], "kids": [], 
        "docs": [], "music": [], "movies": [], "series": []
    }

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_CHECKS, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        
        # 1. Procesar Fuentes Manuales
        tasks.append(process_manual_streams(session, playlist, semaphore))

        # 2. Procesar Fuentes Xtream
        for src in SOURCES:
            if not src.get('host') or not XT_HOST: continue 
            tasks.append(process_xtream_live(session, src, playlist, semaphore))
            tasks.append(process_xtream_vod(session, src, playlist))
            tasks.append(process_xtream_series(session, src, playlist))
        
        await asyncio.gather(*tasks)

    deduplicate_and_prioritize(playlist)

    final_filename = 'playlist.json'
    with open(final_filename, 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)

    logger.info(f"--- SUCCESS: {time.time() - start_time:.2f}s ---")
    push_to_github(final_filename)

if __name__ == "__main__":
    if not all([XT_HOST, XT_USER, XT_PASS]):
        logger.error("🚫 Faltan variables de entorno XT_*")
    else:
        try:
            if os.name == 'nt':
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(main())
        except KeyboardInterrupt:
            pass









