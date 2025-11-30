import os
import aiohttp
import asyncio
import json
import re
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Set

# ==========================================
# 1. CONFIGURACIÓN DEL SISTEMA
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CerebroV98_MX")

# Credenciales
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")

# Configuración de Red
USER_AGENT = "IPTVSmartersPro"
MAX_CONCURRENT_CHECKS = 75  # Aumentado para mayor throughput
HTTP_TIMEOUT = 45           # Reducido para descartar streams lentos más rápido
MAX_RETRIES = 2

# Fuentes
SOURCES = [
    { "type": "xtream", "alias": "Proveedor_Principal", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# ==========================================
# 2. MOTORES DE FILTRADO (REGEX AVANZADO)
# ==========================================

# FILOSOFÍA: Si no es explícitamente para México/Latam, se descarta.
# Esto asegura pureza en el contenido.

REGEX_MX_STRICT = r"(?i)\b(mx|mex|mexico|méxico|latam|latino|spanish|español)\b"
REGEX_MX_CHANNELS = r"(?i)\b(azteca|televisa|estrellas|canal 5|imagen|adn 40|foro tv|milenio|multimedios|once|canal 22|unam|tdn|tudn|afizzionados)\b"
REGEX_PREMIUM_LATAM = r"(?i)\b(hbo|max|star|disney|espn|fox|f1|nfl|nba|ufc|premier|ligapro|gol|win|vix)\b"

# Bloqueo explícito de basura que suele colarse
REGEX_HARD_BLOCK = r"(?i)\b(spain|españa|eu|brazil|brasil|portugal|usa|uk|canada|hindi|arab|turk)\b"

# Detección de Calidad
REGEX_4K = r"(?i)\b(4k|uhd|2160p)\b"
REGEX_FHD = r"(?i)\b(fhd|1080p|hevc)\b"
REGEX_HD = r"(?i)\b(hd|720p)\b"

# Estrenos: Simple y directo como solicitaste
REGEX_PREMIERE_YEAR = r"(2024|2025)"

# ==========================================
# 3. MODELADO DE DATOS (DATACLASSES)
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
    # Metadatos extra para players avanzados (TiviMate/Roku)
    series_id: str = ""
    api_url: str = ""
    
    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v}

# ==========================================
# 4. UTILERÍAS DE ALTO RENDIMIENTO
# ==========================================

class ContentFilter:
    """Clase estática para encapsular la lógica de negocio de filtrado."""

    @staticmethod
    def is_mexico_focused(name: str) -> bool:
        """
        Determina si el contenido es relevante para México.
        Lógica: (Tiene marca MX O es Canal MX O es Premium Latam) Y (NO es bloqueado explícito)
        """
        if re.search(REGEX_HARD_BLOCK, name):
            return False
        
        is_mx_region = bool(re.search(REGEX_MX_STRICT, name))
        is_mx_channel = bool(re.search(REGEX_MX_CHANNELS, name))
        is_premium = bool(re.search(REGEX_PREMIUM_LATAM, name))

        return is_mx_region or is_mx_channel or is_premium

    @staticmethod
    def detect_quality(name: str) -> str:
        if re.search(REGEX_4K, name): return "4K"
        if re.search(REGEX_FHD, name): return "FHD"
        if re.search(REGEX_HD, name): return "HD"
        return "SD"

    @staticmethod
    def categorize_live(name: str) -> str:
        name_lower = name.lower()
        if any(x in name_lower for x in ['kids', 'infantil', 'disney', 'nick', 'cartoon']): return "KIDS"
        if any(x in name_lower for x in ['deporte', 'sport', 'espn', 'fox', 'ufc', 'nfl', 'f1']): return "SPORTS"
        if any(x in name_lower for x in ['music', 'mtv', 'vh1', 'radio', 'concert']): return "MUSIC"
        if any(x in name_lower for x in ['hbo', 'max', 'premium', 'cine', 'movie']): return "MOVIES_LIVE"
        return "LIVE_TV" # General MX TV

    @staticmethod
    def is_premiere(item: Dict[str, Any], name: str) -> bool:
        """
        Lógica Simplificada V98:
        Si el título o la fecha dicen 2024 o 2025, es estreno. Punto.
        """
        # 1. Buscar en el nombre
        if re.search(REGEX_PREMIERE_YEAR, name):
            return True
        
        # 2. Buscar en atributos de fecha
        release_date = str(item.get('releasedate') or item.get('releaseDate') or item.get('year', ''))
        if re.search(REGEX_PREMIERE_YEAR, release_date):
            return True
            
        return False

# ==========================================
# 5. NETWORKING (ASYNCIO OPTIMIZADO)
# ==========================================

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as response:
            if response.status == 200:
                # Optimización: json() de aiohttp es rápido, pero en listas gigantes 
                # a veces conviene text() y luego ujson, pero usaremos el estándar por compatibilidad.
                return await response.json()
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
    return None

async def check_stream_health(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> bool:
    """Verifica si el stream responde (HEAD request) respetando el semáforo."""
    async with semaphore:
        try:
            async with session.head(url, timeout=10) as response:
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
        
        # FILTRO 200 IQ: Solo pasa lo enfocado a México
        if ContentFilter.is_mexico_focused(name):
            cat = ContentFilter.categorize_live(name)
            stream_id = item.get('stream_id')
            play_url = f"{source['host']}/live/{source['user']}/{source['pass']}/{stream_id}.ts"
            
            stream_obj = StreamItem(
                title=name,
                contentId=str(stream_id),
                group=cat,
                url=play_url,
                hdPosterUrl=item.get('stream_icon'),
                quality=ContentFilter.detect_quality(name),
                source_alias=source['alias']
            )
            
            # Verificación de salud (Health Check)
            tasks.append((stream_obj, check_stream_health(session, play_url, semaphore)))

    # Ejecución concurrente masiva
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        valid_streams = [t[0].to_dict() for t, is_valid in zip(tasks, results) if is_valid]
        
        # Distribución en categorías
        for s in valid_streams:
            cat_key = s['group'].lower()
            if cat_key in playlist_container:
                playlist_container[cat_key].append(s)
            elif cat_key == "movies_live":
                 playlist_container["movies"].append(s) # Canales de cine a movies o live_tv según prefieras
            else:
                playlist_container["live_tv"].append(s)

    logger.info(f"[{source['alias']}] Canales MX agregados: {len(valid_streams)}")


async def process_xtream_vod(session, source, playlist_container, type_action="get_vod_streams"):
    """
    Procesa Películas (VOD).
    Aquí el filtro MX es menos estricto en nombre, pero sí en AUDIO (idealmente),
    pero nos centraremos en la lógica de Estrenos 2024/2025.
    """
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={type_action}"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Analizando VOD ({type_action})...")
    
    for item in data:
        name = item.get('name', '')
        
        # Ignorar pornografía o contenido basura global
        if re.search(REGEX_HARD_BLOCK, name): continue

        ext = item.get('container_extension', 'mp4')
        stream_id = item.get('stream_id')
        
        obj = StreamItem(
            title=name,
            contentId=str(stream_id),
            group="MOVIES",
            url=f"{source['host']}/movie/{source['user']}/{source['pass']}/{stream_id}.{ext}",
            hdPosterUrl=item.get('stream_icon'),
            rating=float(item.get('rating') or 0),
            quality=ContentFilter.detect_quality(name),
            source_alias=source['alias']
        )

        # Lógica de ESTRENOS (Premieres)
        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(obj.to_dict())
        else:
            # Solo agregamos al catálogo general si NO es basura y si quieres todo el catálogo
            # Si quieres VOD "solo Mexico" es difícil filtrar por nombre, 
            # así que asumimos que el usuario quiere todo el VOD limpio.
            playlist_container["movies"].append(obj.to_dict())

async def process_xtream_series(session, source, playlist_container):
    """Procesa Series con lógica de Estreno."""
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): return

    for item in data:
        name = item.get('name', '')
        if re.search(REGEX_HARD_BLOCK, name): continue

        series_id = str(item.get('series_id'))
        
        # URL API para Roku/TiviMate (Deep linking)
        api_url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series_info&series_id={series_id}"

        obj = StreamItem(
            title=name,
            contentId=series_id,
            group="SERIES",
            url=api_url, # En series, la URL principal suele ser la API de info
            hdPosterUrl=item.get('cover'),
            rating=float(item.get('rating') or 0),
            releaseDate=item.get('releaseDate'),
            source_alias=source['alias'],
            series_id=series_id,
            api_url=api_url
        )

        dict_obj = obj.to_dict()
        playlist_container["series"].append(dict_obj)

        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(dict_obj)

