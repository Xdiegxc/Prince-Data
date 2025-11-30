# =====================================================================================
# Cerebro V99_MX_PRO - Refactored IPTV Playlist Generator
#
# IMPROVEMENTS:
# - Pydantic Models: For robust, self-validating data structures.
# - Centralized Settings: Easy configuration via a Settings class (reads from env vars).
# - Modular & OOP Design: Logic is split into classes for clarity and extensibility.
# - Type Hinting & Readability: Fully type-hinted for better developer experience.
# - Enhanced Deduplication: Clearer logic for identifying and removing duplicates.
#
# REQUIREMENTS:
# pip install pydantic pydantic-settings aiohttp
# =====================================================================================

import os
import aiohttp
import asyncio
import json
import re
import time
import logging
from typing import List, Dict, Any, Optional, Literal, Set
from pydantic import BaseModel, Field, HttpUrl, validator
from pydantic_settings import BaseSettings

# ==========================================
# 1. CONFIGURATION & SETTINGS
# Manages all configuration via environment variables with sensible defaults.
# ==========================================

class Settings(BaseSettings):
    """ Loads settings from environment variables. """
    XT_HOST: Optional[str] = None
    XT_USER: Optional[str] = None
    XT_PASS: Optional[str] = None
    
    USER_AGENT: str = "IPTVSmartersPro/3.1.3"
    MAX_CONCURRENT_CHECKS: int = 60
    HTTP_TIMEOUT: int = 45
    MAX_RETRIES: int = 2
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'

# Initialize settings and logger
settings = Settings()
logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CerebroV99_MX_PRO")


# ==========================================
# 2. DATA MODELS
# Defines the shape of our data, providing validation and structure.
# ==========================================

class BaseItem(BaseModel):
    title: str
    contentId: str
    group: str
    hdPosterUrl: HttpUrl
    quality: str
    source_alias: str

class LiveItem(BaseItem):
    url: HttpUrl

class VodItem(BaseItem):
    url: HttpUrl
    plot: str
    genre: str
    releaseDate: str
    cast: str
    rating: float

class SeriesItem(VodItem):
    url: HttpUrl  # This becomes the episodes API URL
    backdrop_path: List[HttpUrl] = Field(default_factory=list)
    youtube_trailer: Optional[str] = ""


# ==========================================
# 3. CONSTANTS & REGEX DEFINITIONS
# Centralized, non-changing values for easy management.
# ==========================================

