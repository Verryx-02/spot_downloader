"""
Command-line interface for spot-downloader.

This module implements the CLI using Click, providing all commands
for downloading Spotify playlists via YouTube Music.
rich-click is used for the output colors.

Commands:
    spot --url <playlist_url>           Download a playlist (all phases)
    spot --liked                        Download liked songs (all phases)
    spot --url <url> --sync             Sync mode (only new tracks)
    spot --sync                         Sync ALL known playlists + liked songs
    spot --sync --no-liked              Sync ALL playlists (no Spotify login needed)
    spot --1 --url <url>                Run only PHASE 1 (fetch metadata)
    spot --2                            Run only PHASE 2 (YouTube match)
    spot --3                            Run only PHASE 3 (download audio)
    spot --4                            Run only PHASE 4 (fetch lyrics)
    spot --5                            Run only PHASE 5 (embed metadata)
    spot --replace <file> <youtube_url> Replace audio in existing file

Options:
    --cookie-file <path>                Path to cookies.txt for YT Premium

Usage:
    # Download entire playlist (all 5 phases)
    spot --url "https://open.spotify.com/playlist/..."
    
    # Sync mode (download only new tracks)
    spot --url "https://open.spotify.com/playlist/..." --sync
    
    # Sync ALL known playlists and liked songs
    spot --sync
    
    # Sync ALL playlists only (no Spotify login required)
    spot --sync --no-liked
    
    # Download liked songs
    spot --liked
    
    # Run phases separately
    spot --1 --url "https://..."        # Fetch Spotify metadata
    spot --2                            # Match on YouTube
    spot --3                            # Download audio
    spot --4                            # Fetch lyrics
    spot --5                            # Embed metadata and lyrics
    
    # Replace audio in existing file
    spot --replace ~/Music/01-Song-Artist.m4a "https://youtube.com/watch?v=..."

Configuration:
    The CLI requires a config.yaml file in the current directory with:
    - Spotify API credentials (client_id, client_secret)
    - Output directory path
    - Number of download threads
    - Optional cookie file path

Phase Dependencies:
    --1 (fetch metadata): Requires --url or --liked. Creates database entries.
    --2 (YouTube match): Requires tracks in database without youtube_url.
    --3 (download audio): Requires tracks with youtube_url but not downloaded.
    --4 (fetch lyrics): Requires tracks downloaded but lyrics not fetched.
    --5 (embed metadata): Requires tracks downloaded but metadata not embedded.
    
    When running without phase flags, all 5 phases run in sequence.
"""

import sys
from pathlib import Path
from typing import Optional

import rich_click as click

# Configure rich-click for better help formatting
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_ERRORS_SUGGESTION = "magenta italic"
click.rich_click.ERRORS_SUGGESTION = ""
click.rich_click.MAX_WIDTH = 100
click.rich_click.OPTION_GROUPS = {
    "cli": [
        {
            "name": "Input Sources",
            "options": ["--url", "--liked"],
        },
        {
            "name": "Sync Options",
            "options": ["--sync", "--no-liked"],
        },
        {
            "name": "Export Options",
            "options": ["--export-all", "--export", "--copy-files"],
        },
        {
            "name": "Phase Selection",
            "options": ["--1", "--2", "--3", "--4", "--5"],
        },
        {
            "name": "Advanced Options",
            "options": ["--replace", "--cookie-file", "--force-rematch"],
        },
        {
            "name": "Info",
            "options": ["--version", "--help"],
        },
    ],
}

from spot_downloader.core import (
    Config,
    ConfigError,
    Database,
    DatabaseError,
    DownloadError,
    MetadataError,
    FileManager,
    SpotDownloaderError,
    SpotifyError,
    get_logger,
    load_config,
    setup_logging,
    shutdown_logging,
)
from spot_downloader.spotify import Track
from spot_downloader.core.database import LIKED_SONGS_KEY
from spot_downloader.download import (
    download_tracks_phase3,
    fetch_lyrics_phase4,
    embed_metadata_phase5,
)
from spot_downloader.download.lyrics_phase import LyricsStats
from spot_downloader.utils.replace import replace_track_audio
from spot_downloader.spotify import (
    SpotifyClient,
    fetch_liked_songs_phase1,
    fetch_playlist_phase1,
)
from spot_downloader.utils import ensure_directory, extract_playlist_id
from spot_downloader.youtube import match_tracks_phase2, get_tracks_needing_match

logger = get_logger(__name__)


# Version string (if updated, update also in config.toml)
__version__ = "0.4.0"


