# Soundloom

**Weave new music into your library.** A local music library that fills itself:
watch artists for new releases, mirror Spotify/Deezer playlists, download with
per-source adapters, and keep metadata and covers clean.

- **Download** — Deezer (deemix), YouTube (yt-dlp), and SoundCloud adapters, plus
  a local-filesystem source for a file you already have
- **Sync** — Soundiiz-style: keep a Spotify or Deezer playlist mirrored into
  your library on a schedule, downloading what you don't already own
- **Watch** — auto-download new releases from followed artists
- **Identify / Fix / Convert** — metadata fixes against MusicBrainz and Spotify,
  cover-art fetching, duplicate resolution; Convert runs inside your browser
  (FFmpeg.wasm) and never writes to your library
- **Playlist Hub** — import playlists (Spotify, Deezer, iTunes XML, M3U, CSV)
  and export to M3U8, CSV, iTunes XML, JSON, Spotify, or SoundCloud
- **Library forensics** — an append-only log records when a tracked file goes
  missing or its content changes, without editing your library

Everything is local. Your music stays on your disk, the database is one SQLite
file in `data/`, and the server binds to `127.0.0.1` only.

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

To run without a browser window, or on another port, run the server directly:

```bat
.venv\Scripts\python.exe -m backend.main
```

`server_port` and `server_host` in `data/settings.json` control where it binds.

## Point it at your music

Two settings decide where Soundloom reads and writes. Set them in
`data/settings.json`, or in the Settings tab:

```json
{
  "library_path": "E:/Music",
  "download_path": "E:/Music/_incoming"
}
```

- **`library_path`** is your existing music. Soundloom only reads it during a
  scan, and only writes there for tracks it just acquired or that you moved
  there yourself.
- **`download_path`** is a staging folder for files on their way in. Set it to a
  path you do not mind filling up.

`folder_template` decides where an acquired track lands inside your library:

```json
{ "folder_template": "{album_artist}/{album}/{track_number} - {title}.{format}" }
```

After changing `library_path`, run a scan from the Library tab (or
`GET /api/library/scan`) so Soundloom learns what is already there. A scan only
reads tags and file sizes; it does not rename, move, or rewrite anything.

### Two settings worth knowing

Both are in `data/settings.example.json`, and both default to the safe answer:

- **`verify_library_on_startup`** (default `true`) asks, at every start,
  whether each tracked file is still on disk, and marks the ones that are not.
  It checks existence only, never reads tags, and it refuses to run at all if
  `library_path` is not reachable. Turn it off if your library lives on storage
  that is often offline.
- **`auto_resume_operations`** (default `false`) decides whether an
  acquisition interrupted by a crash or a closed laptop is re-run automatically
  at the next start. It is off because a track that already reached the library
  is cheap to recover, while re-downloading one costs bandwidth and time. With
  it off, `GET /api/operations/interrupted` lists what was left half-done and
  `POST /api/operations/{op_id}/retry` finishes it when you say so.

## The basic workflow

1. **Scan.** Library tab, or `GET /api/library/scan`. Reads tags from every
   audio file under `library_path` and records what it found. Follow along at
   `GET /api/library/scan/status`.
2. **Add music.** Three ways:
   - **Add tab** — paste a URL or type an artist plus title and queue it.
   - **Watch tab** — follow an artist and check on a schedule.
   - **Sync tab** — mirror a Spotify or Deezer playlist; Soundloom downloads
     what it does not already own.
   A local path also works: give the Add tab the full path to a file you
   already have and it is copied into the library, never moved.
3. **Watch the queue.** Queue tab, or `GET /api/queue`. Each job runs
   resolve → match → download → convert → tag → organize → index.
4. **Tidy.** Identify fixes metadata against MusicBrainz; Fix resolves duplicate
   artists; Attention lists what needs a decision; Metadata edits tags.
5. **Recover.** If a file moves or disappears, the Library tab offers Find
   (exact SHA-256 content match) and Locate (point a track at a path you found).

## Where your data lives

Everything Soundloom owns is under `data/` in the project directory. That
folder is machine-local and git-ignored, so it never leaves your machine.

| Path | What it is |
| --- | --- |
| `data/library.db` | the library database (SQLite) |
| `data/settings.json` | your settings, including API credentials |
| `data/logs/` | application logs, readable from the Logs tab |
| `data/covers/` | cached cover art |
| `data/exports/` | playlist exports |
| `data/backups/db/` | automatic pre-migration database snapshots |
| `data/tmp/` | staging area, safe to clear when nothing is downloading |