class Constants:
    ACTIONS = {"LIVE": "get_live_streams", "VOD": "get_vod_streams", "SERIES": "get_series"}

    # Geoblocking and compatibility filters
    GLOBAL_BLOCKLIST = r"(?i)\b(spain|españa|tve|antena 3|telecinco|rtve|portugal|french|italian|arab|korea|hindi|turkish|xxx|adult|porn|hdcam|cam|vose|subt|subtitulada)|\b(peru|perú|chile|argentina|colombia|venezuela|ecuador|uruguay|paraguay|bolivia|costa rica|guatemala|honduras|salvador|panama|dominicana|brasil|brazil|br|pe|cl|ar|co|uy|py|bo|cr|gt|hn|sv|pa|do)"
    STREAM_COMPATIBILITY_BLOCKLIST = r"(?i)(youtube\.com|youtu\.be|twitch\.tv|facebook\.com|dailymotion\.com)|(\.html|\.php|\.aspx|\.rss|\.xml)$"

    # Categorization Regex
    REGEX_SPORTS = r"(?i)\b(espn|fox|sport|deporte|tudn|dazn|nba|nfl|mlb|ufc|wwe|f1|gp|futbol|soccer|liga|match|gol|win|afizzionados|claro sports|fighting|racing|tennis|golf|bein)\b"
    REGEX_MUSIC = r"(?i)\b(mtv|vh1|telehit|banda|musica|music|radio|fm|pop|rock|viva|beat|exa|concert|recital|deezer|spotify|tidal|k-pop|ritmoson|cmtv|htv|vevo)\b"
    REGEX_KIDS = r"(?i)\b(kids|infantil|cartoon|nick|disney|discovery kids|paka paka|boing|clantv|cbeebies|zaz|toons|baby|junior)\b"
    REGEX_DOCS = r"(?i)\b(discovery|history|nat geo|national geographic|documental|docu|a&e|misterio|science|viajes|travel|animal planet|h&h)\b"
    REGEX_GENERAL = r"(?i)\b(mexico|mx|cdmx|azteca|televisa|estrellas|canal 5|imagen|multimedios|milenio|foro tv|noticias|news|telemundo|univision|hbo|tnt|space|universal|sony|warner|axn|cine|cinema|golden|edge|distrito comedia)\b"
    
    # Feature Regex
    REGEX_PREMIERE = r"(?i)(2024|2025)"
    REGEX_4K = r"(?i)\b(4k|uhd|2160p)\b"
    REGEX_FHD = r"(?i)\b(fhd|1080p|hevc)\b"
    REGEX_HD = r"(?i)\b(hd|720p)\b"

    # M3U Parser Regex
    M3U_REGEX = r'#EXTINF:-1.*?(?:tvg-logo="(.*?)")?.*?(?:group-title="(.*?)")?,(.*?)\n(http.*)'
    
    FALLBACK_IMAGE_URL = "https://via.placeholder.com/300x450?text=No+Image"


# ==========================================
# 4. CONTENT TRANSFORMATION & LOGIC
# Handles data cleaning, categorization, and transformation into our models.
# ==========================================

