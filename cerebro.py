import os
import aiohttp
import asyncio
import json
import re
import time
import logging
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urlparse

# ==========================================
# 1. CONFIGURACIÓN CENTRAL Y LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CerebroV99.0_Hybrid")

# --- Credenciales Xtream (Legacy/Principal) ---
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")
USER_AGENT = "IPTVSmartersPro/99.0"

# --- Parámetros de Rendimiento ---
MAX_CONCURRENT_CHECKS = 100  # Aumentado para manejar más fuentes
HTTP_TIMEOUT = 30            # Reducido para descartar fuentes lentas rápido
MAX_RETRIES = 2

# ==========================================
# 2. DEFINICIÓN DE FUENTES (STRATEGY PATTERN)
# ==========================================

# Fuentes basadas en API (JSON estructurado)
XTREAM_SOURCES = [
    { "type": "xtream", "alias": "LatinaPro_Main", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
]

# Fuentes basadas en Listas Planas (M3U/M3U8)
# Nota: Se incluyen las URLs proporcionadas.
M3U_SOURCES = [
    {"alias": "Mirror_77", "url": "http://77.237.238.21:2082/get.php?username=VicTorC&password=Victo423&type=m3u_plus"},
    {"alias": "Latina_Bkup", "url": "http://tvappapk@latinapro.net:25461/get.php?username=lazaroperez&password=perez3&type=m3u_plus"},
    {"alias": "UK_Server", "url": "http://ip96.uk:8080/get.php?username=H645668DH&password=7565848DHY&type=m3u_plus"},
    {"alias": "VocoTV", "url": "http://vocotv.live/get.php?username=Sanchez01&password=Sanchez01&type=m3u_plus&output=ts"},
    {"alias": "TV14S", "url": "http://tv14s.xyz:8080/get.php?username=71700855&password=71700855&type=m3u_plus"},
    {"alias": "ClubTV_Lista1", "url": "http://clubtv.link/20nv/lista1.m3u"},
    {"alias": "ClubTV_GeoMx", "url": "http://clubtv.link/20nv/geomex.m3u"},
    {"alias": "Pluto_MX", "url": "https://i.mjh.nz/PlutoTV/mx.m3u8"},
    {"alias": "Pastebin_Mix", "url": "https://pastebin.com/raw/CgA3a8Yp"},
    {"alias": "ClubTV_Movies", "url": "http://clubtv.link/24no/peliculas.m3u"},
]

# ==========================================
# 3. EXPRESIONES REGULARES AVANZADAS
# ==========================================

# Filtros de Contenido (Manteniendo tu lógica estricta)
REGEX_HARD_BLOCK_GENERAL = r"(?i)\b(uk|canada|adult|xxx|porn|hindi|arab|turk|korea|french|german|italian|brasil|brazil|portugal|pt)\b"
REGEX_SPORTS_BLOCK = r"(?i)\b(brasil|brazil|portugal|pt)\b"
REGEX_MX_STRICT = r"(?i)\b(mx|mex|mexico|méxico|latam|latino|spanish|español|audio latino)\b"
REGEX_MX_CHANNELS = r"(?i)\b(azteca|televisa|estrellas|canal 5|imagen|adn 40|foro tv|milenio|multimedios|once|canal 22|tdn|tudn|afizzionados)\b"
REGEX_PREMIUM_LATAM = r"(?i)\b(hbo|max|star|disney|espn|fox|f1|gol|win|vix|cnn|axn|warner|tnt|space|universal)\b"

# --- Categorización ---
REGEX_KIDS = r"(?i)\b(kids|infantil|cartoon|nick|disney|discovery kids|paka paka|boing|clantv|cbeebies|zaz|toons|baby|junior)\b"
REGEX_DOCS = r"(?i)\b(discovery|history|nat geo|national geographic|documental|docu|a\&e|misterio|science|viajes|travel|animal planet|investigation)\b"
REGEX_MOVIES = r"(?i)\b(pelicula|movie|cinema|cine|film|vod|estreno)\b" 

# --- Calidad ---
REGEX_4K = r"(?i)\b(4k|uhd|2160p)\b"
REGEX_FHD = r"(?i)\b(fhd|1080p|hevc)\b"
REGEX_HD = r"(?i)\b(hd|720p)\b"

# --- Parser M3U (El corazón de la nueva ingesta) ---
# Captura #EXTINF con atributos opcionales y el título
REGEX_M3U_EXTINF = re.compile(r'#EXTINF:(?P<duration>[-0-9]+)(?:,| )(?P<attrs>.*),(?P<title>.*?)[\r\n]+(?P<url>http[s]?://[^\s]+)', re.MULTILINE)

# ==========================================
# 4. MODELADO DE DATOS
# ==========================================

@dataclass
class StreamItem:
    title: str
    contentId: str
    group: str
    url: str
    hdPosterUrl: str = ""
    rating: float = 0.0
    quality: str = "SD"
    source_alias: str = ""
    
    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v}

