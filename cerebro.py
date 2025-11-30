import os
import aiohttp
import asyncio
import json
import re
import time
import logging
import subprocess
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Set

# ==========================================
# 1. CONFIGURACIÓN CENTRAL Y LOGGING
# ==========================================

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CerebroV98.1")

# Credenciales (Lectura de Variables de Entorno)
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")
USER_AGENT = "IPTVSmartersPro/98"

# Parámetros de Rendimiento
MAX_CONCURRENT_CHECKS = 75  # Paralelismo agresivo para Health Check
HTTP_TIMEOUT = 45           
MAX_RETRIES = 2

# Fuentes (Configuración única con las variables de entorno)
SOURCES = [
    { "type": "xtream", "alias": "LatinaPro_MX", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# ==========================================
# 2. MOTORES DE FILTRADO (REGEX HIPER-FOCALIZADO EN MX)
# ==========================================

# Filtro de Inclusión (Whitelisting): Contenido que debe pasar
REGEX_MX_STRICT = r"(?i)\b(mx|mex|mexico|méxico|latam|latino|spanish|español|audio latino)\b"
REGEX_MX_CHANNELS = r"(?i)\b(azteca|televisa|estrellas|canal 5|imagen|adn 40|foro tv|milenio|multimedios|once|canal 22|tdn|tudn|afizzionados)\b"
REGEX_PREMIUM_LATAM = r"(?i)\b(hbo|max|star|disney|espn|fox|gol|win|vix|cnn|axn|warner|tnt|space|universal)\b"

# Filtro de Exclusión (Blocklist): Contenido que debe ser descartado
REGEX_HARD_BLOCK = r"(?i)\b(spain|españa|eu|brazil|brasil|portugal|usa|uk|canada|adult|xxx|porn|hindi|arab|turk|korea|french|german|italian)\b"

# Detección de Calidad y Estrenos
REGEX_4K = r"(?i)\b(4k|uhd|2160p)\b"
REGEX_FHD = r"(?i)\b(fhd|1080p|hevc)\b"
REGEX_HD = r"(?i)\b(hd|720p)\b"
REGEX_PREMIERE_YEAR = r"(2024|2025)" # Lógica simple V98.1

# ==========================================
# 3. MODELADO DE DATOS (DATACLASSES)
# ==========================================

@dataclass
class StreamItem:
    """Modelo de datos inmutable para un stream o VOD."""
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
        """Convierte la dataclass a un diccionario, omitiendo campos vacíos."""
        return {k: v for k, v in asdict(self).items() if v}

# ==========================================
# 4. LÓGICA DE FILTROS Y UTILERÍAS
# ==========================================

class ContentFilter:
    """Lógica de negocio para determinar inclusión, calidad y categoría."""

    @staticmethod
    def is_mexico_focused(name: str) -> bool:
        """
        Determina si el contenido es relevante para la audiencia MX.
        """
        if re.search(REGEX_HARD_BLOCK, name):
            return False
        
        is_mx_region = bool(re.search(REGEX_MX_STRICT, name))
        is_mx_channel = bool(re.search(REGEX_MX_CHANNELS, name))
        is_premium = bool(re.search(REGEX_PREMIUM_LATAM, name))

        return is_mx_region or is_mx_channel or is_premium

    @staticmethod
    def detect_quality(name: str) -> str:
        """Clasifica la calidad del stream basado en el nombre."""
        if re.search(REGEX_4K, name): return "4K"
        if re.search(REGEX_FHD, name): return "FHD"
        if re.search(REGEX_HD, name): return "HD"
        return "SD"

    @staticmethod
    def categorize_live(name: str) -> str:
        """Asigna una categoría de TV en vivo."""
        name_lower = name.lower()
        if any(x in name_lower for x in ['kids', 'infantil', 'disney', 'nick', 'cartoon']): return "kids"
        if any(x in name_lower for x in ['deporte', 'sport', 'espn', 'fox', 'ufc', 'nfl', 'f1', 'liga', 'chivas']): return "sports"
        if any(x in name_lower for x in ['music', 'mtv', 'vh1', 'radio', 'concert']): return "music"
        if any(x in name_lower for x in ['discovery', 'history', 'nat geo', 'documental']): return "docs"
        return "live_tv" 

    @staticmethod
    def is_premiere(item: Dict[str, Any], name: str) -> bool:
        """
        Lógica de Estrenos: Título/Metadata contiene 2024 o 2025.
        """
        if re.search(REGEX_PREMIERE_YEAR, name):
            return True
        release_date = str(item.get('releasedate') or item.get('releaseDate') or item.get('year', ''))
        if re.search(REGEX_PREMIERE_YEAR, release_date):
            return True
            
        return False
        
    @staticmethod
    def clean_rating(value: Any) -> float:
        """Limpia y normaliza el rating a un valor flotante entre 0.0 y 10.0."""
        if not value: return 0.0
        try:
            val_str = str(value).lower()
            val_str = re.sub(r"[^0-9.]", "", val_str.split('/')[0])
            r = float(val_str) if val_str else 0.0
            return r if r <= 10 else 10.0
        except: return 0.0


# ==========================================
# 5. NETWORKING Y CHECKEO DE SALUD
# ==========================================

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[List[Dict[str, Any]]]:
    """Descarga JSON de la API con manejo de errores."""
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT) as response:
                if response.status == 200:
                    data = await response.json()
                    # A menudo la API retorna { "streams": [...] } o solo [...]
                    if isinstance(data, dict) and 'streams' in data:
                        return data['streams']
                    if isinstance(data, list):
                        return data
                elif response.status in (401, 403):
                    logger.error(f"Acceso denegado a {url}. Revisar credenciales.")
                    return None
        except Exception as e:
            logger.warning(f"Error ({attempt+1}/{MAX_RETRIES}) fetching {url}: {e}")
            await asyncio.sleep(2 ** attempt)
    return None

async def check_stream_health(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> bool:
    """Verifica si el stream responde mediante una petición HEAD."""
    async with semaphore:
        try:
            async with session.head(url, timeout=10, allow_redirects=True) as response:
                return response.status in (200, 301, 302)
        except:
            return False

# ==========================================
# 6. PROCESADORES DE CONTENIDO
# ==========================================

async def process_xtream_live(session, source, playlist_container, semaphore):
    """Procesa TV en vivo con filtro estricto MX."""
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_live_streams"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): return

    tasks = []
    logger.info(f"[{source['alias']}] Procesando {len(data)} canales en vivo...")

    for item in data:
        name = item.get('name', '')
        
        if ContentFilter.is_mexico_focused(name):
            cat_key = ContentFilter.categorize_live(name)
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
        valid_streams = [t[0].to_dict() for t, is_valid in zip(tasks, results) if is_valid]
        
        for s in valid_streams:
            playlist_container[s['group']].append(s)

    logger.info(f"[{source['alias']}] Canales MX funcionales agregados: {len(valid_streams)}")


async def process_xtream_vod(session, source, playlist_container, type_action="get_vod_streams"):
    """Procesa Películas (VOD) con filtro de Estrenos 2024/2025."""
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={type_action}"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Analizando VOD ({len(data)} items)...")
    count_premieres = 0
    
    for item in data:
        name = item.get('name', '')
        if re.search(REGEX_HARD_BLOCK, name): continue

        stream_id = item.get('stream_id')
        ext = item.get('container_extension', 'mp4')
        
        obj = StreamItem(
            title=name,
            contentId=str(stream_id),
            group="movies",
            url=f"{source['host']}/movie/{source['user']}/{source['pass']}/{stream_id}.{ext}",
            hdPosterUrl=item.get('stream_icon'),
            rating=ContentFilter.clean_rating(item.get('rating')),
            plot=item.get('plot', 'Sin descripción.'),
            genre=item.get('genre', 'General'),
            releaseDate=item.get('releasedate') or item.get('releaseDate'),
            quality=ContentFilter.detect_quality(name),
            source_alias=source['alias']
        )
        
        dict_obj = obj.to_dict()
        
        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(dict_obj)
            count_premieres += 1
            
        playlist_container["movies"].append(dict_obj)
        
    logger.info(f"[{source['alias']}] VOD total: {len(data)} | Estrenos: {count_premieres}")

async def process_xtream_series(session, source, playlist_container):
    """Procesa Series con filtro de Estrenos 2024/2025."""
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Analizando Series ({len(data)} items)...")
    count_premieres_series = 0

    for item in data:
        name = item.get('name', '')
        if re.search(REGEX_HARD_BLOCK, name): continue

        series_id = str(item.get('series_id'))
        api_url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series_info&series_id={series_id}"

        obj = StreamItem(
            title=name,
            contentId=series_id,
            group="series",
            url=api_url, 
            hdPosterUrl=item.get('cover'),
            rating=ContentFilter.clean_rating(item.get('rating')),
            plot=item.get('plot', 'Sin descripción.'),
            genre=item.get('genre', 'General'),
            releaseDate=item.get('releaseDate'),
            source_alias=source['alias'],
            series_id=series_id,
            api_url=api_url
        )
        
        dict_obj = obj.to_dict()
        playlist_container["series"].append(dict_obj)

        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(dict_obj)
            count_premieres_series += 1
            
    logger.info(f"[{source['alias']}] Series total: {len(data)} | Estrenos Series: {count_premieres_series}")

# ==========================================
# 7. ORQUESTADOR Y DEDUPLICACIÓN
# ==========================================

def deduplicate_and_prioritize(playlist: Dict[str, Any]):
    """Deduplica contenido y prioriza la mejor calidad (4K > FHD > HD)."""
    logger.info("Iniciando Deduplicación inteligente...")
    quality_map = {"4K": 4, "FHD": 3, "HD": 2, "SD": 1}

    for category in playlist:
        if category == "meta": continue
        
        # 1. Ordenar por Título (primario) y Calidad (secundario, descendente)
        playlist[category].sort(key=lambda x: (x['title'], -quality_map.get(x.get('quality', 'SD'), 1)))

        seen_titles = set()
        unique_list = []
        
        for item in playlist[category]:
            # Usamos una clave de deduplicación simple: Título limpio.
            # Al estar ordenado, el primer item con un título dado será el de mejor calidad.
            clean_title = re.sub(r'[^a-z0-9]', '', item['title'].lower())
            
            if clean_title not in seen_titles:
                unique_list.append(item)
                seen_titles.add(clean_title)
        
        playlist[category] = unique_list
        logger.info(f"Categoría '{category}': {len(unique_list)} items únicos.")

def push_to_github(filename: str):
    """Sube automáticamente el archivo generado a GitHub."""
    logger.info("--- INICIANDO AUTO-PUSH A GITHUB ---")
    try:
        subprocess.run(["git", "add", filename], check=True)
        commit_msg = f"Auto-update Playlist: {time.strftime('%Y-%m-%d %H:%M')}"
        # check=False: Permite que falle si no hay cambios para hacer commit.
        subprocess.run(["git", "commit", "-m", commit_msg], check=False) 
        
        result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)
        
        if result.returncode == 0 or "Everything up-to-date" in result.stdout:
            logger.info("✅ ÉXITO: Archivo subido/actualizado en GitHub.")
        else:
            logger.warning(f"⚠️ Alerta Git ({result.returncode}): {result.stderr.strip()}")
            
    except Exception as e:
        logger.error(f"❌ Error crítico en Git Automation: {e}")


