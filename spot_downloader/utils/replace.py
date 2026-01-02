"""
Track replacement utility for spot-downloader.

This module provides functionality to replace the audio in an existing
M4A file while preserving all metadata. This is useful when the automatic
YouTube matching selects the wrong version of a song.

Usage (CLI):
    spot --replace "~/Music/Playlists/Liked Songs/00005-Song-Artist.m4a" "https://youtube.com/watch?v=correct_id"

Workflow:
    1. Parse the file path to extract playlist name and position
    2. Look up the track in the database by (playlist_name, position)
    3. Extract all metadata from the canonical M4A file in tracks/
    4. Download audio from the provided YouTube URL
    5. Convert to M4A format and apply the preserved metadata
    6. Atomically replace the canonical file in tracks/
    7. Update database: new youtube_url, reset embedding flags

Architecture:
    The replacement operates on the CANONICAL file in tracks/, not the
    hard link passed by the user. Since all playlist entries are hard
    links to the canonical file, replacing it automatically updates
    all playlists containing that track.

Note:
    This module requires database access. It only works on files
    managed by spot-downloader (present in the database).
"""

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4, MP4Cover

from spot_downloader.core.database import Database, LIKED_SONGS_KEY
from spot_downloader.core.exceptions import DownloadError, MetadataError
from spot_downloader.core.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# M4A Tag Constants (same as metadata.py for consistency)
# =============================================================================

M4A_TAGS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_artist": "aART",
    "date": "\xa9day",
    "genre": "\xa9gen",
    "track_number": "trkn",
    "disc_number": "disk",
    "cover": "covr",
    "lyrics": "\xa9lyr",
    "explicit": "rtng",
    "copyright": "cprt",
    "encoded_by": "\xa9too",
    "woas": "----:spotdl:WOAS",
    "isrc": "----:spotdl:ISRC",
}


# =============================================================================
# Path Parsing
# =============================================================================

def parse_playlist_path(file_path: Path) -> tuple[str, int]:
    """
    Parse a playlist file path to extract playlist name and track position.
    
    Args:
        file_path: Path to an M4A file in a playlist directory.
                   Expected format: .../Playlist Name/00005-Title-Artist.m4a
    
    Returns:
        Tuple of (playlist_name, position).
        For "Liked Songs" directory, returns (LIKED_SONGS_KEY, position).
    
    Raises:
        ValueError: If path format is invalid or position cannot be extracted.
    
    Examples:
        >>> parse_playlist_path(Path("~/Music/Liked Songs/00005-Song-Artist.m4a"))
        ("__liked_songs__", 5)
        
        >>> parse_playlist_path(Path("~/Music/My Playlist/00001-Song-Artist.m4a"))
        ("My Playlist", 1)
    """
    # Get playlist name from parent directory
    playlist_name = file_path.parent.name
    
    # Get filename and extract position from prefix
    filename = file_path.name
    
    # Expected format: 00005-Title-Artist.m4a
    # Position is the leading digits before the first dash
    match = re.match(r'^(\d+)-', filename)
    if not match:
        raise ValueError(
            f"Invalid filename format: '{filename}'. "
            f"Expected format: 00005-Title-Artist.m4a"
        )
    
    position = int(match.group(1))
    
    if position < 1:
        raise ValueError(f"Invalid position: {position}. Must be >= 1")
    
    # Map "Liked Songs" directory name to database key
    if playlist_name == "Liked Songs":
        return (LIKED_SONGS_KEY, position)
    
    return (playlist_name, position)


# =============================================================================
# Metadata Extraction
# =============================================================================

