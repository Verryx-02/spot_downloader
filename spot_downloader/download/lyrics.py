"""
Lyrics fetching for spot-downloader.

This module handles fetching song lyrics from various providers.
Lyrics are OPTIONAL - failure to fetch lyrics should never prevent
a track from being downloaded and saved.

Provider Strategy:
    The module performs two separate searches:
    
    1. Plain text lyrics (for embedding in audio files):
       Genius → AZLyrics
       These providers have better quality/moderation for plain text.
    
    2. Synced LRC lyrics (for separate .lrc files):
       NetEase → Lrclib (synced_only=True)
       These providers support timestamped lyrics.
    
    If plain text is not found but LRC is found, plain text is derived
    from the LRC by stripping timestamps.

Excluded Providers:
    - Musixmatch: Aggressive rate limiting (401 errors)
    - Megalobiz, Deezer, Lyricsify: Broken/unreliable

FRAGILE WARNING:
    Lyrics scraping is inherently fragile because:
    - Websites change their HTML structure frequently
    - Anti-bot protections may block requests
    - Rate limiting may cause failures
    - Some songs simply don't have lyrics available
    
    Always treat lyrics as a "nice to have" feature that may fail.

Usage:
    from spot_downloader.download.lyrics import LyricsFetcher
    
    fetcher = LyricsFetcher()
    
    # Get both plain and synced lyrics
    plain, synced = fetcher.fetch_lyrics("Song Title", "Artist Name")
    
    if plain:
        # Embed plain text in audio file
        embed_lyrics(file_path, plain)
    
    if synced:
        # Save .lrc file
        save_lrc_file(lrc_path, synced)
"""

import re
import time
import random
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from spot_downloader.core.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Request timeout in seconds
REQUEST_TIMEOUT = 10

# User agent for web requests (mimics a real browser)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Base delay between requests (seconds)
BASE_DELAY = 0.5

# LRC timestamp pattern for detection
LRC_TIMESTAMP_PATTERN = re.compile(r'\[\d{2}:\d{2}[.:]\d{2,3}\]')


# =============================================================================
# Data Classes
# =============================================================================

@dataclass(frozen=True)
class Lyrics:
    """
    Container for fetched lyrics.
    
    Attributes:
        text: The lyrics text content.
              May be plain text or LRC format with timestamps.
        
        is_synced: Whether lyrics are timestamped (LRC format).
                   True for synced lyrics: "[00:15.00]First line..."
                   False for plain text lyrics.
        
        source: Name of the provider that returned the lyrics.
                Example: "genius", "synced", "azlyrics"
    
    LRC Format:
        Synced lyrics use LRC (LyRiCs) format with timestamps:
        [00:15.00]First line of the song
        [00:18.50]Second line continues
        
        These can be embedded as SYLT tags in MP3 or stored as
        separate .lrc files.
    """
    
    text: str
    is_synced: bool
    source: str
    
    @property
    def is_lrc(self) -> bool:
        """Check if lyrics are in LRC format (alias for is_synced)."""
        return self.is_synced
    
    def get_plain_text(self) -> str:
        """
        Get lyrics as plain text (strips LRC timestamps if present).
        
        Returns:
            Plain text lyrics without timestamps.
        """
        if not self.is_synced:
            return self.text
        
        # Remove LRC timestamps
        lines = []
        for line in self.text.split('\n'):
            # Remove timestamp at start of line
            clean_line = LRC_TIMESTAMP_PATTERN.sub('', line).strip()
            # Skip metadata lines like [ar:Artist]
            if clean_line and not (clean_line.startswith('[') and ':' in clean_line):
                lines.append(clean_line)
        
        return '\n'.join(lines)


# =============================================================================
# Helper Functions
# =============================================================================

def _is_lrc_format(text: str) -> bool:
    """
    Check if text contains LRC timestamp format.
    
    Args:
        text: Text to check.
    
    Returns:
        True if text contains LRC timestamps.
    """
    return bool(LRC_TIMESTAMP_PATTERN.search(text))