class ContentTransformer:
    @staticmethod
    def clean_rating(value: Any) -> float:
        if not value: return 0.0
        try:
            val_str = str(value).lower().split('/')[0]
            if "n/a" in val_str: return 0.0
            val_str = re.sub(r"[^0-9.]", "", val_str)
            if not val_str: return 0.0
            rating = float(val_str)
            return min(rating, 10.0)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def detect_quality(name: str) -> str:
        if re.search(Constants.REGEX_4K, name): return "4K"
        if re.search(Constants.REGEX_FHD, name): return "FHD"
        if re.search(Constants.REGEX_HD, name): return "HD"
        return "SD"

    @staticmethod
    def categorize(name: str) -> Optional[str]:
        if re.search(Constants.GLOBAL_BLOCKLIST, name):
            return None
        if re.search(Constants.REGEX_KIDS, name): return "KIDS"
        if re.search(Constants.REGEX_SPORTS, name): return "SPORTS"
        if re.search(Constants.REGEX_MUSIC, name): return "MUSIC"
        if re.search(Constants.REGEX_DOCS, name): return "DOCS"
        if re.search(Constants.REGEX_GENERAL, name) or "latino" in name.lower():
            return "LIVE_TV"
        return None  # Default to ignore if no category matches

    @staticmethod
    def is_premiere(name: str) -> bool:
        return bool(re.search(Constants.REGEX_PREMIERE, name))

    def transform_xtream_live(self, item: Dict[str, Any], source: Dict, category: str) -> Optional[LiveItem]:
        name = item.get('name')
        stream_id = item.get('stream_id')
        if not name or not stream_id:
            return None

        try:
            return LiveItem(
                title=name,
                contentId=str(stream_id),
                url=f"{source['host']}/live/{source['user']}/{source['pass']}/{stream_id}.ts",
                hdPosterUrl=item.get('stream_icon') or Constants.FALLBACK_IMAGE_URL,
                group=category,
                quality=self.detect_quality(name),
                source_alias=source['alias']
            )
        except Exception as e:
            logger.debug(f"Skipping Live item due to validation error: {e} | Item: {item}")
            return None

    def transform_xtream_vod(self, item: Dict[str, Any], source: Dict[str, Any]) -> Optional[VodItem]:
        name = item.get('name')
        stream_id = item.get('stream_id')
        
        # Essential data check: name and ID are required to build a valid item.
        if not name or not stream_id:
            return None

        image = item.get('stream_icon') or item.get('cover')
        ext = item.get('container_extension', 'mp4')
        try:
            return VodItem(
                title=name,
                contentId=str(stream_id),
                url=f"{source['host']}/movie/{source['user']}/{source['pass']}/{stream_id}.{ext}",
                hdPosterUrl=image or Constants.FALLBACK_IMAGE_URL, # Use fallback if image is missing
                group="MOVIE",
                quality=self.detect_quality(name),
                rating=self.clean_rating(item.get('rating')),
                plot=item.get('plot', 'Sin descripción disponible.'),
                genre=item.get('genre', 'General'),
                releaseDate=item.get('releasedate') or item.get('releaseDate', 'N/A'),
                cast=item.get('cast', 'N/A'),
                source_alias=source['alias']
            )
        except Exception as e:
            logger.debug(f"Skipping VOD item due to validation error: {e} | Item: {item}")
            return None

    def transform_xtream_series(self, item: Dict[str, Any], source: Dict[str, Any]) -> Optional[SeriesItem]:
        name = item.get('name')
        series_id = item.get('series_id')

        # Essential data check: name and ID are required.
        if not name or not series_id:
            return None

        image = item.get('cover') or item.get('stream_icon')
        api_url = f"{source['host']}/player_api.php?username={source['user']}&password={source['pass']}&action=get_series_info&series_id={series_id}"
        
        try:
            return SeriesItem(
                title=name,
                contentId=str(series_id),
                url=api_url,
                hdPosterUrl=image or Constants.FALLBACK_IMAGE_URL, # Use fallback
                group="SERIES",
                quality=self.detect_quality(name), # Quality for series is often indicative
                rating=self.clean_rating(item.get('rating')),
                plot=item.get('plot', 'Sin descripción disponible.'),
                genre=item.get('genre', 'General'),
                releaseDate=item.get('releaseDate') or item.get('releasedate', 'N/A'),
                cast=item.get('cast', 'N/A'),
                source_alias=source['alias'],
                backdrop_path=item.get('backdrop_path', []),
                youtube_trailer=item.get('youtube_trailer', '')
            )
        except Exception as e:
            logger.debug(f"Skipping Series item due to validation error: {e} | Item: {item}")
            return None


# ==========================================
# 5. ASYNCHRONOUS NETWORKING UTILITIES
# ==========================================

async def fetch_with_retry(session: aiohttp.ClientSession, url: str) -> Optional[Any]:
    for attempt in range(settings.MAX_RETRIES):
        try:
            async with session.get(url, timeout=settings.HTTP_TIMEOUT) as response:
                response.raise_for_status()
                ctype = response.headers.get('Content-Type', '').lower()
                if 'json' in ctype:
                    return await response.json()
                return await response.text()
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on attempt {attempt+1} for {url}")
        except aiohttp.ClientError as e:
            logger.warning(f"Client error on attempt {attempt+1} for {url}: {e}")
        
        if attempt < settings.MAX_RETRIES - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
            
    logger.error(f"Final failure to fetch URL: {url}")
    return None