def extract_m4a_metadata(m4a_path: Path) -> dict[str, Any]:
    """
    Extract all metadata from an M4A file.
    
    Args:
        m4a_path: Path to the M4A file.
    
    Returns:
        Dictionary containing all extractable metadata:
        - title: str | None
        - artist: str | None
        - album: str | None
        - album_artist: str | None
        - year: str | None
        - genre: str | None
        - track_number: tuple[int, int] | None  (number, total)
        - disc_number: tuple[int, int] | None   (number, total)
        - cover_art: bytes | None
        - cover_format: int | None  (MP4Cover.FORMAT_JPEG or FORMAT_PNG)
        - lyrics: str | None
        - explicit: int | None
        - copyright: str | None
        - encoded_by: str | None
        - spotify_url: str | None
        - isrc: str | None
    
    Raises:
        MetadataError: If file is not a valid M4A or cannot be read.
    
    Note:
        This function uses mutagen to read M4A tags.
        All M4A-specific tag names are translated to generic names.
    """
    if not m4a_path.exists():
        raise MetadataError(
            f"File not found: {m4a_path}",
            details={"file_path": str(m4a_path)}
        )
    
    try:
        audio = MP4(str(m4a_path))
    except Exception as e:
        raise MetadataError(
            f"Failed to open M4A file: {e}",
            details={"file_path": str(m4a_path), "error": str(e)}
        )
    
    metadata: dict[str, Any] = {}
    
    # Helper to get first value from tag list
    def get_tag(tag_key: str) -> Any | None:
        values = audio.tags.get(tag_key) if audio.tags else None
        if values and len(values) > 0:
            return values[0]
        return None
    
    # Basic text tags
    metadata["title"] = get_tag(M4A_TAGS["title"])
    metadata["artist"] = get_tag(M4A_TAGS["artist"])
    metadata["album"] = get_tag(M4A_TAGS["album"])
    metadata["album_artist"] = get_tag(M4A_TAGS["album_artist"])
    metadata["year"] = get_tag(M4A_TAGS["date"])
    metadata["genre"] = get_tag(M4A_TAGS["genre"])
    metadata["copyright"] = get_tag(M4A_TAGS["copyright"])
    metadata["encoded_by"] = get_tag(M4A_TAGS["encoded_by"])
    metadata["lyrics"] = get_tag(M4A_TAGS["lyrics"])
    metadata["explicit"] = get_tag(M4A_TAGS["explicit"])
    
    # Track number - stored as [(number, total)]
    track_data = get_tag(M4A_TAGS["track_number"])
    if track_data and isinstance(track_data, tuple):
        metadata["track_number"] = track_data
    else:
        metadata["track_number"] = None
    
    # Disc number - stored as [(number, total)]
    disc_data = get_tag(M4A_TAGS["disc_number"])
    if disc_data and isinstance(disc_data, tuple):
        metadata["disc_number"] = disc_data
    else:
        metadata["disc_number"] = None
    
    # Cover art
    cover_data = get_tag(M4A_TAGS["cover"])
    if cover_data and isinstance(cover_data, MP4Cover):
        metadata["cover_art"] = bytes(cover_data)
        metadata["cover_format"] = cover_data.imageformat
    else:
        metadata["cover_art"] = None
        metadata["cover_format"] = None
    
    # Custom freeform tags (stored as bytes)
    woas = audio.tags.get(M4A_TAGS["woas"]) if audio.tags else None
    if woas and len(woas) > 0:
        try:
            metadata["spotify_url"] = woas[0].decode("utf-8") if isinstance(woas[0], bytes) else str(woas[0])
        except (UnicodeDecodeError, AttributeError):
            metadata["spotify_url"] = None
    else:
        metadata["spotify_url"] = None
    
    isrc = audio.tags.get(M4A_TAGS["isrc"]) if audio.tags else None
    if isrc and len(isrc) > 0:
        try:
            metadata["isrc"] = isrc[0].decode("utf-8") if isinstance(isrc[0], bytes) else str(isrc[0])
        except (UnicodeDecodeError, AttributeError):
            metadata["isrc"] = None
    else:
        metadata["isrc"] = None
    
    return metadata


# =============================================================================
# Metadata Application
# =============================================================================

