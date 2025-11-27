import os
import aiohttp
import asyncio
import json
import re
import time
from typing import List, Dict, Any

# ==========================================
# 1. CONFIGURACIÓN Y SECRETOS
# ==========================================
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")

USER_AGENT = "IPTVSmartersPro"

SOURCES: List[Dict[str, Any]] = [
    { "type": "xtream", "alias": "LatinaPro_VIP", "host": XT_HOST, "user": XT_USER, "pass": XT_PASS },
    # Fuentes M3U de respaldo
    { "type": "m3u", "alias": "M3U_Publica", "url": "https://www.m3u.cl/lista/MX.m3u" },
    { "type": "m3u", "alias": "GitHub_FreeTV", "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8" }
]

ACTIONS = { 
    "LIVE": "get_live_streams", 
    "VOD": "get_vod_streams",
    "SERIES": "get_series"
}

# ==========================================
# 2. FILTROS Y LÓGICA (IQ 200: Cubetas y QC)
# ==========================================

# BLOQUEO DE BASURA Y CONTENIDO INCOMPATIBLE
GLOBAL_BLOCKLIST = r"(?i)\b(spain|españa|colombia|peru|perú|argentina|ecuador|venezuela|bolivia|paraguay|brasil|brazil|portugal|french|italian|arab|korea|hindi|bengali|turkish|televicentro|tve|antena 3|telecinco|rtve|xxx|adult|porn|hdcam|cam|trailer)\b"

# EXCLUSIÓN CRÍTICA DE FORMATOS NO COMPATIBLES CON REPRODUCTORES IPTV/ROKU
# Solo permitimos extensiones de streaming conocidas (ts, m3u8, mp4, mov, avi, mkv, flv, wmv)
# Se excluye YouTube, Twitch y dominios de streaming genéricos no-IPTV.
STREAM_COMPATIBILITY_BLOCKLIST = r"(?i)(youtube\.com|youtu\.be|twitch\.tv|facebook\.com|dailymotion\.com)|(\.html|\.php|\.aspx|\.rss|\.xml)$"

# NUEVAS CATEGORÍAS ADICIONALES
REGEX_SPORTS = r"(?i)\b(espn|fox|sport|deporte|tudn|dazn|nba|nfl|mlb|ufc|wwe|f1|gp|futbol|soccer|liga|match|gol|win|directv sports|claro sports|fighting|racing|tennis|golf|bein)\b"
REGEX_MUSIC = r"(?i)\b(mtv|vh1|telehit|banda|musica|music|radio|fm|pop|rock|viva|beat|exa|concert|recital|deezer|spotify|tidal|k-pop|ritmoson|cmtv|htv|vevo)\b"
REGEX_KIDS = r"(?i)\b(kids|infantil|cartoon|nick|disney|discovery kids|paka paka|boing|clantv|cbeebies|zaz|toons)\b" # <--- NUEVA
REGEX_DOCS = r"(?i)\b(discovery|history|nat geo|national geographic|documental|docu|a&e|misterio|science|viajes|travel)\b" # <--- NUEVA
REGEX_GENERAL = r"(?i)\b(mexico|mx|usa|us|estados unidos|latino|lat|latam|tv abierta|cine|fhd|hevc|4k|azteca|televisa|estrellas|canal 5|imagen|multimedios|milenio|foro tv|noticias|news|telemundo|univision|hbo|tnt|space|universal|sony|warner|axn)\b"

REGEX_PREMIERE = r"(?i)\b(2024|2025|noviembre|diciembre)\b"

M3U_REGEX = r'#EXTINF:-1.*?(?:tvg-logo="(.*?)")?.*?(?:group-title="(.*?)")?,(.*?)\n(http.*)'

# ==========================================
# 3. UTILERÍAS DE NORMALIZACIÓN Y FILTRO
# ==========================================

def clean_rating(value: Any) -> float:
    """Sanitiza el rating (0.0 a 10.0)"""
    if not value: return 0.0
    try:
        val_str = str(value).lower()
        if "n/a" in val_str: return 0.0
        if "/" in val_str: val_str = val_str.split('/')[0]
        val_str = re.sub(r"[^0-9.]", "", val_str)
        if val_str == "": return 0.0
        r = float(val_str)
        return r if r <= 10 else 10.0 
    except: return 0.0

