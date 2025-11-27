# ==========================================
# PARCHE PARA SERIES (Agrega esto antes de process_xtream)
# ==========================================

def transform_xtream_series_legacy(item: Dict[str, Any], source_alias: str) -> Dict[str, Any]:
    """
    Transformación especializada para Series que mantiene compatibilidad 
    con la lógica de llamadas 'get_series_info' de Roku.
    """
    rating = clean_rating(item.get('rating'))
    
    # Xtream a veces usa 'series_id' y a veces 'stream_id' en la lista de series.
    # Capturamos ambos por seguridad.
    raw_id = str(item.get('series_id') or item.get('stream_id'))
    
    return {
        # --- CAMPOS ESTANDARIZADOS (Para tu UI moderna) ---
        "title": item.get('name', 'N/A'),
        "contentId": raw_id,
        "group": "SERIES",
        "hdPosterUrl": item.get('cover') or item.get('stream_icon'),
        "rating": rating,
        "plot": item.get('plot', 'Sin descripción.'),
        "genre": item.get('genre', 'General'),      
        "releaseDate": item.get('releaseDate') or item.get('releasedate', 'N/A'),
        "cast": item.get('cast', 'N/A'),
        
        # --- CAMPOS LEGACY (CRÍTICO PARA ROKU / XTREAM API) ---
        # Tu app de Roku probablemente busca 'series_id' explícitamente para pedir los episodios.
        "series_id": raw_id, 
        "stream_id": raw_id, # Redundancia de seguridad
        "cover": item.get('cover') or item.get('stream_icon'), # Roku a veces busca 'cover'
        "youtube_trailer": item.get('youtube_trailer', ''),
        "episode_run_time": item.get('episode_run_time', '0'),
        "backdrop_path": item.get('backdrop_path', []),
        
        # Metadata de origen para depuración
        "source_alias": source_alias
    }
