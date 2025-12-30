"""
Audio downloader for spot-downloader (PHASE 3).

This module handles downloading audio from YouTube and saving files
using the central storage architecture with hard links.

Architecture:
    output_directory/
    ├── tracks/                               # Central storage (canonical files)
    │   ├── Bohemian Rhapsody-Queen.m4a
    │   └── Title-Artist.m4a
    ├── My Playlist/                          # Playlist view (hard links)
    │   ├── 00001-Bohemian Rhapsody-Queen.m4a → ../tracks/Bohemian Rhapsody-Queen.m4a
    │   └── 00002-Title-Artist.m4a            → ../tracks/Title-Artist.m4a
    └── Another Playlist/
        └── 00001-Bohemian Rhapsody-Queen.m4a → ../tracks/Bohemian Rhapsody-Queen.m4a

PHASE 3 Workflow:
    1. Get tracks with YouTube URL but not downloaded from database
    2. For each track:
       a. Download audio from YouTube using yt-dlp
       b. Convert to M4A format using FFmpeg (via yt-dlp postprocessor)
       c. Save to tracks/ directory with canonical name: {title}-{artist}.m4a
       d. Update database: downloaded=True, file_path (canonical path)
       e. Create hard links in ALL playlist directories containing this track
    3. Generate statistics

Audio Quality:
    - Free YouTube: 128 kbps (maximum available)
    - YouTube Premium (with cookies): 256 kbps
    Quality is auto-detected based on cookie file.

Rate Limiting:
    YouTube enforces strict rate limits on downloads:
    - Guest session: ~300 videos/hour (~5/min)
    - With cookies: ~2000 videos/hour (~33/min)
    
    To avoid rate limiting, this module uses:
    - Single thread (num_threads=1) - multiple threads cause simultaneous requests
    - Sleep delay of 10-20 seconds between downloads (yt-dlp "-t sleep" preset)
    
    With these settings, expect ~15 seconds per track (~4 tracks/minute).

File Naming:
    - Canonical (in tracks/): {title}-{artist}.m4a
    - Playlist links: {position:05d}-{title}-{artist}.m4a

Dependencies:
    - yt-dlp: YouTube download and extraction
    - FFmpeg: Audio conversion (must be installed)

Usage:
    from spot_downloader.download.downloader import download_tracks_phase3
    
    stats = download_tracks_phase3(
        database=db,
        output_dir=Path("/music"),
        playlist_id=playlist_id,
        cookie_file=Path("cookies.txt"),  # Optional
        num_threads=1  # Use 1 thread to avoid YouTube rate limiting
    )
    print(f"Downloaded: {stats.downloaded}/{stats.total}")
"""

import random
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

from yt_dlp import YoutubeDL

from spot_downloader.core.database import Database
from spot_downloader.core.exceptions import DownloadError
from spot_downloader.core.file_manager import FileManager
from spot_downloader.core.logger import get_logger, log_download_failure
from spot_downloader.core.progress import DownloadProgressBar

logger = get_logger(__name__)


# =============================================================================
# Retry Configuration
# =============================================================================

MAX_RETRIES = 3  # 3 tentativi totali
BASE_DELAY = 1.5  # seconds
MAX_DELAY = 15.0  # seconds
JITTER_FACTOR = 0.3  # randomness factor for backoff


class YtDlpSilentLogger:
    """
    Custom logger for yt-dlp that suppresses output during retry attempts.
    
    yt-dlp ignores quiet=True for certain errors and prints directly to stderr.
    This logger intercepts those messages and only shows them if we want to.
    """
    
    def __init__(self, show_errors: bool = False):
        """
        Initialize the silent logger.
        
        Args:
            show_errors: If True, errors are logged. If False, suppressed.
        """
        self.show_errors = show_errors
        self.last_error: str | None = None
    
    def debug(self, msg: str) -> None:
        """Suppress debug messages."""
        pass
    
    def info(self, msg: str) -> None:
        """Suppress info messages."""
        pass
    
    def warning(self, msg: str) -> None:
        """Suppress warning messages."""
        pass
    
    def error(self, msg: str) -> None:
        """Capture error but only show if configured to."""
        self.last_error = msg
        if self.show_errors:
            logger.error(msg)


