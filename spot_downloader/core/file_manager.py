"""
File management for spot-downloader.

This module handles the file storage architecture with a central tracks/
directory and hard links in playlist directories.

Architecture:
    output_directory/
    ├── spot_downloader.db
    ├── tracks/                               # Central storage (canonical files)
    │   ├── Bohemian Rhapsody-Queen.m4a
    │   ├── Bohemian Rhapsody-Queen.lrc       # LRC file (if synced lyrics)
    │   ├── Hey Jude-The Beatles.m4a
    │   └── Back In Black-AC_DC.m4a
    ├── logs/
    │   └── ...
    └── Playlists/                            # Playlist views container
        ├── My Playlist/                      # Playlist view (hard links)
        │   ├── 00001-Bohemian Rhapsody-Queen.m4a → ../../tracks/Bohemian Rhapsody-Queen.m4a
        │   ├── 00001-Bohemian Rhapsody-Queen.lrc → ../../tracks/Bohemian Rhapsody-Queen.lrc
        │   ├── 00002-Hey Jude-The Beatles.m4a    → ../../tracks/Hey Jude-The Beatles.m4a
        │   └── 00003-Back In Black-AC_DC.m4a     → ../../tracks/Back In Black-AC_DC.m4a
        └── Another Playlist/
            ├── 00001-Back In Black-AC_DC.m4a     → ../../tracks/Back In Black-AC_DC.m4a
            └── 00002-Bohemian Rhapsody-Queen.m4a → ../../tracks/Bohemian Rhapsody-Queen.m4a

File Naming:
    - Canonical (in tracks/): {title}-{artist}.m4a
    - Playlist links: {position:05d}-{title}-{artist}.m4a
    
    Position uses 5-digit padding (00001-99999) to support Spotify's
    maximum of 10,000 tracks per playlist.

Hard Links vs Symlinks:
    - Hard links are preferred (same inode, no storage duplication)
    - Symlinks used as fallback for cross-filesystem scenarios

Usage:
    from spot_downloader.core.file_manager import FileManager
    
    fm = FileManager(output_dir)
    
    # Get canonical path for download
    canonical = fm.get_canonical_path("Queen", "Bohemian Rhapsody")
    # Returns: tracks/Bohemian Rhapsody-Queen.m4a
    
    # After download, create links in all playlists
    fm.create_playlist_link(canonical, "My Playlist", 1, "Bohemian Rhapsody", "Queen")
"""

import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spot_downloader.core.database import Database