@click.group(invoke_without_command=True)
@click.option(
    "--url",
    type=str,
    default=None,
    metavar="<spotify-url>",
    help="Spotify playlist URL"
)
@click.option(
    "--liked",
    is_flag=True,
    help="Download user's Liked Songs"
)
@click.option(
    "--sync",
    is_flag=True,
    help="Sync mode: new tracks + playlist changes detection"
)
@click.option(
    "--no-liked",
    is_flag=True,
    help="Skip Liked Songs in sync mode"
)
@click.option(
    "--1", "phase1_only",
    is_flag=True,
    help="PHASE 1: Fetch Spotify metadata"
)
@click.option(
    "--2", "phase2_only",
    is_flag=True,
    help="PHASE 2: Match tracks on YouTube Music"
)
@click.option(
    "--3", "phase3_only",
    is_flag=True,
    help="PHASE 3: Download audio files"
)
@click.option(
    "--4", "phase4_only",
    is_flag=True,
    help="PHASE 4: Fetch lyrics"
)
@click.option(
    "--5", "phase5_only",
    is_flag=True,
    help="PHASE 5: Embed metadata and lyrics"
)
@click.option(
    "--export-all",
    is_flag=True,
    help="Export all playlists as M3U"
)
@click.option(
    "--export",
    type=str,
    default=None,
    metavar="<playlist-name>",
    help="Export specific playlist as M3U"
)
@click.option(
    "--copy-files",
    is_flag=True,
    help="Export as folder copies instead of M3U"
)
@click.option(
    "--replace",
    nargs=2,
    type=(click.Path(path_type=Path), str),
    default=None,
    metavar="<file.m4a> <youtube-url>",
    help="Replace audio in existing M4A file"
)
@click.option(
    "--cookie-file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    metavar="<cookies.txt>",
    help="Cookies for YouTube Music Premium quality"
)
@click.option(
    "--force-rematch",
    is_flag=True,
    help="Retry failed YouTube matches"
)
@click.option(
    "--version",
    is_flag=True,
    help="Show version and exit."
)
@click.pass_context
def cli(
    ctx: click.Context,
    url: Optional[str],
    liked: bool,
    sync: bool,
    no_liked: bool,
    phase1_only: bool,
    phase2_only: bool,
    phase3_only: bool,
    phase4_only: bool,
    phase5_only: bool,
    export_all: bool,
    export: Optional[str],
    copy_files: bool,
    replace: Optional[tuple[Path, str]],
    cookie_file: Optional[Path],
    force_rematch: bool,
    version: bool
) -> None:
    """
    spot-downloader: Download Spotify playlists via YouTube Music.
    
    Downloads tracks from Spotify playlists by matching them on YouTube Music
    and downloading the audio in M4A format with full metadata.
    
    \b
    BASIC USAGE:
        spot --url "https://open.spotify.com/playlist/..."    # Download playlist
        spot --liked                                          # Download Liked Songs
    
    \b
    SYNC MODE:
        spot --url "https://..." --sync        # Sync specific playlist
        spot --sync                            # Sync ALL playlists + Liked Songs
        spot --sync --no-liked                 # Sync ALL playlists (skip Liked Songs)
        
        Sync mode downloads new tracks and detects playlist changes
        (removed tracks, position changes). Prompts before applying changes locally.
    
    \b
    EXPORT:
        spot --export-all                         # Export all playlists as M3U
        spot --export "My Playlist"               # Export single playlist as M3U
        spot --export-all --copy-files            # Export all as folder copies
        spot --export "My Playlist" --copy-files  # Export single as folder copy
    
    \b
    PHASE-BY-PHASE:
        spot --1 --url "https://..."           # Fetch Spotify metadata only
        spot --2                               # Match on YouTube Music only
        spot --3                               # Download audio files only
        spot --4                               # Fetch lyrics only
        spot --5                               # Embed metadata only
    
    \b
    ADVANCED:
        spot --replace song.m4a "https://youtube.com/watch?v=..."
        spot --cookie-file cookies.txt --url "https://..."
    """
    # Handle --version
    if version:
        click.echo(f"spot-downloader {__version__}")
        ctx.exit(0)
    
    # Handle --export-all or --export (standalone operations)
    if export_all or export:
        export_arg = export if export else "__ALL__"
        _handle_export(export_arg, copy_files)
        ctx.exit(0)
    
    # Handle --replace (standalone operation)
    if replace:
        _handle_replace(replace, cookie_file)
        ctx.exit(0)
    
    # --copy-files only makes sense with --export-all or --export
    if copy_files and not export_all and not export:
        raise click.UsageError("--copy-files can only be used with --export-all or --export")

    
    # Phase flags
    phase_flags = [phase1_only, phase2_only, phase3_only, phase4_only, phase5_only]
    has_phase_flag = any(phase_flags)
    
    # --sync without --url and --liked means "sync all known playlists"
    sync_all = sync and not url and not liked and not has_phase_flag
    
    # Validate: show help only if no meaningful arguments
    # Phases 2-5 can run without --url/--liked (they use database)
    if not url and not liked and not has_phase_flag and not sync:
        # No arguments at all - show help
        click.echo(ctx.get_help())
        ctx.exit(0)
    
    if url and liked:
        raise click.UsageError("Cannot use both --url and --liked")
    
    # --no-liked only makes sense with --sync (and without --liked)
    if no_liked and not sync:
        raise click.UsageError("--no-liked can only be used with --sync")
    if no_liked and liked:
        raise click.UsageError("Cannot use both --liked and --no-liked")
    
    # Validate URL is a playlist URL (not track, album, or artist)
    if url and "/playlist/" not in url:
        raise click.UsageError(
            "--url must be a Spotify playlist URL (containing '/playlist/'). "
            "For tracks, albums, or artists, use the appropriate Spotify feature."
        )
    
    # Phase flags are mutually exclusive
    if sum(phase_flags) > 1:
        raise click.UsageError("Only one phase flag (--1, --2, --3, --4, --5) can be used")
    
    # --1 requires --url or --liked
    if phase1_only and not url and not liked:
        raise click.UsageError("--1 requires --url or --liked")
    
    # --url can only be used with --1 (or when running all phases)
    if url and any([phase2_only, phase3_only, phase4_only, phase5_only]):
        raise click.UsageError("--url can only be used with --1 or when running all phases")
    
    # --liked can only be used with --1 (or when running all phases)
    if liked and any([phase2_only, phase3_only, phase4_only, phase5_only]):
        raise click.UsageError("--liked can only be used with --1 or when running all phases")
    
    # --sync only makes sense with --1 or when running all phases
    if sync and any([phase2_only, phase3_only, phase4_only, phase5_only]):
        raise click.UsageError("--sync can only be used with --1 or when running all phases")
    
    # Determine which phases to run
    if has_phase_flag:
        # Single phase mode
        run_phase1 = phase1_only
        run_phase2 = phase2_only
        run_phase3 = phase3_only
        run_phase4 = phase4_only
        run_phase5 = phase5_only
    else:
        # Run all phases
        run_phase1 = True
        run_phase2 = True
        run_phase3 = True
        run_phase4 = True
        run_phase5 = True
    
    # Determine if user authentication is needed
    # - --liked always requires user auth
    # - --sync (all) requires user auth unless --no-liked is specified
    needs_user_auth = liked or (sync_all and not no_liked)
    
    # Store in context for the command
    ctx.ensure_object(dict)
    ctx.obj["url"] = url
    ctx.obj["liked"] = liked
    ctx.obj["sync"] = sync
    ctx.obj["sync_all"] = sync_all
    ctx.obj["no_liked"] = no_liked
    ctx.obj["run_phase1"] = run_phase1
    ctx.obj["run_phase2"] = run_phase2
    ctx.obj["run_phase3"] = run_phase3
    ctx.obj["run_phase4"] = run_phase4
    ctx.obj["run_phase5"] = run_phase5
    ctx.obj["cookie_file"] = cookie_file
    ctx.obj["force_rematch"] = force_rematch
    ctx.obj["user_auth"] = needs_user_auth
    
    # Run the download workflow
    _run_download(ctx.obj)