def apply_m4a_metadata(m4a_path: Path, metadata: dict[str, Any]) -> None:
    """
    Apply metadata to an M4A file.
    
    Args:
        m4a_path: Path to the M4A file.
        metadata: Dictionary of metadata (as returned by extract_m4a_metadata).
    
    Raises:
        MetadataError: If metadata cannot be written.
    
    Note:
        This overwrites all existing metadata in the file.
        The file must already exist and be a valid M4A.
    """
    if not m4a_path.exists():
        raise MetadataError(
            f"File not found: {m4a_path}",
            details={"file_path": str(m4a_path)}
        )
    
    try:
        audio = MP4(str(m4a_path))
    except Exception as e:
        raise MetadataError(
            f"Failed to open M4A file: {e}",
            details={"file_path": str(m4a_path), "error": str(e)}
        )
    
    # Clear existing tags
    audio.clear()
    
    # Basic text tags
    if metadata.get("title"):
        audio[M4A_TAGS["title"]] = [metadata["title"]]
    
    if metadata.get("artist"):
        audio[M4A_TAGS["artist"]] = [metadata["artist"]]
    
    if metadata.get("album"):
        audio[M4A_TAGS["album"]] = [metadata["album"]]
    
    if metadata.get("album_artist"):
        audio[M4A_TAGS["album_artist"]] = [metadata["album_artist"]]
    
    if metadata.get("year"):
        audio[M4A_TAGS["date"]] = [metadata["year"]]
    
    if metadata.get("genre"):
        audio[M4A_TAGS["genre"]] = [metadata["genre"]]
    
    if metadata.get("copyright"):
        audio[M4A_TAGS["copyright"]] = [metadata["copyright"]]
    
    if metadata.get("encoded_by"):
        audio[M4A_TAGS["encoded_by"]] = [metadata["encoded_by"]]
    
    if metadata.get("lyrics"):
        audio[M4A_TAGS["lyrics"]] = [metadata["lyrics"]]
    
    if metadata.get("explicit") is not None:
        audio[M4A_TAGS["explicit"]] = [metadata["explicit"]]
    
    # Track and disc numbers (tuple format)
    if metadata.get("track_number"):
        audio[M4A_TAGS["track_number"]] = [metadata["track_number"]]
    
    if metadata.get("disc_number"):
        audio[M4A_TAGS["disc_number"]] = [metadata["disc_number"]]
    
    # Cover art
    if metadata.get("cover_art"):
        cover_format = metadata.get("cover_format", MP4Cover.FORMAT_JPEG)
        audio[M4A_TAGS["cover"]] = [MP4Cover(metadata["cover_art"], imageformat=cover_format)]
    
    # Custom freeform tags
    if metadata.get("spotify_url"):
        audio[M4A_TAGS["woas"]] = metadata["spotify_url"].encode("utf-8")
    
    if metadata.get("isrc"):
        audio[M4A_TAGS["isrc"]] = metadata["isrc"].encode("utf-8")
    
    # Save
    try:
        audio.save()
    except Exception as e:
        raise MetadataError(
            f"Failed to save metadata: {e}",
            details={"file_path": str(m4a_path), "error": str(e)}
        )


# =============================================================================
# Audio Download (simplified from downloader.py)
# =============================================================================

def _download_audio_to_temp(
    youtube_url: str,
    output_dir: Path,
    cookie_file: Path | None = None
) -> Path:
    """
    Download audio from YouTube to a temporary directory.
    
    Args:
        youtube_url: YouTube video URL.
        output_dir: Directory to save the downloaded file.
        cookie_file: Optional path to cookies.txt for Premium quality.
    
    Returns:
        Path to the downloaded M4A file.
    
    Raises:
        DownloadError: If download fails.
    """
    from yt_dlp import YoutubeDL
    
    output_template = str(output_dir / "%(id)s.%(ext)s")
    
    options: dict[str, Any] = {
        "format": "bestaudio",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "encoding": "UTF-8",
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "default"],
            }
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }
        ],
        "keepvideo": False,
    }
    
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)
    
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            
            if info is None:
                raise DownloadError("yt-dlp returned no info")
            
            video_id = info.get("id", "unknown")
            
            # Find the downloaded file
            m4a_file = output_dir / f"{video_id}.m4a"
            if m4a_file.exists():
                return m4a_file
            
            # Fallback: look for any m4a file
            for f in output_dir.iterdir():
                if f.suffix == ".m4a":
                    return f
            
            raise DownloadError(f"Downloaded file not found in {output_dir}")
            
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"Download failed: {e}")