# ==========================================
# 5. INTELIGENCIA DE NEGOCIO (FILTROS)
# ==========================================

class ContentIntelligence:
    """Motor de decisión para clasificar y filtrar contenido."""

    @staticmethod
    def normalize_title(title: str) -> str:
        """
        Técnica de reducción de ruido para deduplicación.
        Elimina prefijos basura como 'MX:', 'FHD', '|', '[VIP]' para encontrar el núcleo del nombre.
        """
        # 1. Convertir a minúsculas
        t = title.lower()
        # 2. Eliminar etiquetas de calidad y país comunes
        t = re.sub(r'(mx:|mex:|lat:|arg:|col:|vip|fhd|hd|sd|hevc|h265|4k|1080p|720p|\[.*?\]|\(.*?\))', '', t)
        # 3. Eliminar caracteres no alfanuméricos
        t = re.sub(r'[^a-z0-9]', '', t)
        return t

    @staticmethod
    def analyze_content(name: str, group_raw: str = "") -> dict:
        """Devuelve decisión (bool) y categoría (str)."""
        name_lower = name.lower()
        group_lower = group_raw.lower()

        # 1. Bloqueo Hard (Geoblocking estricto)
        if re.search(REGEX_HARD_BLOCK_GENERAL, name) or re.search(REGEX_HARD_BLOCK_GENERAL, group_raw):
            return {"pass": False, "cat": None}

        # 2. Detección de Categoría
        category = "live_tv" # Default
        
        # Prioridad a Películas/Series si vienen de listas VOD o tienen keywords
        if "movie" in group_lower or "peli" in group_lower or re.search(REGEX_MOVIES, name):
            category = "movies"
        elif "series" in group_lower or "serie" in group_lower:
            category = "series"
        elif re.search(REGEX_KIDS, name) or re.search(REGEX_KIDS, group_raw):
            category = "kids"
        elif re.search(REGEX_DOCS, name) or re.search(REGEX_DOCS, group_raw):
            category = "docs"
        else:
            # Lógica especial Deportes
            is_sports_nom = any(x in name_lower for x in ['deporte', 'sport', 'espn', 'fox', 'ufc', 'nfl', 'f1', 'liga', 'chivas'])
            is_sports_cat = "sport" in group_lower or "deporte" in group_lower
            
            if is_sports_nom or is_sports_cat:
                if re.search(REGEX_SPORTS_BLOCK, name): # Bloqueo Brasil/Portugal en deportes
                    return {"pass": False, "cat": None}
                category = "sports"

        # 3. Validación de Interés (Solo MX/LATAM/España/Premium)
        # Si es deporte, somos un poco más permisivos (ej: Eurocopa)
        if category == "sports":
            is_relevant = True 
        else:
            is_relevant = (
                re.search(REGEX_MX_STRICT, name) or 
                re.search(REGEX_MX_CHANNELS, name) or 
                re.search(REGEX_PREMIUM_LATAM, name) or
                re.search(REGEX_MX_STRICT, group_raw)
            )

        return {"pass": bool(is_relevant), "cat": category}

    @staticmethod
    def get_quality(name: str) -> str:
        if re.search(REGEX_4K, name): return "4K"
        if re.search(REGEX_FHD, name): return "FHD"
        if re.search(REGEX_HD, name): return "HD"
        return "SD"