Only `data/settings.example.json` is tracked. Deleting `data/` resets
Soundloom completely; it does not touch your music.

### Backing up

`data/library.db` is your index, not your music, and it can be rebuilt with a
scan. If you want a real backup, copy `data/` with the app closed, or stop
first — copying a SQLite file while it is being written can capture a torn
page. `data/backups/db/` is written automatically before every migration.

## What is safe

Soundloom is built so that the worst answer to an uncertain question is "I did
not change anything".

**Never touches your files:**

- **Scanning** reads tags and stats. No rename, no move, no tag write.
- **The library startup check** only asks whether each path exists. It refuses
  to run at all if `library_path` is not reachable, so an unplugged drive
  cannot blank your library. Turn it off with `verify_library_on_startup`.
- **The forensic observer** is an append-only log. It never writes to your
  files and never edits a track row; it only records what it saw. Read it at
  `GET /api/forensics/summary`, or force a pass with `POST /api/forensics/poll`.
- **Convert** runs FFmpeg.wasm inside the browser on files you pick. The server
  is not involved and your library is not touched.
- **Local acquisition copies, never moves.** Hand it a path and the file at
  that path is left exactly as it was.
- **Metadata writes are previewed first.** `backup_before_write` (on by
  default) keeps a copy of the old tags before overwriting them.

**Does change things, on purpose:**

- **Acquiring** a track copies it into your library under `folder_template`.
  Duplicate policy decides what happens if a file already exists there.
- **Identify, Fix, and Metadata** write tags to the files they are pointed at.
  They preview before they commit, and Identify only writes when you accept a
  candidate.
- **Deleting a track from the Library tab** removes the database row only. The
  audio file is left on disk exactly where it was.
- **`duplicate_policy: overwrite`** replaces an existing file. Leave it on
  `keep_separate` unless you mean it.

When something looks wrong, the answer is never an automatic repair. Find
resolves a move only on an exact SHA-256 match, and Locate records the old
path alongside the new one so the correction stays auditable.

## Limitations

Known and current. None of these are silently worked around.

- **The content index does not join to the library.** The `library_content`
  table (built by the content index) is keyed by path in its own format and
  does not match `tracks.file_path`, so its results cannot currently be used to
  resolve moves automatically. Find and Locate work off SHA-256 and the file
  system instead. There is no button for it in the UI; rebuild it with
  `POST /api/library/content-index/rebuild` and read it back at
  `GET /api/library/content-index`.
- **`tracks.file_size` is a scan-time snapshot and drifts.** It records the size
  the file had when it was indexed, so it can disagree with the current file on
  disk after an external tagger rewrites a file. Playback and the file size
  shown by your file manager are unaffected; do not treat the stored number as
  authoritative. Re-scanning refreshes it.
- **Forensics only watches files with a recorded digest.** A track indexed by a
  plain scan, with no acquisition row and no content-index entry, has nothing to
  compare against, so the observer does not watch it and does not hash your
  library to find out. Tracks that came through acquisition or are registered
  canaries are watched.
- **`bitrate` is source metadata, not a measurement.** It can disagree with what
  the file's own headers say, for example on an older library.
- **A move can read as `missing` rather than `moved`.** The observer resolves a
  move from recorded digests only; with no index entry at the new path it says
  "not on disk", which is honest but not specific. Find and Locate turn that
  into a repair when you confirm it.
- **Deezer needs `deemix` and Spotify and SoundCloud need your own API
  credentials.** Soundloom cannot download from a source you have not
  configured. A local path always works, with no credentials and no network.
- **Convert needs internet on first use** to fetch the FFmpeg engine into your
  browser. It is a standalone tool: it does not update your library index.
- **Windows paths in pasted JSON need escaping.** Prefer forward slashes
  (`E:/Music`) when you hand Soundloom a path; both work everywhere.

## Connecting Spotify / SoundCloud (Playlist Hub export)

Create an API app at developer.spotify.com (free) or soundcloud.com (API
access requires Artist Pro), put the client id + secret in Settings, and
register this redirect URI:

```
http://127.0.0.1:5555/api/connections/callback
```

Then hit Connect on the Playlists tab to authorize your account.

## Tests

```bat
.venv\Scripts\python.exe -m pytest tests -q
```

## Notes

- Paths are portable: `backend/config.py` derives the project root from
  `__file__`, and `run.bat` resolves its own directory via `%~dp0`.
- Desktop build: `build_exe.bat` produces `dist\Soundloom\Soundloom.exe`
  (PyInstaller; the exe keeps its own `data/` directory next to it).
