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
- **Export**: generate M3U playlists or folder copies for portability
- M4A audio (128 kbps free, 256 kbps with YouTube Premium)
- Full metadata embedding (title, artist, album, cover art, lyrics, etc.)
- Automatic lyrics fetching from multiple providers
- Multi-threaded matching and downloads (configurable separately)
- Hard link architecture: shared tracks stored once, linked to multiple playlists
- Resume interrupted downloads
- Detailed logging and error reporting

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

Create a `config.yaml` file in your working directory:

```yaml
spotify:
  client_id: "your_spotify_client_id"
  client_secret: "your_spotify_client_secret"

output:
  directory: "~/Desktop/Music/SpotDownloader"
  export_directory: "~/Desktop/Music/SpotDownloader/Exported"  # Optional

download:
  threads:
    matching: 8   # Phase 2: YouTube matching (can be higher)
    download: 4   # Phase 3: Audio download (lower to avoid rate limiting)
  cookie_file: null  # Optional: path to cookies.txt
```

### Spotify Credentials

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new application
3. Copy the Client ID and Client Secret to your `config.yaml`

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

> **Warning**: Using cookies may increase rate limiting from YouTube. If you experience many failures, try running without cookies first, then use cookies only for the remaining tracks.

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
spot --export

# Export a specific playlist
spot --export "My Playlist"

# Export as folder copies (actual files, not links)
spot --export --copy-files
spot --export "My Playlist" --copy-files
```

### Run Phases Separately

You can run each phase independently:

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

Replace the audio of a downloaded track while preserving metadata:

```bash
spot --replace ~/Music/01-Song-Artist.m4a "https://youtube.com/watch?v=..."
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
  --export [playlist-name]      Export playlists as M3U (all if no name)
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

## Output Structure

```
output_directory/
├── spot_downloader.db          # SQLite database
├── logs/                       # Log files
│   ├── log_full.log
│   ├── log_errors.log
│   ├── download_failures.log
│   ├── lyrics_failures.log
│   └── match_close_alternatives.log
├── tracks/                     # Master audio files (canonical)
│   ├── Bohemian Rhapsody-Queen.m4a
│   └── ...
└── Playlists/                  # Playlist directories (hard links)
    ├── My Playlist/
    │   ├── 00001-Bohemian Rhapsody-Queen.m4a  → ../../tracks/...
    │   └── ...
    └── Another Playlist/
        └── ...
```

### Hard Link Architecture

- Audio files are stored **once** in `tracks/`
- Playlist directories contain **hard links** to the master files
- If a song appears in 10 playlists, it uses disk space only once
- Deleting a playlist link doesn't delete the master file

### File Naming

| Location | Format |
|----------|--------|
| `tracks/` | `{title}-{artist}.m4a` |
| `Playlists/` | `{position:05d}-{title}-{artist}.m4a` |

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
2. Re-run the download

### "Sign in to confirm your age"

This video is age-restricted:

1. Set up a cookie file (see YouTube Cookies section)
2. Make sure you're logged into YouTube in your browser
3. Re-export fresh cookies

### Rate Limiting ("try again later")

YouTube is temporarily blocking requests:

1. Wait 10-30 minutes before retrying
2. Reduce download threads in `config.yaml`:
   ```yaml
   download:
     threads:
       download: 2  # Lower = less aggressive
   ```
3. Try running without cookies (sometimes causes more rate limiting)

### "Spotify authentication failed"

- Verify `client_id` and `client_secret` in `config.yaml`
- Check that your Spotify app is properly configured

### Low audio quality (128 kbps)

- Without cookies, YouTube limits quality to 128 kbps
- Use a cookie file with YouTube Premium for 256 kbps

## Metadata Tags

The following metadata is embedded in each M4A file:

| Tag | Source |
|-----|--------|
| Title | Spotify |
| Artist | Spotify |
| Album | Spotify |
| Album Artist | Spotify |
| Year | Spotify |
| Genre | Spotify (from artist) |
| Track Number | Spotify |
| Disc Number | Spotify |
| Cover Art | Spotify (downloaded) |
| Lyrics | Genius, AZLyrics, MusixMatch, Synced |
| Explicit | Spotify |
| Copyright | Spotify |
| ISRC | Spotify |

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