def _run_download(options: dict) -> None:
    """
    Execute the download workflow based on CLI options.
    
    This is the main orchestration function that:
    1. Loads configuration
    2. Sets up logging
    3. Initializes database and Spotify client
    4. Runs the appropriate phases
    5. Reports results
    
    Args:
        options: Dictionary with CLI options from click context.
    
    Raises:
        SystemExit: On fatal errors (with appropriate exit code).
    """
    config: Config | None = None
    database: Database | None = None
    
    try:
        # Load configuration
        config = _load_configuration()
        
        # Setup logging
        setup_logging(config.output.directory)
        logger.info("spot-downloader starting")
        
        # Ensure output directory exists
        ensure_directory(config.output.directory)
        
        # Initialize database
        database = _initialize_database(config.output.directory)
        
        # Initialize Spotify client
        _initialize_spotify(config, options["user_auth"])
        
        # Determine playlist ID
        # Phase 1 requires --url or --liked explicitly
        # Phases 2-5 can work without playlist_id (process all playlists)
        if options["liked"]:
            playlist_id = LIKED_SONGS_KEY
        elif options["url"]:
            playlist_id = extract_playlist_id(options["url"])
        else:
            # Running phase 2-5 without --url: playlist_id is None
            # Phase 2 will process ALL playlists
            # Phases 3-5 still need a specific playlist (use active one)
            playlist_id = None
        
        # Override cookie file from CLI if provided
        cookie_file = options["cookie_file"] or config.download.cookie_file
        
        # Run phases
        tracks = None
        lyrics_stats = None
        
        # Handle sync_all mode (--sync without --url or --liked)
        if options.get("sync_all"):
            tracks = _run_sync_all(
                database=database,
                include_liked=not options.get("no_liked", False),
                output_dir=config.output.directory
            )
            # After sync_all, we don't need to run phase1 again
            # and playlist_id stays None (phases work globally)
        elif options["run_phase1"]:
            tracks = _run_phase1(
                database=database,
                url=options["url"],
                liked=options["liked"],
                sync=options["sync"]
            )
        
        if options["run_phase2"]:
            _run_phase2(
                database=database,
                playlist_id=playlist_id,
                tracks=tracks,
                num_threads=config.download.matching_threads,
                force_rematch=options["force_rematch"]
            )
        
        # Phases 3-5 require a specific playlist
        # If playlist_id is None, get the active one
        if playlist_id is None and any([options["run_phase3"], options["run_phase4"], options["run_phase5"]]):
            playlist_id = database.get_active_playlist_id()
            if playlist_id is None:
                click.echo("No playlist found in database. Run with --url first.", err=True)
                sys.exit(1)
        
        if options["run_phase3"]:
             _run_phase3(
                database=database,
                playlist_id=playlist_id,
                output_dir=config.output.directory,
                cookie_file=cookie_file,
                num_threads=config.download.download_threads
            )

        
        if options["run_phase4"]:
            lyrics_stats = _run_phase4(
                database=database,
                output_dir=config.output.directory,
                num_threads=config.download.lyrics_threads
            )

        if options["run_phase5"]:
            _run_phase5(
                database=database,
                playlist_id=playlist_id,
                output_dir=config.output.directory,
                num_threads=config.download.embedding_threads
            )
        
        # Final statistics
        if options.get("sync_all"):
            # Print global stats for sync_all mode
            _print_global_stats(database)
        elif playlist_id is not None:
            _print_final_stats(database, playlist_id, lyrics_stats)
        
        logger.info("spot-downloader completed successfully")
        
    except ConfigError as e:
        click.echo(f"Configuration error: {e.message}", err=True)
        sys.exit(1)
        
    except DatabaseError as e:
        click.echo(f"Database error: {e.message}", err=True)
        logger.error(f"Database error: {e.message}", exc_info=True)
        sys.exit(2)
        
    except SpotifyError as e:
        click.echo(f"Spotify error: {e.message}", err=True)
        if e.is_auth_error:
            click.echo("Check your client_id and client_secret in config.yaml", err=True)
        logger.error(f"Spotify error: {e.message}", exc_info=True)
        sys.exit(3)
        
    except SpotDownloaderError as e:
        click.echo(f"Error: {e.message}", err=True)
        logger.error(f"Error: {e.message}", exc_info=True)
        sys.exit(4)
        
    except KeyboardInterrupt:
        click.echo("\nInterrupted by user", err=True)
        logger.info("Interrupted by user")
        sys.exit(130)
        
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        logger.exception("Unexpected error")
        sys.exit(1)
        
    finally:
        shutdown_logging()

def _load_configuration() -> Config:
    """
    Load and validate configuration from config.yaml.
    
    Returns:
        Config object with validated settings.
    
    Raises:
        ConfigError: If configuration is invalid or missing.
    """
    return load_config()


def _initialize_database(output_dir: Path) -> Database:
    """
    Initialize the SQLite database.
    
    Args:
        output_dir: Directory where database.db is stored.
    
    Returns:
        Database instance.
    
    Raises:
        DatabaseError: If database cannot be initialized.
    """
    db_path = output_dir / "database.db"
    return Database(db_path)


