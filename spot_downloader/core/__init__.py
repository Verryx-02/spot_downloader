"""
Core module for spot-downloader.

This module provides the foundational components used throughout the application:
    - exceptions: Custom exception classes for error handling
    - config: Configuration loading and validation
    - database: Thread-safe SQLite database for persistent storage
    - file_manager: File storage with hard links for playlist views
    - logger: Logging system with multiple outputs

Usage:
    from spot_downloader.core import (
        Config, load_config,
        Database, FileManager,
        setup_logging, get_logger,
        SpotDownloaderError, ConfigError, DatabaseError
    )
"""

from spot_downloader.core.config import (
    Config,
    DownloadConfig,
    OutputConfig,
    SpotifyConfig,
    load_config,
)
from spot_downloader.core.database import Database, LIKED_SONGS_KEY, YOUTUBE_MATCH_FAILED
from spot_downloader.core.file_manager import FileManager, sanitize_filename
from spot_downloader.core.exceptions import (
    ConfigError,
    DatabaseError,
    DownloadError,
    LyricsError,
    MetadataError,
    SpotDownloaderError,
    SpotifyError,
    YouTubeError,
)
from spot_downloader.core.logger import (
    get_logger,
    log_download_failure,
    log_embed_failure,
    log_lyrics_failure,
    setup_logging,
    shutdown_logging,
)

from spot_downloader.core.progress import MatchingProgressBar

__all__ = [
    # Config
    "Config",
    "SpotifyConfig",
    "OutputConfig",
    "DownloadConfig",
    "load_config",
    # Database
    "Database",
    "LIKED_SONGS_KEY",
    "YOUTUBE_MATCH_FAILED",
    # File Manager
    "FileManager",
    "sanitize_filename",
    # Exceptions
    "SpotDownloaderError",
    "ConfigError",
    "DatabaseError",
    "SpotifyError",
    "YouTubeError",
    "DownloadError",
    "MetadataError",
    "LyricsError",
    # Logger
    "setup_logging",
    "get_logger",
    "log_download_failure",
    "log_embed_failure",
    "log_lyrics_failure",
    "shutdown_logging",
    "MatchingProgressBar",
]