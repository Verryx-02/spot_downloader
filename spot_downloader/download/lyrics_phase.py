"""
Lyrics fetching phase (PHASE 4) for spot-downloader.

This module provides the orchestration for fetching lyrics for all
downloaded tracks that don't yet have lyrics.

PHASE 4 Workflow:
    1. Query database for tracks where downloaded=True and lyrics_fetched=False
    2. For each track:
       a. Attempt to fetch lyrics using LyricsFetcher
       b. If found: store lyrics in database, create .lrc file if synced
       c. If not found: collect for batch logging (DON'T log during progress)
       d. Mark lyrics_fetched=True ONLY if lyrics were found
    3. Create hard links for .lrc files in playlist directories
    4. Log failures in batch after progress bar completes
    5. Report statistics

Retry Logic:
    If all providers fail for a track, the database is NOT updated.
    This means the track will be retried on the next run of phase 4.
    Only successful fetches update the database.

Output Files:
    - .lrc files are created in tracks/ directory alongside audio files
    - Hard links to .lrc files are created in playlist directories
    - Lyrics text is stored in database for embedding in phase 5

Usage:
    from spot_downloader.download.lyrics_phase import fetch_lyrics_phase4
    
    stats = fetch_lyrics_phase4(database, file_manager, num_threads=2)
    print(f"Found lyrics for {stats.found}/{stats.total} tracks")
"""

import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spot_downloader.core.database import Database
from spot_downloader.core.file_manager import FileManager, sanitize_filename
from spot_downloader.core.logger import get_logger, Colors
from spot_downloader.core.progress import LyricsProgressBar
from spot_downloader.download.lyrics import LyricsFetcher, Lyrics

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Base delay between requests per thread (seconds)
BASE_DELAY = 1.0

# Maximum delay after backoff (seconds)
MAX_DELAY = 10.0

# Backoff multiplier
BACKOFF_MULTIPLIER = 1.5


# =============================================================================
# Statistics
# =============================================================================

@dataclass
class LyricsStats:
    """
    Statistics from a lyrics fetch batch.
    
    Attributes:
        total: Total number of tracks processed.
        found: Tracks where lyrics were found.
        not_found: Tracks where no lyrics were found.
        synced: Tracks with synced (LRC) lyrics.
        plain: Tracks with plain text lyrics.
        lrc_created: Number of .lrc files created.
        lrc_links_created: Number of .lrc hard links in playlists.
    """
    
    total: int = 0
    found: int = 0
    not_found: int = 0
    synced: int = 0
    plain: int = 0
    lrc_created: int = 0
    lrc_links_created: int = 0
    
    @property
    def found_rate(self) -> float:
        """Calculate lyrics found rate as percentage."""
        if self.total == 0:
            return 0.0
        return (self.found / self.total) * 100


# =============================================================================
# Thread-safe Rate Limiter
# =============================================================================