def _initialize_spotify(config: Config, user_auth: bool) -> None:
    """
    Initialize the Spotify client singleton.
    
    Args:
        config: Configuration with Spotify credentials.
        user_auth: Whether to enable user authentication.
    
    Raises:
        SpotifyError: If authentication fails.
    """
    SpotifyClient.init(
        client_id=config.spotify.client_id,
        client_secret=config.spotify.client_secret,
        user_auth=user_auth
    )


def _run_phase1(
    database: Database,
    url: str | None,
    liked: bool,
    sync: bool
) -> list[Track]:
    """
    Run PHASE 1: Fetch Spotify metadata.
    
    Args:
        database: Database instance.
        url: Playlist URL (None if using --liked).
        liked: Whether to fetch Liked Songs.
        sync: Whether to filter to new tracks only.
    
    Returns:
        List of Track objects to process.
    
    Behavior:
        1. Log phase start
        2. Call appropriate fetcher (playlist or liked songs)
        3. Log track counts
        4. Return tracks for next phase
    """
    logger.info("=" * 60)
    logger.info("PHASE 1: Fetching Spotify metadata")
    logger.info("=" * 60)
    
    if liked:
        liked_songs, tracks = fetch_liked_songs_phase1(database, sync_mode=sync)
        logger.info(f"Fetched {liked_songs.total_tracks} liked songs")
    else:
        playlist, tracks = fetch_playlist_phase1(database, url, sync_mode=sync)
        logger.info(f"Fetched playlist: {playlist.name}")
        logger.info(f"Total tracks: {playlist.total_tracks}")
    
    if sync:
        logger.info(f"Sync mode: {len(tracks)} new tracks to process")
    else:
        logger.info(f"Tracks to process: {len(tracks)}")
    
    logger.info("PHASE 1 complete")
    return list(tracks)


def _run_sync_all(
    database: Database, 
    include_liked: bool = True,
    output_dir: Path | None = None
) -> list[Track]:
    """
    Sync all known playlists (and optionally Liked Songs).
    
    This is the main function for `spot --sync` without --url or --liked.
    It fetches metadata for all playlists previously added to the database,
    detects changes (removed tracks, position changes), and prompts for rebuild.
    
    Args:
        database: Database instance.
        include_liked: Whether to also sync Liked Songs (requires user auth).
        output_dir: Output directory for FileManager (needed for rebuild).
    
    Returns:
        Combined list of new Track objects from all playlists.
    
    Behavior:
        1. Get all playlists from database
        2. For each playlist:
           a. Take snapshot of current state
           b. Run Phase 1 in sync mode
           c. Detect changes (removals, position changes)
           d. If changes found, prompt user and rebuild if confirmed
        3. If include_liked, also sync Liked Songs
        4. Return combined list of new tracks
    """
    logger.info("=" * 60)
    logger.info("SYNC ALL: Syncing all known playlists")
    logger.info("=" * 60)
    
    all_playlists = database.get_all_playlists()
    
    # Filter out liked songs entry if present (we handle it separately)
    playlists = [p for p in all_playlists if p["spotify_id"] != LIKED_SONGS_KEY]
    
    if not playlists and not include_liked:
        logger.warning("No playlists found in database. Add a playlist first with --url")
        return []
    
    all_new_tracks: list[Track] = []
    file_manager = FileManager(output_dir) if output_dir else None
    
    # Sync each playlist
    for i, playlist_info in enumerate(playlists, 1):
        spotify_url = playlist_info.get("spotify_url")
        playlist_id = playlist_info["spotify_id"]
        name = playlist_info.get("name", "Unknown")
        
        if not spotify_url:
            logger.warning(f"Skipping playlist '{name}': no URL stored")
            continue
        
        logger.info(f"[{i}/{len(playlists)}] Syncing: {name}")
        
        # Take snapshot BEFORE sync
        snapshot_before = database.get_playlist_tracks_snapshot(playlist_id)
        
        try:
            playlist, tracks = fetch_playlist_phase1(
                database, 
                spotify_url, 
                sync_mode=True
            )
            all_new_tracks.extend(tracks)
            logger.info(f"  → {len(tracks)} new tracks")
            
            # Take snapshot AFTER sync
            snapshot_after = database.get_playlist_tracks_snapshot(playlist_id)
            
            # Detect changes
            changes = _detect_playlist_changes(snapshot_before, snapshot_after, name)
            
            if changes["has_changes"] and file_manager:
                _handle_playlist_changes(
                    database=database,
                    file_manager=file_manager,
                    playlist_id=playlist_id,
                    playlist_name=name,
                    changes=changes
                )
                
        except Exception as e:
            logger.error(f"  → Failed to sync '{name}': {e}")
            continue
    
    # Check for deleted playlists (playlists in DB but not on Spotify)
    # This would require fetching user's playlists from Spotify API
    # For now, we skip this feature as it requires additional API calls
    
    # Sync Liked Songs if requested
    if include_liked:
        logger.info(f"Syncing: Liked Songs")
        
        # Take snapshot BEFORE sync
        snapshot_before = database.get_playlist_tracks_snapshot(LIKED_SONGS_KEY)
        
        try:
            liked_songs, tracks = fetch_liked_songs_phase1(database, sync_mode=True)
            all_new_tracks.extend(tracks)
            logger.info(f"  → {len(tracks)} new tracks")
            
            # Take snapshot AFTER sync
            snapshot_after = database.get_playlist_tracks_snapshot(LIKED_SONGS_KEY)
            
            # Detect changes
            changes = _detect_playlist_changes(snapshot_before, snapshot_after, "Liked Songs")
            
            if changes["has_changes"] and file_manager:
                _handle_playlist_changes(
                    database=database,
                    file_manager=file_manager,
                    playlist_id=LIKED_SONGS_KEY,
                    playlist_name="Liked Songs",
                    changes=changes
                )
                
        except Exception as e:
            logger.error(f"  → Failed to sync Liked Songs: {e}")
    
    logger.info("-" * 60)
    logger.info(f"SYNC ALL complete: {len(all_new_tracks)} total new tracks")
    
    return all_new_tracks


