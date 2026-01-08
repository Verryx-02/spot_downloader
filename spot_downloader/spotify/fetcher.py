"""
Spotify metadata fetcher for spot-downloader (PHASE 1).

This module handles fetching track metadata from Spotify playlists
and Liked Songs, storing them in the Global Track Registry.

PHASE 1 Workflow:
    1. Parse input (playlist URL or --liked flag)
    2. Fetch playlist/liked songs metadata from Spotify API
    3. Batch fetch artist/album data (for genres, publisher, etc.)
    4. Convert to Track objects
    5. Store in Global Track Registry:
       - get_or_create_global_track() for each unique track
       - link_track_to_playlist() for playlist membership
    6. Return tracks for PHASE 2

Sync Mode:
    When --sync is used, compares fetched tracks against database
    and returns only new tracks (not already in playlist).

Batch Optimization:
    Instead of N+2 API calls per track, collects unique IDs and batches:
    - Artists: up to 50 per request
    - Albums: up to 20 per request
    
    100 tracks with 50 artists and 40 albums:
    - Without batching: 200+ API calls
    - With batching: ~4 API calls
"""

from dataclasses import replace
from typing import Any

from spot_downloader.core.database import Database, LIKED_SONGS_KEY
from spot_downloader.core.exceptions import SpotifyError
from spot_downloader.core.logger import get_logger
from spot_downloader.spotify.client import SpotifyClient
from spot_downloader.spotify.models import LikedSongs, Playlist, Track

logger = get_logger(__name__)


def _assign_track_numbers(tracks: list[Track]) -> list[Track]:
    """
    Assign position numbers based on Spotify's order.
    
    Tracks are assigned sequential numbers based on their position
    in the array (which reflects Spotify's order).
    
    For playlists: position 1 = first track in playlist
    For Liked Songs: position 1 = most recently liked (top of list)
    
    Args:
        tracks: List of Track objects in Spotify order.
    
    Returns:
        New list of Track objects with assigned_number set.
    """
    return [
        replace(track, assigned_number=i + 1)
        for i, track in enumerate(tracks)
    ]


