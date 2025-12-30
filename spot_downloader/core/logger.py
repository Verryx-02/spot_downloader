"""
Logging configuration for spot-downloader.

This module sets up the logging system with multiple outputs:
    - Console: Real-time progress with tqdm-compatible formatting
    - log_full.log: Complete log of all events (DEBUG and above)
    - log_errors.log: Only ERROR and CRITICAL level messages
    - download_failures.log: Failed download track names with Spotify URLs
    - lyrics_failures.log: Tracks where lyrics were not found
    - embed_failures.log: Tracks where metadata embedding failed
    - match_close_alternatives.log: Matches with close alternatives for verification

The logging system follows the principle: everything to screen is also saved
to file, then filtered into specialized files.

Log File Locations:
    All log files are created in the output directory specified in config.yaml.
    Files are overwritten on each run (no rotation).

Usage:
    from spot_downloader.core.logger import setup_logging, get_logger
    
    setup_logging(output_dir)  # Call once at startup
    logger = get_logger(__name__)  # Get logger for each module
    
    logger.info("Starting download")
    logger.error("Download failed", extra={'track': 'Song Name', 'url': 'spotify:...'})
"""

import logging
import sys
from pathlib import Path
from typing import TextIO
from datetime import datetime

from rich import get_console


# Log file names (created in output directory)
LOG_FULL_FILENAME = "log_full.log"
LOG_ERRORS_FILENAME = "log_errors.log"
DOWNLOAD_FAILURES_FILENAME = "download_failures.log"
LYRICS_FAILURES_FILENAME = "lyrics_failures.log"
EMBED_FAILURES_FILENAME = "embed_failures.log"
MATCH_CLOSE_ALTERNATIVES_FILENAME = "match_close_alternatives.log"


# Log format for file output (detailed with timestamp)
FILE_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Log format for console output (compact, tqdm-friendly)
CONSOLE_LOG_FORMAT = "%(levelname)s: %(message)s"

# ANSI color codes
class Colors:
    """ANSI color codes for terminal output."""
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BOLD = "\033[1m"


class ColoredConsoleFormatter(logging.Formatter):
    """
    Custom formatter that adds colors to console output.
    
    Colors:
        - DEBUG: Blue
        - INFO: Green
        - WARNING: Yellow
        - ERROR: Red
        - CRITICAL: Bold Red
    """
    
    LEVEL_COLORS = {
        logging.DEBUG: Colors.BLUE,
        logging.INFO: Colors.GREEN,
        logging.WARNING: Colors.YELLOW,
        logging.ERROR: Colors.RED,
        logging.CRITICAL: Colors.BOLD + Colors.RED,
    }
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format the log record with colors.
        
        Args:
            record: The log record to format.
        
        Returns:
            Formatted string with ANSI color codes.
        """
        # Get color for this level
        color = self.LEVEL_COLORS.get(record.levelno, Colors.WHITE)
        
        # Format the level name with color
        colored_levelname = f"{color}{record.levelname}{Colors.RESET}"
        
        # Build the message
        message = f"{colored_levelname}: {record.getMessage()}"
        
        return message


class RichLoggingHandler(logging.Handler):
    """
    Custom logging handler that writes to console without breaking Rich progress bars.
    
    Rich progress bars use the console and update in-place.
    Standard logging to stderr can interfere with this, causing visual glitches.
    This handler uses rich.console.print() which properly coordinates with active progress bars.
    
    Behavior:
        - Formats the log record using the handler's formatter
        - Writes using rich.console.print() to avoid progress bar corruption
        - Messages appear above any active progress bars
    
    Example:
        handler = RichLoggingHandler()
        handler.setFormatter(ColoredConsoleFormatter())
        logger.addHandler(handler)
    """
    
    def __init__(self) -> None:
        """
        Initialize the Rich-compatible handler.
        """
        super().__init__()
        self._console = get_console()
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Emit a log record using rich.console.print() for proper progress bar compatibility.
        
        Args:
            record: The log record to emit.
        
        Behavior:
            1. Format the record using the handler's formatter
            2. Write to console using rich.console.print()
            3. Handle any exceptions by calling handleError()
        
        Thread Safety:
            This method is thread-safe as rich.console handles synchronization.
        """
        try:
            msg = self.format(record)
            self._console.print(msg, highlight=False, markup=False)
        except Exception:
            self.handleError(record)