async def main():
    start_time = time.time()
    
    # Estructura de la Playlist Final
    playlist = {
        "meta": { 
            "generated_at": time.ctime(), 
            "version": "v98.1_MX_Strict_Final", 
            "focus": "Mexico_Only" 
        },
        "premieres": [],
        "live_tv": [], 
        "sports": [], 
        "kids": [], 
        "docs": [],
        "movies": [], 
        "series": []
    }

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
    
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        for src in SOURCES:
            if not src.get('host') or not XT_HOST: continue 

            tasks.append(process_xtream_live(session, src, playlist, semaphore))
            tasks.append(process_xtream_vod(session, src, playlist))
            tasks.append(process_xtream_series(session, src, playlist))
        
        await asyncio.gather(*tasks)

    # Post-Proceso
    deduplicate_and_prioritize(playlist)

    # Output (Nombre de archivo solicitado)
    final_filename = 'playlist.json'
    with open(final_filename, 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)

    elapsed = time.time() - start_time
    logger.info(f"--- PROCESO FINALIZADO EN {elapsed:.2f}s ---")
    
    # Subida automática a GitHub
    push_to_github(final_filename)


if __name__ == "__main__":
    if not XT_HOST or not XT_USER or not XT_PASS:
        logger.error("🚫 ERROR: Faltan variables de entorno (XT_HOST, XT_USER, XT_PASS).")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Proceso detenido por el usuario.")