async def check_stream_health(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> bool:
    if re.search(Constants.STREAM_COMPATIBILITY_BLOCKLIST, url):
        return False
    
    async with semaphore:
        try:
            async with session.head(url, timeout=10, allow_redirects=True) as response:
                return response.status < 400  # OK if not an error status
        except Exception:
            return False

# ==========================================
# 6. SOURCE PROCESSORS
# Logic for handling different types of playlist sources (Xtream, M3U, etc.)
# ==========================================
PlaylistData = Dict[str, List[BaseModel]]

class BaseSourceProcessor:
    def __init__(self, source_config: Dict, transformer: ContentTransformer):
        self.source = source_config
        self.transformer = transformer
        self.alias = source_config.get("alias", "Unknown")

    async def process(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> PlaylistData:
        raise NotImplementedError


class XtreamProcessor(BaseSourceProcessor):
    async def process(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> PlaylistData:
        logger.info(f"[{self.alias}] Starting processing...")
        playlist: PlaylistData = { "live_tv": [], "sports": [], "music": [], "kids": [], "docs": [], "movies": [], "series": [], "premieres": [] }
        
        # Concurrently fetch all categories
        live_task = self._fetch_category(session, 'LIVE')
        vod_task = self._fetch_category(session, 'VOD')
        series_task = self._fetch_category(session, 'SERIES')
        
        raw_live, raw_vod, raw_series = await asyncio.gather(live_task, vod_task, series_task)

        if raw_live: self._process_live(raw_live, playlist, session, semaphore)
        if raw_vod: self._process_vod(raw_vod, playlist)
        if raw_series: self._process_series(raw_series, playlist)

        return playlist

    async def _fetch_category(self, session: aiohttp.ClientSession, action_key: str) -> Optional[List[Dict]]:
        action = Constants.ACTIONS.get(action_key)
        if not action: return None
        
        url = f"{self.source['host']}/player_api.php?username={self.source['user']}&password={self.source['pass']}&action={action}"
        data = await fetch_with_retry(session, url)
        
        if isinstance(data, list):
            logger.info(f"[{self.alias}] Fetched {len(data)} items for {action_key}")
            return data
        
        logger.warning(f"[{self.alias}] Failed to fetch or got invalid data for {action_key}")
        return None

    def _process_live(self, data: List[Dict], playlist: PlaylistData, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
        # This part remains sequential in logic but fetches health checks concurrently.
        # A fully concurrent version would be more complex. Let's keep it simple and robust.
        # The performance bottleneck is health checks, which are already concurrent.
        logger.info(f"[{self.alias}] Processing {len(data)} Live TV items...")
        
        valid_items = []
        for item in data:
            name = item.get('name', '')
            category = self.transformer.categorize(name)
            if category:
                live_item = self.transformer.transform_xtream_live(item, self.source, category)
                if live_item:
                    valid_items.append(live_item)
        
        # This part could be a performance bottleneck if we want to run it inside the main async loop.
        # For now, it's a synchronous loop that gathers async tasks.
        async def run_health_checks():
            tasks = [check_stream_health(session, item.url, semaphore) for item in valid_items]
            results = await asyncio.gather(*tasks)
            
            added_count = 0
            for item, is_online in zip(valid_items, results):
                if is_online:
                    playlist_key = item.group.lower()
                    if playlist_key in playlist:
                        playlist[playlist_key].append(item)
                        added_count +=1
            logger.info(f"[{self.alias}] LIVE: Added {added_count} online channels.")

        # Run health checks in a separate async context
        asyncio.create_task(run_health_checks())


    def _process_vod(self, data: List[Dict], playlist: PlaylistData):
        logger.info(f"[{self.alias}] Processing {len(data)} VOD items...")
        premieres_count = 0
        added_count = 0
        for item in data:
            if re.search(Constants.GLOBAL_BLOCKLIST, item.get('name', '')):
                continue
            
            vod_item = self.transformer.transform_xtream_vod(item, self.source)
            if vod_item:
                playlist["movies"].append(vod_item)
                added_count += 1
                if self.transformer.is_premiere(vod_item.title):
                    playlist["premieres"].append(vod_item)
                    premieres_count += 1
        logger.info(f"[{self.alias}] VOD: Added {added_count} movies. Found {premieres_count} premieres.")

    def _process_series(self, data: List[Dict], playlist: PlaylistData):
        logger.info(f"[{self.alias}] Processing {len(data)} Series items...")
        premieres_count = 0
        added_count = 0
        for item in data:
            if re.search(Constants.GLOBAL_BLOCKLIST, item.get('name', '')):
                continue

            series_item = self.transformer.transform_xtream_series(item, self.source)
            if series_item:
                playlist["series"].append(series_item)
                added_count += 1
                if self.transformer.is_premiere(series_item.title):
                    # For series, we append the whole series object to premieres
                    playlist["premieres"].append(series_item)
                    premieres_count += 1
        logger.info(f"[{self.alias}] SERIES: Added {added_count} series. Found {premieres_count} premieres.")


# ==========================================
# 7. MAIN ORCHESTRATION
# ==========================================

async def main():
    start_time = time.time()
    logger.info("===== Starting CerebroV99 MX PRO =====")

    if not all([settings.XT_HOST, settings.XT_USER, settings.XT_PASS]):
        logger.error("XT_HOST, XT_USER, and XT_PASS environment variables must be set.")
        return

    SOURCES: List[Dict[str, Any]] = [
        {"type": "xtream", "alias": "LatinaPro_VIP", "host": settings.XT_HOST, "user": settings.XT_USER, "pass": settings.XT_PASS},
    ]

    final_playlist: PlaylistData = {
        "live_tv": [], "sports": [], "music": [], "kids": [], "docs": [],
        "movies": [], "series": [], "premieres": []
    }

    transformer = ContentTransformer()
    semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_CHECKS)
    
    # Setup connection pool
    conn = aiohttp.TCPConnector(limit=100, ssl=False) # ssl=False can help with some misconfigured servers
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": settings.USER_AGENT}) as session:
        
        processors = []
        for src in SOURCES:
            if src['type'] == 'xtream':
                processors.append(XtreamProcessor(src, transformer))
            # M3UProcessor could be added here if needed
        
        # Run all source processors concurrently
        results = await asyncio.gather(*(p.process(session, semaphore) for p in processors))

        # Merge results from all processors
        for res_playlist in results:
            for key, items in res_playlist.items():
                final_playlist[key].extend(items)
    
    # Allow some time for health check tasks to complete
    await asyncio.sleep(15)

    # --- Final Deduplication and Cleaning ---
    logger.info("Starting final deduplication...")
    unique_hashes: Set[int] = set()
    total_deduped = 0
    for key, items in final_playlist.items():
        if isinstance(items, list):
            original_count = len(items)
            unique_items = []
            for item in items:
                # Create a robust hash from title and quality
                clean_title = re.sub(r'[^a-z0-9]', '', item.title.lower())
                item_hash = hash(f"{clean_title}-{item.quality}")
                
                if item_hash not in unique_hashes:
                    unique_items.append(item)
                    unique_hashes.add(item_hash)
            
            final_playlist[key] = unique_items
            deduped_count = original_count - len(unique_items)
            if deduped_count > 0:
                total_deduped += deduped_count
                logger.info(f"Removed {deduped_count} duplicates from '{key}' category.")
    
    # --- Prepare for JSON Output ---
    output_dict = {
        "meta": {
            "updated": time.ctime(),
            "version": "v99_mx_pro",
            "user_agent": settings.USER_AGENT,
            "sources": [s['alias'] for s in SOURCES]
        }
    }
    for key, items in final_playlist.items():
        output_dict[key] = [item.dict() for item in items]
        logger.info(f"Category '{key}': {len(items)} items.")

    # --- Export to JSON File ---
    output_filename = 'playlist_mx_v99.json'
    with open(output_filename, 'w', encoding='utf-8') as f:
        json.dump(output_dict, f, indent=2, ensure_ascii=False)

    end_time = time.time()
    logger.info(f"Removed a total of {total_deduped} duplicate items.")
    logger.info(f"✅ Process completed in {end_time - start_time:.2f} seconds.")
    logger.info(f"Playlist saved to {output_filename}")


if __name__ == "__main__":
    # For Windows, we might need to set a different event loop policy
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