class ErrorType(Enum):
    """Classification of download errors for retry strategy."""
    FORBIDDEN = auto()          # 403 / no data - retry with backoff
    RATE_LIMITED = auto()       # 429 - retry with longer delay
    FORMAT_UNAVAILABLE = auto() # Format not available - retry with fallback format
    AGE_RESTRICTED = auto()     # Requires sign-in - needs cookies
    NETWORK_ERROR = auto()      # Connection issues - retry with backoff
    VIDEO_UNAVAILABLE = auto()  # Video removed/private - no retry
    EMPTY_FILE = auto()         # Downloaded file is empty - retry with backoff
    UNKNOWN = auto()            # Other errors - limited retry


def classify_error(error_message: str) -> ErrorType:
    """
    Classify a yt-dlp error message to determine retry strategy.
    
    Args:
        error_message: The error message from yt-dlp.
    
    Returns:
        ErrorType indicating which retry strategy to use.
    """
    msg = error_message.lower()
    
    # IMPORTANT: Check rate limiting FIRST because YouTube's rate limit message
    # also contains "video unavailable" which would match VIDEO_UNAVAILABLE
    if any(x in msg for x in ["rate-limited", "rate limit", "429", "too many requests", "try again later"]):
        return ErrorType.RATE_LIMITED
    
    # 403 or "no data blocks" (masked 403)
    if "403" in msg or "forbidden" in msg or "did not get any data" in msg:
        return ErrorType.FORBIDDEN
    
    if "format" in msg and ("not available" in msg or "unavailable" in msg):
        return ErrorType.FORMAT_UNAVAILABLE
    
    if "sign in" in msg or "age" in msg or "confirm your age" in msg:
        return ErrorType.AGE_RESTRICTED
    
    if any(x in msg for x in ["connection", "timeout", "network", "urlopen error"]):
        return ErrorType.NETWORK_ERROR
    
    if any(x in msg for x in ["video unavailable", "private video", "removed", "deleted"]):
        return ErrorType.VIDEO_UNAVAILABLE
    
    if "file is empty" in msg or "empty file" in msg:
        return ErrorType.EMPTY_FILE
    
    return ErrorType.UNKNOWN


def calculate_backoff(attempt: int, base_delay: float = BASE_DELAY) -> float:
    """
    Calculate exponential backoff delay with jitter.
    
    Args:
        attempt: Current attempt number (0-indexed).
        base_delay: Base delay in seconds.
    
    Returns:
        Delay in seconds with jitter applied.
    """
    # Exponential backoff: 2^attempt * base_delay
    delay = min(base_delay * (2 ** attempt), MAX_DELAY)
    
    # Add jitter: ±50% randomness
    jitter = delay * JITTER_FACTOR * (2 * random.random() - 1)
    
    return max(0.5, delay + jitter)


@dataclass
class DownloadStats:
    """
    Statistics from a download batch.
    
    Attributes:
        total: Total number of tracks to download.
        downloaded: Successfully downloaded and processed.
        failed: Failed to download.
        skipped: Already downloaded (skipped).
    """
    
    total: int = 0
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total == 0:
            return 0.0
        return (self.downloaded / self.total) * 100