def _detect_playlist_changes(
    before: dict[str, int],
    after: dict[str, int],
    playlist_name: str
) -> dict:
    """
    Detect changes between two playlist snapshots.
    
    Args:
        before: Snapshot before sync {spotify_id: position}
        after: Snapshot after sync {spotify_id: position}
        playlist_name: For logging purposes
    
    Returns:
        Dict with:
            - has_changes: bool
            - removed_tracks: list of spotify_ids removed
            - position_changes: list of (spotify_id, old_pos, new_pos)
            - added_tracks: list of spotify_ids added
    """
    removed = []
    position_changes = []
    added = []
    
    # Find removed tracks
    for track_id in before:
        if track_id not in after:
            removed.append(track_id)
    
    # Find added tracks and position changes
    for track_id, new_pos in after.items():
        if track_id not in before:
            added.append(track_id)
        elif before[track_id] != new_pos:
            position_changes.append((track_id, before[track_id], new_pos))
    
    has_changes = bool(removed or position_changes)
    
    return {
        "has_changes": has_changes,
        "removed_tracks": removed,
        "position_changes": position_changes,
        "added_tracks": added
    }


def _handle_playlist_changes(
    database: Database,
    file_manager: FileManager,
    playlist_id: str,
    playlist_name: str,
    changes: dict
) -> None:
    """
    Handle detected playlist changes with user confirmation.
    
    The database has already been updated by the fetcher to match Spotify.
    This function only rebuilds the local playlist directory if the user confirms.
    
    Args:
        database: Database instance
        file_manager: FileManager instance
        playlist_id: Spotify playlist ID
        playlist_name: Human-readable playlist name
        changes: Dict from _detect_playlist_changes
    """
    # Report changes
    logger.info(f"  Changes detected in '{playlist_name}':")
    
    if changes["removed_tracks"]:
        logger.info(f"    - {len(changes['removed_tracks'])} tracks removed")
    
    if changes["position_changes"]:
        logger.info(f"    - {len(changes['position_changes'])} tracks moved")
    
    # Ask for confirmation
    if not click.confirm(f"  Rebuild local playlist '{playlist_name}'?"):
        logger.info(f"  Skipping rebuild for '{playlist_name}'")
        return
    
    # Get tracks from database (already updated by fetcher)
    tracks = database.get_playlist_tracks_for_export(playlist_id)
    
    if tracks:
        # Rebuild the playlist directory with correct hard links
        created = file_manager.rebuild_playlist_from_tracks(playlist_name, tracks)
        logger.info(f"  Rebuilt playlist directory with {created} tracks")
    else:
        # No downloaded tracks, just delete the directory
        file_manager.delete_playlist_directory(playlist_name)
        logger.info(f"  Removed empty playlist directory")


def _run_phase2(
    database: Database,
    playlist_id: str | None,
    tracks: list[Track] | None,
    num_threads: int,
    force_rematch: bool = False
) -> None:
    """
    Run PHASE 2: Match tracks on YouTube Music.
    
    With Global Track Registry, matching is done globally - the same track
    is only matched once regardless of how many playlists contain it.
    
    Args:
        database: Database instance.
        playlist_id: Optional playlist ID for --force-rematch scope.
                    Not used for actual matching (that's global).
        tracks: Tracks from PHASE 1 (None if running phase separately).
        num_threads: Number of parallel matching threads.
        force_rematch: If True, reset failed matches before processing.
    """
    logger.info("=" * 60)
    logger.info("PHASE 2: Matching tracks on YouTube Music")
    logger.info("=" * 60)
    
    # Handle force_rematch
    if force_rematch:
        # Reset failed matches (globally or for specific playlist)
        reset_count = database.reset_failed_matches(playlist_id)
        if reset_count > 0:
            logger.info(f"Reset {reset_count} failed matches for re-matching")
    
    # Get tracks to process
    if tracks is None:
        # Running phase 2 separately - get ALL tracks needing match globally
        track_dicts = get_tracks_needing_match(database)
        
        if not track_dicts:
            logger.info("No tracks need YouTube matching")
            logger.info("PHASE 2 complete")
            return
        
        # Convert to Track objects
        tracks = [
            Track.from_database_dict(d["track_id"], d)
            for d in track_dicts
        ]
        logger.info(f"Found {len(tracks)} tracks needing YouTube match")
    else:
        # Tracks from phase 1 - filter to only those needing match
        existing_matched = set()
        for t in tracks:
            track_data = database.get_global_track(t.spotify_id)
            if track_data and track_data.get("youtube_url"):
                existing_matched.add(t.spotify_id)
        
        if existing_matched:
            tracks = [t for t in tracks if t.spotify_id not in existing_matched]
            logger.info(f"Skipping {len(existing_matched)} already matched tracks")
    
    if not tracks:
        logger.info("No tracks to match")
        logger.info("PHASE 2 complete")
        return
    
    logger.info(f"Matching {len(tracks)} tracks using {num_threads} threads")
    
    # Run matching (global - no playlist_id needed)
    match_tracks_phase2(database, tracks, num_threads)
    
    logger.info("PHASE 2 complete")