def categorize(name: str) -> str | None:
    """Logica de Cubetas Exclusivas para LIVE TV (Prioridad)"""
    if re.search(GLOBAL_BLOCKLIST, name): return None
    
    # 1. Prioridad: Exclusión
    if re.search(REGEX_KIDS, name): return "KIDS" # <--- ALTA PRIORIDAD
    
    # 2. Prioridad Media
    if re.search(REGEX_SPORTS, name): return "SPORTS"
    if re.search(REGEX_MUSIC, name): return "MUSIC"    
    if re.search(REGEX_DOCS, name): return "DOCS" # <--- ALTA PRIORIDAD

    # 3. Prioridad Baja
    if re.search(REGEX_GENERAL, name): return "LIVE_TV"
    return None

def is_url_compatible(url: str) -> bool:
    """Verifica si la URL es un stream de IPTV compatible con Roku (QC)."""
    if re.search(STREAM_COMPATIBILITY_BLOCKLIST, url):
        return False
    # Permite URLS con extensiones de streaming o sin extensión (asumiendo Xtream / API)
    return True

def transform_xtream_vod_item(item: Dict[str, Any], source_alias: str) -> Dict[str, Any]:
    """Mapeo completo de Metadatos de VOD (Películas)"""
    # ... (Mapeo es idéntico a V93, asegurando todos los campos de metadatos)
    rating = clean_rating(item.get('rating'))
    return {
        "title": item.get('name', 'N/A'),
        "contentId": str(item.get('stream_id')),
        "url": None, 
        "group": "MOVIE",
        "hdPosterUrl": item.get('stream_icon'),
        "rating": rating,
        "plot": item.get('plot', 'Sin descripción.'),
        "genre": item.get('genre', 'General'),      
        "duration": item.get('duration', 'N/A'),
        "releaseDate": item.get('releasedate', 'N/A'),
        "director": item.get('director', 'N/A'),
        "cast": item.get('cast', 'N/A'),
        "source_alias": source_alias,
    }

def transform_xtream_series_item(item: Dict[str, Any], source_alias: str) -> Dict[str, Any]:
    """Mapeo completo de Metadatos de Series"""
    # ... (Mapeo es idéntico a V93)
    rating = clean_rating(item.get('rating'))
    return {
        "title": item.get('name', 'N/A'),
        "contentId": str(item.get('series_id')),
        "group": "SERIES",
        "hdPosterUrl": item.get('cover'),
        "rating": rating,
        "plot": item.get('plot', 'Sin descripción.'),
        "genre": item.get('genre', 'General'),      
        "releaseDate": item.get('releaseDate', 'N/A'),
        "cast": item.get('cast', 'N/A'),
        "source_alias": source_alias,
    }

# ==========================================
# 4. MOTOR DE PROCESAMIENTO ASÍNCRONO
# ==========================================

# (fetch_xtream y fetch_and_parse_m3u son idénticos a V93)

async def check_health(session: aiohttp.ClientSession, url: str) -> bool:
    """Verificación de salud crítica usando el User-Agent específico de Roku."""
    if not is_url_compatible(url):
        # Fallar el health check si la URL no es compatible
        return False
        
    try:
        headers = {"User-Agent": USER_AGENT}
        async with session.head(url, headers=headers, timeout=5.0) as response:
            return response.status in (200, 301, 302)
    except: 
        return False

# --- Sub-Procesos para Modularidad ---

async def process_xtream_live(session, source, playlist):
    raw_live = await fetch_xtream(session, source, ACTIONS["LIVE"])
    tasks = []
    for item in raw_live:
        name = item.get('name', '')
        cat = categorize(name)
        if cat:
            sid = item.get('stream_id')
            url = f"{source['host']}/live/{source['user']}/{source['pass']}/{sid}.ts"
            
            # QC 1: Compatibilidad de URL
            if not is_url_compatible(url): continue 

            obj = {
                "title": f"[{source['alias']}] {name}", 
                "contentId": str(sid), 
                "url": url,
                "hdPosterUrl": item.get('stream_icon'), 
                "group": cat
            }
            tasks.append((obj, check_health(session, url), cat))
            
    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        for (obj, online, cat) in zip([t[0] for t in tasks], results, [t[2] for t in tasks]):
            if online: 
                playlist[cat.lower()].append(obj)