def _clean_search_term(term: str) -> str:
    """
    Clean a search term for better matching.
    
    Removes:
        - Content in parentheses (feat., remix, etc.)
        - Content in brackets
        - Extra whitespace
    
    Args:
        term: The term to clean.
    
    Returns:
        Cleaned term.
    """
    # Remove content in parentheses
    term = re.sub(r'\([^)]*\)', '', term)
    # Remove content in brackets
    term = re.sub(r'\[[^\]]*\]', '', term)
    # Remove extra whitespace
    term = ' '.join(term.split())
    return term.strip()


def _normalize_for_url(text: str) -> str:
    """
    Normalize text for use in URLs (AZLyrics style).
    
    Removes all non-alphanumeric characters and converts to lowercase.
    
    Args:
        text: Text to normalize.
    
    Returns:
        Normalized text suitable for URLs.
    """
    # Remove everything except letters and numbers
    normalized = re.sub(r'[^a-zA-Z0-9]', '', text)
    return normalized.lower()


# =============================================================================
# LyricsFetcher Class
# =============================================================================

class LyricsFetcher:
    """
    Fetches lyrics from multiple providers with fallback.
    
    This class tries multiple lyrics sources in order until one
    succeeds or all fail. It handles errors gracefully and never
    raises exceptions to the caller (returns None instead).
    
    Provider Order:
        1. syncedlyrics - Best quality (timestamped)
        2. Genius - Large database, good coverage
        3. AZLyrics - Alternative source
        4. MusixMatch - Additional fallback (via syncedlyrics plain mode)
    
    Thread Safety:
        This class is thread-safe. Multiple threads can call
        fetch_lyrics() simultaneously.
    
    Example:
        fetcher = LyricsFetcher()
        lyrics = fetcher.fetch_lyrics("Bohemian Rhapsody", "Queen")
        
        if lyrics:
            if lyrics.is_synced:
                print("Got synced lyrics from", lyrics.source)
            else:
                print("Got plain lyrics from", lyrics.source)
        else:
            print("No lyrics available")
    """
    
    def __init__(self) -> None:
        """
        Initialize the LyricsFetcher with HTTP session.
        
        Behavior:
            Creates a requests session with appropriate headers
            for web scraping. Also configures syncedlyrics logging
            to use our logging system.
        """
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        
        # Check if syncedlyrics is available
        self._syncedlyrics_available = False
        try:
            import syncedlyrics
            import logging as stdlib_logging
            
            # Silence ALL syncedlyrics loggers completely
            # They print too much noise (INFO messages, retry warnings, etc.)
            syncedlyrics_loggers = [
                "syncedlyrics",
                "syncedlyrics.providers",
                "syncedlyrics.providers.musixmatch",
                "syncedlyrics.providers.lrclib", 
                "syncedlyrics.providers.netease",
                "syncedlyrics.providers.megalobiz",
                "syncedlyrics.providers.genius",
            ]
            
            for logger_name in syncedlyrics_loggers:
                sl_logger = stdlib_logging.getLogger(logger_name)
                sl_logger.handlers.clear()
                sl_logger.setLevel(stdlib_logging.CRITICAL)  # Silence everything
                sl_logger.propagate = False  # Don't propagate to root
            
            self._syncedlyrics_available = True
        except ImportError:
            logger.warning("syncedlyrics library not available - synced lyrics disabled")
    
    def fetch_lyrics(
        self,
        track_name: str,
        artist: str,
        album: str | None = None,
        duration_seconds: int | None = None
    ) -> tuple[Lyrics | None, Lyrics | None]:
        """
        Fetch lyrics for a track from multiple providers.
        
        Performs two separate searches:
        1. Plain text (Genius → AZLyrics) for embedding in audio
        2. Synced LRC (NetEase → Lrclib) for .lrc files
        
        Args:
            track_name: The song title.
            artist: The primary artist name.
            album: Optional album name (helps some providers).
            duration_seconds: Optional duration (for synced lyrics matching).
        
        Returns:
            Tuple of (plain_lyrics, synced_lyrics).
            Either or both can be None if not found.
            If plain is not found but synced is, plain is derived from synced.
        
        Error Handling:
            - Provider errors are caught and logged
            - Never raises exceptions (returns None values)
        """
        plain_lyrics: Lyrics | None = None
        synced_lyrics: Lyrics | None = None
        
        # =====================================================================
        # STEP 1: Search for plain text lyrics (for embedding)
        # Priority: Genius → AZLyrics (better quality/moderation)
        # =====================================================================
        
        # Try Genius first
        logger.debug(f"Trying Genius for plain text: {artist} - {track_name}")
        plain_lyrics = self._try_genius(track_name, artist)
        
        if plain_lyrics:
            logger.debug("Found plain lyrics via Genius")
        else:
            # Try AZLyrics
            time.sleep(BASE_DELAY + random.uniform(0, 0.3))
            logger.debug(f"Trying AZLyrics for plain text: {artist} - {track_name}")
            plain_lyrics = self._try_azlyrics(track_name, artist)
            
            if plain_lyrics:
                logger.debug("Found plain lyrics via AZLyrics")
        
        # =====================================================================
        # STEP 2: Search for synced LRC lyrics (for .lrc files)
        # Priority: NetEase → Lrclib (synced_only=True)
        # =====================================================================
        
        if self._syncedlyrics_available:
            time.sleep(BASE_DELAY + random.uniform(0, 0.3))
            logger.debug(f"Trying syncedlyrics for LRC: {artist} - {track_name}")
            synced_lyrics = self._try_synced_lyrics_lrc(track_name, artist, duration_seconds)
            
            if synced_lyrics:
                logger.debug("Found synced LRC lyrics via syncedlyrics")
        
        # =====================================================================
        # STEP 3: If no plain but synced found, derive plain from synced
        # =====================================================================
        
        if plain_lyrics is None and synced_lyrics is not None:
            logger.debug("Deriving plain text from synced LRC")
            plain_text = synced_lyrics.get_plain_text()
            plain_lyrics = Lyrics(
                text=plain_text,
                is_synced=False,
                source=f"{synced_lyrics.source}-derived"
            )
        
        # Log final result
        if plain_lyrics is None and synced_lyrics is None:
            logger.debug(f"No lyrics found for: {artist} - {track_name}")
        else:
            logger.debug(
                f"Lyrics result for {artist} - {track_name}: "
                f"plain={'yes' if plain_lyrics else 'no'}, "
                f"synced={'yes' if synced_lyrics else 'no'}"
            )
        
        return (plain_lyrics, synced_lyrics)
    
    def _try_synced_lyrics_lrc(
        self,
        track_name: str,
        artist: str,
        duration_seconds: int | None
    ) -> Lyrics | None:
        """
        Try to fetch synced (LRC) lyrics using syncedlyrics library.
        
        Only searches for synced lyrics (synced_only=True).
        Uses NetEase and Lrclib - providers that support timestamped lyrics.
        
        Args:
            track_name: Song title.
            artist: Artist name.
            duration_seconds: Track duration for matching.
        
        Returns:
            Lyrics object with is_synced=True if found, None otherwise.
        """
        try:
            import syncedlyrics
            
            # Clean search terms
            clean_track = _clean_search_term(track_name)
            clean_artist = _clean_search_term(artist)
            
            # SpotDL format: "{track} - {artist}"
            search_query = f"{clean_track} - {clean_artist}"
            
            # Only providers that support synced LRC lyrics
            # Musixmatch excluded - aggressive rate limiting (401 errors)
            lrc_providers = ["NetEase", "Lrclib"]
            
            try:
                lyrics_text = syncedlyrics.search(
                    search_query,
                    synced_only=True,  # Only synced LRC
                    providers=lrc_providers,
                )
            except requests.exceptions.SSLError:
                return None
            except TypeError:
                return None
            except Exception as e:
                logger.debug(f"syncedlyrics LRC search error: {e}")
                return None
            
            if not lyrics_text or len(lyrics_text.strip()) < 10:
                return None
            
            # Verify it's actually synced
            if not _is_lrc_format(lyrics_text):
                logger.debug("syncedlyrics returned non-LRC text despite synced_only=True")
                return None
            
            return Lyrics(
                text=lyrics_text.strip(),
                is_synced=True,
                source="syncedlyrics"
            )
            
        except ImportError:
            logger.debug("syncedlyrics not installed")
            return None
        except Exception as e:
            logger.debug(f"syncedlyrics LRC error: {e}")
            return None
    
    def _try_genius(self, track_name: str, artist: str) -> Lyrics | None:
        """
        Try to fetch lyrics from Genius via web scraping.
        
        Args:
            track_name: Song title.
            artist: Artist name.
        
        Returns:
            Lyrics with is_synced=False if found, None otherwise.
        
        Behavior:
            1. Search Genius for the song
            2. Get the song page URL
            3. Scrape lyrics from the page
        """
        try:
            # Clean search terms
            clean_track = _clean_search_term(track_name)
            clean_artist = _clean_search_term(artist)
            
            # SpotDL format: "{track} - {artist}" (track FIRST!)
            search_query = f"{clean_track} - {clean_artist}"
            
            # Search Genius
            search_url = f"https://genius.com/api/search/song?q={quote_plus(search_query)}"
            
            response = self._session.get(search_url, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                logger.debug(f"Genius search returned {response.status_code}")
                return None
            
            data = response.json()
            hits = data.get('response', {}).get('sections', [{}])[0].get('hits', [])
            
            if not hits:
                logger.debug("No Genius search results")
                return None
            
            # Get the first result's URL
            song_url = hits[0].get('result', {}).get('url')
            if not song_url:
                return None
            
            # Fetch the song page
            time.sleep(BASE_DELAY * 0.5)  # Small delay between requests
            
            page_response = self._session.get(song_url, timeout=REQUEST_TIMEOUT)
            if page_response.status_code != 200:
                return None
            
            # Parse lyrics from page
            soup = BeautifulSoup(page_response.text, 'html.parser')
            
            # Genius uses data-lyrics-container attribute
            lyrics_containers = soup.find_all(attrs={"data-lyrics-container": "true"})
            
            if not lyrics_containers:
                # Fallback: try older class-based selectors
                lyrics_containers = soup.find_all('div', class_=re.compile(r'Lyrics__Container'))
            
            if not lyrics_containers:
                logger.debug("Could not find lyrics container on Genius page")
                return None
            
            # Extract text from containers
            lyrics_parts = []
            for container in lyrics_containers:
                # Replace <br> tags with newlines
                for br in container.find_all('br'):
                    br.replace_with('\n')
                
                text = container.get_text(separator='\n')
                lyrics_parts.append(text)
            
            lyrics_text = '\n'.join(lyrics_parts)
            
            # Clean up the text
            lyrics_text = self._clean_genius_lyrics(lyrics_text)
            
            if len(lyrics_text.strip()) < 10:
                return None
            
            return Lyrics(
                text=lyrics_text.strip(),
                is_synced=False,
                source="genius"
            )
            
        except requests.RequestException as e:
            logger.debug(f"Genius request error: {e}")
            return None
        except Exception as e:
            logger.debug(f"Genius error: {e}")
            return None
    
    def _clean_genius_lyrics(self, text: str) -> str:
        """
        Clean up Genius lyrics text.
        
        Removes:
            - Embed markers like [Verse 1], [Chorus], etc. (optional)
            - Extra whitespace
            - Common artifacts
        
        Args:
            text: Raw lyrics text from Genius.
        
        Returns:
            Cleaned lyrics text.
        """
        # Remove "Embed" text that sometimes appears
        text = re.sub(r'\d*Embed$', '', text, flags=re.MULTILINE)
        
        # Remove "You might also like" text
        text = re.sub(r'You might also like', '', text)
        
        # Normalize whitespace (but preserve single newlines)
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            cleaned_lines.append(line)
        
        # Remove excessive blank lines (more than 2 consecutive)
        result = '\n'.join(cleaned_lines)
        result = re.sub(r'\n{3,}', '\n\n', result)
        
        return result.strip()
    
    def _try_azlyrics(self, track_name: str, artist: str) -> Lyrics | None:
        """
        Try to fetch lyrics from AZLyrics.
        
        Args:
            track_name: Song title.
            artist: Artist name.
        
        Returns:
            Lyrics with is_synced=False if found, None otherwise.
        
        Note:
            AZLyrics has aggressive anti-bot protection.
            May fail frequently due to rate limiting or blocks.
        """
        try:
            # Normalize for AZLyrics URL format
            # Example: azlyrics.com/lyrics/queen/bohemianrhapsody.html
            artist_normalized = _normalize_for_url(artist)
            track_normalized = _normalize_for_url(track_name)
            
            # Handle "The" prefix (AZLyrics removes it)
            if artist_normalized.startswith('the'):
                artist_normalized = artist_normalized[3:]
            
            url = f"https://www.azlyrics.com/lyrics/{artist_normalized}/{track_normalized}.html"
            
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 404:
                logger.debug(f"AZLyrics page not found: {url}")
                return None
            
            if response.status_code != 200:
                logger.debug(f"AZLyrics returned {response.status_code}")
                return None
            
            # Parse the page
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # AZLyrics lyrics are in a div without class, after the comment
            # <!-- Usage of azlyrics.com content... -->
            # We find the main content div
            
            # Find all divs in the main content area
            main_div = soup.find('div', class_='col-xs-12 col-lg-8 text-center')
            if not main_div:
                return None
            
            # The lyrics div is the one without a class attribute,
            # usually after the album art and before the submit corrections div
            lyrics_div = None
            for div in main_div.find_all('div', class_=False, recursive=False):
                # Check if this div contains substantial text
                text = div.get_text(strip=True)
                if len(text) > 100:  # Lyrics should be longer than 100 chars
                    lyrics_div = div
                    break
            
            if not lyrics_div:
                # Alternative: find by looking for the specific structure
                divs = main_div.find_all('div')
                for div in divs:
                    if div.get('class') is None:  # No class attribute
                        text = div.get_text(strip=True)
                        if len(text) > 100:
                            lyrics_div = div
                            break
            
            if not lyrics_div:
                logger.debug("Could not find lyrics div on AZLyrics page")
                return None
            
            # Get the text
            lyrics_text = lyrics_div.get_text(separator='\n')
            
            # Clean up
            lyrics_text = lyrics_text.strip()
            
            # Remove excessive blank lines
            lyrics_text = re.sub(r'\n{3,}', '\n\n', lyrics_text)
            
            if len(lyrics_text) < 10:
                return None
            
            return Lyrics(
                text=lyrics_text,
                is_synced=False,
                source="azlyrics"
            )
            
        except requests.RequestException as e:
            logger.debug(f"AZLyrics request error: {e}")
            return None
        except Exception as e:
            logger.debug(f"AZLyrics error: {e}")
            return None
    
    def _try_musixmatch(self, track_name: str, artist: str) -> Lyrics | None:
        """
        Try to fetch lyrics from MusixMatch.
        
        Note:
            MusixMatch is already covered by syncedlyrics library.
            This method exists for direct API access if needed.
            Currently returns None as syncedlyrics handles this.
        
        Args:
            track_name: Song title.
            artist: Artist name.
        
        Returns:
            Lyrics with is_synced=False if found, None otherwise.
        """
        # MusixMatch requires API key for direct access
        # syncedlyrics already handles MusixMatch through its providers
        # This method is a placeholder for potential direct implementation
        return None


# =============================================================================
# Convenience Functions
# =============================================================================

def fetch_lyrics_for_track(
    track_name: str,
    artist: str,
    album: str | None = None,
    duration_seconds: int | None = None
) -> tuple[Lyrics | None, Lyrics | None]:
    """
    Convenience function to fetch lyrics without creating fetcher instance.
    
    This is a stateless wrapper that creates a LyricsFetcher and
    calls fetch_lyrics(). Use this for one-off requests.
    
    Args:
        track_name: Song title.
        artist: Artist name.
        album: Optional album name.
        duration_seconds: Optional duration.
    
    Returns:
        Tuple of (plain_lyrics, synced_lyrics). Either can be None.
    
    Example:
        plain, synced = fetch_lyrics_for_track("Song Title", "Artist")
        if plain:
            print(plain.text)
        if synced:
            save_lrc(synced.text)
    """
    fetcher = LyricsFetcher()
    return fetcher.fetch_lyrics(track_name, artist, album, duration_seconds)