class Downloader:
    """
    Downloads audio from YouTube and saves with hard link architecture.
    
    This class implements PHASE 3 of the download workflow:
    1. Downloads audio to tracks/ directory (canonical files)
    2. Creates hard links in playlist directories
    
    Attributes:
        _database: Database for tracking download state.
        _file_manager: FileManager for file operations.
        _cookie_file: Optional cookies.txt for YouTube Premium.
        _num_threads: Number of parallel downloads.
    
    Thread Safety:
        The download_track() method is thread-safe. Multiple threads
        can download different tracks simultaneously.
    
    Note:
        This class does NOT handle lyrics fetching or metadata embedding.
        Those operations are handled by PHASE 4 and PHASE 5 respectively.
    """
    
    def __init__(
        self,
        database: Database,
        output_dir: Path,
        cookie_file: Path | None = None,
        num_threads: int = 1
    ) -> None:
        """
        Initialize the Downloader.
        
        Args:
            database: Database instance for state tracking.
            output_dir: Base output directory (contains tracks/, playlists, etc.)
            cookie_file: Optional path to cookies.txt file for
                        YouTube Premium quality (256 kbps).
                        If None, downloads at 128 kbps.
            num_threads: Default number of parallel download threads.
                        Recommended: 1 to avoid YouTube rate limiting.
        """
        self._database = database
        self._file_manager = FileManager(output_dir)
        self._cookie_file = cookie_file
        self._num_threads = num_threads
        
        # Validate cookie file exists if provided
        if self._cookie_file is not None:
            if not self._cookie_file.exists():
                logger.warning(
                    f"Cookie file not found: {self._cookie_file}. "
                    "Downloads will be limited to 128 kbps."
                )
                self._cookie_file = None
            else:
                logger.debug(f"Using cookies for premium quality: {self._cookie_file}")
    
    def download_tracks(
        self,
        tracks: list[dict[str, Any]],
        playlist_id: str,
        num_threads: int | None = None
    ) -> DownloadStats:
        """
        Download multiple tracks using parallel processing.
        
        This is the main entry point for PHASE 3 batch processing.
        
        Args:
            tracks: List of track data dicts from database.
                   Each dict must have 'spotify_id', 'youtube_url',
                   'name', 'artist', and other metadata fields.
            playlist_id: Playlist ID (used for logging context).
            num_threads: Override default thread count.
        
        Returns:
            DownloadStats with counts of success/failure.
        
        Behavior:
            1. Create thread pool with num_threads workers
            2. Submit download_track() task for each track
            3. Track progress with Rich progress bar
            4. Collect results and statistics
            5. Return final stats
        """
        threads = num_threads if num_threads is not None else self._num_threads
        stats = DownloadStats(total=len(tracks))
        
        if not tracks:
            logger.info("No tracks to download")
            return stats
        
        logger.info(f"Starting download of {len(tracks)} tracks with {threads} threads")
        
        with DownloadProgressBar(total=len(tracks), description="Downloading") as progress:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                # Submit all download tasks
                future_to_track = {
                    executor.submit(self.download_track, track_data): track_data
                    for track_data in tracks
                }
                
                # Process completed tasks
                for future in as_completed(future_to_track):
                    track_data = future_to_track[future]
                    track_name = track_data.get("name", "Unknown")
                    artist = track_data.get("artist", "Unknown")
                    
                    try:
                        success = future.result()
                        if success:
                            stats.downloaded += 1
                            progress.update(success=True)
                        else:
                            stats.failed += 1
                            progress.update(success=False)
                    except Exception as e:
                        stats.failed += 1
                        progress.update(success=False)
                        logger.debug(f"Unexpected error downloading {artist} - {track_name}: {e}")
        
        # Log final statistics
        logger.info(
            f"Download complete: {stats.downloaded}/{stats.total} successful, "
            f"{stats.failed} failed"
        )
        
        return stats
    
    def download_track(self, track_data: dict[str, Any]) -> bool:
        """
        Download a single track from YouTube and create playlist links.
        
        This method handles the PHASE 3 download workflow for one track:
        1. Download audio from YouTube
        2. Save to tracks/ directory with canonical name
        3. Update database with canonical path
        4. Create hard links in all playlist directories
        
        Args:
            track_data: Track data dictionary from database.
                       Required keys: spotify_id, youtube_url, name, artist
        
        Returns:
            True if download succeeded, False otherwise.
        
        Thread Safety:
            This method is thread-safe. Uses per-track temp directories.
        """
        # Extract track info
        spotify_id = track_data.get("spotify_id") or track_data.get("track_id")
        youtube_url = track_data.get("youtube_url")
        name = track_data.get("name", "Unknown")
        artist = track_data.get("artist", "Unknown")
        spotify_url = track_data.get("spotify_url", "")
        
        if not spotify_id or not youtube_url:
            logger.error(f"Missing required fields for track: {name}")
            return False
        
        # Check if canonical file already exists
        canonical_path = self._file_manager.get_canonical_path(artist, name)
        if canonical_path.exists():
            logger.debug(f"File already exists: {canonical_path.name}")
            # Still need to update database and create links
            self._database.mark_downloaded(spotify_id, canonical_path)
            self._file_manager.update_playlist_links_from_db(
                self._database, spotify_id, canonical_path, name, artist
            )
            return True
        
        logger.debug(f"Downloading: {artist} - {name}")
        
        # Create a unique temp directory for this download
        temp_dir = Path(tempfile.mkdtemp(prefix=f"spot_dl_{spotify_id[:8]}_"))
        
        try:
            # Download audio to temp directory
            downloaded_file = self._download_audio(youtube_url, temp_dir)
            
            if downloaded_file is None:
                log_download_failure(
                    logger,
                    track_name=name,
                    artist=artist,
                    spotify_url=spotify_url,
                    error_message="yt-dlp returned no file"
                )
                return False
            
            # Move to canonical location in tracks/
            shutil.move(str(downloaded_file), str(canonical_path))
            
            # Update database with canonical path
            self._database.mark_downloaded(spotify_id, canonical_path)
            
            # Create hard links in all playlist directories containing this track
            self._file_manager.update_playlist_links_from_db(
                self._database, spotify_id, canonical_path, name, artist
            )
            
            logger.debug(f"Downloaded: {artist} - {name} -> {canonical_path.name}")
            return True
            
        except DownloadError as e:
            log_download_failure(
                logger,
                track_name=name,
                artist=artist,
                spotify_url=spotify_url,
                error_message=str(e)
            )
            return False
        except Exception as e:
            log_download_failure(
                logger,
                track_name=name,
                artist=artist,
                spotify_url=spotify_url,
                error_message=f"Unexpected error: {e}"
            )
            return False
        finally:
            # Clean up temp directory
            self._cleanup_temp_files(temp_dir)
    
    def _download_audio(
        self,
        youtube_url: str,
        output_path: Path
    ) -> Path | None:
        """
        Download audio from YouTube using yt-dlp with intelligent retry.
        
        Args:
            youtube_url: YouTube video URL to download.
            output_path: Directory for temporary download file.
        
        Returns:
            Path to the downloaded M4A file, or None if download failed.
        
        Raises:
            DownloadError: If download fails after all retries.
        """
        # Generate unique output template
        output_template = str(output_path / "%(id)s.%(ext)s")
        
        last_error: str | None = None
        
        for attempt in range(MAX_RETRIES):
            # Use silent logger for retry attempts, show errors only on last attempt
            is_last_attempt = (attempt == MAX_RETRIES - 1)
            yt_logger = YtDlpSilentLogger(show_errors=is_last_attempt)
            
            try:
                options = self._get_yt_dlp_options(output_template, yt_logger=yt_logger)
                
                with YoutubeDL(options) as ydl:
                    # Extract info and download
                    info = ydl.extract_info(youtube_url, download=True)
                    
                    if info is None:
                        raise DownloadError("yt-dlp returned no info")
                    
                    # Find the downloaded file
                    return self._find_downloaded_file(output_path, info.get("id", "unknown"))
                    
            except Exception as e:
                error_msg = str(e)
                # Also check if yt-dlp logged an error we didn't catch
                if yt_logger.last_error and yt_logger.last_error not in error_msg:
                    error_msg = f"{error_msg} | {yt_logger.last_error}"
                
                last_error = error_msg
                error_type = classify_error(error_msg)
                
                # Determine retry strategy based on error type
                should_retry, delay = self._get_retry_strategy(error_type, attempt)
                
                if not should_retry:
                    # No point retrying - raise immediately
                    raise DownloadError(f"yt-dlp error: {error_msg}") from e
                
                if attempt < MAX_RETRIES - 1:
                    logger.debug(f"Retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s ({error_type.name})")
                    time.sleep(delay)
                    
                    # Clean up any partial downloads before retry
                    self._cleanup_partial_downloads(output_path)
        
        # All retries exhausted
        raise DownloadError(f"yt-dlp error: {last_error}")
    
    def _get_retry_strategy(
        self,
        error_type: ErrorType,
        attempt: int
    ) -> tuple[bool, float]:
        """
        Determine retry strategy based on error type.
        
        Args:
            error_type: Classified error type.
            attempt: Current attempt number (0-indexed).
        
        Returns:
            Tuple of (should_retry, delay_seconds)
        """
        if error_type == ErrorType.FORBIDDEN:
            # 403 / no data: Retry with reasonable delay (1.5-2.5s)
            delay = 1.5 + random.random()  # 1.5-2.5s
            return (True, delay)
        
        elif error_type == ErrorType.RATE_LIMITED:
            # YouTube rate limiting can last up to an hour
            # Don't waste time with short retries - just fail and let user retry later
            if attempt == 0:
                logger.debug(
                    "YouTube rate limiting detected. Consider reducing threads "
                    "or waiting before re-running. Track will be retried on next run."
                )
                # One retry after 30 seconds in case it's temporary
                return (True, 30.0)
            else:
                # Don't keep retrying - it won't help
                return (False, 0)
        
        elif error_type == ErrorType.FORMAT_UNAVAILABLE:
            # Format issue: Retry with delay (yt-dlp will try different clients)
            delay = 1.0 + random.random()
            return (True, delay)
        
        elif error_type == ErrorType.AGE_RESTRICTED:
            # Age restriction: Only retry if we have cookies (might be expired)
            if self._cookie_file is not None and attempt == 0:
                logger.debug("Age-restricted video - cookies may be expired")
                return (True, 1.0)
            else:
                logger.debug(
                    "Age-restricted video requires cookies. "
                    "Add cookie_file to config.yaml"
                )
                return (False, 0)
        
        elif error_type == ErrorType.NETWORK_ERROR:
            # Network issues: Retry with moderate backoff (1.5s, 3s, 6s)
            return (True, calculate_backoff(attempt, base_delay=1.5))
        
        elif error_type == ErrorType.EMPTY_FILE:
            # Empty file: Retry with short delay (1.5-2.5s)
            delay = 1.5 + random.random()
            return (True, delay)
        
        elif error_type == ErrorType.VIDEO_UNAVAILABLE:
            # Video gone: No point retrying
            return (False, 0)
        
        else:  # UNKNOWN
            # Unknown errors: One retry with short delay
            if attempt == 0:
                return (True, 1.5)
            return (False, 0)
    
    def _find_downloaded_file(self, output_path: Path, video_id: str) -> Path:
        """
        Find the downloaded audio file in the output directory.
        
        Args:
            output_path: Directory where file was downloaded.
            video_id: YouTube video ID.
        
        Returns:
            Path to the downloaded file.
        
        Raises:
            DownloadError: If no audio file is found.
        """
        # Look for the m4a file (postprocessor converts to m4a)
        m4a_file = output_path / f"{video_id}.m4a"
        if m4a_file.exists():
            return m4a_file
        
        # Fallback: look for any audio file with video_id
        for ext in [".m4a", ".webm", ".opus", ".mp3", ".mp4"]:
            candidate = output_path / f"{video_id}{ext}"
            if candidate.exists():
                return candidate
        
        # Last resort: find any audio file in the directory
        for f in output_path.iterdir():
            if f.is_file() and f.suffix in [".m4a", ".webm", ".opus", ".mp3", ".mp4"]:
                return f
        
        raise DownloadError(f"Downloaded file not found in {output_path}")
    
    def _cleanup_partial_downloads(self, output_path: Path) -> None:
        """
        Remove any partial/incomplete download files before retry.
        
        Args:
            output_path: Directory containing download files.
        """
        try:
            for f in output_path.iterdir():
                if f.is_file():
                    # Remove .part files and any audio files (incomplete downloads)
                    if f.suffix in [".part", ".m4a", ".webm", ".opus", ".mp3", ".mp4", ".ytdl"]:
                        f.unlink()
        except Exception:
            pass  # Best effort cleanup
    
    def _get_yt_dlp_options(
        self,
        output_template: str,
        yt_logger: YtDlpSilentLogger | None = None
    ) -> dict[str, Any]:
        """
        Build yt-dlp options dictionary.
        
        Uses spotDL-style approach:
        - Simple format: "bestaudio" (let yt-dlp choose best available)
        - extractor_args to try multiple YouTube player clients
        - FFmpeg postprocessor converts to m4a
        
        Args:
            output_template: Output path template for yt-dlp.
            yt_logger: Custom logger to suppress yt-dlp output during retries.
        
        Returns:
            Dictionary of yt-dlp options.
        """
        options: dict[str, Any] = {
            # Simple format: best audio available, yt-dlp picks the best
            "format": "bestaudio",
            
            # Output
            "outtmpl": output_template,
            
            # Quiet mode (we handle our own logging)
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            
            # Encoding
            "encoding": "UTF-8",
            
            # Retries (yt-dlp internal retries for fragments)
            "retries": 3,
            "fragment_retries": 3,
            
            # Sleep between requests to avoid rate limiting
            # Values from yt-dlp official "-t sleep" preset
            # See: https://github.com/yt-dlp/yt-dlp/wiki/Extractors
            "sleep_interval_requests": 0.75,  # 0.75s between HTTP requests
            
            # Sleep between downloads (crucial for avoiding 403/rate limit)
            # Random delay between 10-20 seconds before each video download
            "sleep_interval": 5,       # Minimum seconds before each download
            "max_sleep_interval": 15,   # Maximum seconds (random between min-max)
            
            # Enable remote JS challenge solver (requires deno)
            # This is needed for some age-restricted/protected videos
            "remote_components": "ejs:github",
            
            # Try multiple YouTube player clients (fixes "format not available")
            # This is the key fix from spotDL issues
            "extractor_args": {
                "youtube": {
                    "player_client": ["web", "android", "default"],
                }
            },
            
            # Postprocessors: convert to m4a
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "0",  # Best quality
                }
            ],
            
            # Don't keep intermediate files
            "keepvideo": False,
        }
        
        # Add custom logger to suppress yt-dlp stderr output
        if yt_logger is not None:
            options["logger"] = yt_logger
        
        # Add cookies if available
        if self._cookie_file is not None:
            options["cookiefile"] = str(self._cookie_file)
        
        return options
    
    def _cleanup_temp_files(self, temp_dir: Path) -> None:
        """
        Clean up temporary download files.
        
        Args:
            temp_dir: Directory containing temp files.
        """
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.debug(f"Failed to clean up temp directory {temp_dir}: {e}")