def _run_phase3(
    database: Database,
    playlist_id: str,
    output_dir: Path,
    cookie_file: Path | None,
    num_threads: int
) -> None:
    """
    Run PHASE 3: Download audio files.
    
    Uses the central storage architecture:
    - Audio files saved ONCE in tracks/ directory (canonical files)
    - Hard links created in playlist directories with position-based names
    
    Args:
        database: Database instance.
        playlist_id: Playlist ID (used for logging context).
        output_dir: Base output directory (contains tracks/, playlists, etc.)
        cookie_file: Optional cookies.txt for YT Premium.
        num_threads: Number of parallel downloads.
    
    Behavior:
        1. Log phase start
        2. Get tracks with youtube_url but not downloaded (global)
        3. For each track:
           a. Download audio from YouTube using yt-dlp
           b. Convert to M4A format via FFmpeg postprocessor
           c. Save to tracks/ with canonical name: {title}-{artist}.m4a
           d. Update database: downloaded=True, file_path (canonical path)
           e. Create hard links in ALL playlist directories containing this track
        4. Log download statistics
        5. Write failures to download_failures.log
    
    File Naming:
        - Canonical (in tracks/): {title}-{artist}.m4a
        - Playlist links: {position:05d}-{title}-{artist}.m4a
    
    Important:
        This phase does NOT fetch lyrics or embed metadata.
        Those operations are handled by PHASE 4 and PHASE 5.
    
    Database Updates:
        - Sets downloaded=True
        - Sets file_path to the canonical path in tracks/
        - Sets download_timestamp
    """
    logger.info("=" * 60)
    logger.info("PHASE 3: Downloading audio files")
    logger.info("=" * 60)
    
    # Get tracks needing download (global - not playlist-specific)
    tracks = database.get_tracks_needing_download()
    
    if not tracks:
        logger.info("No tracks need downloading")
        logger.info("PHASE 3 complete")
        return
    
    logger.info(f"Found {len(tracks)} tracks to download")
    
    if cookie_file:
        logger.info(f"Using cookie file: {cookie_file}")
    else:
        logger.info("No cookies provided - downloads limited to 128 kbps")
    
    # Run download
    stats = download_tracks_phase3(
        database=database,
        output_dir=output_dir,
        playlist_id=playlist_id,
        cookie_file=cookie_file,
        num_threads=num_threads
    )
    
    # Log results
    logger.info(f"Download results: {stats.downloaded}/{stats.total} successful")
    if stats.failed > 0:
        logger.warning(f"Failed downloads: {stats.failed} (see download_failures.log)")
    if stats.skipped > 0:
        logger.info(f"Skipped (already downloaded): {stats.skipped}")

def _run_phase4(
    database: Database,
    output_dir: Path,
    num_threads: int = 2
) -> None:
    """
    Run PHASE 4: Fetch lyrics for downloaded tracks.
    
    Args:
        database: Database instance.
        output_dir: Output directory for .lrc files.
        num_threads: Number of parallel fetching threads.
    
    Behavior:
        1. Log phase start
        2. Get tracks that are downloaded but don't have lyrics
        3. For each track:
           a. Attempt to fetch lyrics from multiple providers
           b. If found: store lyrics in database, create .lrc file
           c. If not found: log to lyrics_failures.log (no DB update)
        4. Log lyrics fetch statistics
    
    Database Updates:
        - Sets lyrics_text, lyrics_synced, lyrics_source for successful fetches
        - Sets lyrics_fetched=True ONLY for tracks where lyrics were found
        - Tracks without lyrics remain pending for retry on next run
    
    Logging:
        - INFO: Phase start, progress, completion
        - DEBUG: Individual track processing
        - Writes to lyrics_failures.log for tracks without lyrics
    
    Returns:
        LyricsStats object with results.
    """
    logger.info("=" * 60)
    logger.info("PHASE 4: Fetching lyrics")
    logger.info("=" * 60)
    
    stats = fetch_lyrics_phase4(
        database=database,
        output_dir=output_dir,
        num_threads=num_threads
    )
    
    return stats


def _run_phase5(
    database: Database,
    playlist_id: str,
    output_dir: Path,
    num_threads: int = 4
) -> None:
    """
    Run PHASE 5: Embed metadata and lyrics into M4A files.
    
    Args:
        database: Database instance.
        playlist_id: Playlist ID for database queries.
        output_dir: Directory containing the M4A files.
        num_threads: Number of parallel embedding threads.
                    Cover art download benefits from parallelization.
    
    Behavior:
        1. Log phase start
        2. Get tracks that are downloaded but don't have metadata_embedded=True
           OR have lyrics available but not embedded
        3. For each track (parallel with num_threads):
           a. Load file from file_path in database
           b. Embed all Spotify metadata (title, artist, album, cover, etc.)
           c. If lyrics_text exists in database, embed lyrics
           d. Mark metadata_embedded=True and lyrics_embedded=True (if lyrics present)
           e. Mark cover_embedded=True if cover was successfully embedded
        4. Log embedding statistics
    
    File Naming:
        Files already have their final names from PHASE 3.
        This phase does NOT rename files, only embeds metadata.
    
    Database Updates:
        - Sets metadata_embedded=True
        - Sets lyrics_embedded=True if lyrics were embedded
        - Sets cover_embedded=True if cover art was embedded
    
    Logging:
        - INFO: Phase start, progress, completion
        - DEBUG: Individual track processing
        - ERROR: Files that couldn't be processed (also written to embed_failures.log)
    """
    logger.info("=" * 60)
    logger.info("PHASE 5: Embedding metadata and lyrics")
    logger.info("=" * 60)
    
    stats = embed_metadata_phase5(
        database=database,
        playlist_id=playlist_id,
        output_dir=output_dir,
        num_threads=num_threads
    )
    
    logger.info(f"PHASE 5 complete: {stats.embedded}/{stats.total} tracks processed")
    if stats.failed > 0:
        logger.warning(f"Failed to embed: {stats.failed} tracks (see embed_failures.log)")


