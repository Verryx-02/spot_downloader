![Banner](/Asset/banner.png)


## Overview

spot-downloader converts Spotify playlists to local M4A audio files by:

- **PHASE 1**: Fetching track metadata from Spotify (title, artist, album, cover, etc.)  
- **PHASE 2**: Matching each track on YouTube Music
- **PHASE 3**: Downloading audio  
- **PHASE 4**: Downloading lyrics  
- **PHASE 5**: Embedding metadata and lyrics  

The result is a collection of properly tagged M4A files ready for any music player.

## Features

- Download entire Spotify playlists or Liked Songs
- **Sync mode**: download new tracks and detect playlist changes
- **Export**: generate M3U playlists or folder copies for portability (includes LRC lyrics files)
- M4A audio (128 kbps free, 256 kbps with YouTube Premium)
- Full metadata embedding (title, artist, album, cover art, lyrics, etc.)
- Automatic lyrics fetching from multiple providers
- Multi-threaded matching and downloads (configurable separately)
- Hard link architecture: shared tracks stored once, linked to multiple playlists
- Resume interrupted downloads

## Requirements

- Python 3.11+
- FFmpeg
- Spotify Developer credentials
- Deno (required for some protected videos)
- Optional: YouTube Music cookies for Premium quality and age-restricted content

## Installation

```bash
git clone https://github.com/Verryx-02/spot-downloader.git
cd spot-downloader
pip install -e .
```

## Configuration

Rename the `config.yaml.example` file in `config.yaml`.

### Spotify Credentials

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new application
3. Choose an app name and description. (you can invent them)
4. Set the Website to `http://localhost` and the Redirect URIs to `http://127.0.0.1:8888/callback`
5. Copy the Client ID and Client Secret to your `config.yaml` file
6. Do NOT share or post these IDs with anyone, EVER.

### YouTube Cookies (Recommended)

Cookies are useful for two reasons:

| Benefit | Description |
|---------|-------------|
| **Higher quality** | 256 kbps instead of 128 kbps (requires YouTube Premium) |
| **Age-restricted content** | Access videos that require sign-in to confirm age |

To set up cookies:

1. Install a browser extension like [this one](https://github.com/kairi003/Get-cookies.txt-LOCALLY)
2. Log in to [YouTube Music](https://music.youtube.com)
3. Export cookies to a file
4. Set `cookie_file` in `config.yaml` to the path of your cookies file

## Usage

### Download a Playlist

```bash
spot --url "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
```

### Download Liked Songs

```bash
spot --liked
```

This will open your browser for Spotify authentication (required to access your Liked Songs).

### Sync Mode

Sync mode downloads new tracks and detects changes in playlists:

```bash
# Sync a specific playlist
spot --url "https://open.spotify.com/playlist/..." --sync

# Sync ALL known playlists and Liked Songs
spot --sync

# Sync ALL playlists only (no Spotify login required)
spot --sync --no-liked
```

### Export Playlists

Export your downloaded music for use on other devices:

```bash
# Export all playlists as M3U files
spot --export-all

# Export a specific playlist
spot --export "My Playlist"

# Export as folder copies (actual files, not links)
spot --export-all --copy-files
spot --export "My Playlist" --copy-files
```

LRC lyrics files are automatically included in the export if available.

### Run Phases Separately

You can run each phase independently if you want:

```bash
# PHASE 1: Fetch Spotify metadata
spot --1 --url "https://open.spotify.com/playlist/..."
spot --1 --liked

# PHASE 2: Match tracks on YouTube Music
spot --2
spot --2 --force-rematch  # Retry failed matches

# PHASE 3: Download audio
spot --3

# PHASE 4: Fetch lyrics
spot --4

# PHASE 5: Embed metadata and lyrics
spot --5
```

### Replace Audio in Existing File

Sometimes the matching algorithm may match the Spotify song with the wrong YouTube song.
This is a problem with any matching algorithm. (You can see a formal explanation [here](https://github.com/Verryx-02/spot_downloader/issues/11))
If you notice that the audio track isn't the same as the one you can listen to on Spotify, you can replace it using the `--replace` function.
Find the link to the song you want on Spotify and run the command:

```bash
spot --replace <path of the song to replace> <youtube_link of the correct song>
```


## CLI Reference

```
spot [OPTIONS]

Input Sources:
  --url <spotify-url>           Spotify playlist URL to download
  --liked                       Download Liked Songs

Sync Options:
  --sync                        Sync mode: new tracks + change detection
  --no-liked                    Skip Liked Songs in sync mode

Export Options:
  --export-all                  Export all playlists as M3U
  --export <playlist-name>      Export specific playlist as M3U
  --copy-files                  Export as folder copies instead of M3U

Phase Selection:
  --1                           PHASE 1: Fetch Spotify metadata
  --2                           PHASE 2: Match on YouTube Music
  --3                           PHASE 3: Download audio
  --4                           PHASE 4: Fetch lyrics
  --5                           PHASE 5: Embed metadata and lyrics

Advanced Options:
  --replace <file> <url>        Replace audio in M4A file
  --cookie-file <path>          Path to cookies.txt
  --force-rematch               Retry failed YouTube matches

Info:
  --version                     Show version and exit
  --help                        Show this message and exit
```

### Hard Link Architecture

- Audio files are stored **once** in `tracks/`
- Playlist directories contain **hard links** to the master files
- If a song appears in 10 playlists, it uses disk space only once
- Deleting a playlist link doesn't delete the master file
- LRC lyrics files follow the same architecture

### File Naming

| Location | Format |
|----------|--------|
| `tracks/` | `{title}-{artist}.m4a` |
| `tracks/` | `{title}-{artist}.lrc` (if synced lyrics available) |
| `Playlists/` | `{position:05d}-{title}-{artist}.m4a` |
| `Playlists/` | `{position:05d}-{title}-{artist}.lrc` (if synced lyrics available) |

Position uses 5-digit padding (00001-99999) to support Spotify's maximum of 10,000 tracks per playlist.

### Log Files

| File | Description |
|------|-------------|
| `log_full.log` | Complete log of all events |
| `log_errors.log` | Only errors and critical issues |
| `download_failures.log` | Tracks whose audio download failed |
| `lyrics_failures.log` | Tracks whose lyrics could not be retrieved |
| `match_close_alternatives.log` | Tracks with multiple similar YouTube matches |

## Troubleshooting

### "Requested format is not available"

This error occurs when YouTube requires JavaScript to unlock video formats:

1. **Install Deno**
2. Re-run the song download

### "Sign in to confirm your age"

This video is age-restricted:

1. Set up a cookie file (see YouTube Cookies section)
2. Make sure you're logged into YouTube in your browser
3. Re-export fresh cookies

### Rate Limiting ("try again later")

YouTube is temporarily blocking requests:

1. Wait 10-30 minutes before retrying
2. Try running without cookies (you will have low audio quality)

### "Spotify authentication failed"

- Verify `client_id` and `client_secret` in `config.yaml`
- Check that your Spotify app is properly configured

### Low audio quality (128 kbps)

- Without cookies, YouTube limits quality to 128 kbps
- Use a cookie file with YouTube Premium for 256 kbps

## Project Structure

``` bash
spot_downloader
├── Asset
│   ├── banner.png
│   ├── Demo_phase1_2.mov
│   └── support_me_on_kofi_beige.png
├── config.yaml.example
├── LICENSE
├── pyproject.toml
├── README.md
└── spot_downloader
    ├── __init__.py
    ├── cli.py                    # CLI entry point
    ├── core
    │   ├── __init__.py
    │   ├── config.py             # Configuration loading
    │   ├── database.py           # SQLite database
    │   ├── exceptions.py         # Custom exceptions
    │   ├── file_manager.py       # Hard link architecture
    │   ├── logger.py             # Multi-file logging
    │   └── progress.py           # Progress bars for all phases
    ├── download
    │   ├── __init__.py
    │   ├── downloader.py         # PHASE 3: Audio download
    │   ├── embed_phase.py        # PHASE 5: Metadata embedding
    │   ├── lyrics_phase.py       # PHASE 4: Lyrics fetching
    │   ├── lyrics.py             # Lyrics providers
    │   └── metadata.py           # M4A metadata handling
    ├── spotify
    │   ├── __init__.py
    │   ├── client.py             # Spotify API client
    │   ├── fetcher.py            # PHASE 1: Metadata fetching
    │   └── models.py             # Track, Playlist dataclasses
    ├── utils
    │   ├── __init__.py
    │   └── replace.py            # --replace functionality
    └── youtube
        ├── __init__.py
        ├── matcher.py            # PHASE 2: YouTube matching
        └── models.py             # MatchResult dataclass

8 directories, 33 files
```

## Metadata Tags

The following metadata is embedded in each M4A file:

| Tag | Source |
|-----|--------|
| Title | Spotify |
| Artist | Spotify |
| Album | Spotify |
| Album Artist | Spotify |
| Release Date | Spotify |
| Genre | Spotify (from artist) |
| Track Number | Spotify |
| Disc Number | Spotify |
| Cover Art | Spotify (downloaded) |
| Lyrics | Genius, AZLyrics, MusixMatch, Synced |
| Explicit | Spotify |
| Copyright | Spotify |
| Publisher | Spotify |
| ISRC | Spotify |
| Spotify URL | Spotify (custom tag) |

## Dependencies

| Package | Purpose |
|---------|---------|
| spotipy | Spotify API client |
| ytmusicapi | YouTube Music search |
| yt-dlp | YouTube download |
| mutagen | M4A metadata |
| rapidfuzz | Fuzzy string matching |
| rich-click | CLI framework with colors |
| rich | Progress bars and formatting |
| pyyaml | Config parsing |
| syncedlyrics | Timestamped lyrics |
| requests | HTTP requests |
| beautifulsoup4 | HTML parsing |

## License

[MIT](LICENSE)

<a href="https://ko-fi.com/verryx02">
  <img src="Asset/support_me_on_kofi_beige.png" alt="Support me on Ko-fi" width="170">
</a>