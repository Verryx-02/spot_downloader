"""
Configuration management for spot-downloader.

This module handles loading, validating, and providing access to the
application configuration stored in config.yaml.

The configuration file contains:
    - Spotify API credentials (client_id, client_secret)
    - Output directory for downloaded files
    - Export directory for portable playlist exports
    - Thread counts for matching and downloading phases
    - Optional cookie file path for YouTube Premium quality

Configuration File Location:
    The config.yaml file must be in the current working directory
    when running the application.

Example config.yaml:
    spotify:
      client_id: "your_client_id_here"
      client_secret: "your_client_secret_here"
    
    output:
      directory: "~/Desktop/Music/SpotDownloader"
      export_directory: "~/Desktop/Music/SpotDownloader/export"
    
    download:
      threads:
        matching: 8   # Phase 2: YouTube matching (higher is faster)
        download: 1   # Phase 3: Audio download (must be 1 to avoid rate limiting)
        lyrics: 2     # Phase 4: Lyrics fetching
        embedding: 4  # Phase 5: Metadata embedding
      cookie_file: null  # Optional: path to cookies.txt for YT Premium
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from spot_downloader.core.exceptions import ConfigError


# Default configuration file name (always in current working directory)
CONFIG_FILENAME = "config.yaml"


@dataclass(frozen=True)
class SpotifyConfig:
    """
    Spotify API credentials configuration.
    
    These credentials are obtained from the Spotify Developer Dashboard:
    https://developer.spotify.com/dashboard
    
    Attributes:
        client_id: The Spotify application client ID.
                   A 32-character hexadecimal string.
        client_secret: The Spotify application client secret.
                   A 32-character hexadecimal string.
    """
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OutputConfig:
    """
    Output directory configuration.
    
    Attributes:
        directory: Absolute path to the directory where downloaded files will be saved.
                   Path expansion is performed (~ is expanded to home directory).
                   The directory will be created if it doesn't exist.
        export_directory: Absolute path for --export command output.
                         Defaults to {directory}/export if not specified.
                         Used for portable M3U playlists and file copies.
    """
    directory: Path
    export_directory: Path


@dataclass(frozen=True)
class DownloadConfig:
    """
    Download behavior configuration.
    
    Attributes:
        matching_threads: Number of parallel threads for YouTube matching (Phase 2).
                         Higher values speed up matching but increase API rate limit risk.
                         Recommended range: 4-8. Default: 8.
        download_threads: Number of parallel threads for downloading (Phase 3).
                         Must be 1 to avoid YouTube rate limiting.
                         Default: 1.
        lyrics_threads: Number of parallel threads for lyrics fetching (Phase 4).
                       Recommended range: 1-4. Default: 2.
        embedding_threads: Number of parallel threads for metadata embedding (Phase 5).
                          Higher values speed up cover art downloads.
                          Recommended range: 4-8. Default: 4.
        cookie_file: Optional path to a cookies.txt file exported from browser.
                     Required for YouTube Music Premium quality (256 kbps).
                     Without cookies, downloads are limited to 128 kbps.
                     The file should be exported from music.youtube.com using
                     a browser extension like "Get cookies.txt".
    """
    matching_threads: int
    download_threads: int
    lyrics_threads: int
    embedding_threads: int
    cookie_file: Path | None


@dataclass(frozen=True)
class Config:
    """
    Complete application configuration.
    
    This is the main configuration object that aggregates all configuration
    sections. It is created by load_config() and should be treated as
    immutable (frozen dataclass).
    
    Attributes:
        spotify: Spotify API credentials.
        output: Output directory settings.
        download: Download behavior settings.
    
    Example:
        config = load_config()
        print(f"Saving to: {config.output.directory}")
        print(f"Export to: {config.output.export_directory}")
        print(f"Using {config.download.embedding_threads} embedding threads")
    """
    spotify: SpotifyConfig
    output: OutputConfig
    download: DownloadConfig


def load_config(config_path: Path | None = None) -> Config:
    """
    Load and validate configuration from config.yaml.
    
    This function reads the YAML configuration file, validates all required
    fields are present and have valid values, expands paths, and returns
    a frozen Config object.
    
    Args:
        config_path: Optional explicit path to config file.
                     If None, looks for config.yaml in current working directory.
    
    Returns:
        Config: A frozen dataclass containing all configuration values.
    
    Raises:
        ConfigError: If the config file is not found, has invalid YAML syntax,
                     is missing required fields, or contains invalid values.
                     The error message will indicate the specific problem.
    
    Behavior:
        1. Locate config file (explicit path or CWD/config.yaml)
        2. Read and parse YAML content
        3. Validate structure (required sections exist)
        4. Validate and extract spotify credentials
        5. Validate and expand output directory path
        6. Validate download settings with defaults
        7. Create and return frozen Config object
    
    Example:
        try:
            config = load_config()
        except ConfigError as e:
            print(f"Configuration error: {e.message}")
            sys.exit(1)
    
    Thread Safety:
        This function is NOT thread-safe. It should be called once at
        application startup, before any threads are created.
    """
    # Resolve config path
    if config_path is None:
        config_path = Path.cwd() / CONFIG_FILENAME
    
    # Check file exists
    if not config_path.exists():
        raise ConfigError(
            f"Configuration file not found: {config_path}",
            details={"file_path": str(config_path)}
        )
    
    # Read file content
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
    except IOError as e:
        raise ConfigError(
            f"Failed to read configuration file: {e}",
            details={"file_path": str(config_path), "original_error": str(e)}
        ) from e
    
    # Parse YAML
    try:
        raw_config = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Invalid YAML syntax in configuration file: {e}",
            details={"file_path": str(config_path), "original_error": str(e)}
        ) from e
    
    # Validate raw config is a dictionary
    if not isinstance(raw_config, dict):
        raise ConfigError(
            "Configuration file must contain a YAML dictionary",
            details={"file_path": str(config_path)}
        )
    
    # Validate structure
    _validate_config(raw_config)
    
    # Parse sections
    spotify_config = _parse_spotify_config(raw_config["spotify"])
    output_config = _parse_output_config(raw_config["output"])
    download_config = _parse_download_config(raw_config.get("download"))
    
    return Config(
        spotify=spotify_config,
        output=output_config,
        download=download_config
    )


def _validate_config(raw_config: dict[str, Any]) -> None:
    """
    Validate the raw configuration dictionary structure and values.
    
    This is an internal helper function that checks the configuration
    has all required sections and valid values.
    
    Args:
        raw_config: Dictionary parsed from config.yaml.
    
    Raises:
        ConfigError: If validation fails, with a descriptive message
                     indicating what is missing or invalid.
    """
    required_sections = ["spotify", "output"]
    
    for section in required_sections:
        if section not in raw_config:
            raise ConfigError(
                f"Missing required section: '{section}'",
                details={"missing_section": section}
            )
        
        if not isinstance(raw_config[section], dict):
            raise ConfigError(
                f"Section '{section}' must be a dictionary",
                details={"section": section}
            )


def _parse_spotify_config(spotify_section: dict[str, Any]) -> SpotifyConfig:
    """
    Parse and validate the Spotify configuration section.
    
    Args:
        spotify_section: The 'spotify' section from config.yaml.
    
    Returns:
        SpotifyConfig: Validated Spotify credentials.
    
    Raises:
        ConfigError: If client_id or client_secret is missing or empty.
    """
    client_id = spotify_section.get("client_id", "")
    client_secret = spotify_section.get("client_secret", "")
    
    if not isinstance(client_id, str) or not client_id.strip():
        raise ConfigError(
            "'spotify.client_id' must be a non-empty string",
            details={"field": "spotify.client_id"}
        )
    
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise ConfigError(
            "'spotify.client_secret' must be a non-empty string",
            details={"field": "spotify.client_secret"}
        )
    
    return SpotifyConfig(
        client_id=client_id.strip(),
        client_secret=client_secret.strip()
    )


def _parse_output_config(output_section: dict[str, Any]) -> OutputConfig:
    """
    Parse and validate the output configuration section.
    
    Expands ~ to home directory and converts to absolute Path.
    Does NOT create the directory (that happens at download time).
    
    Args:
        output_section: The 'output' section from config.yaml.
    
    Returns:
        OutputConfig: Validated output configuration with expanded paths.
    
    Raises:
        ConfigError: If directory is missing or empty.
    """
    directory = output_section.get("directory", "")
    
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigError(
            "'output.directory' must be a non-empty string",
            details={"field": "output.directory"}
        )
    
    # Expand ~ and make absolute
    path = Path(directory.strip()).expanduser().resolve()
    
    # Parse export_directory (optional, defaults to directory/export)
    export_dir_raw = output_section.get("export_directory")
    if export_dir_raw is not None:
        if not isinstance(export_dir_raw, str) or not export_dir_raw.strip():
            raise ConfigError(
                "'output.export_directory' must be a non-empty string",
                details={"field": "output.export_directory"}
            )
        export_path = Path(export_dir_raw.strip()).expanduser().resolve()
    else:
        # Default: output_directory/export
        export_path = path / "export"
    
    return OutputConfig(directory=path, export_directory=export_path)


def _parse_download_config(download_section: dict[str, Any] | None) -> DownloadConfig:
    """
    Parse and validate the download configuration section.
    
    Applies defaults if section is missing or fields are not specified.
    
    Args:
        download_section: The 'download' section from config.yaml, or None.
    
    Returns:
        DownloadConfig: Validated download configuration with defaults applied.
                        Default matching_threads: 8
                        Default download_threads: 1
                        Default lyrics_threads: 2
                        Default embedding_threads: 4
                        Default cookie_file: None
    
    Raises:
        ConfigError: If threads values are not positive integers, or if
                     cookie_file path doesn't exist when specified.
    
    Supported formats:
        # New format (recommended):
        download:
          threads:
            matching: 8
            download: 1
            lyrics: 2
            embedding: 4
        
        # Legacy format (uses value for matching and download only):
        download:
          threads: 4
    """
    # Defaults
    matching_threads = 8
    download_threads = 1
    lyrics_threads = 2
    embedding_threads = 4
    cookie_file = None
    
    if download_section is not None:
        # Parse threads (supports both old and new format)
        raw_threads = download_section.get("threads")
        if raw_threads is not None:
            if isinstance(raw_threads, int):
                # Legacy format: single integer for matching and download
                if raw_threads < 1:
                    raise ConfigError(
                        "'download.threads' must be a positive integer",
                        details={"field": "download.threads", "value": raw_threads}
                    )
                matching_threads = raw_threads
                download_threads = raw_threads
            elif isinstance(raw_threads, dict):
                # New format: separate values for matching, download, lyrics, embedding
                raw_matching = raw_threads.get("matching")
                raw_download = raw_threads.get("download")
                raw_lyrics = raw_threads.get("lyrics")
                raw_embedding = raw_threads.get("embedding")
                
                if raw_matching is not None:
                    if not isinstance(raw_matching, int) or raw_matching < 1:
                        raise ConfigError(
                            "'download.threads.matching' must be a positive integer",
                            details={"field": "download.threads.matching", "value": raw_matching}
                        )
                    matching_threads = raw_matching
                
                if raw_download is not None:
                    if not isinstance(raw_download, int) or raw_download < 1:
                        raise ConfigError(
                            "'download.threads.download' must be a positive integer",
                            details={"field": "download.threads.download", "value": raw_download}
                        )
                    download_threads = raw_download
                
                if raw_lyrics is not None:
                    if not isinstance(raw_lyrics, int) or raw_lyrics < 1:
                        raise ConfigError(
                            "'download.threads.lyrics' must be a positive integer",
                            details={"field": "download.threads.lyrics", "value": raw_lyrics}
                        )
                    lyrics_threads = raw_lyrics
                
                if raw_embedding is not None:
                    if not isinstance(raw_embedding, int) or raw_embedding < 1:
                        raise ConfigError(
                            "'download.threads.embedding' must be a positive integer",
                            details={"field": "download.threads.embedding", "value": raw_embedding}
                        )
                    embedding_threads = raw_embedding
            else:
                raise ConfigError(
                    "'download.threads' must be an integer or a dictionary with 'matching', 'download', 'lyrics', and 'embedding' keys",
                    details={"field": "download.threads", "value": raw_threads}
                )
        
        # Parse cookie_file
        raw_cookie = download_section.get("cookie_file")
        if raw_cookie is not None:
            if not isinstance(raw_cookie, str):
                raise ConfigError(
                    "'download.cookie_file' must be a string path or null",
                    details={"field": "download.cookie_file"}
                )
            
            # Expand and check existence
            cookie_path = Path(raw_cookie).expanduser().resolve()
            if not cookie_path.exists():
                raise ConfigError(
                    f"Cookie file not found: {cookie_path}",
                    details={"field": "download.cookie_file", "path": str(cookie_path)}
                )
            cookie_file = cookie_path
    
    return DownloadConfig(
        matching_threads=matching_threads,
        download_threads=download_threads,
        lyrics_threads=lyrics_threads,
        embedding_threads=embedding_threads,
        cookie_file=cookie_file
    )
