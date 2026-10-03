# Soundloom

**Weave new music into your library.** A local music library that fills itself:
watch artists for new releases, mirror Spotify/Deezer playlists, download with
per-source adapters, and keep metadata and covers clean.

- **Download** — Deezer (deemix), YouTube (yt-dlp), and SoundCloud adapters
- **Sync** — Soundiiz-style: keep a Spotify or Deezer playlist mirrored into
  your library on a schedule, downloading what you don't already own
- **Watch** — auto-download new releases from followed artists
- **Identify / Clean up / Convert** — metadata fixes against MusicBrainz and
  Spotify, cover-art fetching, duplicate resolution, format conversion
- **Playlist Hub** — import playlists (Spotify, Deezer, iTunes XML, M3U, CSV)
  and export to M3U8, CSV, iTunes XML, JSON, Spotify, or SoundCloud

## Requirements

- Python 3.12+
- `ffmpeg` on PATH
- `deemix` (npm, installed globally) for Deezer downloads

## Setup

```bat
python -m venv .venv
.venv\Scripts\pip install -e .
copy data\settings.example.json data\settings.json
run.bat
```

The UI opens at `http://127.0.0.1:5555` and auto-shuts down when the browser
closes (heartbeat watchdog).

## Tests

```bat
.venv\Scripts\python.exe -m pytest tests -q
```

## Connecting Spotify / SoundCloud (Playlist Hub export)

Create an API app at developer.spotify.com (free) or soundcloud.com (API
access requires Artist Pro), put the client id + secret in Settings, and
register this redirect URI:

```
http://127.0.0.1:5555/api/connections/callback
```

Then hit Connect on the Playlists tab to authorize your account.

## Notes

- `data/` is machine-local and git-ignored: it holds the SQLite library DB,
  the real `settings.json` (credentials), logs, and exports. Only
  `data/settings.example.json` is tracked.
- Paths are portable: `backend/config.py` derives the project root from
  `__file__`, and `run.bat` resolves its own directory via `%~dp0`.
- Desktop build: `build_exe.bat` produces `dist\Soundloom\Soundloom.exe`
  (PyInstaller; the exe keeps its own `data/` directory next to it).
