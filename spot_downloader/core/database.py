"""
Thread-safe SQLite database for spot-downloader.

This module uses a Global Track Registry pattern: each unique Spotify track
is stored once in `global_tracks`, and linked to playlists via `playlist_tracks`.

Schema:
    playlists:          Playlist metadata (id, name, spotify_url, last_synced)
    global_tracks:      One row per unique spotify_id (metadata + processing state)
    playlist_tracks:    Junction table (playlist_id, track_id, position, added_at)

Benefits:
    - Same track in N playlists = 1 download, 1 YouTube match, 1 lyrics fetch
    - --replace updates globally, affecting all playlists automatically
    - No duplicate files on disk

Usage:
    db = Database(output_dir / "database.db")
    
    # PHASE 1: Add tracks
    track_db_id = db.get_or_create_global_track(spotify_id, metadata)
    db.link_track_to_playlist(playlist_id, track_db_id, position=1)
    
    # PHASE 2-5: Process globally
    for track in db.get_tracks_needing_youtube_match():
        db.set_youtube_url(track["spotify_id"], youtube_url)
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from spot_downloader.core.exceptions import DatabaseError


DATABASE_VERSION = 3
LIKED_SONGS_KEY = "__liked_songs__"
YOUTUBE_MATCH_FAILED = "MATCH_FAILED"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spotify_id TEXT UNIQUE NOT NULL,
    spotify_url TEXT,
    name TEXT,
    last_synced TEXT
);

CREATE TABLE IF NOT EXISTS global_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spotify_id TEXT UNIQUE NOT NULL,
    
    -- Spotify metadata
    name TEXT,
    artist TEXT,
    artists TEXT,  -- JSON array
    album TEXT,
    duration_ms INTEGER,
    spotify_url TEXT,
    isrc TEXT,
    cover_url TEXT,
    release_date TEXT,
    track_number INTEGER,
    disc_number INTEGER,
    year INTEGER,
    genres TEXT,  -- JSON array
    publisher TEXT,
    copyright TEXT,
    explicit INTEGER,
    popularity INTEGER,
    preview_url TEXT,
    metadata TEXT,  -- JSON blob for full Spotify response
    
    -- YouTube matching
    youtube_url TEXT,
    match_score REAL,
    match_timestamp TEXT,
    
    -- Download state
    downloaded INTEGER DEFAULT 0,
    download_timestamp TEXT,
    file_path TEXT,
    
    -- Lyrics
    lyrics_fetched INTEGER DEFAULT 0,
    lyrics_text TEXT,
    lyrics_synced INTEGER DEFAULT 0,
    lyrics_source TEXT,
    
    -- Metadata embedding
    metadata_embedded INTEGER DEFAULT 0,
    lyrics_embedded INTEGER DEFAULT 0,
    cover_embedded INTEGER DEFAULT 0,
    
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    added_at TEXT,
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES global_tracks(id) ON DELETE CASCADE,
    UNIQUE(playlist_id, track_id)
);

CREATE INDEX IF NOT EXISTS idx_global_tracks_spotify_id ON global_tracks(spotify_id);
CREATE INDEX IF NOT EXISTS idx_global_tracks_youtube_url ON global_tracks(youtube_url);
CREATE INDEX IF NOT EXISTS idx_global_tracks_downloaded ON global_tracks(downloaded);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks(playlist_id);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track ON playlist_tracks(track_id);
"""


