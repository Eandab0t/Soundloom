# ETuner — AI Handoff (BETA)

**Purpose**: Music library management suite. Two components:
1. **VividlyMusicaly ("Big Pickle")** — Web-based library intelligence system. Downloads from YouTube/SoundCloud/etc, auto-tags, auto-organizes.
2. **E-Tuner Desktop** — Windows WPF app for offline tag fixing, dedup, sorting. Plus Python CLI for automation.

**Runtime**: Python 3.12 + FastAPI + SQLite + vanilla HTML/CSS/JS SPA. Runs at `localhost:5555`. Auto-shuts down when browser closes (heartbeat watchdog).

> **BETA STATUS**: This software is in beta. Expect bugs, missing features, and breaking changes.

---

## Architecture

```
Browser (SPA) ──WebSocket──▶ FastAPI ──aiosqlite──▶ SQLite DB
     │                           │
     │   REST API                │  Pipeline stages:
     ├─ /api/library/*           │  resolve → match → download → convert → tag → cleanup → organize
     ├─ /api/sources/*           │
     ├─ /api/queue/*             │  Adapters:
     ├─ /api/watch/*             │  yt-dlp (YouTube/SoundCloud/etc)
     ├─ /api/autofix/*           │  mutagen (audio tags)
     ├─ /api/settings            │  FFmpeg (conversion)
     └─ /ws/events               │  pydantic validation
```

**Key pattern**: Pipeline adapter architecture. The active resolver and downloader contracts (`SourceResolver`, `DownloadProvider`) live in `backend/pipeline/interfaces.py`. Source adapters implement them without changing the pipeline.

---

## Directory Structure

```
ETuner/
├── backend/                 # VividlyMusicaly - Python FastAPI web app
│   ├── main.py              # FastAPI app, lifespan, WebSocket, auto-shutdown watchdog
│   ├── config.py            # Settings persistence (data/settings.json), DEFAULTS dict
│   ├── database.py          # SQLite schema (tracks/jobs/watched_artists), asyncio.Lock on writes
│   ├── migrations.py        # Versioned schema migrations with schema_version table
│   ├── errors.py            # Exception hierarchy: VividlyError → 13 subclasses
│   ├── validation.py        # validate_path (anti-traversal), validate_folder_template, sanitize_filename
│   ├── logging_config.py    # JsonFormatter, HumanFormatter (color), RotatingFileHandler
│   ├── events.py            # Pub/sub event bus → WebSocket broadcasting
│   ├── api/                 # 7 API routers (see endpoints below)
│   ├── pipeline/            # Core pipeline logic
│   │   ├── models.py        # JobState enum, TrackMetadata, SourceCandidate, QualityProfile
│   │   ├── interfaces.py    # Resolver and downloader contracts
│   │   ├── normalize.py     # normalize_artist/title/album, parse_artists
│   │   ├── matcher.py       # Weighted scoring (title 30%, artist 25%, album 15%, etc.)
│   │   ├── editor.py        # apply_metadata_to_file (mutagen)
│   │   └── cleanup.py       # E-Tuner-adapted cleanup rules (397 lines, largest pipeline file)
│   ├── services/            # Business logic layer
│   │   ├── downloader.py    # Queue worker, semaphore concurrency, full pipeline orchestrator
│   │   ├── converter.py     # FFmpeg conversion via QualityProfile
│   │   ├── tagger.py        # mutagen read/write tags (MP3/M4A/FLAC/OGG)
│   │   ├── organizer.py     # Template-based file organization + dedup
│   │   ├── scanner.py       # Library reconciliation scan (fingerprint-based diff)
│   │   ├── matcher.py       # Local library search
│   └── sources/
│       └── ytdlp_source.py  # yt-dlp adapter: YtdlpResolver + YtdlpDownloader
├── frontend/
│   ├── index.html           # SPA shell: 7 tabs, topbar, sidebar, statusbar (297 lines)
│   ├── css/styles.css       # Dark/light theme via CSS variables (213 lines)
│   └── js/app.js            # Single-file SPA: all modules (754 lines)
├── etuner/                  # E-Tuner Desktop - Windows WPF + Python CLI
│   ├── src/
│   │   ├── etuner/          # Python source (run_etuner.py, build_exe.py)
│   │   └── dotnet/          # .NET WPF source (csproj, xaml, cs files)
│   │       ├── Converters/
│   │       ├── Models/
│   │       ├── Services/
│   │       └── ViewModels/
│   ├── tests/               # Python tests (8 files)
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── config.json
│   └── CHANGELOG.md
├── tests/                   # VividlyMusicaly tests (150 tests, pytest + asyncio_mode=auto)
├── data/                    # Runtime data (SQLite DB, logs, settings, covers)
├── HANDOFF.md               # This file
├── run.bat                  # Windows launcher
└── pyproject.toml           # Dependencies and project config
```

