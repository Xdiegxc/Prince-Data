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

# ==============================================================================
# 1. CONFIGURACIÓN Y CONSTANTES
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("Cerebro_V100")

# --- CREDENCIALES (Variables de Entorno) ---
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")

# User Agent rotativo o fijo de alta compatibilidad
USER_AGENT = "IPTVSmartersPro/98.2"

# --- TUNING DE RENDIMIENTO ---
# 50 es un número seguro para no saturar al proveedor y evitar bloqueos de IP
MAX_CONCURRENT_CHECKS = 50 
HTTP_TIMEOUT = 25
MAX_RETRIES = 3

# --- CANALES MANUALES (VIP / PROTEGIDOS) ---
MANUAL_OVERRIDES = [
    {
        "type": "manual_stream",
        "title": "Canal VIP Ejemplo (Manual)",
        "contentId": "manual.vip.01",
        "group": "live_tv",
        "url": "http://ejemplo.com/video.m3u8",
        "hdPosterUrl": "https://via.placeholder.com/300",
        "quality": "HD"
    }
]

SOURCES = [
    { "type": "xtream", "alias": "MainProvider", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# ==============================================================================
# 2. MOTORES DE EXPRESIÓN REGULAR (FILTROS)
# ==============================================================================

RE_FLAGS = re.IGNORECASE

PATTERNS = {
    # --- Región & Idioma ---
    "MX_STRICT": re.compile(r"\b(mx|mex|mexico|méxico|latam|latino|spanish|español)\b", RE_FLAGS),
    "MX_CHANNELS": re.compile(r"\b(azteca|televisa|estrellas|canal 5|imagen|adn 40|foro tv|milenio|multimedios|once|canal 22|tdn|tudn|afizzionados|univision|unimas|telemundo)\b", RE_FLAGS),
    "SPAIN_ALLOW": re.compile(r"\b(spain|españa|es)\b", RE_FLAGS),

    # --- Bloqueos (Hard Block) ---
    # Eliminamos canales 24/7 de series repetitivas, adultos y países no deseados
    "HARD_BLOCK": re.compile(r"\b(usa|uk|canada|adult|xxx|porn|sex|hindi|arab|turk|korea|french|german|italian|brasil|brazil|portugal|pt|24/7)\b", RE_FLAGS),
    "SPORTS_BLOCK": re.compile(r"\b(brasil|brazil|portugal|pt)\b", RE_FLAGS), # Deportes que NO queremos

    # --- Categorías ---
    "SPORTS": re.compile(r"(deporte|sport|espn|fox|ufc|nfl|nba|mlb|f1|liga|chivas|beisbol|tenis|racing|dazn|claro|win|gol|tudn|tyc)", RE_FLAGS),
    "KIDS": re.compile(r"\b(kids|infantil|cartoon|cn|nick|disney|discovery kids|toons|baby|junior|boomerang|dreamworks|semillitas)\b", RE_FLAGS),
    "DOCS": re.compile(r"\b(discovery|history|h2|nat geo|animal planet|investigation|tlc|h\&h|cocina|food|travel|arts)\b", RE_FLAGS),
    "MUSIC": re.compile(r"(music|mtv|vh1|radio|concert|htv|telehit|bandamax)", RE_FLAGS),

    # --- Calidad & Meta ---
    "4K": re.compile(r"\b(4k|uhd|2160p)\b", RE_FLAGS),
    "FHD": re.compile(r"\b(fhd|1080p|hevc)\b", RE_FLAGS),
    "HD": re.compile(r"\b(hd|720p)\b", RE_FLAGS),
    "PREMIERE_YEAR": re.compile(r"(2024|2025)", RE_FLAGS)
}

# ==============================================================================
# 3. MODELADO DE DATOS
# ==============================================================================

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
    is_manual: bool = False  # Flag crítico para que el deduplicador no borre canales manuales
    
    def to_dict(self):
        # Solo retornamos valores que no estén vacíos para ahorrar espacio en el JSON
        return {k: v for k, v in asdict(self).items() if v}

class ContentFilter:
    @staticmethod
    def detect_quality(name: str) -> str:
        if PATTERNS["4K"].search(name): return "4K"
        if PATTERNS["FHD"].search(name): return "FHD"
        if PATTERNS["HD"].search(name): return "HD"
        return "SD"

    @staticmethod
    def clean_rating(value: Any) -> float:
        if not value: return 0.0
        try:
            val_str = str(value).split('/')[0]
            # Extraer el primer número flotante encontrado
            r = float(re.findall(r"[\d\.]+", val_str)[0])
            return min(r, 10.0)
        except: return 0.0

    @staticmethod
    def is_premiere(item: Dict, name: str) -> bool:
        if PATTERNS["PREMIERE_YEAR"].search(name): return True
        r_date = str(item.get('releasedate') or item.get('releaseDate') or item.get('year', ''))
        return bool(PATTERNS["PREMIERE_YEAR"].search(r_date))

# ==============================================================================
# 4. CAPA DE RED (NETWORKING BLINDADO)
# ==============================================================================

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    """ Descarga JSON robusta. Ignora errores SSL y valida Content-Type. """
    for attempt in range(MAX_RETRIES):
        try:
            # ssl=False es OBLIGATORIO para el 90% de los proveedores IPTV
            async with session.get(url, timeout=HTTP_TIMEOUT, ssl=False) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except:
                        # Fallback: Algunos servers envían JSON con header text/html
                        text = await response.text()
                        return json.loads(text)
                elif response.status == 429:
                    # Rate limiting
                    await asyncio.sleep(2)
        except Exception as e:
            # logger.debug(f"Fetch error ({attempt+1}/{MAX_RETRIES}): {e}")
            await asyncio.sleep(1)
    return None

async def check_stream_health(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> bool:
    """ Verificación Híbrida: Intenta HEAD, si falla o da 405, intenta GET headers. """
    async with semaphore:
        try:
            # Método 1: HEAD (Rápido)
            async with session.head(url, timeout=6, ssl=False, allow_redirects=True) as response:
                if response.status < 400: return True
                if response.status == 405: pass # Method Not Allowed -> Intentar GET
                else: return False
            
            # Método 2: GET (Fallback seguro)
            async with session.get(url, timeout=6, ssl=False, allow_redirects=True) as response:
                return response.status < 400
        except:
            return False

# ==============================================================================
# 5. PROCESADORES DE CONTENIDO
# ==============================================================================

async def process_manual_streams(session, playlist_container, semaphore):
    """ Procesa la lista MANUAL_OVERRIDES. Estos tienen prioridad máxima. """
    if not MANUAL_OVERRIDES: return
    
    logger.info(f"[Manual] Procesando {len(MANUAL_OVERRIDES)} canales VIP...")
    tasks = []
    
    for item in MANUAL_OVERRIDES:
        stream_obj = StreamItem(
            title=item['title'],
            contentId=item['contentId'],
            group=item['group'],
            url=item['url'],
            hdPosterUrl=item.get('hdPosterUrl', ''),
            quality=item.get('quality', 'SD'),
            source_alias="ManualVIP",
            is_manual=True
        )
        # Verificamos salud también de los manuales
        tasks.append((stream_obj, check_stream_health(session, item['url'], semaphore)))
    
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        count = 0
        for (stream, is_alive) in zip(tasks, results):
            # Opcional: Si quieres forzar que aparezcan aunque estén offline, quita el 'if is_alive'
            if is_alive:
                playlist_container[stream[0].group].append(stream[0].to_dict())
                count += 1
        logger.info(f"[Manual] Agregados: {count}")

async def process_xtream_live(session, source, playlist_container, semaphore):
    """ Procesa canales en vivo desde Xtream Codes. """
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_live_streams"
    data = await fetch_json(session, url)
    
    if not isinstance(data, list): 
        logger.error(f"[{source['alias']}] Error: No se recibió lista de canales.")
        return

    tasks = []
    logger.info(f"[{source['alias']}] Analizando {len(data)} canales LIVE...")

    for item in data:
        name = item.get('name', '')
        
        # 1. BLOQUEO DURO (Hard Block)
        if PATTERNS["HARD_BLOCK"].search(name): continue
        
        # 2. CATEGORIZACIÓN INTELIGENTE
        group = None
        should_include = False

        # El orden de estos ifs define la prioridad de categorización
        if PATTERNS["KIDS"].search(name):
            group = "kids"
            should_include = True
        elif PATTERNS["DOCS"].search(name):
            group = "docs"
            should_include = True
        elif PATTERNS["MUSIC"].search(name):
            group = "music"
            should_include = True
        elif PATTERNS["SPORTS"].search(name):
            if not PATTERNS["SPORTS_BLOCK"].search(name):
                group = "sports"
                should_include = True
        elif PATTERNS["MX_STRICT"].search(name) or PATTERNS["MX_CHANNELS"].search(name):
            group = "live_tv"
            should_include = True
        
        # 3. CREACIÓN DEL OBJETO
        if should_include and group:
            stream_id = item.get('stream_id')
            # URL final para el reproductor (.ts)
            play_url = f"{source['host']}/live/{source['user']}/{source['pass']}/{stream_id}.ts"
            
            stream_obj = StreamItem(
                title=name,
                contentId=str(stream_id),
                group=group,
                url=play_url,
                hdPosterUrl=item.get('stream_icon'),
                quality=ContentFilter.detect_quality(name),
                source_alias=source['alias']
            )
            
            # Agregamos a la cola de verificación de salud
            tasks.append((stream_obj, check_stream_health(session, play_url, semaphore)))

    # 4. EJECUCIÓN PARALELA (Async Gather)
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        
        valid_count = 0
        for (task, is_alive) in zip(tasks, results):
            if is_alive:
                stream_item = task[0]
                playlist_container[stream_item.group].append(stream_item.to_dict())
                valid_count += 1

        logger.info(f"[{source['alias']}] Canales LIVE procesados: {valid_count} aceptados.")

async def process_xtream_vod(session, source, playlist_container, action_type="get_vod_streams"):
    """ Procesa Películas (VOD). No hacemos health check individual para ahorrar tiempo. """
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action={action_type}"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Procesando VOD ({action_type})...")
    
    count = 0
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
        count += 1
    
    logger.info(f"[{source['alias']}] Películas agregadas: {count}")

async def process_xtream_series(session, source, playlist_container):
    """ Procesa Series. Solo la info base, Roku carga episodios bajo demanda. """
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    logger.info(f"[{source['alias']}] Procesando Series...")
    count = 0
    for item in data:
        name = item.get('name', '')
        if PATTERNS["HARD_BLOCK"].search(name): continue

        series_id = str(item.get('series_id'))
        # La URL aquí no es de video, sino para que la Task de Roku pida la info
        api_url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series_info&series_id={series_id}"
        
        obj = StreamItem(
            title=name,
            contentId=series_id,
            group="series",
            url=api_url, # URL lógica para SeriesLoaderTask
            hdPosterUrl=item.get('cover'),
            rating=ContentFilter.clean_rating(item.get('rating')),
            plot=item.get('plot', ''),
            genre=item.get('genre', ''),
            releaseDate=item.get('releaseDate'),
            source_alias=source['alias'],
            series_id=series_id # Importante para Roku
        ).to_dict()

        playlist_container["series"].append(obj)
        if ContentFilter.is_premiere(item, name):
            playlist_container["premieres"].append(obj)
        count += 1

    logger.info(f"[{source['alias']}] Series agregadas: {count}")

# ==============================================================================
# 6. UTILIDADES DE LIMPIEZA Y SYNC
# ==============================================================================

def deduplicate_and_sort(playlist: Dict[str, Any]):
    """ Ordena por prioridad y elimina duplicados basados en nombre normalizado. """
    logger.info("Optimizando y Desduplicando Playlist...")
    
    # Rango de calidad para el sort
    quality_rank = {"4K": 4, "FHD": 3, "HD": 2, "SD": 1}

    for category in playlist:
        if category == "meta": continue
        
        # 1. ORDENAR: Manual primero -> Título -> Mejor Calidad
        playlist[category].sort(key=lambda x: (
            not x.get('is_manual', False), # False < True, así que manual va primero
            x['title'], 
            -quality_rank.get(x.get('quality', 'SD'), 1)
        ))

        # 2. DEDUPLICAR
        seen = set()
        unique_list = []
        
        for item in playlist[category]:
            # Normalizar título (eliminar signos, espacios, minúsculas)
            norm_title = re.sub(r'[^a-z0-9]', '', item['title'].lower())
            
            # Si es manual, siempre entra (aunque parezca duplicado)
            if item.get('is_manual', False):
                unique_list.append(item)
                seen.add(norm_title)
            elif norm_title not in seen:
                unique_list.append(item)
                seen.add(norm_title)
        
        playlist[category] = unique_list
        logger.info(f"   └── {category}: {len(unique_list)} items finales.")

def push_to_github(filename: str):
    logger.info("--- SYNC GITHUB ---")
    try:
        # Verificar si hay cambios
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if not status.stdout.strip():
            logger.info("⚡ Sin cambios detectados. No se hace push.")
            return

        subprocess.run(["git", "add", filename], check=True)
        commit_msg = f"Playlist Update: {time.strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)
        subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, timeout=60)
        logger.info("✅ GitHub Push Exitoso.")
    except Exception as e:
        logger.error(f"❌ Error en Git Sync: {e}")

# ==============================================================================
# 7. EJECUCIÓN PRINCIPAL
# ==============================================================================

async def main():
    start_time = time.time()
    
    # Estructura JSON que espera Roku
    playlist = {
        "meta": { "generated_at": time.ctime(), "version": "v100.0_Stable" },
        "premieres": [],
        "live_tv": [],
        "sports": [],
        "kids": [],
        "docs": [],
        "music": [],
        "movies": [],
        "series": []
    }

    # Configuración de Conexión (SSL False Global)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_CHECKS, ssl=False, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        
        # 1. Procesar Manuales
        tasks.append(process_manual_streams(session, playlist, semaphore))
        
        # 2. Procesar Fuentes Xtream
        for src in SOURCES:
            if not src.get('host'): continue
            
            # Live TV
            tasks.append(process_xtream_live(session, src, playlist, semaphore))
            # VOD
            tasks.append(process_xtream_vod(session, src, playlist))
            # Series
            tasks.append(process_xtream_series(session, src, playlist))
        
        # Esperar a que todo termine
        await asyncio.gather(*tasks)

    # Limpieza final
    deduplicate_and_sort(playlist)

    # Guardar archivo
    final_filename = 'playlist.json'
    with open(final_filename, 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)

    logger.info(f"--- PROCESO COMPLETADO EN {time.time() - start_time:.2f} SEGUNDOS ---")
    
    # Subir a la nube
    push_to_github(final_filename)

if __name__ == "__main__":
    if not XT_HOST:
        logger.error("🚫 ERROR FATAL: No se encontraron credenciales XT_HOST en variables de entorno.")
    else:
        try:
            # Fix para Windows SelectorEventLoop
            if os.name == 'nt': 
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Proceso interrumpido por el usuario.")
        except Exception as e:
            logger.exception(f"Error inesperado: {e}")