class DownloadFailedTrackHandler(logging.Handler):
    """
    Custom handler that captures download failures for the download report file.
    
    This handler listens for log records that contain track download failure
    information and writes them to download_failures.log in a simple,
    human-readable format:
    
        42-Song Title-Artist Name.m4a
        https://open.spotify.com/track/xxxxx
        
        43-Another Song-Another Artist.m4a
        https://open.spotify.com/track/yyyyy
    
    The handler looks for specific extra fields in log records:
        - 'download_failed_track_name': The name of the track that failed
        - 'download_failed_track_artist': The artist name
        - 'download_failed_track_url': The Spotify URL
        - 'download_failed_track_number': The assigned track number (optional)
    
    Only records containing these fields are written to the report.
    
    Attributes:
        report_path: Path to the download_failures.log file.
        report_file: Open file handle (opened lazily on first write).
    
    Usage:
        logger.error(
            "Download failed for track",
            extra={
                'download_failed_track_name': 'Song Title',
                'download_failed_track_artist': 'Artist Name',
                'download_failed_track_url': 'https://open.spotify.com/track/xxx',
                'download_failed_track_number': 42
            }
        )
    """

    
    def __init__(self, report_path: Path) -> None:
        """
        Initialize the failed track handler.
        
        Args:
            report_path: Path to the report.txt file.
                         File will be created/overwritten.
        
        Raises:
            IOError: If the file cannot be opened for writing.
        """
        super().__init__()
        self.report_path = report_path
        self.report_file: TextIO | None = None
    
    def open(self) -> None:
        """
        Open the report file for writing.
        
        Called by setup_logging() after handler is created.
        File is opened in write mode (overwrites existing content).
        """
        self.report_file = open(self.report_path, "w", encoding="utf-8")
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Write failed track info to report if present in the log record.
        
        Args:
            record: The log record to check and potentially write.
        
        Behavior:
            1. Check if record has 'download_failed_track_name' attribute
            2. If not present, ignore the record (return immediately)
            3. If present, extract track info and write to report file
            4. Format: "{number}-{track_name}-{artist}.m4a\\nSpotify URL\\n\\n"
               If number is None, omit it from the filename.
        
        Thread Safety:
            Writes are NOT automatically thread-safe. The file should be
            protected by a lock if multiple threads may log simultaneously.
        """
        # Check if this record has failed track info
        if not hasattr(record, "download_failed_track_name"):
            return
        
        if self.report_file is None:
            return
        
        try:
            track_name = getattr(record, "download_failed_track_name", "Unknown")
            artist = getattr(record, "download_failed_track_artist", "Unknown")
            url = getattr(record, "download_failed_track_url", "")
            number = getattr(record, "download_failed_track_number", None)
            
            # Format filename
            if number is not None:
                filename = f"{number}-{track_name}-{artist}.m4a"
            else:
                filename = f"{track_name}-{artist}.m4a"
            
            self.report_file.write(f"{filename}\n")
            self.report_file.write(f"{url}\n\n")
            self.report_file.flush()
        except Exception:
            self.handleError(record)
    
    def close(self) -> None:
        """
        Close the report file handle.
        
        Called automatically when logging is shut down.
        Safe to call multiple times.
        """
        if self.report_file is not None:
            try:
                self.report_file.close()
            except Exception:
                pass
            self.report_file = None
        super().close()


class LyricsFailedTrackHandler(logging.Handler):
    """
    Custom handler that captures lyrics fetch failures for the lyrics report file.
    
    This handler listens for log records that contain lyrics failure information
    and writes them to lyrics_failures.log in a simple, human-readable format:
    
        42-Song Title-Artist Name.m4a
        https://open.spotify.com/track/xxxxx
        
        43-Another Song-Another Artist.m4a
        https://open.spotify.com/track/yyyyy
    
    The handler looks for specific extra fields in log records:
        - 'lyrics_failed_track_name': The name of the track
        - 'lyrics_failed_track_artist': The artist name
        - 'lyrics_failed_track_url': The Spotify URL
        - 'lyrics_failed_track_number': The assigned track number (optional)
    
    Only records containing these fields are written to the lyrics report.
    
    Attributes:
        report_path: Path to the lyrics_failures.log file.
        report_file: Open file handle (opened lazily on first write).
    
    Usage:
        logger.warning(
            "No lyrics found for track",
            extra={
                'lyrics_failed_track_name': 'Song Title',
                'lyrics_failed_track_artist': 'Artist Name',
                'lyrics_failed_track_url': 'https://open.spotify.com/track/xxx',
                'lyrics_failed_track_number': 42
            }
        )
    """
    
    def __init__(self, report_path: Path) -> None:
        """
        Initialize the lyrics failed track handler.
        
        Args:
            report_path: Path to the lyrics_failures.log file.
                         File will be created/overwritten.
        
        Raises:
            IOError: If the file cannot be opened for writing.
        """
        super().__init__()
        self.report_path = report_path
        self.report_file: TextIO | None = None
    
    def open(self) -> None:
        """
        Open the report file for writing.
        
        Called by setup_logging() after handler is created.
        File is opened in write mode (overwrites existing content).
        """
        self.report_file = open(self.report_path, "w", encoding="utf-8")
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Write lyrics failed track info to report if present in the log record.
        
        Args:
            record: The log record to check and potentially write.
        
        Behavior:
            1. Check if record has 'lyrics_failed_track_name' attribute
            2. If not present, ignore the record (return immediately)
            3. If present, extract track info and write to report file
            4. Format: "{number}-{track_name}-{artist}.m4a\n{spotify_url}\n\n"
               If number is None, use "??" as placeholder.
        
        Thread Safety:
            Writes are NOT automatically thread-safe. The file should be
            protected by a lock if multiple threads may log simultaneously.
        """
        # Check if this record has lyrics failed track info
        if not hasattr(record, "lyrics_failed_track_name"):
            return
        
        if self.report_file is None:
            return
        
        try:
            track_name = getattr(record, "lyrics_failed_track_name", "Unknown")
            artist = getattr(record, "lyrics_failed_track_artist", "Unknown")
            url = getattr(record, "lyrics_failed_track_url", "")
            number = getattr(record, "lyrics_failed_track_number", None)
            
            # Format filename
            if number is not None:
                filename = f"{number}-{track_name}-{artist}.m4a"
            else:
                filename = f"??-{track_name}-{artist}.m4a"
            
            self.report_file.write(f"{filename}\n")
            self.report_file.write(f"{url}\n\n")
            self.report_file.flush()
        except Exception:
            self.handleError(record)
    
    def close(self) -> None:
        """
        Close the report file handle.
        
        Called automatically when logging is shut down.
        Safe to call multiple times.
        """
        if self.report_file is not None:
            try:
                self.report_file.close()
            except Exception:
                pass
            self.report_file = None
        super().close()