# Characters that are invalid in filenames on various operating systems
_INVALID_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Maximum filename length (conservative for cross-platform compatibility)
_MAX_FILENAME_LENGTH = 200


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use in a filename.
    
    Args:
        name: The string to sanitize.
    
    Returns:
        A sanitized string safe for use in filenames.
    
    Behavior:
        - Replaces invalid characters with underscores
        - Strips leading/trailing whitespace and dots
        - Truncates to maximum length
        - Returns "Unknown" if result is empty
    """
    if not name:
        return "Unknown"
    
    # Replace invalid characters
    result = _INVALID_CHARS_PATTERN.sub("_", name)
    
    # Strip whitespace and dots (dots at start can hide files on Unix)
    result = result.strip(" .")
    
    # Truncate if too long
    if len(result) > _MAX_FILENAME_LENGTH:
        result = result[:_MAX_FILENAME_LENGTH].rstrip(" .")
    
    return result if result else "Unknown"


class FileManager:
    """
    Manages file storage with hard links for playlist views.
    
    This class implements the central storage pattern where:
    - Audio files are stored once in tracks/ directory
    - Playlist directories contain hard links with position-based names
    
    Attributes:
        output_dir: Base output directory.
        tracks_dir: Central tracks storage directory.
    """
    
    def __init__(self, output_dir: Path) -> None:
        """
        Initialize FileManager.
        
        Args:
            output_dir: Base output directory for all files.
        
        Behavior:
            Creates tracks/ and Playlists/ directories if they don't exist.
        """
        self.output_dir = output_dir
        self.tracks_dir = output_dir / "tracks"
        self.playlists_dir = output_dir / "Playlists"
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        self.playlists_dir.mkdir(parents=True, exist_ok=True)
    
    def get_canonical_filename(self, artist: str, title: str) -> str:
        """
        Generate canonical filename (no position number).
        
        Format: {title}-{artist}.m4a
        
        Args:
            artist: Artist name.
            title: Track title.
        
        Returns:
            Sanitized filename.
        
        Example:
            get_canonical_filename("Queen", "Bohemian Rhapsody")
            # Returns: "Bohemian Rhapsody-Queen.m4a"
        """
        safe_artist = sanitize_filename(artist)
        safe_title = sanitize_filename(title)
        return f"{safe_title}-{safe_artist}.m4a"
    
    def get_canonical_path(self, artist: str, title: str) -> Path:
        """
        Get full path in central tracks/ directory.
        
        Args:
            artist: Artist name.
            title: Track title.
        
        Returns:
            Path to file in tracks/ directory.
        """
        return self.tracks_dir / self.get_canonical_filename(artist, title)
    
    def get_playlist_filename(self, position: int, title: str, artist: str) -> str:
        """
        Generate playlist-specific filename with position.
        
        Format: {position:05d}-{title}-{artist}.m4a
        
        Uses 5-digit padding to support up to 99,999 tracks
        (Spotify max is 10,000 per playlist).
        
        Args:
            position: Position in playlist (1-indexed).
            title: Track title.
            artist: Artist name.
        
        Returns:
            Sanitized filename with position prefix.
        
        Example:
            get_playlist_filename(42, "Bohemian Rhapsody", "Queen")
            # Returns: "00042-Bohemian Rhapsody-Queen.m4a"
        """
        safe_title = sanitize_filename(title)
        safe_artist = sanitize_filename(artist)
        return f"{position:05d}-{safe_title}-{safe_artist}.m4a"
    
    def get_playlist_dir(self, playlist_name: str) -> Path:
        """
        Get or create playlist directory.
        
        Args:
            playlist_name: Human-readable playlist name.
        
        Returns:
            Path to playlist directory (created if needed).
        """
        safe_name = sanitize_filename(playlist_name)
        playlist_dir = self.playlists_dir / safe_name
        playlist_dir.mkdir(parents=True, exist_ok=True)
        return playlist_dir
    
    def create_playlist_link(
        self,
        canonical_path: Path,
        playlist_name: str,
        position: int,
        title: str,
        artist: str
    ) -> Path:
        """
        Create hard link in playlist directory.
        
        Args:
            canonical_path: Path to canonical file in tracks/.
            playlist_name: Playlist name for directory.
            position: Track position in playlist.
            title: Track title (for filename).
            artist: Artist name (for filename).
        
        Returns:
            Path to the created link in playlist directory.
        
        Behavior:
            - Tries hard link first (same filesystem)
            - Falls back to symlink (cross-filesystem)
            - Removes existing link if present
            - Also creates LRC link if LRC file exists
        
        Raises:
            FileNotFoundError: If canonical_path doesn't exist.
        """
        if not canonical_path.exists():
            raise FileNotFoundError(f"Canonical file not found: {canonical_path}")
        
        playlist_dir = self.get_playlist_dir(playlist_name)
        link_name = self.get_playlist_filename(position, title, artist)
        link_path = playlist_dir / link_name
        
        # Remove existing link if present
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        
        try:
            # Try hard link first
            os.link(canonical_path, link_path)
        except OSError:
            # Fall back to symlink (cross-filesystem)
            link_path.symlink_to(canonical_path.resolve())
        
        # Also create LRC link if LRC file exists
        lrc_canonical = canonical_path.with_suffix(".lrc")
        if lrc_canonical.exists():
            lrc_link_path = link_path.with_suffix(".lrc")
            
            # Remove existing LRC link if present
            if lrc_link_path.exists() or lrc_link_path.is_symlink():
                lrc_link_path.unlink()
            
            try:
                os.link(lrc_canonical, lrc_link_path)
            except OSError:
                try:
                    lrc_link_path.symlink_to(lrc_canonical.resolve())
                except OSError:
                    pass  # LRC link failure is not critical
        
        return link_path
    
    def update_playlist_links_from_db(
        self,
        database: "Database",
        spotify_id: str,
        canonical_path: Path,
        title: str,
        artist: str
    ) -> int:
        """
        Create/update hard links in all playlists containing a track.
        
        This is called after downloading a track to ensure it appears
        in all playlist directories that reference it.
        
        Args:
            database: Database instance for querying playlists.
            spotify_id: Spotify track ID.
            canonical_path: Path to file in tracks/.
            title: Track title.
            artist: Artist name.
        
        Returns:
            Number of links successfully created.
        """
        playlists = database.get_playlists_containing_track(spotify_id)
        created = 0
        
        for playlist in playlists:
            playlist_spotify_id = playlist["playlist_spotify_id"]
            position = playlist["position"]
            playlist_name = playlist["name"]
            
            # Map LIKED_SONGS_KEY to "Liked Songs" for directory name
            if playlist_spotify_id == "__liked_songs__":
                playlist_name = "Liked Songs"
            
            try:
                self.create_playlist_link(
                    canonical_path=canonical_path,
                    playlist_name=playlist_name,
                    position=position,
                    title=title,
                    artist=artist
                )
                created += 1
            except Exception:
                pass
        
        return created
    
    def remove_orphaned_links(self, playlist_name: str, valid_positions: set[int]) -> int:
        """
        Remove playlist links that are no longer in the playlist.
        
        Called during sync to remove tracks that were deleted from
        the Spotify playlist.
        
        Args:
            playlist_name: Playlist name.
            valid_positions: Set of positions that should exist.
        
        Returns:
            Number of orphaned links removed.
        """
        safe_name = sanitize_filename(playlist_name)
        playlist_dir = self.playlists_dir / safe_name
        
        if not playlist_dir.exists():
            return 0
        
        removed = 0
        for link in playlist_dir.iterdir():
            if not link.name.endswith(".m4a"):
                continue
            
            # Extract position from filename (first 5 digits)
            try:
                position = int(link.name[:5])
            except ValueError:
                continue
            
            if position not in valid_positions:
                try:
                    link.unlink()
                    # Also remove LRC if exists
                    lrc_path = link.with_suffix(".lrc")
                    if lrc_path.exists():
                        lrc_path.unlink()
                    removed += 1
                except OSError:
                    pass
        
        return removed
    
    def get_track_file_count(self) -> int:
        """Get number of audio files in tracks/ directory."""
        return sum(1 for f in self.tracks_dir.iterdir() if f.suffix.lower() == ".m4a")
    
    def get_total_size_bytes(self) -> int:
        """Get total size of files in tracks/ directory."""
        return sum(
            f.stat().st_size
            for f in self.tracks_dir.iterdir()
            if f.is_file()
        )
    
    def file_exists_in_tracks(self, artist: str, title: str) -> bool:
        """Check if a track file already exists."""
        return self.get_canonical_path(artist, title).exists()
    
    # =========================================================================
    # Sync Rebuild Operations
    # =========================================================================
    
    def delete_playlist_directory(self, playlist_name: str) -> bool:
        """
        Delete a playlist directory and all its contents.
        
        Since playlist directories contain only hard links (not actual files),
        this operation is very fast and doesn't delete any audio data.
        
        Args:
            playlist_name: Human-readable playlist name.
        
        Returns:
            True if directory existed and was deleted, False otherwise.
        """
        safe_name = sanitize_filename(playlist_name)
        playlist_dir = self.playlists_dir / safe_name
        
        if not playlist_dir.exists():
            return False
        
        shutil.rmtree(playlist_dir)
        return True
    
    def rebuild_playlist_from_tracks(
        self,
        playlist_name: str,
        tracks: list[dict]
    ) -> int:
        """
        Recreate playlist directory with correct hard links.
        
        Deletes existing directory and creates new links based on
        the provided track list.
        
        Args:
            playlist_name: Human-readable playlist name.
            tracks: List of dicts with: position, name, artist, file_path
                   (from Database.get_playlist_tracks_for_export)
        
        Returns:
            Number of links successfully created.
        """
        # Delete existing directory
        self.delete_playlist_directory(playlist_name)
        
        # Create new links
        created = 0
        for track in tracks:
            canonical_path = Path(track["file_path"])
            if not canonical_path.exists():
                continue
            
            try:
                self.create_playlist_link(
                    canonical_path=canonical_path,
                    playlist_name=playlist_name,
                    position=track["position"],
                    title=track["name"],
                    artist=track["artist"]
                )
                created += 1
            except Exception:
                pass
        
        return created
    
    # =========================================================================
    # Export Operations
    # =========================================================================
    
    def export_playlist_m3u(
        self,
        playlist_name: str,
        tracks: list[dict],
        export_dir: Path,
        tracks_subdir: str = "tracks"
    ) -> Path:
        """
        Generate extended M3U playlist file.
        
        Creates an M3U file with #EXTINF metadata for each track.
        The M3U references tracks in a relative 'tracks/' subdirectory.
        
        Args:
            playlist_name: Human-readable playlist name.
            tracks: List of dicts with: position, name, artist, duration_ms, file_path
            export_dir: Directory where M3U file will be created.
            tracks_subdir: Subdirectory name for tracks (default: "tracks").
        
        Returns:
            Path to the created M3U file.
        """
        export_dir.mkdir(parents=True, exist_ok=True)
        
        safe_name = sanitize_filename(playlist_name)
        m3u_path = export_dir / f"{safe_name}.m3u"
        
        with open(m3u_path, "w", encoding="utf-8") as f:
            # Extended M3U header
            f.write("#EXTM3U\n")
            
            for track in tracks:
                # Duration in seconds (M3U uses seconds, not milliseconds)
                duration_sec = (track.get("duration_ms") or 0) // 1000
                
                artist = track.get("artist", "Unknown")
                name = track.get("name", "Unknown")
                
                # Get just the filename from the canonical path
                canonical_path = Path(track["file_path"])
                filename = canonical_path.name
                
                # #EXTINF:duration,Artist - Title
                f.write(f"#EXTINF:{duration_sec},{artist} - {name}\n")
                # Relative path to track
                f.write(f"{tracks_subdir}/{filename}\n")
        
        return m3u_path
    
    def export_playlist_copy(
        self,
        playlist_name: str,
        tracks: list[dict],
        export_dir: Path
    ) -> tuple[Path, int]:
        """
        Export playlist as folder with actual file copies.
        
        Creates a playlist folder with numbered copies of the audio files.
        Also copies any .lrc files that exist alongside the audio files.
        
        Args:
            playlist_name: Human-readable playlist name.
            tracks: List of dicts with: position, name, artist, file_path
            export_dir: Base export directory.
        
        Returns:
            Tuple of (playlist_folder_path, number_of_audio_files_copied).
        """
        safe_name = sanitize_filename(playlist_name)
        playlist_folder = export_dir / safe_name
        playlist_folder.mkdir(parents=True, exist_ok=True)
        
        copied = 0
        for track in tracks:
            src_path = Path(track["file_path"])
            if not src_path.exists():
                continue
            
            # Generate position-prefixed filename
            dest_filename = self.get_playlist_filename(
                position=track["position"],
                title=track["name"],
                artist=track["artist"]
            )
            dest_path = playlist_folder / dest_filename
            
            try:
                shutil.copy2(src_path, dest_path)
                copied += 1
                
                # Copy LRC file if exists
                lrc_src = src_path.with_suffix(".lrc")
                if lrc_src.exists():
                    lrc_dest = dest_path.with_suffix(".lrc")
                    try:
                        shutil.copy2(lrc_src, lrc_dest)
                    except Exception:
                        pass  # LRC copy failure is not critical
                        
            except Exception:
                pass
        
        return playlist_folder, copied
    
    def copy_tracks_to_export(
        self,
        tracks: list[dict],
        export_dir: Path,
        tracks_subdir: str = "tracks"
    ) -> int:
        """
        Copy master audio files and LRC files to export directory.
        
        Used for M3U export to create the tracks/ folder with actual files.
        Also copies any .lrc files that exist alongside the audio files.
        
        Args:
            tracks: List of dicts with file_path key.
            export_dir: Base export directory.
            tracks_subdir: Subdirectory name for tracks (default: "tracks").
        
        Returns:
            Number of audio files successfully copied.
        """
        tracks_folder = export_dir / tracks_subdir
        tracks_folder.mkdir(parents=True, exist_ok=True)
        
        copied = 0
        seen_files: set[str] = set()
        
        for track in tracks:
            src_path = Path(track["file_path"])
            if not src_path.exists():
                continue
            
            # Skip duplicates (same track in multiple playlists)
            if src_path.name in seen_files:
                continue
            seen_files.add(src_path.name)
            
            dest_path = tracks_folder / src_path.name
            
            # Copy audio file if not already exists
            if not dest_path.exists():
                try:
                    shutil.copy2(src_path, dest_path)
                except Exception:
                    continue
            
            copied += 1
            
            # Copy LRC file if exists
            lrc_src = src_path.with_suffix(".lrc")
            if lrc_src.exists():
                lrc_dest = dest_path.with_suffix(".lrc")
                if not lrc_dest.exists():
                    try:
                        shutil.copy2(lrc_src, lrc_dest)
                    except Exception:
                        pass  # LRC copy failure is not critical
        
        return copied