def download_tracks_phase3(
    database: Database,
    output_dir: Path,
    playlist_id: str,
    cookie_file: Path | None = None,
    num_threads: int = 1
) -> DownloadStats:
    """
    Convenience function for PHASE 3 track downloading.
    
    This is the main entry point called by the CLI for downloads.
    
    Args:
        database: Database instance.
        output_dir: Base output directory (contains tracks/, playlists, etc.)
        playlist_id: Playlist ID (used for logging context).
        cookie_file: Optional cookies.txt for Premium quality.
        num_threads: Number of parallel downloads.
    
    Returns:
        DownloadStats with download results.
    
    Behavior:
        1. Get tracks needing download from database (global)
        2. Create Downloader instance
        3. Download all tracks to tracks/ directory
        4. Rebuild ALL playlist links (ensures consistency)
        5. Return statistics
    """
    file_manager = FileManager(output_dir)
    
    tracks = database.get_tracks_needing_download()
    
    if not tracks:
        logger.info("No tracks to download")
    else:
        logger.info(f"Found {len(tracks)} tracks to download")
        
        downloader = Downloader(
            database=database,
            output_dir=output_dir,
            cookie_file=cookie_file,
            num_threads=num_threads
        )
        
        stats = downloader.download_tracks(tracks, playlist_id, num_threads)
    
    # Always rebuild all playlist links at the end
    # This ensures consistency even for tracks that were already downloaded
    # but added to new playlists
    logger.info("Rebuilding playlist links...")
    _rebuild_all_playlist_links(database, file_manager)
    
    if not tracks:
        return DownloadStats(total=0)
    return stats