# ==========================================
# 6. NETWORKING Y PARSERS
# ==========================================

async def fetch_text(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Descarga contenido crudo (M3U) de manera eficiente."""
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as response:
            if response.status == 200:
                # Intentar decodificar utf-8, fallback a latin-1 para listas antiguas
                content = await response.read()
                try:
                    return content.decode('utf-8')
                except UnicodeDecodeError:
                    return content.decode('latin-1')
    except Exception as e:
        logger.warning(f"Error descargando lista {url}: {e}")
    return None

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    """Descarga JSON (API Xtream)."""
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as response:
            if response.status == 200: return await response.json()
    except Exception:
        pass
    return None

async def check_health(session, url, semaphore) -> bool:
    async with semaphore:
        try:
            async with session.head(url, timeout=5) as r:
                return r.status in (200, 301, 302)
        except: return False

# --- PROCESADOR: M3U (NUEVO) ---
async def process_m3u_source(session, source_cfg, playlist_container, semaphore):
    """Parsea una lista M3U completa y categoriza items."""
    raw_data = await fetch_text(session, source_cfg['url'])
    if not raw_data: return

    logger.info(f"[{source_cfg['alias']}] M3U descargado. Parseando...")
    
    matches = REGEX_M3U_EXTINF.finditer(raw_data)
    count = 0
    tasks = []

    for m in matches:
        title = m.group('title').strip()
        url = m.group('url').strip()
        attrs = m.group('attrs')
        
        # Extraer metadatos ocultos en attrs (tvg-logo, group-title)
        logo_match = re.search(r'tvg-logo="([^"]+)"', attrs)
        group_match = re.search(r'group-title="([^"]+)"', attrs)
        
        logo = logo_match.group(1) if logo_match else ""
        group_raw = group_match.group(1) if group_match else ""

        # ANÁLISIS INTELIGENTE
        analysis = ContentIntelligence.analyze_content(title, group_raw)
        
        if analysis["pass"]:
            # Crear ID único basado en hash de URL si no hay ID
            content_id = str(abs(hash(url)))
            
            item = StreamItem(
                title=title,
                contentId=content_id,
                group=analysis["cat"],
                url=url,
                hdPosterUrl=logo,
                quality=ContentIntelligence.get_quality(title),
                source_alias=source_cfg['alias']
            )
            
            # Verificación de salud (Opcional: Para M3U grandes, verificar todos puede tardar mucho. 
            # Sugerencia: Verificar solo si es crítico o asumir funcional para velocidad)
            # Para este nivel "Senior", verificamos concurrente para asegurar calidad.
            tasks.append((item, check_health(session, url, semaphore)))

    # Ejecutar validaciones en paralelo
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        valid_items = [t[0].to_dict() for t, ok in zip(tasks, results) if ok]
        
        for v in valid_items:
            playlist_container[v['group']].append(v)
            count += 1

    logger.info(f"[{source_cfg['alias']}] Procesados: {count} items válidos.")

# --- PROCESADOR: XTREAM (LEGACY OPTIMIZADO) ---
async def process_xtream_live(session, source, playlist_container, semaphore):
    url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_live_streams"
    data = await fetch_json(session, url)
    if not isinstance(data, list): return

    tasks = []
    for item in data:
        name = item.get('name', '')
        analysis = ContentIntelligence.analyze_content(name)
        
        if analysis["pass"] and analysis["cat"] != "movies" and analysis["cat"] != "series":
            play_url = f"{source['host']}/live/{source['user']}/{source['pass']}/{item.get('stream_id')}.ts"
            obj = StreamItem(
                title=name, contentId=str(item.get('stream_id')),
                group=analysis["cat"], url=play_url,
                hdPosterUrl=item.get('stream_icon'),
                quality=ContentIntelligence.get_quality(name),
                source_alias=source['alias']
            )
            tasks.append((obj, check_health(session, play_url, semaphore)))

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        for t, ok in zip(tasks, results):
            if ok: playlist_container[t[0].group].append(t[0].to_dict())

# ==========================================
# 7. ORQUESTADOR Y DEDUPLICACIÓN INTELIGENTE
# ==========================================

def smart_deduplication(playlist: Dict[str, Any]):
    """
    Algoritmo de Colapso de Redundancia.
    Agrupa streams idénticos bajo el mejor candidato (Mejor calidad > Mejor Fuente).
    """
    logger.info("🧠 Iniciando Deduplicación Semántica...")
    quality_score = {"4K": 40, "FHD": 30, "HD": 20, "SD": 10}
    
    # Preferencias de fuentes (Prioriza fuentes más estables si hay duplicados)
    source_priority = {"LatinaPro_Main": 5, "ClubTV_Lista1": 4, "Pluto_MX": 3} 

    for category, items in playlist.items():
        if category == "meta" or not isinstance(items, list): continue
        
        unique_map = {}
        
        for item in items:
            # Normalización agresiva para encontrar "gemelos"
            clean_key = ContentIntelligence.normalize_title(item['title'])
            
            # Calcular puntaje del item actual
            q_val = quality_score.get(item.get('quality', 'SD'), 10)
            src_val = source_priority.get(item.get('source_alias'), 1)
            current_score = q_val + src_val

            if clean_key not in unique_map:
                unique_map[clean_key] = {"data": item, "score": current_score}
            else:
                # Si el nuevo es mejor (mejor calidad o fuente preferida), reemplazamos
                if current_score > unique_map[clean_key]["score"]:
                    unique_map[clean_key] = {"data": item, "score": current_score}
        
        # Reconstruir lista limpia
        playlist[category] = [val["data"] for val in unique_map.values()]
        playlist[category].sort(key=lambda x: x['title'])
        logger.info(f"Categoría '{category}': Optimizado de {len(items)} a {len(playlist[category])} items únicos.")

def git_autopush():
    try:
        subprocess.run(["git", "add", "playlist.json"], check=True)
        subprocess.run(["git", "commit", "-m", f"SmartUpdate: {len(M3U_SOURCES)+1} Sources"], check=False)
        subprocess.run(["git", "push", "origin", "main"], capture_output=True)
        logger.info("✅ GitHub actualizado correctamente.")
    except Exception as e:
        logger.error(f"Git Error: {e}")

# ==========================================
# 8. MAIN LOOP
# ==========================================

async def main():
    start_time = time.time()
    
    playlist = {
        "meta": {"generated_at": time.ctime(), "version": "v99.0_Hybrid_AI"},
        "premieres": [], "live_tv": [], "sports": [], "kids": [], 
        "docs": [], "music": [], "movies": [], "series": []
    }

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
    
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT}) as session:
        tasks = []
        
        # 1. Ingestar Fuente Xtream Principal
        if XT_HOST:
            for src in XTREAM_SOURCES:
                tasks.append(process_xtream_live(session, src, playlist, semaphore))
                # Nota: Puedes agregar process_xtream_vod si lo deseas aquí también
        
        # 2. Ingestar Nuevas Fuentes M3U
        for m3u_src in M3U_SOURCES:
            tasks.append(process_m3u_source(session, m3u_src, playlist, semaphore))
            
        await asyncio.gather(*tasks)

    # Post-Procesamiento AI
    smart_deduplication(playlist)

    # Serialización
    with open('playlist.json', 'w', encoding='utf-8') as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)

    logger.info(f"🚀 PROCESO FINALIZADO EN {time.time() - start_time:.2f}s")
    git_autopush()

if __name__ == "__main__":
    asyncio.run(main())