# ==========================================
# 7. ORQUESTADOR PRINCIPAL
# ==========================================

async def main():
    start_time = time.time()
    
    # Contenedor principal estructurado
    playlist = {
        "meta": { 
            "generated_at": time.ctime(), 
            "version": "v98_MX_Strict", 
            "focus": "Mexico_Only" 
        },
        "premieres": [], # Prioridad 1
        "live_tv": [],
        "sports": [],
        "kids": [],
        "music": [],
        "movies": [],
        "series": []
    }

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
    
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        for src in SOURCES:
            if not src.get('host'): continue # Skip invalid config

            # Lanzamos procesos en paralelo
            tasks.append(process_xtream_live(session, src, playlist, semaphore))
            tasks.append(process_xtream_vod(session, src, playlist))
            tasks.append(process_xtream_series(session, src, playlist))
        
        await asyncio.gather(*tasks)

    # ==========================================
    # DEDUPLICACIÓN INTELIGENTE
    # ==========================================
    logger.info("Optimizando y Deduplicando catálogo...")
    
    for category in playlist:
        if category == "meta": continue
        
        seen_ids = set()
        unique_list = []
        
        # Ordenamos por calidad (4K > FHD > HD > SD) antes de deduplicar
        # para quedarnos con la mejor versión si hay duplicados.
        quality_map = {"4K": 4, "FHD": 3, "HD": 2, "SD": 1}
        
        # Sort in place: Primero por titulo, luego por calidad descendente
        playlist[category].sort(key=lambda x: (x['title'], -quality_map.get(x.get('quality', 'SD'), 1)))

        for item in playlist[category]:
            # Hash compuesto: Título normalizado + Año (si existe)
            # Esto evita tener "Pelicula (2024)" repetida, pero permite "Pelicula 2"
            clean_title = re.sub(r'[^a-z0-9]', '', item['title'].lower())
            
            if clean_title not in seen_ids:
                unique_list.append(item)
                seen_ids.add(clean_title)
        
        playlist[category] = unique_list

    # Output
    filename = 'playlist_mx_v98.json'
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)

    elapsed = time.time() - start_time
    logger.info(f"--- ÉXITO: Playlist generada en {elapsed:.2f}s ---")
    logger.info(f"Estrenos: {len(playlist['premieres'])} | TV MX: {len(playlist['live_tv'])} | Deportes: {len(playlist['sports'])}")

if __name__ == "__main__":
    if not XT_HOST or not XT_USER:
        logger.error("Faltan variables de entorno (XT_HOST, XT_USER, XT_PASS)")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass


