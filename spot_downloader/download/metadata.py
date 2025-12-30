"""
Metadata embedding for spot-downloader.

This module handles embedding ID3/M4A metadata tags into downloaded
audio files. It supports:
    - Basic metadata (title, artist, album, year, genre)
    - Album artwork (cover image)
    - Lyrics (plain text and synced LRC)
    - Extended metadata (copyright, publisher, ISRC)

Supported Format:
    Only M4A (AAC) format is supported. The application always
    downloads and converts to M4A, so no other formats are handled.

M4A Tag Mapping:
    Spotify Field      -> M4A Tag
    ----------------   ---------
    name               -> \xa9nam (title)
    artist             -> \xa9ART (artist)
    artists            -> \xa9ART (all artists joined)
    album              -> \xa9alb (album)
    album_artist       -> aART (album artist)
    year               -> \xa9day (year/date)
    genre              -> \xa9gen (genre)
    track_number       -> trkn (track number/total)
    disc_number        -> disk (disc number/total)
    cover_url          -> covr (cover art)
    lyrics             -> \xa9lyr (lyrics)
    explicit           -> rtng (rating - 4=explicit, 2=clean)
    copyright          -> cprt (copyright)
    publisher          -> \xa9too (encoded by)
    spotify_url        -> ----:spotdl:WOAS (custom tag)
    isrc               -> ----:spotdl:ISRC (custom tag)

Dependencies:
    - mutagen: Audio metadata library
    - requests: For downloading cover art

Usage:
    from spot_downloader.download.metadata import MetadataEmbedder
    
    embedder = MetadataEmbedder()
    embedder.embed_metadata(
        file_path=Path("/path/to/song.m4a"),
        track=track,
        lyrics=lyrics  # Optional
    )
"""

from pathlib import Path

import requests
from mutagen.mp4 import MP4, MP4Cover

from spot_downloader.core.exceptions import MetadataError
from spot_downloader.core.logger import get_logger
from spot_downloader.download.lyrics import Lyrics
from spot_downloader.spotify.models import Track

logger = get_logger(__name__)


# M4A tag mapping (same as spotDL for consistency)
# See: https://mutagen.readthedocs.io/en/latest/api/mp4.html
M4A_TAGS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_artist": "aART",
    "date": "\xa9day",
    "genre": "\xa9gen",
    "track_number": "trkn",  # Tuple: (track, total)
    "disc_number": "disk",   # Tuple: (disc, total)
    "cover": "covr",
    "lyrics": "\xa9lyr",
    "explicit": "rtng",      # 4 = explicit, 2 = clean
    "copyright": "cprt",
    "encoded_by": "\xa9too",
    "comment": "\xa9cmt",
    # Custom freeform tags (for Spotify URL, ISRC)
    "woas": "----:spotdl:WOAS",  # Web Original Audio Source
    "isrc": "----:spotdl:ISRC",
}


