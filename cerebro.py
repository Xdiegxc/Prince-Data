import os
import aiohttp
import asyncio
import json
import re
import time

# ==========================================
# 1. CONFIGURACIÓN Y SECRETOS (NUBE)
# ==========================================
XT_HOST = os.getenv("XT_HOST")
XT_USER = os.getenv("XT_USER")
XT_PASS = os.getenv("XT_PASS")

SOURCES = [
    # FUENTE 1: Tu Servidor Xtream Privado (Prioridad)
    {
        "type": "xtream",
        "alias": "LatinaPro_VIP",
        "host": XT_HOST,
        "user": XT_USER,
        "pass": XT_PASS
    },
    # FUENTE 2: Lista M3U Pública (Respaldo)
    {
        "type": "m3u",
        "alias": "M3U_Publica",
        "url": "https://www.m3u.cl/lista/MX.m3u"
    },
    # FUENTE 3: GitHub Free-TV
    {
        "type": "m3u",
        "alias": "GitHub_FreeTV",
        "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
    }
]

# ==========================================
# 2. FILTROS DE INTELIGENCIA (REGEX)
# ==========================================

# BLACKLIST: Si tiene esto, se elimina automáticamente.
GLOBAL_BLOCKLIST = r"(?i)\b(spain|espana|españa|colombia|peru|perú|argentina|chile|ecuador|venezuela|bolivia|uruguay|paraguay|brasil|brazil|portugal|french|italian|arab|korea|hindi|bengali|turkish|televicentro|tve|antena 3|telecinco|rtve)\b"

# CATEGORIZADORES
REGEX_SPORTS = r"(?i)\b(espn|fox|sport|deporte|tudn|dazn|nba|nfl|mlb|ufc|wwe|f1|gp|futbol|soccer|liga|match|gol|win|directv sports|claro sports|fighting|racing|tennis|golf|bein)\b"
REGEX_MUSIC = r"(?i)\b(mtv|vh1|telehit|banda|musica|music|radio|fm|pop|rock|viva|beat|exa|concert|recital|deezer|spotify|tidal|k-pop|ritmoson|cmtv|htv|vevo)\b"
REGEX_GENERAL = r"(?i)\b(mexico|mx|usa|us|estados unidos|latino|lat|latam|tv abierta|cine|fhd|hevc|4k|azteca|televisa|estrellas|canal 5|imagen|multimedios|milenio|foro tv|noticias|news|telemundo|univision|hbo|tnt|space|universal|sony|warner|discovery|history|a&e|axn)\b"

# Regex para parsear archivos M3U
M3U_REGEX = r'#EXTINF:.*?(?:tvg-logo="(.*?)")?.*?(?:group-title="(.*?)")?,(.*?)\n(http.*)'

ACTIONS = {
    "LIVE": "get_live_streams",
    "MOVIES": "get_vod_streams",
    "SERIES": "get_series"
}

# ==========================================
# 3. FUNCIONES DE CONEXIÓN
# ==========================================

async def fetch_xtream(session, server, action):
    if not server['host']: return [] 
    url = f"{server['host']}/player_api.php?username={server['user']}&password={server['pass']}&action={action}"
    print(f"[>] {server['alias']} (Xtream): Solicitando {action}...")
    try:
        async with session.get(url, timeout=45) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        print(f"[!] {server['alias']} Error: {e}")
    return []

async def fetch_and_parse_m3u(session, source):
    print(f"[>] {source['alias']} (M3U): Descargando lista...")
    try:
        async with session.get(source['url'], timeout=30) as response:
            if response.status == 200:
                content = await response.text()
                matches = re.findall(M3U_REGEX, content, re.MULTILINE)
                print(f"    [{source['alias']}] Encontrados {len(matches)} items crudos.")
                
                parsed_items = []
                for logo, group, name, url in matches:
                    clean_item = {
                        "name": name.strip(),
                        "stream_icon": logo if logo else "",
                        "url": url.strip(),
                        "category_name": group if group else "General",
                        "stream_id": "m3u_" + str(hash(url.strip()))
                    }
                    parsed_items.append(clean_item)
                return parsed_items
    except Exception as e:
        print(f"[!] {source['alias']} Error leyendo M3U: {e}")
    return []

async def check_health(session, url):
    """Ping HEAD para verificar si el canal funciona."""
    try:
        async with session.head(url, timeout=2.0) as response:
            return response.status == 200
    except:
        return False

def categorize(name):
    """Clasificación inteligente."""
    if re.search(GLOBAL_BLOCKLIST, name): return None
    if re.search(REGEX_SPORTS, name): return "SPORTS"
    if re.search(REGEX_MUSIC, name): return "MUSIC"
    if re.search(REGEX_GENERAL, name): return "LIVE_TV"
    return None