class EmbedFailedTrackHandler(logging.Handler):
    """
    Custom handler that captures metadata embedding failures for the embed report file.
    
    This handler listens for log records that contain embedding failure information
    and writes them to embed_failures.log in a simple, human-readable format:
    
        42-Song Title-Artist Name.m4a
        https://open.spotify.com/track/xxxxx
        Error: File not found
        
        43-Another Song-Another Artist.m4a
        https://open.spotify.com/track/yyyyy
        Error: Cover art download failed
    
    The handler looks for specific extra fields in log records:
        - 'embed_failed_track_name': The name of the track
        - 'embed_failed_track_artist': The artist name
        - 'embed_failed_track_url': The Spotify URL
        - 'embed_failed_error': The error message
        - 'embed_failed_track_number': The assigned track number (optional)
    
    Only records containing these fields are written to the embed report.
    
    Attributes:
        report_path: Path to the embed_failures.log file.
        report_file: Open file handle (opened lazily on first write).
    
    Usage:
        logger.error(
            "Embedding failed for track",
            extra={
                'embed_failed_track_name': 'Song Title',
                'embed_failed_track_artist': 'Artist Name',
                'embed_failed_track_url': 'https://open.spotify.com/track/xxx',
                'embed_failed_error': 'File not found',
                'embed_failed_track_number': 42
            }
        )
    """
    
    def __init__(self, report_path: Path) -> None:
        """
        Initialize the embed failed track handler.
        
        Args:
            report_path: Path to the embed_failures.log file.
                         File will be created/overwritten.
        
        Raises:
            IOError: If the file cannot be opened for writing.
        """
        super().__init__()
        self.report_path = report_path
        self.report_file: TextIO | None = None
    
    def open(self) -> None:
        """
        Open the report file for writing.
        
        Called by setup_logging() after handler is created.
        File is opened in write mode (overwrites existing content).
        """
        self.report_file = open(self.report_path, "w", encoding="utf-8")
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Write embed failed track info to report if present in the log record.
        
        Args:
            record: The log record to check and potentially write.
        
        Behavior:
            1. Check if record has 'embed_failed_track_name' attribute
            2. If not present, ignore the record (return immediately)
            3. If present, extract track info and write to report file
            4. Format includes filename, URL, and error message
               If number is None, use "??" as placeholder.
        
        Thread Safety:
            Writes are NOT automatically thread-safe. The file should be
            protected by a lock if multiple threads may log simultaneously.
        """
        # Check if this record has embed failed track info
        if not hasattr(record, "embed_failed_track_name"):
            return
        
        if self.report_file is None:
            return
        
        try:
            track_name = getattr(record, "embed_failed_track_name", "Unknown")
            artist = getattr(record, "embed_failed_track_artist", "Unknown")
            url = getattr(record, "embed_failed_track_url", "")
            error = getattr(record, "embed_failed_error", "Unknown error")
            number = getattr(record, "embed_failed_track_number", None)
            
            # Format filename
            if number is not None:
                filename = f"{number}-{track_name}-{artist}.m4a"
            else:
                filename = f"??-{track_name}-{artist}.m4a"
            
            self.report_file.write(f"{filename}\n")
            self.report_file.write(f"{url}\n")
            self.report_file.write(f"Error: {error}\n\n")
            self.report_file.flush()
        except Exception:
            self.handleError(record)
    
    def close(self) -> None:
        """
        Close the report file handle.
        
        Called automatically when logging is shut down.
        Safe to call multiple times.
        """
        if self.report_file is not None:
            try:
                self.report_file.close()
            except Exception:
                pass
            self.report_file = None
        super().close()


class MatchCloseAlternativesHandler(logging.Handler):
    """
    Custom handler that captures matches with close alternatives for review.
    
    This handler listens for log records that contain match alternative
    information and writes them to match_close_alternatives.log in a
    human-readable format for user verification:
    
        42-Song Title-Artist Name.m4a
        Spotify: Song Title https://open.spotify.com/track/xxxxx
        Selected: Song Title (Official Audio) https://music.youtube.com/watch?v=yyyyy (score: 87.5)
        Alternatives:
          - Song Title (Acoustic) https://music.youtube.com/watch?v=zzzzz (score: 85.2)
          - Song Title (Live) https://www.youtube.com/watch?v=wwwww (score: 83.1)
        Multiple close matches found. Verify if correct.
        
    The handler looks for specific extra fields in log records:
        - 'match_alt_track_name': The name of the track (Spotify)
        - 'match_alt_track_artist': The artist name (Spotify)
        - 'match_alt_spotify_url': The Spotify URL
        - 'match_alt_youtube_url': The selected YouTube URL
        - 'match_alt_youtube_title': The title of the selected YouTube match
        - 'match_alt_score': The score of the selected match
        - 'match_alt_alternatives': List of (title, url, score) tuples for alternatives
        - 'match_alt_track_number': The assigned track number (optional)
    
    Only records containing these fields are written to the report.
    
    Attributes:
        report_path: Path to the match_close_alternatives.log file.
        report_file: Open file handle (opened lazily on first write).
    
    Usage:
        logger.warning(
            "Multiple close matches found",
            extra={
                'match_alt_track_name': 'Song Title',
                'match_alt_track_artist': 'Artist Name',
                'match_alt_spotify_url': 'https://open.spotify.com/track/xxx',
                'match_alt_youtube_url': 'https://music.youtube.com/watch?v=yyy',
                'match_alt_youtube_title': 'Song Title (Official Audio)',
                'match_alt_score': 87.5,
                'match_alt_alternatives': [
                    ('Song Title (Acoustic)', 'https://music.youtube.com/watch?v=zzz', 85.2),
                    ('Song Title (Live)', 'https://www.youtube.com/watch?v=www', 83.1),
                ],
                'match_alt_track_number': 42
            }
        )
    """
    
    def __init__(self, report_path: Path) -> None:
        """
        Initialize the match close alternatives handler.
        
        Args:
            report_path: Path to the match_close_alternatives.log file.
                         File will be created/overwritten.
        
        Raises:
            IOError: If the file cannot be opened for writing.
        """
        super().__init__()
        self.report_path = report_path
        self.report_file: TextIO | None = None
    
    def open(self) -> None:
        """
        Open the report file for writing.
        
        Called by setup_logging() after handler is created.
        File is opened in write mode (overwrites existing content).
        """
        self.report_file = open(self.report_path, "w", encoding="utf-8")
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Write match close alternatives info to report if present in the log record.
        
        Args:
            record: The log record to check and potentially write.
        
        Behavior:
            1. Check if record has 'match_alt_track_name' attribute
            2. If not present, ignore the record (return immediately)
            3. If present, extract match info and write to report file
        
        Thread Safety:
            Writes are NOT automatically thread-safe. The file should be
            protected by a lock if multiple threads may log simultaneously.
        """
        # Check if this record has match alternatives info
        if not hasattr(record, "match_alt_track_name"):
            return
        
        if self.report_file is None:
            return
        
        try:
            track_name = getattr(record, "match_alt_track_name", "Unknown")
            artist = getattr(record, "match_alt_track_artist", "Unknown")
            spotify_url = getattr(record, "match_alt_spotify_url", "")
            youtube_url = getattr(record, "match_alt_youtube_url", "")
            youtube_title = getattr(record, "match_alt_youtube_title", "")
            score = getattr(record, "match_alt_score", 0.0)
            alternatives = getattr(record, "match_alt_alternatives", [])
            number = getattr(record, "match_alt_track_number", None)
            
            # Format filename
            if number is not None:
                filename = f"{number}-{track_name}-{artist}.m4a"
            else:
                filename = f"??-{track_name}-{artist}.m4a"
            
            self.report_file.write(f"{filename}\n")
            self.report_file.write(f"Spotify: {track_name} {spotify_url}\n")
            self.report_file.write(f"Selected: {youtube_title} {youtube_url} (score: {score:.1f})\n")
            
            if alternatives:
                self.report_file.write("Alternatives:\n")
                for alt_title, alt_url, alt_score in alternatives:
                    self.report_file.write(f"  - {alt_title} {alt_url} (score: {alt_score:.1f})\n")
            
            self.report_file.write("Multiple close matches found. Verify if correct.\n\n")
            self.report_file.flush()
        except Exception:
            self.handleError(record)
    
    def close(self) -> None:
        """
        Close the report file handle.
        
        Called automatically when logging is shut down.
        Safe to call multiple times.
        """
        if self.report_file is not None:
            try:
                self.report_file.close()
            except Exception:
                pass
            self.report_file = None
        super().close()