def _handle_replace(replace_args: tuple[Path, str], cookie_file: Path | None) -> None:
    """
    Handle the --replace standalone operation.
    
    This function replaces the audio in an existing M4A file with audio
    downloaded from a YouTube URL, while preserving all metadata.
    
    Args:
        replace_args: Tuple of (m4a_file_path, youtube_url).
        cookie_file: Optional path to cookies.txt for Premium quality.
    
    Behavior:
        1. Load configuration (for cookie_file fallback and database path)
        2. Initialize database
        3. Parse the file path to get (playlist_name, position)
        4. Look up track in database
        5. Show confirmation prompt with track details
        6. If confirmed, replace the audio and update database
    
    Raises:
        SystemExit: On any error (file not found, download failed, etc.)
    
    Note:
        This operation updates the database with the new YouTube URL
        and resets embedding flags so the track will be re-embedded
        on the next Phase 5 run.
    """
    m4a_path, youtube_url = replace_args
    
    # Normalize path: handle shell-escaped spaces (macOS/Linux: "\ " → " ")
    # Also expand ~ and resolve to absolute path
    path_str = str(m4a_path).replace('\\ ', ' ')
    m4a_path = Path(path_str).expanduser().resolve()
    
    # Verify file exists after normalization
    if not m4a_path.exists():
        click.echo(f"Error: File not found: {m4a_path}", err=True)
        sys.exit(1)
    
    try:
        # Load configuration
        config = load_config()
        
        # Setup logging (minimal, just for errors)
        setup_logging(config.output.directory)
        
        # Resolve cookie file (CLI argument takes precedence)
        effective_cookie_file = cookie_file or config.download.cookie_file
        
        # Initialize database
        db_path = config.output.directory / "database.db"
        if not db_path.exists():
            click.echo("Error: No database found. Run a download first.", err=True)
            sys.exit(1)
        
        database = Database(db_path)
        
        # Parse the file path to get playlist name and position
        try:
            from spot_downloader.utils.replace import parse_playlist_path
            playlist_name, position = parse_playlist_path(m4a_path)
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        
        # Look up track in database
        track = database.get_track_by_playlist_position(playlist_name, position)
        
        if track is None:
            # Provide helpful error message
            display_name = "Liked Songs" if playlist_name == LIKED_SONGS_KEY else playlist_name
            click.echo(
                f"Error: Track not found in database.\n"
                f"  Playlist: {display_name}\n"
                f"  Position: {position}\n"
                f"Make sure the file path is correct and the track has been downloaded.",
                err=True
            )
            sys.exit(1)
        
        # Extract track info for display
        track_name = track.get("name", "Unknown")
        artist = track.get("artist", "Unknown")
        old_youtube_url = track.get("youtube_url", "Unknown")
        
        # Show confirmation prompt
        click.echo("")
        click.echo(f"Replacing: \"{track_name}\" by {artist}")
        click.echo(f"From: {old_youtube_url}")
        click.echo(f"To:   {youtube_url}")
        click.echo("")
        
        if not click.confirm("Proceed?", default=False):
            click.echo("Aborted.")
            sys.exit(0)
        
        click.echo("")
        click.echo("Downloading and replacing...")
        
        # Perform the replacement
        from spot_downloader.utils.replace import replace_track_audio
        
        result = replace_track_audio(
            m4a_path=m4a_path,
            youtube_url=youtube_url,
            database=database,
            cookie_file=effective_cookie_file
        )
        
        click.echo("")
        click.echo(click.style("✓ ", fg="green") + f"Successfully replaced: \"{result['track_name']}\" by {result['artist']}")
        click.echo(f"  File: {result['canonical_path']}")
        click.echo("")
        click.echo("Note: Run Phase 5 (--5) to re-embed metadata if needed.")
        
    except ConfigError as e:
        click.echo(f"Configuration error: {e.message}", err=True)
        sys.exit(1)
    except DatabaseError as e:
        click.echo(f"Database error: {e.message}", err=True)
        sys.exit(2)
    except MetadataError as e:
        click.echo(f"Metadata error: {e.message}", err=True)
        sys.exit(3)
    except DownloadError as e:
        click.echo(f"Download error: {e.message}", err=True)
        sys.exit(4)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        sys.exit(1)
    finally:
        shutdown_logging()