---

## Database Schema (SQLite)

### `tracks` table (22 columns)
```sql
id, file_path (UNIQUE), title, artist, primary_artist, featured_artists,
display_artist, album_artist, album, track_number, disc_number, year, genre,
duration, file_size, format, bitrate, sample_rate, cover_art_path,
source_url, source_type, file_status ('present'|'missing'),
created_at, updated_at
-- Indexed: artist, album, album_artist
```

### `jobs` table (18 columns)
```sql
id, status, source_url, source_type, query, title, artist, album,
album_artist, year, cover_art_url, cover_art_path, output_format,
quality_profile, output_path, error, retries, progress,
created_at, updated_at
```

### `watched_artists` table
```sql
id, artist_name (UNIQUE), auto_download, quality_profile,
last_checked, last_new_release
```

### `schema_version` table
```sql
version INTEGER, applied_at TEXT
```

---

## API Endpoints

| Router | Prefix | Key Endpoints |
|--------|--------|---------------|
| `library` | `/api/library` | `GET /tracks` (search/sort/filter/paginate), `GET /tracks/{id}`, `PUT /tracks/{id}`, `GET /artists`, `GET /artists/{name}`, `GET /albums`, `GET /stats`, `GET /scan`, `GET /scan/status` |
| `sources` | `/api/sources` | `POST /resolve`, `POST /preview` (DownloadPlan), `POST /confirm`, `GET /search` |
| `queue` | `/api/queue` | `GET /` (filtered), `POST /` (add with dedup), `DELETE /{id}`, `POST /{id}/retry`, `GET /stats`, `POST /clear-completed`, `POST /clear-failed` |
| `watch` | `/api/watch` | `GET /`, `POST /` (add artist), `PUT /{id}`, `DELETE /{id}` |
| `autofix` | `/api/autofix` | `POST /scan`, `POST /fix-all`, `POST /fix-file`, `GET /preview/{track_id}` |
| `settings` | `/api/settings` | `GET /`, `PUT /` |
| `system` | `/api/` | `POST /heartbeat`, `GET /health` |
| WebSocket | `/ws/events` | Real-time job_update, library_change, log events |

---

## Download Pipeline Flow

```
pending → resolving → matching → downloading → converting → tagging → cleanup → organizing → indexing → complete
```

1. **Resolving**: yt-dlp extracts metadata (title, artist, duration, thumbnail)
2. **Matching**: Weighted scoring against desired metadata (title 30%, artist 25%, album 15%, duration 10%)
3. **Downloading**: yt-dlp downloads best available audio format
4. **Converting**: FFmpeg converts to target format/bitrate via QualityProfile
5. **Tagging**: mutagen writes metadata tags
6. **Cleanup**: `run_cleanup()` runs E-Tuner-style fixes (whitespace, duplicates, feat. extraction, track numbers, album inference)
7. **Organizing**: Template-based folder structure with dedup policy
8. **Indexing**: INSERT into tracks table

---

## Quality Profiles

| Profile | Format | Codec | Bitrate | Use Case |
|---------|--------|-------|---------|----------|
| `best` | FLAC | flac | lossless | Archival |
| `balanced` | MP3 | libmp3lame | 320k | Default |
| `ipod_saver` | M4A | aac | 128k | iPod Touch 3G (32GB, iOS 5.1.1) |

---

## Frontend Modules (app.js)