class ErrorOnlyFilter(logging.Filter):
    """
    Filter that only allows ERROR and CRITICAL level records.
    
    Used by the error log file handler to exclude DEBUG, INFO, and WARNING.
    
    Example:
        handler.addFilter(ErrorOnlyFilter())
    """
    
    def filter(self, record: logging.LogRecord) -> bool:
        """
        Check if the record should be logged.
        
        Args:
            record: The log record to check.
        
        Returns:
            True if record.levelno >= ERROR (40), False otherwise.
        """
        return record.levelno >= logging.ERROR


def setup_logging(output_dir: Path) -> None:
    """
    Configure the logging system for the application.
    
    This function should be called ONCE at application startup, after
    the configuration is loaded but before any other operations.
    
    Args:
        output_dir: Directory where log files will be created.
                    Logs are stored in logs/{timestamp}/ subdirectory.
    
    Behavior:
        1. Create output_dir/logs/{timestamp} directory
        2. Configure root logger level to DEBUG
        3. Create and configure all handlers with appropriate paths
    
    File Handling:
        - Each run creates a new timestamped subdirectory
        - Files use UTF-8 encoding
        - Files are closed automatically on program exit
    
    Thread Safety:
        This function is NOT thread-safe. Call it once from the main
        thread before starting any worker threads.
    """
    # Generate timestamp for this run
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    # Create timestamped logs subdirectory
    logs_dir = output_dir / "logs" / timestamp
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Remove any existing handlers
    root_logger.handlers.clear()
    
    # Console handler (Rich-compatible) with colors
    console_handler = RichLoggingHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredConsoleFormatter())
    root_logger.addHandler(console_handler)
    
    # Full log file handler
    full_log_path = logs_dir / "log_full.log"
    full_handler = logging.FileHandler(full_log_path, mode="w", encoding="utf-8")
    full_handler.setLevel(logging.DEBUG)
    full_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT, FILE_DATE_FORMAT))
    root_logger.addHandler(full_handler)
    
    # Error-only log file handler
    error_log_path = logs_dir / "log_errors.log"
    error_handler = logging.FileHandler(error_log_path, mode="w", encoding="utf-8")
    error_handler.setLevel(logging.DEBUG)  # Filter handles the level restriction
    error_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT, FILE_DATE_FORMAT))
    error_handler.addFilter(ErrorOnlyFilter())
    root_logger.addHandler(error_handler)
    
    # Download failures handler
    download_failures_path = logs_dir / "download_failures.log"
    download_handler = DownloadFailedTrackHandler(download_failures_path)
    download_handler.open()
    root_logger.addHandler(download_handler)
    
    # Lyrics failures handler
    lyrics_failures_path = logs_dir / "lyrics_failures.log"
    lyrics_handler = LyricsFailedTrackHandler(lyrics_failures_path)
    lyrics_handler.open()
    root_logger.addHandler(lyrics_handler)
    
    # Embed failures handler
    embed_failures_path = logs_dir / "embed_failures.log"
    embed_handler = EmbedFailedTrackHandler(embed_failures_path)
    embed_handler.open()
    root_logger.addHandler(embed_handler)
    
    # Match close alternatives handler
    match_alt_path = logs_dir / "match_close_alternatives.log"
    match_alt_handler = MatchCloseAlternativesHandler(match_alt_path)
    match_alt_handler.open()
    root_logger.addHandler(match_alt_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.
    
    This is a convenience wrapper around logging.getLogger() that ensures
    consistent logger naming throughout the application.
    
    Args:
        name: The logger name, typically __name__ of the calling module.
              This creates a hierarchy like 'spot_downloader.core.config'.
    
    Returns:
        logging.Logger: A logger instance configured by setup_logging().
    
    Example:
        # In any module:
        from spot_downloader.core.logger import get_logger
        
        logger = get_logger(__name__)
        logger.info("Module initialized")
        logger.error("Something failed", extra={'detail': 'value'})
    
    Note:
        Loggers obtained before setup_logging() is called will have no
        handlers and will not produce output. Always call setup_logging()
        first during application startup.
    """
    return logging.getLogger(name)


def format_matched_message(artist: str, name: str, url: str) -> str:
    """
    Format a 'Matched' message with colors.
    
    Args:
        artist: Artist name.
        name: Track name.
        url: YouTube URL.
    
    Returns:
        Colored message string.
    """
    return (
        f"{Colors.GREEN}Matched{Colors.RESET}: "
        f"{artist} - {name} -> "
        f"{Colors.CYAN}{url}{Colors.RESET}"
    )


def format_close_matches_message(name: str, artist: str, score: float) -> str:
    """
    Format a 'Multiple close matches' warning message with colors.
    
    Args:
        name: Track name.
        artist: Artist name.
        score: Selected match score.
    
    Returns:
        Colored message string.
    """
    return (
        f"{Colors.YELLOW}Multiple close matches{Colors.RESET} for: "
        f"{artist} - {name} "
        f"(selected score: {Colors.YELLOW}{score:.1f}{Colors.RESET})"
    )


def format_no_match_message(artist: str, name: str, reason: str) -> str:
    """
    Format a 'No match' error message with colors.
    
    Args:
        artist: Artist name.
        name: Track name.
        reason: Failure reason.
    
    Returns:
        Colored message string.
    """
    return (
        f"{Colors.RED}No match{Colors.RESET}: "
        f"{artist} - {name} "
        f"({reason})"
    )


def format_progress_message(completed: int, total: int, matched: int, failed: int) -> str:
    """
    Format a progress message with colors.
    
    Args:
        completed: Number of completed tracks.
        total: Total number of tracks.
        matched: Number of matched tracks.
        failed: Number of failed tracks.
    
    Returns:
        Formatted message string.
    """
    return (
        f"{Colors.MAGENTA}{Colors.BOLD}Progress{Colors.RESET}: "
        f"{completed}/{total} "
        f"(matched: {Colors.GREEN}{matched}{Colors.RESET}, "
        f"failed: {Colors.RED}{failed}{Colors.RESET})"
    )


def log_download_failure(
    logger: logging.Logger,
    track_name: str,
    artist: str,
    spotify_url: str,
    error_message: str,
    assigned_number: int | None = None
) -> None:
    """
    Log a track whose download failed.
    
    This is a convenience function that logs a download failure with the
    correct extra fields for the DownloadFailedTrackHandler to pick up.
    
    Args:
        logger: The logger to use for the message.
        track_name: The name of the track that failed.
        artist: The artist name.
        spotify_url: The Spotify URL for the track.
        error_message: Description of why the download failed.
        assigned_number: Track number for filename display.
    
    Behavior:
        Logs an ERROR level message with the error_message, and attaches
        extra fields that DownloadFailedTrackHandler will use to write
        to download_failures.log.
    
    Example:
        log_download_failure(
            logger,
            track_name="Song Title",
            artist="Artist Name",
            spotify_url="https://open.spotify.com/track/xxx",
            error_message="Video unavailable in your country",
            assigned_number=42
        )
    """
    logger.error(
        f"Download failed: {track_name} - {error_message}",
        extra={
            "download_failed_track_name": track_name,
            "download_failed_track_artist": artist,
            "download_failed_track_url": spotify_url,
            "download_failed_track_number": assigned_number,
        }
    )


def log_lyrics_failure(
    logger: logging.Logger,
    track_name: str,
    artist: str,
    spotify_url: str,
    assigned_number: int | None = None
) -> None:
    """
    Log a track whose lyrics could not be retrieved.
    
    This is a convenience function that logs a lyrics failure with the
    correct extra fields for the LyricsFailedTrackHandler to pick up.
    
    Args:
        logger: The logger to use for the message.
        track_name: The name of the track.
        artist: The artist name.
        spotify_url: The Spotify URL for the track.
        assigned_number: Track number for filename display.
    
    Behavior:
        Logs a WARNING level message and attaches extra fields that
        LyricsFailedTrackHandler will use to write to lyrics_failures.log.
    
    Note:
        This should be called for ALL tracks where lyrics were not found,
        regardless of the reason (not available, provider error, etc.).
    
    Example:
        log_lyrics_failure(
            logger,
            track_name="Instrumental Track",
            artist="Artist Name",
            spotify_url="https://open.spotify.com/track/xxx",
            assigned_number=42
        )
        
        # This will:
        # 1. Log "No lyrics found for: Instrumental Track" to console/log_full.log
        # 2. Write to lyrics_failures.log:
        #    "42-Instrumental Track-Artist Name.m4a
        #     https://open.spotify.com/track/xxx"
    """
    logger.warning(
        f"No lyrics found for: {track_name}",
        extra={
            "lyrics_failed_track_name": track_name,
            "lyrics_failed_track_artist": artist,
            "lyrics_failed_track_url": spotify_url,
            "lyrics_failed_track_number": assigned_number,
        }
    )


def log_embed_failure(
    logger: logging.Logger,
    track_name: str,
    artist: str,
    spotify_url: str,
    error_message: str,
    assigned_number: int | None = None
) -> None:
    """
    Log a track whose metadata embedding failed.
    
    This is a convenience function that logs an embedding failure with the
    correct extra fields for the EmbedFailedTrackHandler to pick up.
    
    Args:
        logger: The logger to use for the message.
        track_name: The name of the track.
        artist: The artist name.
        spotify_url: The Spotify URL for the track.
        error_message: Description of why the embedding failed.
        assigned_number: Track number for filename display.
    
    Behavior:
        Logs an ERROR level message and attaches extra fields that
        EmbedFailedTrackHandler will use to write to embed_failures.log.
    
    Example:
        log_embed_failure(
            logger,
            track_name="Song Title",
            artist="Artist Name",
            spotify_url="https://open.spotify.com/track/xxx",
            error_message="File not found",
            assigned_number=42
        )
        
        # This will:
        # 1. Log "Embedding failed: Song Title - File not found" to console/log_full.log
        # 2. Write to embed_failures.log:
        #    "42-Song Title-Artist Name.m4a
        #     https://open.spotify.com/track/xxx
        #     Error: File not found"
    """
    logger.error(
        f"Embedding failed: {track_name} - {error_message}",
        extra={
            "embed_failed_track_name": track_name,
            "embed_failed_track_artist": artist,
            "embed_failed_track_url": spotify_url,
            "embed_failed_error": error_message,
            "embed_failed_track_number": assigned_number,
        }
    )


def log_match_close_alternatives(
    logger: logging.Logger,
    track_name: str,
    artist: str,
    spotify_url: str,
    youtube_url: str,
    youtube_title: str,
    score: float,
    alternatives: list[tuple[str, str, float]],
    assigned_number: int | None = None
) -> None:
    """
    Log a track match that has close alternatives requiring verification.
    
    This is a convenience function that logs match alternatives with the
    correct extra fields for the MatchCloseAlternativesHandler to pick up.
    
    Should be called when the matching algorithm finds multiple YouTube
    results with scores within CLOSE_MATCH_THRESHOLD of each other.
    
    Args:
        logger: The logger to use for the message.
        track_name: The name of the track (Spotify).
        artist: The artist name (Spotify).
        spotify_url: The Spotify URL for the track.
        youtube_url: The YouTube URL of the selected match.
        youtube_title: The title of the selected YouTube match.
        score: The score of the selected match.
        alternatives: List of (youtube_title, youtube_url, score) tuples for close alternatives.
                     These are matches within CLOSE_MATCH_THRESHOLD of the best.
        assigned_number: Track number for filename display.
    
    Behavior:
        Logs a WARNING level message and attaches extra fields that
        MatchCloseAlternativesHandler will use to write to
        match_close_alternatives.log.
    
    Note:
        This should only be called when there are actual close alternatives,
        not for every successful match.
    
    Example:
        log_match_close_alternatives(
            logger,
            track_name="Song Title",
            artist="Artist Name",
            spotify_url="https://open.spotify.com/track/xxx",
            youtube_url="https://music.youtube.com/watch?v=yyy",
            youtube_title="Song Title (Official Audio)",
            score=87.5,
            alternatives=[
                ("Song Title (Acoustic)", "https://music.youtube.com/watch?v=zzz", 85.2),
                ("Song Title (Live)", "https://www.youtube.com/watch?v=www", 83.1),
            ],
            assigned_number=42
        )
        
        # This will write to match_close_alternatives.log:
        # 42-Song Title-Artist Name.m4a
        # Spotify: Song Title https://open.spotify.com/track/xxx
        # Selected: Song Title (Official Audio) https://music.youtube.com/watch?v=yyy (score: 87.5)
        # Alternatives:
        #   - Song Title (Acoustic) https://music.youtube.com/watch?v=zzz (score: 85.2)
        #   - Song Title (Live) https://www.youtube.com/watch?v=www (score: 83.1)
        # Multiple close matches found. Verify if correct.
    """
    logger.debug(
        f"Multiple close matches for: {track_name} (selected score: {score:.1f})",
        extra={
            "match_alt_track_name": track_name,
            "match_alt_track_artist": artist,
            "match_alt_spotify_url": spotify_url,
            "match_alt_youtube_url": youtube_url,
            "match_alt_youtube_title": youtube_title,
            "match_alt_score": score,
            "match_alt_alternatives": alternatives,
            "match_alt_track_number": assigned_number,
        }
    )


def shutdown_logging() -> None:
    """
    Properly shut down the logging system.
    
    This function should be called at application exit to ensure all
    log handlers are properly flushed and closed.
    
    Behavior:
        1. Flush all handlers
        2. Close all file handlers
        3. Remove all handlers from root logger
    
    Note:
        This is typically called in a finally block or atexit handler.
        After calling this function, logging will no longer work.
    """
    root_logger = logging.getLogger()
    
    # Flush and close all handlers
    for handler in root_logger.handlers[:]:
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        root_logger.removeHandler(handler)