def _rebuild_all_playlist_links(database: Database, file_manager: FileManager) -> None:
    """
    Rebuild hard links for ALL playlists from database.
    
    This ensures that every downloaded track has links in all playlists
    that contain it, regardless of when it was downloaded or added.
    
    Args:
        database: Database instance.
        file_manager: FileManager instance.
    """
    # Get all playlists
    playlists = database.get_all_playlists()
    
    for playlist in playlists:
        playlist_id = playlist["spotify_id"]
        playlist_name = playlist.get("name", "Unknown")
        
        # Get all tracks for this playlist that are downloaded
        tracks = database.get_playlist_tracks_for_export(playlist_id)
        
        if not tracks:
            continue
        
        # Rebuild playlist directory
        playlist_dir = file_manager.get_playlist_dir(playlist_name)
        
        # Remove all existing links in playlist dir
        for file in playlist_dir.iterdir():
            if file.is_file() or file.is_symlink():
                if file.suffix.lower() == ".m4a":
                    file.unlink()
        
        # Create fresh links for all tracks
        created = 0
        for track in tracks:
            file_path = track.get("file_path")
            if not file_path:
                continue
            
            canonical_path = Path(file_path)
            if not canonical_path.exists():
                continue
            
            try:
                file_manager.create_playlist_link(
                    canonical_path=canonical_path,
                    playlist_name=playlist_name,
                    position=track["position"],
                    title=track["name"],
                    artist=track["artist"]
                )
                created += 1
            except Exception as e:
                logger.debug(f"Failed to create link for {track['name']}: {e}")
        
        logger.debug(f"Rebuilt {created} links for '{playlist_name}'")


def get_tracks_needing_download(database: Database, playlist_id: str | None = None) -> list[dict[str, Any]]:
    """
    Get tracks from database that need downloading.
    
    Convenience function for getting tracks to process in PHASE 3
    when running phases separately.
    
    Args:
        database: Database instance.
        playlist_id: Playlist ID (currently unused - downloads are global).
    
    Returns:
        List of track data dicts for tracks with youtube_url set
        but downloaded=False.
    """
    return database.get_tracks_needing_download()