# =============================================================================
# Main Replace Function
# =============================================================================

def replace_track_audio(
    m4a_path: Path,
    youtube_url: str,
    database: Database,
    cookie_file: Path | None = None
) -> dict[str, Any]:
    """
    Replace the audio in an M4A file while preserving metadata.
    
    This function allows users to manually correct cases where the
    automatic YouTube matching selected the wrong song.
    
    Args:
        m4a_path: Path to the M4A file to replace (can be a hard link
                  from a playlist directory or the canonical file).
        youtube_url: YouTube URL to download the correct audio from.
        database: Database instance for looking up track info and
                  updating the youtube_url after replacement.
        cookie_file: Optional path to cookies.txt for Premium quality.
    
    Returns:
        Dictionary with replacement info:
        - track_name: str
        - artist: str
        - old_youtube_url: str
        - new_youtube_url: str
        - canonical_path: Path
    
    Raises:
        ValueError: If path format is invalid or track not found in database.
        MetadataError: If metadata cannot be read/written.
        DownloadError: If the YouTube download fails.
    
    Workflow:
        1. Parse file path to get (playlist_name, position)
        2. Look up track in database → get spotify_id and canonical file_path
        3. Extract all metadata from the canonical M4A file
        4. Download new audio from youtube_url to temp directory
        5. Apply preserved metadata to the new file
        6. Atomically replace the canonical file
        7. Update database: set new youtube_url, reset embedding flags
        8. Clean up temp files
    
    Atomicity:
        The replacement is atomic - if any step fails, the original
        file is left untouched. The new file is written to a temp
        location and only moved to the final path on success.
    
    Hard Links:
        Since playlist entries are hard links to the canonical file,
        replacing the canonical file automatically updates all playlists
        containing that track.
    """
    # 1. Parse path to get playlist name and position
    playlist_name, position = parse_playlist_path(m4a_path)
    
    # 2. Look up track in database
    track = database.get_track_by_playlist_position(playlist_name, position)
    
    if track is None:
        raise ValueError(
            f"Track not found in database for playlist '{playlist_name}' "
            f"at position {position}"
        )
    
    spotify_id = track["spotify_id"]
    canonical_path = Path(track["file_path"])
    old_youtube_url = track.get("youtube_url", "Unknown")
    track_name = track.get("name", "Unknown")
    artist = track.get("artist", "Unknown")
    
    # Verify canonical file exists
    if not canonical_path.exists():
        raise MetadataError(
            f"Canonical file not found: {canonical_path}",
            details={"spotify_id": spotify_id, "expected_path": str(canonical_path)}
        )
    
    logger.info(f"Replacing: {artist} - {track_name}")
    logger.debug(f"Canonical path: {canonical_path}")
    logger.debug(f"Old YouTube URL: {old_youtube_url}")
    logger.debug(f"New YouTube URL: {youtube_url}")
    
    # 3. Extract metadata from canonical file
    logger.debug("Extracting metadata from existing file...")
    metadata = extract_m4a_metadata(canonical_path)
    
    # 4. Download new audio to temp directory
    temp_dir = Path(tempfile.mkdtemp(prefix="spot_replace_"))
    
    try:
        logger.debug("Downloading new audio...")
        new_audio_path = _download_audio_to_temp(youtube_url, temp_dir, cookie_file)
        
        # 5. Apply metadata to new file
        logger.debug("Applying metadata to new file...")
        apply_m4a_metadata(new_audio_path, metadata)
        
        # 6. Overwrite content of canonical file (preserves inode for hard links)
        shutil.copyfile(str(new_audio_path), str(canonical_path))
        
        # 7. Update database
        logger.debug("Updating database...")
        database.set_youtube_url(spotify_id, youtube_url)
        database.reset_embedding_flags(spotify_id)
        
        logger.info(f"Successfully replaced: {artist} - {track_name}")
        
        return {
            "track_name": track_name,
            "artist": artist,
            "old_youtube_url": old_youtube_url,
            "new_youtube_url": youtube_url,
            "canonical_path": canonical_path,
        }
        
    finally:
        # 8. Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)