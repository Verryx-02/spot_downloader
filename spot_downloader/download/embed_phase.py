"""
Metadata embedding phase (PHASE 5) for spot-downloader.

This module provides the orchestration for embedding metadata and lyrics
into all downloaded M4A files that haven't been processed yet.

PHASE 5 Workflow:
    1. Query database for tracks where downloaded=True and metadata_embedded=False
       OR tracks with lyrics available but not embedded
    2. For each track:
       a. Load the M4A file from file_path
       b. Embed all Spotify metadata (or just lyrics if metadata already embedded)
       c. Update database with embedded flags
    3. Report statistics

Note:
    Files already have their final names from PHASE 3.
    This phase does NOT rename files, only embeds metadata.

Usage:
    from spot_downloader.download.embed_phase import embed_metadata_phase5
    
    stats = embed_metadata_phase5(database, playlist_id, output_dir)
    print(f"Embedded metadata in {stats.embedded}/{stats.total} tracks")
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spot_downloader.core.database import Database
from spot_downloader.core.logger import get_logger, log_embed_failure
from spot_downloader.core.progress import MetadataProgressBar
from spot_downloader.download.metadata import MetadataEmbedder
from spot_downloader.download.lyrics import Lyrics
from spot_downloader.spotify.models import Track

logger = get_logger(__name__)


@dataclass
class EmbedStats:
    """
    Statistics from metadata embedding batch.
    
    Attributes:
        total: Total number of tracks to process.
        embedded: Successfully embedded metadata.
        with_lyrics: Tracks where lyrics were also embedded.
        with_cover: Tracks where cover art was embedded.
        failed: Failed to embed (file issues, etc.).
        lyrics_only: Tracks where only lyrics were embedded (metadata already present).
    """
    
    total: int = 0
    embedded: int = 0
    with_lyrics: int = 0
    with_cover: int = 0
    failed: int = 0
    lyrics_only: int = 0
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total == 0:
            return 0.0
        return (self.embedded / self.total) * 100


def embed_metadata_phase5(
    database: Database,
    playlist_id: str,
    output_dir: Path,
    num_threads: int = 4
) -> EmbedStats:
    """
    Embed metadata and lyrics into all M4A files that need processing.
    
    This is the main entry point for PHASE 5.
    
    Args:
        database: Database instance.
        playlist_id: Playlist ID to process (currently unused, processes globally).
        output_dir: Directory containing the M4A files.
        num_threads: Number of parallel embedding threads.
                    Parallelization helps with cover art downloads.
    
    Returns:
        EmbedStats with embedding results.
            
    Behavior:
        1. Get tracks needing embedding from database
        2. Create MetadataEmbedder instance
        3. For each track (in parallel):
           a. Load file from file_path in database
           b. If metadata_embedded=0: embed all metadata + lyrics
           c. If metadata_embedded=1 but lyrics_embedded=0: embed only lyrics
           d. Update database flags accordingly
        4. Log summary statistics
        5. Return stats
    
    File Naming:
        Files already have their final names from PHASE 3.
        This phase does NOT rename files, only embeds metadata.
    
    Database Updates:
        - Sets metadata_embedded=True
        - Sets lyrics_embedded=True if lyrics were embedded
        - Sets cover_embedded=True if cover was embedded
    
    Error Handling:
        - If a file is missing, log error and skip
        - If embedding fails, log error and skip
        - Continue processing other tracks
    """
    stats = EmbedStats()
    
    # Get tracks needing embedding
    tracks = database.get_tracks_needing_embedding()
    
    if not tracks:
        logger.info("No tracks need metadata embedding")
        return stats
    
    stats.total = len(tracks)
    logger.info(f"Found {stats.total} tracks needing metadata embedding")
    
    # Create embedder instance (stateless, can be shared)
    embedder = MetadataEmbedder()
    
    # Process tracks in parallel
    with MetadataProgressBar(total=stats.total, description="Embedding") as progress:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            # Submit all tasks
            future_to_track = {
                executor.submit(
                    _embed_single_track,
                    embedder,
                    track_data,
                    database
                ): track_data
                for track_data in tracks
            }
            
            # Process completed tasks
            for future in as_completed(future_to_track):
                track_data = future_to_track[future]
                
                try:
                    result = future.result()
                    
                    if result["success"]:
                        stats.embedded += 1
                        if result["had_lyrics"]:
                            stats.with_lyrics += 1
                        if result["cover_embedded"]:
                            stats.with_cover += 1
                        if result["lyrics_only"]:
                            stats.lyrics_only += 1
                        
                        progress.update(success=True, with_lyrics=result["had_lyrics"])
                    else:
                        stats.failed += 1
                        progress.update(success=False, with_lyrics=False)
                        
                except Exception as e:
                    # Unexpected error in future
                    stats.failed += 1
                    progress.update(success=False, with_lyrics=False)
                    
                    track_name = track_data.get("name", "Unknown")
                    artist = track_data.get("artist", "Unknown")
                    spotify_url = track_data.get("spotify_url", "")
                    
                    log_embed_failure(
                        logger,
                        track_name=track_name,
                        artist=artist,
                        spotify_url=spotify_url,
                        error_message=str(e),
                        assigned_number=track_data.get("assigned_number")
                    )
    
    # Log summary
    logger.info(f"Embedding complete: {stats.embedded}/{stats.total} successful")
    if stats.with_lyrics > 0:
        logger.info(f"  - With lyrics: {stats.with_lyrics}")
    if stats.with_cover > 0:
        logger.info(f"  - With cover art: {stats.with_cover}")
    if stats.lyrics_only > 0:
        logger.info(f"  - Lyrics only (metadata already present): {stats.lyrics_only}")
    if stats.failed > 0:
        logger.warning(f"  - Failed: {stats.failed} (see embed_failures.log)")
    
    return stats


def _embed_single_track(
    embedder: MetadataEmbedder,
    track_data: dict[str, Any],
    database: Database
) -> dict[str, Any]:
    """
    Embed metadata into a single track's M4A file.
    
    Args:
        embedder: MetadataEmbedder instance.
        track_data: Track data from database.
        database: Database instance for updating flags.
    
    Returns:
        Dictionary with:
        - success: bool - True if embedding succeeded
        - had_lyrics: bool - True if lyrics were embedded
        - cover_embedded: bool - True if cover art was embedded
        - lyrics_only: bool - True if only lyrics were embedded (metadata already present)
    
    Behavior:
        1. Check file exists at track_data['file_path']
        2. Determine what needs to be embedded:
           - If metadata_embedded=0: embed everything
           - If metadata_embedded=1 but lyrics_embedded=0: embed only lyrics
        3. Reconstruct Track object from track_data
        4. Create Lyrics object if track_data['lyrics_text'] exists
        5. Call embedder methods
        6. Update database flags
        7. Return results
    
    Note:
        This function does NOT rename files. Files already have
        their final names from PHASE 3.
    """
    result = {
        "success": False,
        "had_lyrics": False,
        "cover_embedded": False,
        "lyrics_only": False
    }
    
    spotify_id = track_data.get("track_id") or track_data.get("spotify_id")
    track_name = track_data.get("name", "Unknown")
    artist = track_data.get("artist", "Unknown")
    spotify_url = track_data.get("spotify_url", "")
    assigned_number = track_data.get("assigned_number")
    
    # Get file path
    file_path_str = track_data.get("file_path")
    if not file_path_str:
        log_embed_failure(
            logger,
            track_name=track_name,
            artist=artist,
            spotify_url=spotify_url,
            error_message="No file path in database",
            assigned_number=assigned_number
        )
        return result
    
    file_path = Path(file_path_str)
    
    # Check file exists
    if not file_path.exists():
        log_embed_failure(
            logger,
            track_name=track_name,
            artist=artist,
            spotify_url=spotify_url,
            error_message=f"File not found: {file_path}",
            assigned_number=assigned_number
        )
        return result
    
    # Determine what to embed
    metadata_already_embedded = track_data.get("metadata_embedded", False)
    cover_already_embedded = track_data.get("cover_embedded", False)
    lyrics_text = track_data.get("lyrics_text")
    has_lyrics = lyrics_text is not None and len(lyrics_text) > 0
    
    try:
        if metadata_already_embedded and cover_already_embedded:
            # Only embed lyrics (metadata and cover already present)
            if has_lyrics:
                lyrics = Lyrics(
                    text=lyrics_text,
                    is_synced=track_data.get("lyrics_synced", False),
                    source=track_data.get("lyrics_source", "unknown")
                )
                
                embedder.embed_lyrics_only(file_path, lyrics)
                
                # Update database
                database.mark_lyrics_embedded(spotify_id)
                
                result["success"] = True
                result["had_lyrics"] = True
                result["lyrics_only"] = True
                
                logger.debug(f"Embedded lyrics only for: {track_name}")
            else:
                # Nothing to do (metadata and cover embedded, no lyrics)
                result["success"] = True
                logger.debug(f"Nothing to embed for: {track_name} (metadata and cover done, no lyrics)")
        else:
            # Full embedding (metadata, cover, and/or lyrics missing)
            track = Track.from_database_dict(spotify_id, track_data)
            
            # Create lyrics object if available
            lyrics = None
            if has_lyrics:
                lyrics = Lyrics(
                    text=lyrics_text,
                    is_synced=track_data.get("lyrics_synced", False),
                    source=track_data.get("lyrics_source", "unknown")
                )
            
            # Embed metadata (returns True if cover was embedded)
            cover_embedded = embedder.embed_metadata(file_path, track, lyrics)
            
            # Update database
            database.mark_metadata_embedded(spotify_id)
            
            if cover_embedded:
                database.mark_cover_embedded(spotify_id)
            
            if has_lyrics:
                database.mark_lyrics_embedded(spotify_id)
            
            result["success"] = True
            result["had_lyrics"] = has_lyrics
            result["cover_embedded"] = cover_embedded
            
            logger.debug(f"Embedded metadata for: {track_name} (cover={cover_embedded}, lyrics={has_lyrics})")
        
    except Exception as e:
        log_embed_failure(
            logger,
            track_name=track_name,
            artist=artist,
            spotify_url=spotify_url,
            error_message=str(e),
            assigned_number=assigned_number
        )
        return result
    
    return result