| Module | Responsibility |
|--------|---------------|
| `Library` | Track list with search/sort/pagination, artist/album sub-views, scan trigger |
| `Queue` | Active/completed/failed download jobs with progress, retry, clear |
| `AddSource` | URL paste or local text search → preview DownloadPlan → confirm → queue |
| `Watch` | Artist follow list for auto-downloading new releases |
| `Metadata` | Tag editor for existing library files |
| `Settings` | Library path, quality defaults, folder template, server config |
| `Logs` | Server log viewer |

---

## Key Design Decisions

- **Single-file frontend**: `app.js` (754 lines) — no build step, no framework
- **Single-file CSS**: CSS variables for dark/light theming
- **WebSocket event bus**: All backend events flow through `events.py` → single `/ws/events` endpoint
- **Auto-shutdown**: Heartbeat from browser every 30s. Server calls `os._exit(0)` on timeout.
- **asyncio.Lock on all DB writes**: Prevents concurrent write corruption
- **Download deduplication**: Returns 409 if same URL already queued
- **Migration system**: `schema_version` table + idempotent `ALTER TABLE` handling
- **Cleanup pipeline**: Adapted from E-Tuner project — runs in-memory before write_tags

---

## External Dependencies

| Package | Role |
|---------|------|
| fastapi | Web framework |
| uvicorn | ASGI server |
| aiosqlite | Async SQLite |
| mutagen | Audio tag read/write |
| yt-dlp | Music source extraction + download |
| aiohttp | Async HTTP (Spotify API, MusicBrainz) |
| FFmpeg (system) | Audio conversion |
| pydantic | Data validation |
| websockets | WebSocket support |

---

## Environment

- **OS**: Windows 11 Home
- **Python**: 3.12.10
- **Music library**: `C:\Users\<you>\Music\` (MP3s, flat)
- **Project location**: `E:\<project-root>\Ean Applications\VividlyMusicaly`
- **iPod target**: iPod Touch (32GB, iOS 5.1.1 max)
- **Tests**: 150 passing (pytest + pytest-asyncio)

---

## What's Built (Working)

- Full library management (search, sort, filter, edit, scan)
- Download pipeline with yt-dlp (YouTube, SoundCloud, Vimeo, Bandcamp, Twitch)
- Auto-fix/cleanup pipeline (E-Tuner rules adapted, runs during download)
- Artist watch system (CRUD only — no auto-download polling yet)
- Real-time WebSocket events with auto-reconnect
- Error handling hierarchy + retry decorator
- Schema versioning + migrations
- Input validation + path traversal prevention
- Structured logging with rotation
- Dark/light theme
- DRM error handling (friendly 422 instead of 500)
- Frontend error parsing (clean toast messages)
- 150 tests

## What's Not Yet Built

- Artist Watch auto-download polling (CRUD works, no background checker)
- Lossy→lossless upgrade warning
- API documentation (OpenAPI/Swagger)
- Playlist management (table exists, no UI)

---

## Known Bugs / Tech Debt

1. **No virtual scrolling**: Library renders all 100 tracks as DOM nodes — will throttle at 1000+
2. **No batch queue operations**: "Retry All Failed" and "Clear All Failed" buttons exist but no bulk actions for active jobs
3. **No atomic file processing**: Downloads write directly to temp dir, scanner could index partial files
5. **No rate-limit/backoff on yt-dlp**: HTTP 429 errors from YouTube will fail jobs without retry
6. **Watch polling not implemented**: watched_artists table exists but no background job checks for new releases

---

## Master Plan (Future Work)

### 1. Frontend & UI Reactivity

- **Auto-reconnecting WebSocket** with exponential backoff (currently basic reconnect on 3s timer)
- **Input debouncing** (partially done on library search, needs audit across all inputs)
- **Toast notifications** for backend events via WebSocket (partially implemented via showToast)
- **Virtual scrolling** for Library tab when track count exceeds ~500

### 2. Backend & Extraction Resilience

- **yt-dlp hardening**: Automated cookie management, fallback user-agents, rate-limit handling with backoff
- **Batch queue operations**: "Retry All Failed" bulk action

### 3. System Utility & Infrastructure

- **CLI mode**: Headless terminal interface that hooks into the backend API for remote/VPS/Termux use
- **Atomic file processing**: Download to `.temp` dir, only move to library when 100% complete and tagged
- **Go proxy script**: Offload concurrent network routing to a Go binary for better performance under load