class Database:
    """
    Thread-safe SQLite database with Global Track Registry.
    
    Uses a single persistent connection with thread locking for safety.
    All public methods acquire self._lock before executing.
    """
    
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        
        if not db_path.parent.exists():
            raise DatabaseError(
                f"Parent directory does not exist: {db_path.parent}",
                details={"path": str(db_path.parent)}
            )
        
        try:
            self._init_database()
        except sqlite3.Error as e:
            raise DatabaseError(
                f"Failed to initialize database: {e}",
                details={"path": str(db_path)}
            ) from e
    
    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Get the persistent database connection as a context manager.
        
        The connection is created once and reused for all operations.
        The context manager pattern is kept for compatibility but doesn't
        close the connection on exit.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), 
                timeout=30.0,
                check_same_thread=False  # We handle thread safety with _lock
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
        yield self._conn
    
    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
    
    def __del__(self) -> None:
        """Ensure connection is closed on garbage collection."""
        if hasattr(self, '_conn') and self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
    
    def _init_database(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(_SCHEMA_SQL)
            
            cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cursor.fetchone()
            
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (DATABASE_VERSION,))
            elif row[0] != DATABASE_VERSION:
                raise DatabaseError(
                    f"Database version mismatch: expected {DATABASE_VERSION}, got {row[0]}. "
                    f"Please delete the database and re-run all phases.",
                    details={"expected": DATABASE_VERSION, "actual": row[0]}
                )
            conn.commit()
    
    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()
    
    def _get_playlist_db_id(self, conn: sqlite3.Connection, spotify_id: str) -> int | None:
        cursor = conn.execute("SELECT id FROM playlists WHERE spotify_id = ?", (spotify_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    
    def _serialize_track_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Convert Python types to SQLite-compatible values."""
        row = dict(data)
        
        for field in ["artists", "genres"]:
            if field in row and isinstance(row[field], (list, tuple)):
                row[field] = json.dumps(row[field])
        
        if "metadata" in row and isinstance(row["metadata"], dict):
            row["metadata"] = json.dumps(row["metadata"])
        
        if "explicit" in row:
            row["explicit"] = 1 if row["explicit"] else 0
        
        # Map copyright_text to copyright (database column name)
        if "copyright_text" in row:
            row["copyright"] = row.pop("copyright_text")
        
        return row
    
    def _deserialize_track_row(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert SQLite row to Python dict with proper types."""
        data = dict(row)
        
        for field in ["artists", "genres", "metadata"]:
            if field in data and data[field] is not None:
                try:
                    data[field] = json.loads(data[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        
        for field in ["downloaded", "lyrics_fetched", "lyrics_synced",
                      "metadata_embedded", "lyrics_embedded", "cover_embedded", "explicit"]:
            if field in data and data[field] is not None:
                data[field] = bool(data[field])
        
        return data
    
    # =========================================================================
    # Playlist Operations
    # =========================================================================
    
    def add_playlist(self, playlist_id: str, spotify_url: str, name: str) -> None:
        """Create or update a playlist entry."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO playlists (spotify_id, spotify_url, name, last_synced)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(spotify_id) DO UPDATE SET
                        spotify_url = excluded.spotify_url,
                        name = excluded.name,
                        last_synced = excluded.last_synced
                """, (playlist_id, spotify_url, name, self._now_iso()))
                conn.commit()
    
    def ensure_liked_songs_exists(self) -> None:
        """Ensure the __liked_songs__ playlist entry exists."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO playlists (spotify_id, spotify_url, name, last_synced)
                    VALUES (?, NULL, 'Liked Songs', ?)
                    ON CONFLICT(spotify_id) DO UPDATE SET last_synced = excluded.last_synced
                """, (LIKED_SONGS_KEY, self._now_iso()))
                conn.commit()
    
    def playlist_exists(self, playlist_id: str) -> bool:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT 1 FROM playlists WHERE spotify_id = ?", (playlist_id,))
                return cursor.fetchone() is not None
    
    def get_playlist_info(self, playlist_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT spotify_url, name, last_synced FROM playlists WHERE spotify_id = ?",
                    (playlist_id,)
                )
                row = cursor.fetchone()
                if row:
                    return {"spotify_url": row[0], "name": row[1], "last_synced": row[2]}
                return None
    
    def get_all_playlists(self) -> list[dict[str, Any]]:
        """Get all playlists in the database."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT spotify_id, spotify_url, name, last_synced FROM playlists ORDER BY name"
                )
                return [
                    {
                        "spotify_id": row[0],
                        "spotify_url": row[1],
                        "name": row[2],
                        "last_synced": row[3]
                    }
                    for row in cursor.fetchall()
                ]
    
    def get_active_playlist_id(self) -> str | None:
        """Get the most recently synced playlist ID (for phases 3-5 without --url)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT spotify_id FROM playlists ORDER BY last_synced DESC LIMIT 1"
                )
                row = cursor.fetchone()
                return row[0] if row else None
    
    # =========================================================================
    # Global Track Operations
    # =========================================================================
    
    def get_or_create_global_track(self, spotify_id: str, track_data: dict[str, Any]) -> int:
        """
        Get existing global track or create new one.
        
        Returns the database ID.
        
        If track exists, updates metadata but preserves processing state
        (youtube_url, downloaded, lyrics, etc.).
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT id FROM global_tracks WHERE spotify_id = ?", (spotify_id,))
                row = cursor.fetchone()
                
                if row:
                    self._update_track_metadata(conn, row[0], track_data)
                    return row[0]
                
                return self._insert_global_track(conn, spotify_id, track_data)
    
    def _insert_global_track(self, conn: sqlite3.Connection, spotify_id: str, track_data: dict[str, Any]) -> int:
        row = self._serialize_track_data(track_data)
        now = self._now_iso()
        
        cursor = conn.execute("""
            INSERT INTO global_tracks (
                spotify_id, name, artist, artists, album, duration_ms, spotify_url,
                isrc, cover_url, release_date, track_number, disc_number, year,
                genres, publisher, copyright, explicit, popularity, preview_url,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            spotify_id, row.get("name"), row.get("artist"), row.get("artists"),
            row.get("album"), row.get("duration_ms"), row.get("spotify_url"),
            row.get("isrc"), row.get("cover_url"), row.get("release_date"),
            row.get("track_number"), row.get("disc_number"), row.get("year"),
            row.get("genres"), row.get("publisher"), row.get("copyright"),
            row.get("explicit"), row.get("popularity"), row.get("preview_url"),
            row.get("metadata"), now, now
        ))
        conn.commit()
        return cursor.lastrowid
    
    def _update_track_metadata(self, conn: sqlite3.Connection, track_id: int, track_data: dict[str, Any]) -> None:
        """Update Spotify metadata, preserve processing state."""
        row = self._serialize_track_data(track_data)
        
        conn.execute("""
            UPDATE global_tracks SET
                name = ?, artist = ?, artists = ?, album = ?, duration_ms = ?,
                spotify_url = ?, isrc = ?, cover_url = ?, release_date = ?,
                track_number = ?, disc_number = ?, year = ?, genres = ?,
                publisher = ?, copyright = ?, explicit = ?, popularity = ?,
                preview_url = ?, metadata = ?, updated_at = ?
            WHERE id = ?
        """, (
            row.get("name"), row.get("artist"), row.get("artists"), row.get("album"),
            row.get("duration_ms"), row.get("spotify_url"), row.get("isrc"),
            row.get("cover_url"), row.get("release_date"), row.get("track_number"),
            row.get("disc_number"), row.get("year"), row.get("genres"),
            row.get("publisher"), row.get("copyright"), row.get("explicit"),
            row.get("popularity"), row.get("preview_url"), row.get("metadata"),
            self._now_iso(), track_id
        ))
        conn.commit()
    
    def get_global_track(self, spotify_id: str) -> dict[str, Any] | None:
        """Get a track by its Spotify ID."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM global_tracks WHERE spotify_id = ?", (spotify_id,))
                row = cursor.fetchone()
                if row:
                    data = self._deserialize_track_row(row)
                    data["track_id"] = row["spotify_id"]  # Convenience alias
                    return data
                return None
    
    def get_global_track_by_path(self, file_path: str) -> dict[str, Any] | None:
        """Get a track by its file path (for --replace)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT * FROM global_tracks WHERE file_path = ?", (file_path,))
                row = cursor.fetchone()
                if row:
                    data = self._deserialize_track_row(row)
                    data["track_id"] = row["spotify_id"]
                    return data
                return None
    
    # =========================================================================
    # Playlist-Track Links
    # =========================================================================
    
    def link_track_to_playlist(
        self, 
        playlist_id: str, 
        track_db_id: int, 
        position: int, 
        added_at: str | None = None
    ) -> None:
        """
        Create or update the link between a playlist and a global track.
        
        Args:
            playlist_id: Spotify playlist ID (or LIKED_SONGS_KEY)
            track_db_id: Database ID from get_or_create_global_track()
            position: Track position in playlist (1-indexed)
            added_at: ISO timestamp when track was added to playlist
        """
        with self._lock:
            with self._get_connection() as conn:
                db_playlist_id = self._get_playlist_db_id(conn, playlist_id)
                if db_playlist_id is None:
                    raise DatabaseError(f"Playlist not found: {playlist_id}")
                
                conn.execute("""
                    INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(playlist_id, track_id) DO UPDATE SET
                        position = excluded.position,
                        added_at = COALESCE(excluded.added_at, playlist_tracks.added_at)
                """, (db_playlist_id, track_db_id, position, added_at))
                conn.commit()
    
    def get_playlist_track_ids(self, playlist_id: str) -> set[str]:
        """Get all Spotify track IDs in a playlist (for sync mode filtering)."""
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return set()
                
                cursor = conn.execute("""
                    SELECT g.spotify_id FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ?
                """, (db_id,))
                return {row[0] for row in cursor.fetchall()}
    
    def get_liked_songs_track_ids(self) -> set[str]:
        """Convenience method for sync mode with liked songs."""
        return self.get_playlist_track_ids(LIKED_SONGS_KEY)
    
    def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        """Get all tracks in a playlist, ordered by position."""
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return []
                
                cursor = conn.execute("""
                    SELECT g.*, pt.position, pt.added_at as playlist_added_at
                    FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ?
                    ORDER BY pt.position
                """, (db_id,))
                
                result = []
                for row in cursor.fetchall():
                    track = self._deserialize_track_row(row)
                    track["track_id"] = row["spotify_id"]
                    result.append(track)
                return result
    
    def get_playlists_containing_track(self, spotify_id: str) -> list[dict[str, Any]]:
        """
        Get all playlists containing a track (for --replace and FileManager).
        
        Returns list of dicts with: playlist_spotify_id, name, position
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT p.spotify_id as playlist_spotify_id, p.name, pt.position
                    FROM playlists p
                    JOIN playlist_tracks pt ON p.id = pt.playlist_id
                    JOIN global_tracks g ON pt.track_id = g.id
                    WHERE g.spotify_id = ?
                    ORDER BY p.name
                """, (spotify_id,))
                return [dict(row) for row in cursor.fetchall()]
    
    # =========================================================================
    # Track Lookup for --replace
    # =========================================================================
    
    def get_track_by_playlist_position(
        self,
        playlist_name: str,
        position: int
    ) -> dict[str, Any] | None:
        """
        Get a track by its playlist name and position.
        
        This method is used by --replace to find a track when the user
        provides a path like: ~/Music/Playlists/Liked Songs/00005-Song-Artist.m4a
        
        Args:
            playlist_name: Name of the playlist (from directory name).
                          For Liked Songs, pass LIKED_SONGS_KEY ("__liked_songs__").
            position: Track position in the playlist (1-indexed).
        
        Returns:
            Track data dictionary if found, None otherwise.
            Includes: spotify_id, name, artist, youtube_url, file_path, etc.
        
        Note:
            The lookup uses playlist name (not spotify_id) because that's
            what we can extract from the filesystem path. For regular playlists,
            we match by p.name. For liked songs, we match by p.spotify_id.
        """
        with self._lock:
            with self._get_connection() as conn:
                # For LIKED_SONGS_KEY, match by spotify_id
                # For regular playlists, match by name
                if playlist_name == LIKED_SONGS_KEY:
                    cursor = conn.execute("""
                        SELECT g.* FROM global_tracks g
                        JOIN playlist_tracks pt ON g.id = pt.track_id
                        JOIN playlists p ON pt.playlist_id = p.id
                        WHERE p.spotify_id = ? AND pt.position = ?
                    """, (LIKED_SONGS_KEY, position))
                else:
                    cursor = conn.execute("""
                        SELECT g.* FROM global_tracks g
                        JOIN playlist_tracks pt ON g.id = pt.track_id
                        JOIN playlists p ON pt.playlist_id = p.id
                        WHERE p.name = ? AND pt.position = ?
                    """, (playlist_name, position))
                
                row = cursor.fetchone()
                if row is None:
                    return None
                
                track = self._deserialize_track_row(row)
                track["track_id"] = row["spotify_id"]
                return track
    
    def get_max_position(self, playlist_id: str) -> int:
        """Get the highest position number in a playlist."""
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return 0
                cursor = conn.execute(
                    "SELECT MAX(position) FROM playlist_tracks WHERE playlist_id = ?", (db_id,)
                )
                row = cursor.fetchone()
                return row[0] if row[0] is not None else 0
    
    # =========================================================================
    # Track Processing Queries (Global - not per-playlist)
    # =========================================================================
    
    def get_tracks_needing_youtube_match(self) -> list[dict[str, Any]]:
        """Get all tracks that need YouTube matching (youtube_url IS NULL)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT * FROM global_tracks 
                    WHERE youtube_url IS NULL 
                    ORDER BY created_at
                """)
                return self._fetch_tracks_with_id(cursor)
    
    def get_tracks_needing_download(self) -> list[dict[str, Any]]:
        """Get all tracks matched but not downloaded."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT * FROM global_tracks
                    WHERE youtube_url IS NOT NULL 
                    AND youtube_url != ?
                    AND downloaded = 0
                    ORDER BY created_at
                """, (YOUTUBE_MATCH_FAILED,))
                return self._fetch_tracks_with_id(cursor)
    
    def get_tracks_needing_lyrics(self) -> list[dict[str, Any]]:
        """Get all downloaded tracks that need lyrics fetching."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT * FROM global_tracks
                    WHERE downloaded = 1 AND lyrics_fetched = 0
                    ORDER BY created_at
                """)
                return self._fetch_tracks_with_id(cursor)
    
    def get_tracks_needing_embedding(self) -> list[dict[str, Any]]:
        """
        Get all tracks needing metadata or lyrics embedding.
        
        Returns tracks where:
        - metadata_embedded = 0 (needs full embedding)
        - OR lyrics_embedded = 0 AND lyrics are available (needs lyrics only)
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT * FROM global_tracks
                    WHERE downloaded = 1 AND (
                        metadata_embedded = 0 
                        OR (lyrics_fetched = 1 AND lyrics_text IS NOT NULL AND lyrics_embedded = 0)
                    )
                    ORDER BY created_at
                """)
                return self._fetch_tracks_with_id(cursor)
    
    def _fetch_tracks_with_id(self, cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        """Helper to fetch tracks and add track_id alias."""
        result = []
        for row in cursor.fetchall():
            track = self._deserialize_track_row(row)
            track["track_id"] = row["spotify_id"]
            result.append(track)
        return result
    
    # =========================================================================
    # Track State Updates (Global)
    # =========================================================================
    
    def set_youtube_url(self, spotify_id: str, youtube_url: str, score: float | None = None) -> None:
        """Set YouTube URL for a track (updates globally, affects all playlists)."""
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                cursor = conn.execute("""
                    UPDATE global_tracks 
                    SET youtube_url = ?, match_score = ?, match_timestamp = ?, updated_at = ?
                    WHERE spotify_id = ?
                """, (youtube_url, score, now, now, spotify_id))
                
                if cursor.rowcount == 0:
                    raise DatabaseError(f"Track not found: {spotify_id}")
                conn.commit()
    
    def mark_youtube_match_failed(self, spotify_id: str) -> None:
        """Mark a track as failed to match on YouTube."""
        self.set_youtube_url(spotify_id, YOUTUBE_MATCH_FAILED, score=0.0)
    
    def mark_downloaded(self, spotify_id: str, file_path: Path | str) -> None:
        """Mark track as downloaded with canonical file path."""
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                cursor = conn.execute("""
                    UPDATE global_tracks 
                    SET downloaded = 1, file_path = ?, download_timestamp = ?, updated_at = ?
                    WHERE spotify_id = ?
                """, (str(file_path), now, now, spotify_id))
                
                if cursor.rowcount == 0:
                    raise DatabaseError(f"Track not found: {spotify_id}")
                conn.commit()
    
    def set_lyrics(self, spotify_id: str, lyrics_text: str, is_synced: bool, source: str) -> None:
        """Store fetched lyrics for a track."""
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                cursor = conn.execute("""
                    UPDATE global_tracks 
                    SET lyrics_text = ?, lyrics_synced = ?, lyrics_source = ?, 
                        lyrics_fetched = 1, updated_at = ?
                    WHERE spotify_id = ?
                """, (lyrics_text, 1 if is_synced else 0, source, now, spotify_id))
                
                if cursor.rowcount == 0:
                    raise DatabaseError(f"Track not found: {spotify_id}")
                conn.commit()
    
    def mark_lyrics_not_found(self, spotify_id: str) -> None:
        """Mark that lyrics fetch was attempted but not found."""
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                conn.execute("""
                    UPDATE global_tracks SET lyrics_fetched = 1, updated_at = ?
                    WHERE spotify_id = ?
                """, (now, spotify_id))
                conn.commit()
    
    def mark_metadata_embedded(self, spotify_id: str, new_file_path: Path | str | None = None) -> None:
        """Mark track as having metadata embedded."""
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                if new_file_path:
                    conn.execute("""
                        UPDATE global_tracks 
                        SET metadata_embedded = 1, file_path = ?, updated_at = ?
                        WHERE spotify_id = ?
                    """, (str(new_file_path), now, spotify_id))
                else:
                    conn.execute("""
                        UPDATE global_tracks SET metadata_embedded = 1, updated_at = ?
                        WHERE spotify_id = ?
                    """, (now, spotify_id))
                conn.commit()
    
    def mark_lyrics_embedded(self, spotify_id: str) -> None:
        """Mark track as having lyrics embedded."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE global_tracks SET lyrics_embedded = 1, updated_at = ?
                    WHERE spotify_id = ?
                """, (self._now_iso(), spotify_id))
                conn.commit()
    
    def mark_cover_embedded(self, spotify_id: str) -> None:
        """Mark track as having cover art embedded."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE global_tracks SET cover_embedded = 1, updated_at = ?
                    WHERE spotify_id = ?
                """, (self._now_iso(), spotify_id))
                conn.commit()
    
    def reset_embedding_flags(self, spotify_id: str) -> None:
        """Reset embedding flags after --replace (track needs re-embedding)."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE global_tracks 
                    SET metadata_embedded = 0, lyrics_embedded = 0, cover_embedded = 0, updated_at = ?
                    WHERE spotify_id = ?
                """, (self._now_iso(), spotify_id))
                conn.commit()
    
    def reset_failed_matches(self, playlist_id: str | None = None) -> int:
        """
        Reset failed YouTube matches to allow re-matching.
        
        Args:
            playlist_id: If provided, only reset tracks in this playlist.
                        If None, reset ALL failed matches globally.
        
        Returns:
            Number of tracks reset.
        """
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                
                if playlist_id is not None:
                    db_id = self._get_playlist_db_id(conn, playlist_id)
                    if db_id is None:
                        return 0
                    cursor = conn.execute("""
                        UPDATE global_tracks 
                        SET youtube_url = NULL, match_score = NULL, match_timestamp = NULL, updated_at = ?
                        WHERE youtube_url = ?
                        AND id IN (SELECT track_id FROM playlist_tracks WHERE playlist_id = ?)
                    """, (now, YOUTUBE_MATCH_FAILED, db_id))
                else:
                    cursor = conn.execute("""
                        UPDATE global_tracks 
                        SET youtube_url = NULL, match_score = NULL, match_timestamp = NULL, updated_at = ?
                        WHERE youtube_url = ?
                    """, (now, YOUTUBE_MATCH_FAILED))
                
                conn.commit()
                return cursor.rowcount
    
    def reset_failed_lyrics(self) -> int:
        """
        Reset lyrics_fetched for tracks where lyrics were not found.
        
        This allows re-trying lyrics fetch for tracks that previously failed.
        
        Returns:
            Number of tracks reset.
        """
        with self._lock:
            with self._get_connection() as conn:
                now = self._now_iso()
                cursor = conn.execute("""
                    UPDATE global_tracks 
                    SET lyrics_fetched = 0, updated_at = ?
                    WHERE lyrics_fetched = 1 AND lyrics_text IS NULL
                """, (now,))
                conn.commit()
                return cursor.rowcount
    
    def count_failed_lyrics(self) -> int:
        """
        Count tracks where lyrics fetch was attempted but not found.
        
        Returns:
            Number of tracks with lyrics_fetched=1 and lyrics_text IS NULL.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks
                    WHERE lyrics_fetched = 1 AND lyrics_text IS NULL
                """)
                return cursor.fetchone()[0]
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    def get_playlist_stats(self, playlist_id: str) -> dict[str, int]:
        """Get statistics for a specific playlist."""
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return {
                        "total": 0, "matched": 0, "downloaded": 0,
                        "failed_match": 0, "pending_match": 0, "pending_download": 0
                    }
                
                stats: dict[str, int] = {}
                
                # Total tracks in this playlist
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (db_id,)
                )
                stats["total"] = cursor.fetchone()[0]
                
                # Matched (has youtube_url that isn't MATCH_FAILED)
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ? AND g.youtube_url IS NOT NULL AND g.youtube_url != ?
                """, (db_id, YOUTUBE_MATCH_FAILED))
                stats["matched"] = cursor.fetchone()[0]
                
                # Downloaded
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ? AND g.downloaded = 1
                """, (db_id,))
                stats["downloaded"] = cursor.fetchone()[0]
                
                # Failed match
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ? AND g.youtube_url = ?
                """, (db_id, YOUTUBE_MATCH_FAILED))
                stats["failed_match"] = cursor.fetchone()[0]
                
                # Pending match (no youtube_url)
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ? AND g.youtube_url IS NULL
                """, (db_id,))
                stats["pending_match"] = cursor.fetchone()[0]
                
                # Pending download (matched but not downloaded)
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ? 
                    AND g.youtube_url IS NOT NULL AND g.youtube_url != ?
                    AND g.downloaded = 0
                """, (db_id, YOUTUBE_MATCH_FAILED))
                stats["pending_download"] = cursor.fetchone()[0]
                
                return stats
    
    def get_global_stats(self) -> dict[str, int]:
        """Get global statistics across all playlists."""
        with self._lock:
            with self._get_connection() as conn:
                stats: dict[str, int] = {}
                
                # Total unique tracks
                cursor = conn.execute("SELECT COUNT(*) FROM global_tracks")
                stats["total"] = cursor.fetchone()[0]
                
                # Total playlists
                cursor = conn.execute("SELECT COUNT(*) FROM playlists")
                stats["playlists"] = cursor.fetchone()[0]
                
                # Matched
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE youtube_url IS NOT NULL AND youtube_url != ?",
                    (YOUTUBE_MATCH_FAILED,)
                )
                stats["matched"] = cursor.fetchone()[0]
                
                # Downloaded
                cursor = conn.execute("SELECT COUNT(*) FROM global_tracks WHERE downloaded = 1")
                stats["downloaded"] = cursor.fetchone()[0]
                
                # Failed match
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE youtube_url = ?",
                    (YOUTUBE_MATCH_FAILED,)
                )
                stats["failed_match"] = cursor.fetchone()[0]
                
                # Pending match
                cursor = conn.execute("SELECT COUNT(*) FROM global_tracks WHERE youtube_url IS NULL")
                stats["pending_match"] = cursor.fetchone()[0]
                
                # Pending download
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE youtube_url IS NOT NULL AND youtube_url != ? AND downloaded = 0",
                    (YOUTUBE_MATCH_FAILED,)
                )
                stats["pending_download"] = cursor.fetchone()[0]
                
                # Lyrics statistics
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE lyrics_text IS NOT NULL")
                stats["with_lyrics"] = cursor.fetchone()[0]
                
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE lyrics_synced = 1")
                stats["lyrics_synced"] = cursor.fetchone()[0]
                
                stats["lyrics_plain"] = stats["with_lyrics"] - stats["lyrics_synced"]
                
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE lyrics_fetched = 1 AND lyrics_text IS NULL")
                stats["without_lyrics"] = cursor.fetchone()[0]
                
                # LRC files = synced lyrics count
                stats["lrc_files"] = stats["lyrics_synced"]
                
                # LRC hard links = count of playlist links for tracks with synced lyrics
                cursor = conn.execute("""
                    SELECT COUNT(*) FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE g.lyrics_synced = 1
                """)
                stats["lrc_hard_links"] = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM playlist_tracks")
                stats["playlist_track_links"] = cursor.fetchone()[0]
                
                # Deduplication ratio
                stats["deduplication_ratio"] = (
                    round(stats["playlist_track_links"] / stats["total"], 2)
                    if stats["total"] > 0 else 0
                )
                
                # Embedding statistics
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE metadata_embedded = 1")
                stats["metadata_embedded"] = cursor.fetchone()[0]
                
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE lyrics_embedded = 1")
                stats["lyrics_embedded"] = cursor.fetchone()[0]
                
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM global_tracks WHERE cover_embedded = 1")
                stats["cover_embedded"] = cursor.fetchone()[0]
                
                return stats
    
    # =========================================================================
    # Sync Change Detection & Playlist Management
    # =========================================================================
    
    def get_playlist_tracks_snapshot(self, playlist_id: str) -> dict[str, int]:
        """
        Get current state of playlist as {spotify_track_id: position}.
        
        Used for sync change detection to compare with Spotify's current state.
        
        Args:
            playlist_id: Spotify playlist ID.
        
        Returns:
            Dictionary mapping spotify_id to position for all tracks in playlist.
        """
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return {}
                
                cursor = conn.execute("""
                    SELECT g.spotify_id, pt.position
                    FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ?
                """, (db_id,))
                
                return {row[0]: row[1] for row in cursor.fetchall()}
    
    def clear_playlist_tracks(self, playlist_id: str) -> int:
        """
        Remove all track links for a playlist.
        
        The global_tracks entries are preserved (tracks may still be in other
        playlists). Only the playlist_tracks junction entries are deleted.
        
        Args:
            playlist_id: Spotify playlist ID.
        
        Returns:
            Number of links removed.
        """
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return 0
                
                cursor = conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = ?", (db_id,)
                )
                conn.commit()
                return cursor.rowcount
    
    def delete_playlist(self, playlist_id: str) -> bool:
        """
        Delete a playlist and all its track links.
        
        The global_tracks entries are preserved (tracks may still be in other
        playlists). The cascade delete removes playlist_tracks entries.
        
        Args:
            playlist_id: Spotify playlist ID.
        
        Returns:
            True if playlist existed and was deleted, False otherwise.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM playlists WHERE spotify_id = ?", (playlist_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
    
    def get_playlist_tracks_for_export(self, playlist_id: str) -> list[dict[str, Any]]:
        """
        Get tracks with all info needed for M3U export.
        
        Returns only downloaded tracks with their file paths.
        
        Args:
            playlist_id: Spotify playlist ID.
        
        Returns:
            List of dicts with: position, name, artist, duration_ms, file_path
            Ordered by position.
        """
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return []
                
                cursor = conn.execute("""
                    SELECT pt.position, g.name, g.artist, g.duration_ms, g.file_path
                    FROM global_tracks g
                    JOIN playlist_tracks pt ON g.id = pt.track_id
                    WHERE pt.playlist_id = ? AND g.downloaded = 1 AND g.file_path IS NOT NULL
                    ORDER BY pt.position
                """, (db_id,))
                
                return [
                    {
                        "position": row[0],
                        "name": row[1],
                        "artist": row[2],
                        "duration_ms": row[3],
                        "file_path": row[4]
                    }
                    for row in cursor.fetchall()
                ]
    
    def get_all_downloaded_tracks(self) -> list[dict[str, Any]]:
        """
        Get all downloaded tracks for export.
        
        Returns:
            List of dicts with: spotify_id, name, artist, file_path
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT spotify_id, name, artist, file_path
                    FROM global_tracks
                    WHERE downloaded = 1 AND file_path IS NOT NULL
                    ORDER BY artist, name
                """)
                
                return [
                    {
                        "spotify_id": row[0],
                        "name": row[1],
                        "artist": row[2],
                        "file_path": row[3]
                    }
                    for row in cursor.fetchall()
                ]
    
    def sync_playlist_tracks(
        self,
        playlist_id: str,
        valid_spotify_ids: set[str]
    ) -> int:
        """
        Remove playlist_tracks entries for tracks no longer in the playlist.
        
        Called after storing tracks to remove orphaned links (tracks that
        were removed from the Spotify playlist).
        
        Args:
            playlist_id: Spotify playlist ID.
            valid_spotify_ids: Set of spotify_ids that SHOULD be in the playlist.
        
        Returns:
            Number of orphaned links removed.
        """
        with self._lock:
            with self._get_connection() as conn:
                db_id = self._get_playlist_db_id(conn, playlist_id)
                if db_id is None:
                    return 0
                
                # Find track_ids that are in playlist_tracks but not in valid_spotify_ids
                # and delete them
                if not valid_spotify_ids:
                    # If no valid IDs, remove all links
                    cursor = conn.execute(
                        "DELETE FROM playlist_tracks WHERE playlist_id = ?",
                        (db_id,)
                    )
                else:
                    # Build placeholders for IN clause
                    placeholders = ",".join("?" for _ in valid_spotify_ids)
                    cursor = conn.execute(f"""
                        DELETE FROM playlist_tracks 
                        WHERE playlist_id = ? 
                        AND track_id NOT IN (
                            SELECT id FROM global_tracks 
                            WHERE spotify_id IN ({placeholders})
                        )
                    """, (db_id, *valid_spotify_ids))
                
                conn.commit()
                return cursor.rowcount