class MetadataEmbedder:
    """
    Embeds metadata into M4A audio files.
    
    This class handles all metadata embedding operations for
    downloaded M4A files. It uses mutagen for tag manipulation.
    
    Embedding Process:
        1. Open M4A file with mutagen
        2. Clear existing tags (optional)
        3. Set basic metadata tags
        4. Download and embed cover art
        5. Embed lyrics if available
        6. Save file
    
    Error Handling:
        - Cover art download failures are logged but don't stop embedding
        - Lyrics embedding failures are logged but don't stop embedding
        - File write failures raise MetadataError
    
    Thread Safety:
        Each embed_metadata() call operates on a separate file.
        Multiple threads can embed metadata to different files
        simultaneously.
    
    Example:
        embedder = MetadataEmbedder()
        
        try:
            embedder.embed_metadata(file_path, track, lyrics)
        except MetadataError as e:
            logger.error(f"Failed to embed metadata: {e}")
    """
    
    def __init__(self) -> None:
        """
        Initialize the MetadataEmbedder.
        
        No initialization required - this class is stateless.
        """
        pass
    
    def embed_metadata(
        self,
        file_path: Path,
        track: Track,
        lyrics: Lyrics | None = None
    ) -> bool:
        """
        Embed all metadata into an M4A file.
        
        This is the main method for metadata embedding. It embeds
        all available metadata from the Track object.
        
        Args:
            file_path: Path to the M4A file to update.
            track: Track object containing metadata to embed.
            lyrics: Optional Lyrics object to embed.
        
        Returns:
            bool: True if cover art was successfully embedded, False otherwise.
        
        Raises:
            MetadataError: If file cannot be opened, written, or saved.
        
        Behavior:
            1. Open M4A file with mutagen
            2. Embed basic tags (title, artist, album, etc.)
            3. Embed extended tags (copyright, publisher, ISRC)
            4. Download and embed cover art (if URL available)
            5. Embed lyrics (if provided)
            6. Save file
        
        Non-Fatal Errors:
            - Cover art download failure: Logged, embedding continues
            - Lyrics embedding failure: Logged, embedding continues
        
        Fatal Errors:
            - File not found or not readable
            - File is not valid M4A
            - Permission denied on write
            - Disk full
        
        Example:
            embedder = MetadataEmbedder()
            cover_embedded = embedder.embed_metadata(
                Path("song.m4a"),
                track,
                lyrics=Lyrics("lyrics text", is_synced=False, source="genius")
            )
        """
        # Open file
        audio = self._open_file(file_path)
        
        # Embed basic metadata
        self._embed_basic_tags(audio, track)
        
        # Embed track and disc numbers
        self._embed_track_disc_numbers(audio, track)
        
        # Embed extended tags
        self._embed_extended_tags(audio, track)
        
        # Embed cover art (returns success status)
        cover_embedded = self._embed_cover_art(audio, track.cover_url)
        
        # Embed lyrics
        self._embed_lyrics(audio, lyrics)
        
        # Save file
        self._save_file(audio, file_path)
        
        return cover_embedded
    
    def embed_lyrics_only(
        self,
        file_path: Path,
        lyrics: Lyrics
    ) -> None:
        """
        Embed only lyrics into an M4A file (for re-embedding after lyrics fetch).
        
        This method is used when a track already has metadata embedded
        but lyrics were fetched later.
        
        Args:
            file_path: Path to the M4A file to update.
            lyrics: Lyrics object to embed.
        
        Raises:
            MetadataError: If file cannot be opened, written, or saved.
        """
        # Open file
        audio = self._open_file(file_path)
        
        # Embed lyrics
        self._embed_lyrics(audio, lyrics)
        
        # Save file
        self._save_file(audio, file_path)
    
    def _open_file(self, file_path: Path) -> MP4:
        """
        Open an M4A file for metadata editing.
        
        Args:
            file_path: Path to the M4A file.
        
        Returns:
            MP4 object from mutagen.
        
        Raises:
            MetadataError: If file cannot be opened or is not valid M4A.
        """
        if not file_path.exists():
            raise MetadataError(
                f"File not found: {file_path}",
                details={"file_path": str(file_path)}
            )
        
        try:
            audio = MP4(str(file_path))
            return audio
        except Exception as e:
            raise MetadataError(
                f"Failed to open file: {e}",
                details={"file_path": str(file_path), "error": str(e)}
            )
    
    def _embed_basic_tags(self, audio: MP4, track: Track) -> None:
        """
        Embed basic metadata tags.
        
        Args:
            audio: MP4 object to update.
            track: Track with metadata.
        
        Tags Embedded:
            - title (\xa9nam)
            - artist (\xa9ART) - all artists joined with ", "
            - album (\xa9alb)
            - album_artist (aART)
            - date (\xa9day)
            - genre (\xa9gen) - first genre if multiple
        """
        # Title
        if track.name:
            audio[M4A_TAGS["title"]] = [track.name]
        
        # Artist - join all artists with ", "
        if track.artists:
            audio[M4A_TAGS["artist"]] = [", ".join(track.artists)]
        elif track.artist:
            audio[M4A_TAGS["artist"]] = [track.artist]
        
        # Album
        if track.album:
            audio[M4A_TAGS["album"]] = [track.album]
        
        # Album artist
        if track.album_artist:
            audio[M4A_TAGS["album_artist"]] = [track.album_artist]
        elif track.artist:
            # Fall back to primary artist if no album artist
            audio[M4A_TAGS["album_artist"]] = [track.artist]
        
        # Date/year
        if track.release_date:
            audio[M4A_TAGS["date"]] = [track.release_date]
        elif track.year:
            audio[M4A_TAGS["date"]] = [str(track.year)]
        
        # Genre - first genre only
        if track.genres:
            # Capitalize first letter of genre
            genre = track.genres[0].title() if track.genres[0] else ""
            if genre:
                audio[M4A_TAGS["genre"]] = [genre]
    
    def _embed_track_disc_numbers(self, audio: MP4, track: Track) -> None:
        """
        Embed track and disc number tags.
        
        Args:
            audio: MP4 object to update.
            track: Track with metadata.
        
        Tags Embedded:
            - trkn: (track_number, tracks_count) tuple
            - disk: (disc_number, disc_count) tuple
        
        Format:
            M4A uses tuples for track/disc numbers: (number, total)
            This allows players to display "3 of 12".
        """
        # Track number - format: [(track_number, total_tracks)]
        if track.track_number:
            total = track.tracks_count if track.tracks_count else 0
            audio[M4A_TAGS["track_number"]] = [(track.track_number, total)]
        
        # Disc number - format: [(disc_number, total_discs)]
        if track.disc_number:
            total = track.disc_count if track.disc_count else 0
            audio[M4A_TAGS["disc_number"]] = [(track.disc_number, total)]
    
    def _embed_extended_tags(self, audio: MP4, track: Track) -> None:
        """
        Embed extended metadata tags.
        
        Args:
            audio: MP4 object to update.
            track: Track with metadata.
        
        Tags Embedded:
            - copyright (cprt)
            - encoded_by (\xa9too) - publisher/label
            - explicit (rtng) - 4 for explicit, 2 for clean
            - woas (custom) - Spotify URL
            - isrc (custom) - ISRC code
        """
        # Copyright
        if track.copyright_text:
            audio[M4A_TAGS["copyright"]] = [track.copyright_text]
        
        # Publisher (encoded_by tag)
        if track.publisher:
            audio[M4A_TAGS["encoded_by"]] = [track.publisher]
        
        # Explicit rating: 4 = explicit, 2 = clean
        # Using tuple format as expected by M4A
        explicit_value = 4 if track.explicit else 2
        audio[M4A_TAGS["explicit"]] = [explicit_value]
        
        # Spotify URL (custom freeform tag)
        if track.spotify_url:
            audio[M4A_TAGS["woas"]] = track.spotify_url.encode("utf-8")
        
        # ISRC (custom freeform tag)
        if track.isrc:
            audio[M4A_TAGS["isrc"]] = track.isrc.encode("utf-8")
    
    def _embed_cover_art(self, audio: MP4, cover_url: str | None) -> bool:
        """
        Download and embed album cover art.
        
        Args:
            audio: MP4 object to update.
            cover_url: URL to cover image, or None.
        
        Returns:
            bool: True if cover art was successfully embedded, False otherwise.
        
        Behavior:
            1. If cover_url is None, skip silently
            2. Download image from URL
            3. Detect image format (JPEG or PNG)
            4. Create MP4Cover object
            5. Embed as 'covr' tag
        
        Error Handling:
            Download failures are logged but don't raise exceptions.
            The file will simply not have cover art.
        
        Image Formats:
            Supports JPEG and PNG. JPEG is preferred for smaller size.
        """
        if not cover_url:
            logger.debug("No cover URL provided, skipping cover art")
            return False
        
        # Download cover image
        cover_data = self._download_cover(cover_url)
        if cover_data is None:
            logger.warning(f"Failed to download cover art from {cover_url}")
            return False
        
        # Detect image format
        image_format = self._detect_image_format(cover_data)
        
        # Create MP4Cover and embed
        try:
            # Remove existing cover art if present
            if M4A_TAGS["cover"] in audio:
                del audio[M4A_TAGS["cover"]]
            
            cover = MP4Cover(cover_data, imageformat=image_format)
            audio[M4A_TAGS["cover"]] = [cover]
            logger.debug("Cover art embedded successfully")
            return True
        except Exception as e:
            logger.warning(f"Failed to embed cover art: {e}")
            return False
    
    def _embed_lyrics(self, audio: MP4, lyrics: Lyrics | None) -> None:
        """
        Embed lyrics into the audio file.
        
        Args:
            audio: MP4 object to update.
            lyrics: Lyrics object, or None.
        
        Behavior:
            1. If lyrics is None, skip silently
            2. Embed lyrics text as \xa9lyr tag
            3. If synced (LRC format), text includes timestamps
        
        LRC Handling:
            For M4A files, LRC lyrics are embedded as plain text
            including the timestamp tags. Players that support LRC
            will parse them; others will display as-is.
        
        Note:
            Unlike MP3 which has separate USLT (unsync) and SYLT (sync)
            tags, M4A only has one lyrics tag. Synced lyrics are stored
            with their timestamps intact in the text.
        """
        if lyrics is None:
            logger.debug("No lyrics provided, skipping lyrics embedding")
            return
        
        if not lyrics.text:
            logger.debug("Empty lyrics text, skipping lyrics embedding")
            return
        
        try:
            audio[M4A_TAGS["lyrics"]] = [lyrics.text]
            logger.debug(f"Lyrics embedded successfully (synced={lyrics.is_synced}, source={lyrics.source})")
        except Exception as e:
            logger.warning(f"Failed to embed lyrics: {e}")
    
    def _save_file(self, audio: MP4, file_path: Path) -> None:
        """
        Save the modified audio file.
        
        Args:
            audio: MP4 object to save.
            file_path: Path to save to (for error messages).
        
        Raises:
            MetadataError: If save fails (permissions, disk full, etc.)
        """
        try:
            audio.save()
            logger.debug(f"Metadata saved to {file_path}")
        except Exception as e:
            raise MetadataError(
                f"Failed to save file: {e}",
                details={"file_path": str(file_path), "error": str(e)}
            )
    
    @staticmethod
    def _download_cover(url: str) -> bytes | None:
        """
        Download cover art from URL.
        
        Args:
            url: URL to the cover image.
        
        Returns:
            Image bytes if successful, None if download failed.
        
        Behavior:
            - Uses requests to download image
            - Timeout of 10 seconds
            - Returns None on any error (doesn't raise)
        """
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            logger.debug(f"Cover download failed: {e}")
            return None
    
    @staticmethod
    def _detect_image_format(data: bytes) -> int:
        """
        Detect image format from bytes.
        
        Args:
            data: Image bytes.
        
        Returns:
            MP4Cover format constant:
            - MP4Cover.FORMAT_JPEG for JPEG
            - MP4Cover.FORMAT_PNG for PNG
        
        Detection:
            - JPEG: Starts with FF D8 FF
            - PNG: Starts with 89 50 4E 47 (0x89 'P' 'N' 'G')
        """
        # PNG signature: 89 50 4E 47 0D 0A 1A 0A
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return MP4Cover.FORMAT_PNG
        
        # JPEG signature: FF D8 FF
        if data[:3] == b'\xff\xd8\xff':
            return MP4Cover.FORMAT_JPEG
        
        # Default to JPEG (most common for album art)
        logger.debug("Could not detect image format, defaulting to JPEG")
        return MP4Cover.FORMAT_JPEG


def embed_track_metadata(
    file_path: Path,
    track: Track,
    lyrics: Lyrics | None = None
) -> bool:
    """
    Convenience function to embed metadata without creating embedder instance.
    
    Args:
        file_path: Path to the M4A file.
        track: Track with metadata.
        lyrics: Optional lyrics.
    
    Returns:
        bool: True if cover art was embedded successfully.
    
    Raises:
        MetadataError: If embedding fails.
    
    Example:
        cover_ok = embed_track_metadata(Path("song.m4a"), track, lyrics)
    """
    embedder = MetadataEmbedder()
    return embedder.embed_metadata(file_path, track, lyrics)