# ==========================================
# 4. PROCESAMIENTO PRINCIPAL
# ==========================================

# ... (INICIO IGUAL QUE ANTES) ...

async def process_source(session, source, playlist):
    # --- LOGICA XTREAM ---
    if source['type'] == 'xtream':
        # 1. LIVE TV (VOLVEMOS A HLS STANDARD)
        raw_live = await fetch_xtream(session, source, ACTIONS["LIVE"])
        health_tasks = []
        
        for item in raw_live:
            name = item.get('name', '')
            cat = categorize(name)
            if cat:
                stream_id = item.get('stream_id')
                
                # CORRECCIÓN: Usamos .m3u8. Es más compatible con Roku SI el User-Agent es correcto.
                final_url = f"{source['host']}/live/{source['user']}/{source['pass']}/{stream_id}.m3u8"
                
                clean_obj = {
                    "title": f"[{source['alias']}] {name}",
                    "id": str(stream_id),
                    "url": final_url,
                    "hdPosterUrl": item.get('stream_icon'),
                    "group": cat
                }
                health_tasks.append((clean_obj, check_health(session, final_url), cat))
        
        # Health Check
        if health_tasks:
            results = await asyncio.gather(*[t[1] for t in health_tasks])
            for (obj, is_online, cat) in zip([t[0] for t in health_tasks], results, [t[2] for t in health_tasks]):
                if is_online:
                    playlist[cat.lower()].append(obj)

        # 2. PELICULAS (VOD)
        raw_vod = await fetch_xtream(session, source, ACTIONS["MOVIES"])
        for item in raw_vod:
            name = item.get('name', '')
            if not re.search(GLOBAL_BLOCKLIST, name):
                stream_id = item.get('stream_id')
                ext = item.get('container_extension', 'mp4')
                final_url = f"{source['host']}/movie/{source['user']}/{source['pass']}/{stream_id}.{ext}"
                
                playlist["movies"].append({
                    "title": name,
                    "id": str(stream_id),
                    "url": final_url,
                    "hdPosterUrl": item.get('stream_icon'),
                    "group": "MOVIE"
                })

        # 3. SERIES
        raw_series = await fetch_xtream(session, source, ACTIONS["SERIES"])
        for item in raw_series:
            name = item.get('name', '')
            if not re.search(GLOBAL_BLOCKLIST, name):
                playlist["series"].append({
                    "title": name,
                    "id": str(item.get('series_id')),
                    "hdPosterUrl": item.get('cover'),
                    "group": "SERIES"
                })

    # --- LOGICA M3U ---
    elif source['type'] == 'm3u':
        items = await fetch_and_parse_m3u(session, source)
        health_tasks = []
        
        for item in items:
            cat = categorize(item['name'])
            if cat:
                clean_obj = {
                    "title": f"[{source['alias']}] {item['name']}",
                    "id": item['stream_id'],
                    "url": item['url'],
                    "hdPosterUrl": item['stream_icon'],
                    "group": cat
                }
                health_tasks.append((clean_obj, check_health(session, item['url']), cat))
        
        if health_tasks:
            results = await asyncio.gather(*[t[1] for t in health_tasks])
            for (obj, is_online, cat) in zip([t[0] for t in health_tasks], results, [t[2] for t in health_tasks]):
                if is_online:
                    playlist[cat.lower()].append(obj)

async def main():
    start_time = time.time()
    
    # ESTRUCTURA MAESTRA DEL JSON
    master_playlist = {
        "meta": {
            "updated": time.ctime(),
            "version": "v86_iron_cloud"
        },
        "live_tv": [],
        "sports": [],
        "music": [],
        "movies": [],
        "series": []
    }
    
    async with aiohttp.ClientSession() as session:
        # Ejecutar todas las fuentes en paralelo
        tasks = [process_source(session, src, master_playlist) for src in SOURCES]
        await asyncio.gather(*tasks)

    # GUARDAR JSON FINAL
    filename = 'playlist.json'
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(master_playlist, f, indent=4)

    print(f"\n--- PROCESO TERMINADO ---")
    print(f"Tiempo: {time.time() - start_time:.2f}s")
    print(f"Deportes: {len(master_playlist['sports'])}")
    print(f"TV Viva:  {len(master_playlist['live_tv'])}")
    print(f"Musica:   {len(master_playlist['music'])}")
    print(f"Pelis:    {len(master_playlist['movies'])}")
    print(f"Archivo generado: {filename}")

if __name__ == "__main__":
    asyncio.run(main())