class RateLimiter:
    """
    Thread-safe rate limiter with exponential backoff.
    
    Ensures requests are spaced out to avoid rate limiting from providers.
    Uses exponential backoff when failures occur.
    """
    
    def __init__(self, base_delay: float = BASE_DELAY):
        """
        Initialize rate limiter.
        
        Args:
            base_delay: Base delay between requests in seconds.
        """
        self._base_delay = base_delay
        self._current_delay = base_delay
        self._lock = threading.Lock()
        self._last_request_time = 0.0
    
    def wait(self) -> None:
        """
        Wait for the appropriate delay before next request.
        
        Thread-safe: multiple threads can call this simultaneously.
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            
            if elapsed < self._current_delay:
                sleep_time = self._current_delay - elapsed
                # Add jitter to prevent synchronized retries
                jitter = random.uniform(0, self._base_delay * 0.3)
                time.sleep(sleep_time + jitter)
            
            self._last_request_time = time.time()
    
    def on_success(self) -> None:
        """Reset delay after successful request."""
        with self._lock:
            self._current_delay = self._base_delay
    
    def on_failure(self) -> None:
        """Increase delay after failed request (exponential backoff)."""
        with self._lock:
            self._current_delay = min(
                self._current_delay * BACKOFF_MULTIPLIER,
                MAX_DELAY
            )


# =============================================================================
# LRC File Management
# =============================================================================

def _get_lrc_path(audio_file_path: str | Path) -> Path:
    """
    Get the .lrc file path for an audio file.
    
    Args:
        audio_file_path: Path to the audio file.
    
    Returns:
        Path to the corresponding .lrc file.
    
    Example:
        _get_lrc_path("/music/tracks/Song-Artist.m4a")
        # Returns: Path("/music/tracks/Song-Artist.lrc")
    """
    audio_path = Path(audio_file_path)
    return audio_path.with_suffix('.lrc')


def _write_lrc_file(lrc_path: Path, lyrics: Lyrics) -> bool:
    """
    Write synced lyrics to an .lrc file.
    
    Args:
        lrc_path: Path where to write the .lrc file.
        lyrics: Lyrics object (must be synced).
    
    Returns:
        True if file was written successfully, False otherwise.
    """
    if not lyrics.is_synced:
        return False
    
    try:
        lrc_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(lrc_path, 'w', encoding='utf-8') as f:
            f.write(lyrics.text)
        
        return True
        
    except Exception as e:
        logger.debug(f"Failed to write LRC file: {e}")
        return False


def _create_lrc_playlist_links(
    lrc_path: Path,
    playlists: list[dict[str, Any]],
    file_manager: FileManager,
    track_name: str,
    artist: str
) -> int:
    """
    Create hard links for .lrc file in all playlist directories.
    
    Args:
        lrc_path: Path to the master .lrc file in tracks/.
        playlists: List of playlist info dicts with: name, position.
        file_manager: FileManager instance.
        track_name: Track name for filename.
        artist: Artist name for filename.
    
    Returns:
        Number of links successfully created.
    """
    if not lrc_path.exists():
        return 0
    
    links_created = 0
    
    for playlist in playlists:
        playlist_name = playlist.get('name', 'Unknown')
        position = playlist.get('position', 0)
        
        try:
            playlist_dir = file_manager.get_playlist_dir(playlist_name)
            
            # Generate LRC filename with position (matching audio file naming)
            safe_title = sanitize_filename(track_name)
            safe_artist = sanitize_filename(artist)
            lrc_filename = f"{position:05d}-{safe_title}-{safe_artist}.lrc"
            link_path = playlist_dir / lrc_filename
            
            # Remove existing link if present
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            
            # Try hard link first
            try:
                link_path.hardlink_to(lrc_path)
                links_created += 1
            except OSError:
                # Fallback to symlink for cross-filesystem
                try:
                    link_path.symlink_to(lrc_path)
                    links_created += 1
                except OSError:
                    pass
                    
        except Exception as e:
            logger.debug(f"Failed to create LRC link in {playlist_name}: {e}")
    
    return links_created


# =============================================================================
# Track Result Info
# =============================================================================

@dataclass
class TrackResult:
    """Result of processing a single track."""
    found: bool
    is_synced: bool
    lrc_created: int
    lrc_links: int
    # Info for logging failures (only set if found=False)
    track_name: str = ""
    artist: str = ""
    spotify_url: str = ""


# =============================================================================
# Main Phase Function
# =============================================================================

def fetch_lyrics_phase4(
    database: Database,
    output_dir: Path,
    num_threads: int = 2
) -> LyricsStats:
    """
    Fetch lyrics for all downloaded tracks that need them.
    
    This is the main entry point for PHASE 4.
    
    Args:
        database: Database instance.
        output_dir: Output directory (used to create FileManager).
        num_threads: Number of parallel fetching threads (default: 2).
    
    Returns:
        LyricsStats with fetch results.
    
    Behavior:
        1. Get tracks needing lyrics from database
        2. Create LyricsFetcher instance (shared across threads)
        3. For each track (in parallel):
           a. Call fetcher.fetch_lyrics()
           b. If lyrics found: 
              - Store in database (set_lyrics)
              - Create .lrc file if synced
              - Create hard links in playlist directories
           c. If not found:
              - Collect info for batch logging
              - Do NOT update database (allows retry on next run)
        4. Log failures in batch AFTER progress bar completes
        5. Return stats
    
    Thread Safety:
        Uses thread pool for parallel fetching.
        Database operations are thread-safe.
        Rate limiter coordinates request timing across threads.
    """
    logger.info("PHASE 4: Fetching lyrics")
    
    # Get tracks that need lyrics
    tracks = database.get_tracks_needing_lyrics()
    
    if not tracks:
        logger.info("No tracks need lyrics fetching")
        return LyricsStats()
    
    logger.info(f"Found {len(tracks)} tracks needing lyrics")
    
    # Initialize components
    stats = LyricsStats(total=len(tracks))
    fetcher = LyricsFetcher()
    file_manager = FileManager(output_dir)
    rate_limiter = RateLimiter(BASE_DELAY)
    stats_lock = threading.Lock()
    
    def process_track(track_data: dict[str, Any]) -> TrackResult:
        """
        Process a single track: fetch lyrics and update database.
        
        Returns:
            TrackResult with success/failure info and logging data.
        """
        track_name = track_data.get('name', 'Unknown')
        artist = track_data.get('artist', 'Unknown')
        album = track_data.get('album')
        duration_ms = track_data.get('duration_ms')
        spotify_id = track_data.get('spotify_id')
        spotify_url = track_data.get('spotify_url', '')
        file_path = track_data.get('file_path')
        
        # Calculate duration in seconds
        duration_seconds = duration_ms // 1000 if duration_ms else None
        
        # Rate limit
        rate_limiter.wait()
        
        # Fetch lyrics
        try:
            lyrics = fetcher.fetch_lyrics(
                track_name=track_name,
                artist=artist,
                album=album,
                duration_seconds=duration_seconds
            )
        except Exception as e:
            logger.debug(f"Lyrics fetch exception for {track_name}: {e}")
            lyrics = None
        
        if lyrics:
            # Success - update database
            rate_limiter.on_success()
            
            try:
                # Save PLAIN TEXT (no timestamps) in database for embedding
                plain_text = lyrics.get_plain_text()
                
                database.set_lyrics(
                    spotify_id=spotify_id,
                    lyrics_text=plain_text,  # Plain text for embedding
                    is_synced=lyrics.is_synced,
                    source=lyrics.source
                )
                
                lrc_created = 0
                lrc_links = 0
                
                # Create .lrc file WITH TIMESTAMPS if synced and we have a file path
                if lyrics.is_synced and file_path:
                    lrc_path = _get_lrc_path(file_path)
                    # Write original text WITH timestamps to .lrc file
                    if _write_lrc_file(lrc_path, lyrics):
                        lrc_created = 1
                        
                        # Create hard links in playlist directories
                        playlists = database.get_playlists_containing_track(spotify_id)
                        if playlists:
                            lrc_links = _create_lrc_playlist_links(
                                lrc_path=lrc_path,
                                playlists=playlists,
                                file_manager=file_manager,
                                track_name=track_name,
                                artist=artist
                            )
                
                return TrackResult(
                    found=True,
                    is_synced=lyrics.is_synced,
                    lrc_created=lrc_created,
                    lrc_links=lrc_links
                )
                
            except Exception as e:
                logger.debug(f"Failed to save lyrics for {track_name}: {e}")
                return TrackResult(
                    found=False,
                    is_synced=False,
                    lrc_created=0,
                    lrc_links=0,
                    track_name=track_name,
                    artist=artist,
                    spotify_url=spotify_url
                )
        else:
            # Failure - return info for logging in main loop
            rate_limiter.on_failure()
            
            return TrackResult(
                found=False,
                is_synced=False,
                lrc_created=0,
                lrc_links=0,
                track_name=track_name,
                artist=artist,
                spotify_url=spotify_url
            )
    
    # Process tracks with thread pool and progress bar
    with LyricsProgressBar(total=len(tracks)) as progress:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            # Submit all tasks
            future_to_track = {
                executor.submit(process_track, track): track
                for track in tracks
            }
            
            # Process results as they complete
            for future in as_completed(future_to_track):
                track = future_to_track[future]
                
                try:
                    result = future.result()
                except Exception as e:
                    logger.debug(f"Task failed for {track.get('name', 'Unknown')}: {e}")
                    result = TrackResult(
                        found=False,
                        is_synced=False,
                        lrc_created=0,
                        lrc_links=0,
                        track_name=track.get('name', 'Unknown'),
                        artist=track.get('artist', 'Unknown'),
                        spotify_url=track.get('spotify_url', '')
                    )
                
                # Update stats (thread-safe)
                with stats_lock:
                    if result.found:
                        stats.found += 1
                        if result.is_synced:
                            stats.synced += 1
                        else:
                            stats.plain += 1
                        stats.lrc_created += result.lrc_created
                        stats.lrc_links_created += result.lrc_links
                    else:
                        stats.not_found += 1
                        
                        # Log failure to console (below progress bar)
                        progress.log(
                            f"{Colors.YELLOW}WARNING{Colors.RESET}: "
                            f"No lyrics found for: {result.artist} - {result.track_name}"
                        )
                
                # Update progress bar
                progress.update(found=result.found, synced=result.is_synced)
    
    # Log summary
    logger.info(
        f"PHASE 4 complete: {stats.found}/{stats.total} tracks with lyrics "
        f"({stats.found_rate:.1f}%) - {stats.synced} synced, {stats.plain} plain, "
        f"{stats.lrc_created} LRC files created"
    )
    
    if stats.lrc_links_created > 0:
        logger.info(f"Created {stats.lrc_links_created} LRC hard links in playlists")
    
    if stats.not_found > 0:
        logger.info(
            f"{stats.not_found} tracks without lyrics will be retried on next run"
        )
    
    return stats


def _fetch_lyrics_for_track(
    fetcher: LyricsFetcher,
    track_data: dict[str, Any]
) -> Lyrics | None:
    """
    Fetch lyrics for a single track.
    
    Args:
        fetcher: LyricsFetcher instance.
        track_data: Track data from database.
    
    Returns:
        Lyrics object if found, None otherwise.
    
    Note:
        This is a helper function for parallel processing.
        It does not update the database - that's done by the caller.
    """
    track_name = track_data.get('name', 'Unknown')
    artist = track_data.get('artist', 'Unknown')
    album = track_data.get('album')
    duration_ms = track_data.get('duration_ms')
    
    duration_seconds = duration_ms // 1000 if duration_ms else None
    
    return fetcher.fetch_lyrics(
        track_name=track_name,
        artist=artist,
        album=album,
        duration_seconds=duration_seconds
    )


# =============================================================================
# Utility Functions
# =============================================================================

def retry_failed_lyrics(
    database: Database,
    output_dir: Path,
    num_threads: int = 2
) -> LyricsStats:
    """
    Retry fetching lyrics for tracks that previously failed.
    
    This is equivalent to calling fetch_lyrics_phase4() since failed
    tracks are not marked as fetched and will be included automatically.
    
    Args:
        database: Database instance.
        output_dir: Output directory.
        num_threads: Number of parallel threads.
    
    Returns:
        LyricsStats with results.
    """
    return fetch_lyrics_phase4(database, output_dir, num_threads)


def get_lyrics_statistics(database: Database) -> dict[str, int]:
    """
    Get current lyrics statistics from database.
    
    Args:
        database: Database instance.
    
    Returns:
        Dictionary with:
        - total_downloaded: Total downloaded tracks
        - with_lyrics: Tracks that have lyrics
        - without_lyrics: Tracks where lyrics were not found
        - pending: Tracks not yet processed
        - synced: Tracks with synced lyrics
    """
    stats = database.get_global_stats()
    
    return {
        'total_downloaded': stats.get('downloaded_tracks', 0),
        'with_lyrics': stats.get('tracks_with_lyrics', 0),
    }