def _handle_export(export_arg: str, copy_files: bool) -> None:
    """
    Handle the --export standalone operation.
    
    Exports playlists as M3U files (default) or folder copies (--copy-files).
    
    Args:
        export_arg: Playlist name to export, or "__ALL__" for all playlists.
        copy_files: If True, create folder copies instead of M3U files.
    
    Behavior:
        1. Load configuration
        2. Initialize database
        3. Get playlist(s) to export
        4. For each playlist:
           - Get downloaded tracks
           - Either create M3U file or copy files to export directory
        5. Report export location to user
    
    Output Structure (M3U mode):
        export_directory/
        ├── tracks/
        │   ├── Queen-Bohemian Rhapsody.m4a
        │   └── ...
        ├── My Playlist.m3u
        └── Another Playlist.m3u
    
    Output Structure (copy-files mode):
        export_directory/
        ├── My Playlist/
        │   ├── 00001-Bohemian Rhapsody-Queen.m4a
        │   └── ...
        └── Another Playlist/
            └── ...
    """
    try:
        # Load configuration
        config = load_config()
        export_dir = config.output.export_directory
        
        # Initialize database
        db_path = config.output.directory / "database.db"
        if not db_path.exists():
            click.echo("No database found. Run a download first.", err=True)
            sys.exit(1)
        
        database = Database(db_path)
        file_manager = FileManager(config.output.directory)
        
        # Get playlists to export
        all_playlists = database.get_all_playlists()
        
        if not all_playlists:
            click.echo("No playlists found in database.", err=True)
            sys.exit(1)
        
        # Filter to specific playlist if requested
        if export_arg != "__ALL__":
            playlists_to_export = [
                p for p in all_playlists 
                if p.get("name", "").lower() == export_arg.lower()
            ]
            if not playlists_to_export:
                click.echo(f"Playlist not found: {export_arg}", err=True)
                click.echo("Available playlists:")
                for p in all_playlists:
                    click.echo(f"  - {p.get('name', 'Unknown')}")
                sys.exit(1)
        else:
            playlists_to_export = all_playlists
        
        # Create export directory
        export_dir.mkdir(parents=True, exist_ok=True)
        
        click.echo(f"Exporting {len(playlists_to_export)} playlist(s)...")
        
        if copy_files:
            # Export as folder copies
            total_files = 0
            for playlist in playlists_to_export:
                playlist_id = playlist["spotify_id"]
                playlist_name = playlist.get("name", "Unknown")
                
                tracks = database.get_playlist_tracks_for_export(playlist_id)
                if not tracks:
                    click.echo(f"  Skipping '{playlist_name}' (no downloaded tracks)")
                    continue
                
                folder_path, copied = file_manager.export_playlist_copy(
                    playlist_name=playlist_name,
                    tracks=tracks,
                    export_dir=export_dir
                )
                total_files += copied
                click.echo(f"  Exported '{playlist_name}': {copied} files")
            
            click.echo("")
            click.echo(f"Export complete: {total_files} files copied")
        else:
            # Export as M3U files
            # First, collect all tracks to copy
            all_tracks: list[dict] = []
            for playlist in playlists_to_export:
                playlist_id = playlist["spotify_id"]
                tracks = database.get_playlist_tracks_for_export(playlist_id)
                all_tracks.extend(tracks)
            
            if not all_tracks:
                click.echo("No downloaded tracks to export.", err=True)
                sys.exit(1)
            
            # Copy tracks to export/tracks/
            copied = file_manager.copy_tracks_to_export(all_tracks, export_dir)
            click.echo(f"Copied {copied} unique tracks to {export_dir / 'tracks'}")
            
            # Create M3U files
            for playlist in playlists_to_export:
                playlist_id = playlist["spotify_id"]
                playlist_name = playlist.get("name", "Unknown")
                
                tracks = database.get_playlist_tracks_for_export(playlist_id)
                if not tracks:
                    click.echo(f"  Skipping '{playlist_name}' (no downloaded tracks)")
                    continue
                
                m3u_path = file_manager.export_playlist_m3u(
                    playlist_name=playlist_name,
                    tracks=tracks,
                    export_dir=export_dir
                )
                click.echo(f"  Created '{m3u_path.name}': {len(tracks)} tracks")
            
            click.echo("")
            click.echo(f"Export complete: M3U playlists created")
        
        click.echo(f"Location: {export_dir}")
        
    except ConfigError as e:
        click.echo(f"Configuration error: {e.message}", err=True)
        sys.exit(1)
    except DatabaseError as e:
        click.echo(f"Database error: {e.message}", err=True)
        sys.exit(2)
    except Exception as e:
        click.echo(f"Export failed: {e}", err=True)
        sys.exit(1)


def _print_final_stats(
    database: Database,
    playlist_id: str = None,
    lyrics_stats: "LyricsStats | None" = None
) -> None:
    """
    Print final download statistics (global).
    
    Args:
        database: Database instance.
        playlist_id: Deprecated, not used. Stats are always global.
        lyrics_stats: Deprecated, not used. Stats are read from database.
    
    Output:
        Prints a summary table with global statistics.
    """
    stats = database.get_global_stats()
    
    logger.info("=" * 60)
    logger.info("FINAL STATISTICS")
    logger.info("=" * 60)
    logger.info(f"Total tracks:      {stats['total']}")
    logger.info(f"Matched:           {stats['matched']}")
    logger.info(f"Downloaded:        {stats['downloaded']}")
    
    # Lyrics statistics
    if stats.get('with_lyrics', 0) > 0 or stats.get('without_lyrics', 0) > 0:
        logger.info(f"With lyrics:       {stats['with_lyrics']}")
        logger.info(f"  - Synced (LRC):  {stats['lyrics_synced']}")
        logger.info(f"  - Plain text:    {stats['lyrics_plain']}")
        logger.info(f"Without lyrics:    {stats['without_lyrics']}")
        logger.info(f"LRC files:         {stats['lrc_files']}")
        logger.info(f"LRC hard links:    {stats['lrc_hard_links']}")
    
    logger.info(f"Failed to match:   {stats['failed_match']}")
    logger.info(f"Pending match:     {stats['pending_match']}")
    logger.info(f"Pending download:  {stats['pending_download']}")
    logger.info("=" * 60)


def _print_global_stats(database: Database) -> None:
    """
    Print global statistics across all playlists.
    
    Used after sync_all mode to show overall status.
    
    Args:
        database: Database instance.
    """
    stats = database.get_global_stats()
    
    logger.info("=" * 60)
    logger.info("GLOBAL STATISTICS")
    logger.info("=" * 60)
    logger.info(f"Playlists:         {stats['playlists']}")
    logger.info(f"Unique tracks:     {stats['total']}")
    logger.info(f"Matched:           {stats['matched']}")
    logger.info(f"Downloaded:        {stats['downloaded']}")
    logger.info(f"With lyrics:       {stats['with_lyrics']}")
    logger.info(f"  - Synced (LRC):  {stats['lyrics_synced']}")
    logger.info(f"  - Plain text:    {stats['lyrics_plain']}")
    logger.info(f"Without lyrics:    {stats['without_lyrics']}")
    logger.info(f"LRC files:         {stats['lrc_files']}")
    logger.info(f"LRC hard links:    {stats['lrc_hard_links']}")
    logger.info(f"Failed to match:   {stats['failed_match']}")
    logger.info(f"Pending match:     {stats['pending_match']}")
    logger.info(f"Pending download:  {stats['pending_download']}")
    logger.info(f"Playlist links:    {stats['playlist_track_links']}")
    if stats['deduplication_ratio'] > 1:
        logger.info(f"Dedup ratio:       {stats['deduplication_ratio']}x (storage saved!)")
    logger.info("=" * 60)


def main() -> None:
    """
    Entry point for the CLI.
    
    This function is called when running `spot` from the command line.
    It invokes the Click CLI group.
    """
    cli()


if __name__ == "__main__":
    main()