class SpotifyFetcher:
    """
    Fetches Spotify metadata and stores in Global Track Registry.
    
    This class encapsulates all PHASE 1 logic:
        - Communicating with Spotify API via SpotifyClient
        - Parsing API responses into Track objects
        - Storing tracks in the Global Track Registry
        - Filtering for sync mode
    """
    
    def __init__(self, database: Database) -> None:
        if not SpotifyClient.is_initialized():
            raise SpotifyError(
                "SpotifyClient not initialized. Call SpotifyClient.init() first.",
                is_auth_error=True
            )
        
        self._client = SpotifyClient()
        self._database = database
    
    def fetch_playlist(
        self,
        playlist_url: str,
        sync_mode: bool = False
    ) -> tuple[Playlist, list[Track]]:
        """
        Fetch all tracks from a Spotify playlist.
        
        Args:
            playlist_url: Full Spotify playlist URL.
            sync_mode: If True, only return tracks not yet downloaded.
        
        Returns:
            Tuple of (Playlist, list[Track]) for subsequent phases.
        
        Raises:
            SpotifyError: If playlist not found or network error.
        """
        logger.info(f"Fetching playlist: {playlist_url}")
        
        # 1. Fetch playlist metadata
        playlist_data = self._client.playlist(playlist_url)
        playlist_id = playlist_data["id"]
        playlist_name = playlist_data.get("name", "Unknown Playlist")
        
        logger.info(f"Playlist: {playlist_name}")
        
        # 2. Fetch all track items
        track_items = self._client.playlist_all_items(playlist_url)
        logger.info(f"Found {len(track_items)} track items")
        
        # 3. Filter valid tracks and collect IDs for batch fetching
        valid_items, artist_ids, album_ids = self._collect_valid_items(track_items)
        
        # 4. Batch fetch artists and albums
        artist_map = self._batch_fetch_artists(artist_ids)
        album_map = self._batch_fetch_albums(album_ids)
        
        # 5. Create Track objects
        tracks = self._create_track_objects(valid_items, artist_map, album_map)
        logger.info(f"Successfully parsed {len(tracks)} tracks")
        
        # 6. Get downloaded track IDs (for sync mode filtering)
        # We filter by downloaded=1, not just "exists in DB", so that tracks
        # added in a previous sync but not downloaded can be retried.
        if sync_mode:
            downloaded_track_ids = self._database.get_downloaded_track_ids(playlist_id)
        else:
            downloaded_track_ids = set()
        
        # 7. Assign position numbers based on Spotify order
        tracks = _assign_track_numbers(tracks)
        
        # 8. Create/update playlist in database
        self._database.add_playlist(
            playlist_id=playlist_id,
            spotify_url=playlist_data.get("external_urls", {}).get("spotify", playlist_url),
            name=playlist_name
        )
        
        # 9. Store tracks in Global Track Registry (updates positions for existing tracks)
        self._store_tracks(tracks, playlist_id)
        
        # 10. Remove orphaned links (tracks removed from Spotify playlist)
        valid_ids = {t.spotify_id for t in tracks}
        removed = self._database.sync_playlist_tracks(playlist_id, valid_ids)
        if removed > 0:
            logger.info(f"Removed {removed} tracks no longer in playlist")
        
        # 11. Filter for sync mode (return only tracks not yet downloaded for phases 2-5)
        if sync_mode:
            new_tracks = [t for t in tracks if t.spotify_id not in downloaded_track_ids]
            logger.info(f"Sync mode: {len(new_tracks)} tracks to process")
            tracks_for_phases = new_tracks
        else:
            tracks_for_phases = tracks
        
        # 12. Create Playlist object
        playlist = Playlist.from_spotify_api(playlist_data, tracks)
        
        return playlist, tracks_for_phases
    
    def fetch_liked_songs(self, sync_mode: bool = False) -> tuple[LikedSongs, list[Track]]:
        """
        Fetch user's Liked Songs (saved tracks).
        
        Args:
            sync_mode: If True, only return tracks not yet downloaded.
        
        Returns:
            Tuple of (LikedSongs, list[Track]) for subsequent phases.
        
        Raises:
            SpotifyError: If user auth not enabled.
        """
        logger.info("Fetching Liked Songs...")
        
        if not self._client.has_user_auth:
            raise SpotifyError(
                "User authentication required for Liked Songs. "
                "Initialize SpotifyClient with user_auth=True.",
                is_auth_error=True
            )
        
        # 1. Fetch all saved tracks
        saved_items = self._client.current_user_all_saved_tracks()
        total_count = len(saved_items)
        logger.info(f"Found {total_count} liked songs")
        
        # 2. Filter valid tracks and collect IDs
        valid_items, artist_ids, album_ids = self._collect_valid_items(saved_items)
        
        # 3. Batch fetch artists and albums
        artist_map = self._batch_fetch_artists(artist_ids)
        album_map = self._batch_fetch_albums(album_ids)
        
        # 4. Create Track objects
        tracks = self._create_track_objects(valid_items, artist_map, album_map)
        logger.info(f"Successfully parsed {len(tracks)} tracks")
        
        # 5. Get downloaded track IDs (for sync mode filtering)
        # We filter by downloaded=1, not just "exists in DB", so that tracks
        # added in a previous sync but not downloaded can be retried.
        if sync_mode:
            downloaded_track_ids = self._database.get_downloaded_liked_songs_ids()
        else:
            downloaded_track_ids = set()
        
        # 6. Assign position numbers based on Spotify order
        # For Liked Songs: position 1 = most recently liked (top of list)
        tracks = _assign_track_numbers(tracks)
        
        # 7. Ensure liked_songs entry exists
        self._database.ensure_liked_songs_exists()
        
        # 8. Store tracks in Global Track Registry (updates positions for existing tracks)
        self._store_tracks(tracks, LIKED_SONGS_KEY)
        
        # 9. Remove orphaned links (tracks removed from Liked Songs)
        valid_ids = {t.spotify_id for t in tracks}
        removed = self._database.sync_playlist_tracks(LIKED_SONGS_KEY, valid_ids)
        if removed > 0:
            logger.info(f"Removed {removed} tracks no longer in Liked Songs")
        
        # 10. Filter for sync mode (return only tracks not yet downloaded for phases 2-5)
        if sync_mode:
            new_tracks = [t for t in tracks if t.spotify_id not in downloaded_track_ids]
            logger.info(f"Sync mode: {len(new_tracks)} tracks to process")
            tracks_for_phases = new_tracks
        else:
            tracks_for_phases = tracks
        
        # 11. Create LikedSongs object
        liked_songs = LikedSongs.from_spotify_api(tracks, total_count)
        
        return liked_songs, tracks_for_phases
    
    # =========================================================================
    # Private Helper Methods
    # =========================================================================
    
    def _collect_valid_items(
        self, 
        items: list[dict[str, Any]]
    ) -> tuple[list[dict], set[str], set[str]]:
        """
        Filter valid tracks and collect artist/album IDs for batch fetching.
        
        Returns:
            Tuple of (valid_items, artist_ids, album_ids)
        """
        valid_items: list[dict[str, Any]] = []
        artist_ids: set[str] = set()
        album_ids: set[str] = set()
        skipped = 0
        
        for item in items:
            if not self._is_valid_track(item):
                skipped += 1
                continue
            
            track_data = item["track"]
            valid_items.append(item)
            
            # Collect primary artist ID (for genres)
            if track_data.get("artists"):
                artist_id = track_data["artists"][0].get("id")
                if artist_id:
                    artist_ids.add(artist_id)
            
            # Collect album ID (for publisher/copyright)
            album_id = track_data.get("album", {}).get("id")
            if album_id:
                album_ids.add(album_id)
        
        if skipped > 0:
            logger.warning(f"Skipped {skipped} invalid tracks (local files, unavailable, etc.)")
        
        logger.debug(f"Unique artists: {len(artist_ids)}, albums: {len(album_ids)}")
        
        return valid_items, artist_ids, album_ids
    
    def _batch_fetch_artists(self, artist_ids: set[str]) -> dict[str, dict]:
        """Batch fetch artist data for genres."""
        if not artist_ids:
            return {}
        
        logger.debug("Batch fetching artist data...")
        artist_map = {}
        
        for artist_data in self._client.artists(list(artist_ids)):
            if artist_data:
                artist_map[artist_data["id"]] = artist_data
        
        return artist_map
    
    def _batch_fetch_albums(self, album_ids: set[str]) -> dict[str, dict]:
        """Batch fetch album data for publisher/copyright."""
        if not album_ids:
            return {}
        
        logger.debug("Batch fetching album data...")
        album_map = {}
        
        for album_data in self._client.albums(list(album_ids)):
            if album_data:
                album_map[album_data["id"]] = album_data
        
        return album_map
    
    def _create_track_objects(
        self,
        valid_items: list[dict],
        artist_map: dict[str, dict],
        album_map: dict[str, dict]
    ) -> list[Track]:
        """Convert API items to Track objects with enriched metadata."""
        tracks = []
        
        for item in valid_items:
            track_data = item["track"]
            added_at = item.get("added_at")
            
            # Get artist data for this track
            artist_data = None
            if track_data.get("artists"):
                primary_artist_id = track_data["artists"][0].get("id")
                if primary_artist_id:
                    artist_data = artist_map.get(primary_artist_id)
            
            # Get album data for this track
            album_data = None
            album_id = track_data.get("album", {}).get("id")
            if album_id:
                album_data = album_map.get(album_id)
            
            track = Track.from_spotify_api(
                track_data=track_data,
                artist_data=artist_data,
                album_data=album_data,
                added_at=added_at
            )
            tracks.append(track)
            logger.debug(f"Processed: {track.artist} - {track.name}")
        
        return tracks
    
    def _store_tracks(self, tracks: list[Track], playlist_id: str) -> None:
        """
        Store tracks in Global Track Registry and link to playlist.
        
        This is the core of the new architecture:
        1. get_or_create_global_track() - adds/updates in global registry
        2. link_track_to_playlist() - creates M:N relationship
        
        Deduplicates by spotify_id (keeps first occurrence if duplicates).
        """
        if not tracks:
            return
        
        # Deduplicate (same song added multiple times to playlist)
        seen_ids: set[str] = set()
        unique_tracks: list[Track] = []
        
        for track in tracks:
            if track.spotify_id not in seen_ids:
                seen_ids.add(track.spotify_id)
                unique_tracks.append(track)
        
        if len(unique_tracks) < len(tracks):
            logger.warning(
                f"Skipped {len(tracks) - len(unique_tracks)} duplicate tracks "
                f"(same song added multiple times)"
            )
        
        # Store each track in Global Track Registry and link to playlist
        for track in unique_tracks:
            # Add/update in global registry (returns database ID)
            track_db_id = self._database.get_or_create_global_track(
                spotify_id=track.spotify_id,
                track_data=track.to_database_dict()
            )
            
            # Link to this playlist with position
            self._database.link_track_to_playlist(
                playlist_id=playlist_id,
                track_db_id=track_db_id,
                position=track.assigned_number,
                added_at=track.added_at
            )
        
        logger.debug(f"Stored {len(unique_tracks)} tracks in Global Track Registry")
    
    def _filter_new_tracks(self, tracks: list[Track], playlist_id: str) -> list[Track]:
        """Filter to only tracks not already in the playlist (for sync mode)."""
        existing_ids = self._database.get_playlist_track_ids(playlist_id)
        new_tracks = [t for t in tracks if t.spotify_id not in existing_ids]
        return new_tracks
    
    @staticmethod
    def _is_valid_track(track_item: dict[str, Any] | None) -> bool:
        """
        Check if a playlist track item is valid and processable.
        
        Invalid tracks:
            - None (removed from Spotify)
            - Local files (is_local = True)
            - Missing track object
            - Podcast episodes (type != 'track')
            - No duration or empty name
        """
        if track_item is None or not isinstance(track_item, dict):
            return False
        
        track = track_item.get("track")
        if track is None:
            return False
        
        if track.get("is_local", False):
            return False
        
        if track.get("type") != "track":
            return False
        
        if not track.get("id"):
            return False
        
        if track.get("duration_ms", 0) == 0:
            return False
        
        if not track.get("name", "").strip():
            return False
        
        return True


# =========================================================================
# Convenience Functions (called by CLI)
# =========================================================================

def fetch_playlist_phase1(
    database: Database,
    playlist_url: str,
    sync_mode: bool = False
) -> tuple[Playlist, list[Track]]:
    """
    PHASE 1 entry point for playlist downloads.
    
    Args:
        database: Database instance.
        playlist_url: Spotify playlist URL.
        sync_mode: Whether to filter to new tracks only.
    
    Returns:
        Tuple of (Playlist, list[Track]) for subsequent phases.
    """
    fetcher = SpotifyFetcher(database)
    return fetcher.fetch_playlist(playlist_url, sync_mode)


def fetch_liked_songs_phase1(
    database: Database,
    sync_mode: bool = False
) -> tuple[LikedSongs, list[Track]]:
    """
    PHASE 1 entry point for --liked downloads.
    
    Args:
        database: Database instance.
        sync_mode: Whether to filter to new tracks only.
    
    Returns:
        Tuple of (LikedSongs, list[Track]) for subsequent phases.
    """
    fetcher = SpotifyFetcher(database)
    return fetcher.fetch_liked_songs(sync_mode)