import os
import aiohttp
import asyncio
import json
import re
import time
import logging
from typing import List, Dict, Any, Optional

# ==========================================
# 1. CONFIGURACIÓN DE ENTORNO Y LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CerebroV98_MX")

XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")
USER_AGENT = "IPTVSmartersPro"

# Configuración de Ingeniería de Red
MAX_CONCURRENT_CHECKS = 60  # Aumentado para mayor throughput
HTTP_TIMEOUT = 45           # Ajustado para fail-fast
MAX_RETRIES = 2             

SOURCES: List[Dict[str, Any]] = [
    { "type": "xtream", "alias": "LatinaPro_VIP", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

ACTIONS = { "LIVE": "get_live_streams", "VOD": "get_vod_streams", "SERIES": "get_series" }

# ==========================================
# 2. MOTORES REGEX (OPTIMIZADOS PARA MX)
# ==========================================

# BLOQUEO AGRESIVO: Filtra todo lo que NO sea interés Mexicano/Neutro
GLOBAL_BLOCKLIST = r"(?i)\b(spain|españa|tve|antena 3|telecinco|rtve|portugal|french|italian|arab|korea|hindi|turkish|xxx|adult|porn|hdcam|cam|vose|subt|subtitulada)\b|\b(peru|perú|chile|argentina|colombia|venezuela|ecuador|uruguay|paraguay|bolivia|costa rica|guatemala|honduras|salvador|panama|dominicana|brasil|brazil|br|pe|cl|ar|co|uy|py|bo|cr|gt|hn|sv|pa|do)\b"

# COMPATIBILIDAD
STREAM_COMPATIBILITY_BLOCKLIST = r"(?i)(youtube\.com|youtu\.be|twitch\.tv|facebook\.com|dailymotion\.com)|(\.html|\.php|\.aspx|\.rss|\.xml)$"

# CATEGORÍAS
REGEX_SPORTS = r"(?i)\b(espn|fox|sport|deporte|tudn|dazn|nba|nfl|mlb|ufc|wwe|f1|gp|futbol|soccer|liga|match|gol|win|afizzionados|claro sports|fighting|racing|tennis|golf|bein)\b"
REGEX_MUSIC = r"(?i)\b(mtv|vh1|telehit|banda|musica|music|radio|fm|pop|rock|viva|beat|exa|concert|recital|deezer|spotify|tidal|k-pop|ritmoson|cmtv|htv|vevo)\b"
REGEX_KIDS = r"(?i)\b(kids|infantil|cartoon|nick|disney|discovery kids|paka paka|boing|clantv|cbeebies|zaz|toons|baby|junior)\b"
REGEX_DOCS = r"(?i)\b(discovery|history|nat geo|national geographic|documental|docu|a&e|misterio|science|viajes|travel|animal planet|h&h)\b"
REGEX_GENERAL = r"(?i)\b(mexico|mx|cdmx|azteca|televisa|estrellas|canal 5|imagen|multimedios|milenio|foro tv|noticias|news|telemundo|univision|hbo|tnt|space|universal|sony|warner|axn|cine|cinema|golden|edge|distrito comedia)\b"

# ESTRENOS: Lógica Booleana Simple (Requerimiento Crítico)
REGEX_PREMIERE = r"(?i)(2024|2025)"

REGEX_4K = r"(?i)\b(4k|uhd|2160p)\b"
REGEX_FHD = r"(?i)\b(fhd|1080p|hevc)\b"
REGEX_HD = r"(?i)\b(hd|720p)\b"

M3U_REGEX = r'#EXTINF:-1.*?(?:tvg-logo="(.*?)")?.*?(?:group-title="(.*?)")?,(.*?)\n(http.*)'

# ==========================================
# 3. UTILERÍAS DE NORMALIZACIÓN
# ==========================================

def clean_rating(value: Any) -> float:
    if not value: return 0.0
    try:
        val_str = str(value).lower()
        if "n/a" in val_str: return 0.0
        val_str = re.sub(r"[^0-9.]", "", val_str.split('/')[0])
        if not val_str: return 0.0
        r = float(val_str)
        return r if r <= 10 else 10.0
    except: return 0.0

def detect_quality(name: str) -> str:
    if re.search(REGEX_4K, name): return "4K"
    if re.search(REGEX_FHD, name): return "FHD"
    if re.search(REGEX_HD, name): return "HD"
    return "SD"

def categorize(name: str) -> Optional[str]:
    # 1. Filtro de Geobloqueo PRIMERO (Eficiencia)
    if re.search(GLOBAL_BLOCKLIST, name): return None
    
    # 2. Categorización
    if re.search(REGEX_KIDS, name): return "KIDS"
    if re.search(REGEX_SPORTS, name): return "SPORTS"
    if re.search(REGEX_MUSIC, name): return "MUSIC"     
    if re.search(REGEX_DOCS, name): return "DOCS"
    
    # 3. Para LIVE TV, forzamos contenido neutro o MX
    if re.search(REGEX_GENERAL, name) or "latino" in name.lower(): return "LIVE_TV"
    
    return None # Si no cae en nada de lo anterior y no es MX explicito, se ignora.

def is_url_compatible(url: str) -> bool:
    return not bool(re.search(STREAM_COMPATIBILITY_BLOCKLIST, url))

def transform_xtream_vod(item: Dict[str, Any], source_alias: str, type_group: str) -> Optional[Dict[str, Any]]:
    # VALIDACIÓN DE CALIDAD DE DATOS (DATA INTEGRITY)
    image = item.get('stream_icon') or item.get('cover') or item.get('slideshow')
    
    # REGLA ESTRICTA: Sin imagen, no entra al sistema.
    if not image or str(image).strip() == "":
        return None

    rating = clean_rating(item.get('rating'))
    quality = detect_quality(item.get('name', ''))
    
    return {
        "title": item.get('name', 'N/A'),
        "contentId": str(item.get('stream_id') or item.get('series_id')),
        "group": type_group,
        "hdPosterUrl": image, # Garantizado por el check anterior
        "rating": rating,
        "plot": item.get('plot', 'Sin descripción disponible.'),
        "genre": item.get('genre', 'General'),       
        "releaseDate": item.get('releasedate') or item.get('releaseDate', 'N/A'),
        "cast": item.get('cast', 'N/A'),
        "quality": quality,
        "source_alias": source_alias,
    }

def transform_xtream_series_legacy(item: Dict[str, Any], source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # VALIDACIÓN DE CALIDAD DE DATOS
    image = item.get('cover') or item.get('stream_icon')
    if not image or str(image).strip() == "":
        return None

    rating = clean_rating(item.get('rating'))
    raw_id = str(item.get('series_id') or item.get('stream_id'))
    episodes_api_url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series_info&series_id={raw_id}"

    return {
        "title": item.get('name', 'N/A'),
        "contentId": raw_id,
        "group": "SERIES",
        "hdPosterUrl": image,
        "rating": rating,
        "plot": item.get('plot', 'Sin descripción disponible.'),
        "genre": item.get('genre', 'General'),       
        "releaseDate": item.get('releaseDate') or item.get('releasedate', 'N/A'),
        "cast": item.get('cast', 'N/A'),
        "source_alias": source['alias'],
        "series_id": raw_id, 
        "id": raw_id,
        "category_id": str(item.get('category_id', '0')),
        "url": episodes_api_url, 
        "api_url": episodes_api_url,
        "cover": image, 
        "youtube_trailer": item.get('youtube_trailer', ''),
        "backdrop_path": item.get('backdrop_path', [])
    }

# ==========================================
# 4. NETWORKING AVANZADO
# ==========================================

async def fetch_with_retry(session: aiohttp.ClientSession, url: str, method: str = "GET", headers: dict = None) -> Any:
    for attempt in range(MAX_RETRIES):
        try:
            if method == "HEAD":
                async with session.head(url, headers=headers, timeout=10) as response:
                    return response.status
            else:
                async with session.get(url, headers=headers, timeout=HTTP_TIMEOUT) as response:
                    if response.status == 200:
                        ctype = response.headers.get('Content-Type', '').lower()
                        if 'json' in ctype: return await response.json()
                        return await response.text()
                    elif response.status >= 500:
                        raise aiohttp.ClientError(f"Server Error {response.status}")
                    else:
                        return None
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"Fallo final ({url}): {e}")
                return None
            await asyncio.sleep(1)
    return None

async def check_health_throttled(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> bool:
    if not is_url_compatible(url): return False
    async with semaphore: 
        try:
            status = await fetch_with_retry(session, url, method="HEAD", headers={"User-Agent": USER_AGENT})
            return status in (200, 301, 302)
        except:
            return False

# ==========================================
# 5. LÓGICA DE PROCESAMIENTO
# ==========================================

def is_premiere_strict(name: str) -> bool:
    """
    V98 Strict Logic: Si el titulo contiene 2024 o 2025, es estreno.
    Sin análisis de fechas metadata, solo nombre crudo para máxima captura.
    """
    return bool(re.search(REGEX_PREMIERE, name))

async def process_xtream(session, source, playlist, semaphore):
    # 1. LIVE TV
    url_live = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={ACTIONS['LIVE']}"
    raw_live = await fetch_with_retry(session, url_live)
    
    if isinstance(raw_live, list):
        logger.info(f"[{source['alias']}] Procesando Live TV...")
        tasks = []
        for item in raw_live:
            name = item.get('name', '')
            cat = categorize(name) # Filtro Geográfico ocurre aquí
            
            if cat:
                sid = item.get('stream_id')
                final_url = f"{source['host']}/live/{source['user']}/{source['pass']}/{sid}.ts"
                
                # Check simple de icono para TV (opcional, pero recomendado)
                icon = item.get('stream_icon')
                if not icon: icon = "https://via.placeholder.com/300x450?text=No+Logo" 

                obj = {
                    "title": item.get('name'), 
                    "contentId": str(sid), 
                    "url": final_url,
                    "hdPosterUrl": icon, 
                    "group": cat,
                    "quality": detect_quality(name)
                }
                tasks.append((obj, check_health_throttled(session, final_url, semaphore), cat))
        
        if tasks:
            results = await asyncio.gather(*[t[1] for t in tasks])
            added_count = 0
            for (obj, online, cat) in zip([t[0] for t in tasks], results, [t[2] for t in tasks]):
                if online: 
                    playlist[cat.lower()].append(obj)
                    added_count += 1
            logger.info(f"[{source['alias']}] LIVE: {added_count} canales MX/Neutros agregados.")

    # 2. VOD (Movies)
    url_vod = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={ACTIONS['VOD']}"
    raw_vod = await fetch_with_retry(session, url_vod)
    if isinstance(raw_vod, list):
        count_premieres = 0
        skipped_no_data = 0
        for item in raw_vod:
            name = item.get('name', '')
            
            # Filtro Geo
            if re.search(GLOBAL_BLOCKLIST, name): continue

            # Transformación con Validación de Imagen
            obj = transform_xtream_vod(item, source['alias'], "MOVIE")
            
            if not obj:
                skipped_no_data += 1
                continue # Saltamos si no tiene datos completos

            ext = item.get('container_extension', 'mp4')
            obj['url'] = f"{source['host']}/movie/{source['user']}/{source['pass']}/{obj['contentId']}.{ext}"
            
            playlist["movies"].append(obj)
            
            # LÓGICA ESTRENO SIMPLE
            if is_premiere_strict(name):
                playlist["premieres"].append(obj)
                count_premieres += 1
                    
        logger.info(f"[{source['alias']}] VOD: {len(playlist['movies'])} aprobados | {skipped_no_data} descartados (sin img) | {count_premieres} estrenos.")

    # 3. SERIES
    url_series = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={ACTIONS['SERIES']}"
    raw_series = await fetch_with_retry(session, url_series)
    if isinstance(raw_series, list):
        count_premieres_series = 0
        skipped_no_data_series = 0
        for item in raw_series:
            name = item.get('name', '')
            
            if re.search(GLOBAL_BLOCKLIST, name): continue

            obj = transform_xtream_series_legacy(item, source)
            
            if not obj:
                skipped_no_data_series += 1
                continue

            playlist["series"].append(obj)
            
            # LÓGICA ESTRENO SIMPLE
            if is_premiere_strict(name):
                playlist["premieres"].append(obj)
                count_premieres_series += 1
                    
        logger.info(f"[{source['alias']}] SERIES: {len(playlist['series'])} aprobadas | {skipped_no_data_series} descartadas (sin img) | {count_premieres_series} estrenos.")

async def process_m3u(session, source, playlist, semaphore):
    # Nota: M3U es menos confiable para metadatos, se aplica filtro geo estricto
    raw_text = await fetch_with_retry(session, source['url'])
    if raw_text:
        matches = re.findall(M3U_REGEX, raw_text, re.MULTILINE)
        tasks = []
        for logo, group, name, url in matches:
            name = name.strip()
            cat = categorize(name) # Aplica filtro Geo
            
            # Validación estricta imagen en M3U tambien
            if not logo or "http" not in logo:
                logo = "https://via.placeholder.com/300x450?text=No+Image" # Fallback para live, VOD se descarta
                if "2024" in name: continue # Si es VOD y no tiene logo, mejor saltar
            
            if cat:
                obj = {
                    "title": f"[{source['alias']}] {name}", 
                    "contentId": f"m3u_{hash(url)}",
                    "url": url.strip(), 
                    "hdPosterUrl": logo, 
                    "group": cat,
                    "quality": detect_quality(name)
                }
                tasks.append((obj, check_health_throttled(session, url, semaphore), cat))
        
        if tasks:
            results = await asyncio.gather(*[t[1] for t in tasks])
            for (obj, online, cat) in zip([t[0] for t in tasks], results, [t[2] for t in tasks]):
                if online: playlist[cat.lower()].append(obj)

async def main():
    t0 = time.time()
    playlist = {
        "meta": { "updated": time.ctime(), "version": "v98_mx_prime", "user_agent": USER_AGENT },
        "live_tv": [], "sports": [], "music": [], "kids": [], "docs": [],
        "movies": [], "series": [], "premieres": []
    }
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    conn = aiohttp.TCPConnector(limit=100)

    async with aiohttp.ClientSession(timeout=timeout, connector=conn, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        for src in SOURCES:
            if src['type'] == 'xtream':
                tasks.append(process_xtream(session, src, playlist, semaphore))
            elif src['type'] == 'm3u':
                tasks.append(process_m3u(session, src, playlist, semaphore))
        
        await asyncio.gather(*tasks)

    # DEDUPLICACIÓN FINAL OPTIMIZADA
    logger.info("Iniciando Deduplicación y Limpieza Final...")
    unique_hashes = set()
    for key in playlist.keys():
        if isinstance(playlist[key], list):
            new_list = []
            for item in playlist[key]:
                # Crear ID único basado en titulo limpio
                clean_id = re.sub(r'[^a-z0-9]', '', item['title'].lower() + item.get('quality', ''))
                item_hash = hash(clean_id)
                
                if item_hash not in unique_hashes:
                    new_list.append(item)
                    unique_hashes.add(item_hash)
            playlist[key] = new_list
    
    # Exportar JSON
    with open('playlist_mx.json', 'w', encoding='utf-8') as f: 
        json.dump(playlist, f, indent=4, ensure_ascii=False)

    logger.info(f"--- PROCESO TERMINADO EN {time.time() - t0:.2f}s ---")

if __name__ == "__main__":
    async.run(main())