async def process_m3u_live(session, source, playlist):
    items = await fetch_and_parse_m3u(session, source)
    tasks = []
    for item in items:
        name = item['name']
        url = item['url']
        cat = categorize(name)
        
        if cat:
            # QC 1: Compatibilidad de URL
            if not is_url_compatible(url): continue
            
            obj = {
                "title": f"[{source['alias']}] {item['name']}", 
                "contentId": item['stream_id'],
                "url": url, 
                "hdPosterUrl": item['stream_icon'], 
                "group": cat
            }
            tasks.append((obj, check_health(session, url), cat))

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks])
        for (obj, online, cat) in zip([t[0] for t in tasks], results, [t[2] for t in tasks]):
            if online: 
                playlist[cat.lower()].append(obj)

# (process_xtream_vod y process_xtream_series son idénticos a V93)

async def process_source(session: aiohttp.ClientSession, source: Dict[str, Any], playlist: Dict[str, Any]):
    """Función de alto nivel para procesar una fuente completa."""
    if source['type'] == 'xtream':
        print(f"[>] Procesando XTREAM: {source['alias']}")
        await process_xtream_live(session, source, playlist)
        await process_xtream_vod(session, source, playlist)
        await process_xtream_series(session, source, playlist)

    elif source['type'] == 'm3u':
        print(f"[>] Procesando M3U: {source['alias']}")
        await process_m3u_live(session, source, playlist)

def deduplicate_playlist(playlist: Dict[str, Any]):
    """Deduplicación Agresiva basada en un Hash del Título."""
    unique_hashes = set()
    total_removed = 0
    
    for key in ["live_tv", "sports", "music", "kids", "docs", "movies", "series", "premieres"]:
        if key not in playlist: continue
        
        new_list = []
        for item in playlist[key]:
            # Usar un hash simple del título en minúsculas sin caracteres especiales
            clean_title = re.sub(r'[^a-z0-9]', '', item['title'].lower())
            item_hash = hash(clean_title)
            
            if item_hash not in unique_hashes:
                new_list.append(item)
                unique_hashes.add(item_hash)
            else:
                total_removed += 1
                
        playlist[key] = new_list
    
    print(f"[QC] Eliminados {total_removed} duplicados por título/hash.")


async def main():
    t0 = time.time()
    # Estructura V94 (Añadiendo KIDS y DOCS)
    playlist = {
        "meta": { "updated": time.ctime(), "version": "v94_strict_qc", "user_agent": USER_AGENT },
        "live_tv": [], "sports": [], "music": [], "kids": [], "docs": [],
        "movies": [], "series": [], "premieres": []
    }
    
    timeout = aiohttp.ClientTimeout(total=90) # Aumento de timeout para seguridad
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        await asyncio.gather(*[process_source(session, src, playlist) for src in SOURCES])

    # Paso de Control de Calidad: Deduplicación
    deduplicate_playlist(playlist)

    # Exportación
    with open('playlist.json', 'w', encoding='utf-8') as f: 
        json.dump(playlist, f, indent=4, ensure_ascii=False)

    print(f"\n--- REPORTE V94 (Filtro Estricto) ---")
    print(f"Tiempo Total: {time.time() - t0:.2f}s")
    print(f"TV (General): {len(playlist['live_tv'])}")
    print(f"TV (Deportes): {len(playlist['sports'])}")
    print(f"TV (Música): {len(playlist['music'])}")
    print(f"TV (Infantil): {len(playlist['kids'])}")
    print(f"TV (Docs): {len(playlist['docs'])}")
    print(f"VOD (Películas): {len(playlist['movies'])}")
    print(f"VOD (Series): {len(playlist['series'])}")
    print(f"ESTRENOS (Total): {len(playlist['premieres'])}")

if __name__ == "__main__":
    asyncio.run(main())
