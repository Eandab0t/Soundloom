"""SQLite database management via aiosqlite."""
import asyncio
import aiosqlite
import logging
from contextlib import asynccontextmanager
from . import config

logger = logging.getLogger(__name__)

_db_lock = asyncio.Lock()

# Pragmas applied once per connection. WAL lets the API read while the worker
# writes (the old rollback journal mode blocked readers on every commit), and
# NORMAL sync keeps durability without an fsync on every transaction.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    title TEXT DEFAULT '',
    artist TEXT DEFAULT '',
    primary_artist TEXT DEFAULT '',
    featured_artists TEXT DEFAULT '',
    display_artist TEXT DEFAULT '',
    album_artist TEXT DEFAULT '',
    album TEXT DEFAULT '',
    track_number INTEGER DEFAULT 0,
    disc_number INTEGER DEFAULT 0,
    year INTEGER DEFAULT 0,
    genre TEXT DEFAULT '',
    duration REAL DEFAULT 0.0,
    file_size INTEGER DEFAULT 0,
    format TEXT DEFAULT '',
    bitrate INTEGER DEFAULT 0,
    sample_rate INTEGER DEFAULT 0,
    cover_art_path TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    source_type TEXT DEFAULT 'local',
    file_status TEXT DEFAULT 'present',
    library_status TEXT DEFAULT 'active',
    archived_at TEXT,
    archived_reason TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT DEFAULT 'pending',
    source_url TEXT DEFAULT '',
    source_type TEXT DEFAULT '',
    query TEXT DEFAULT '',
    title TEXT DEFAULT '',
    artist TEXT DEFAULT '',
    album TEXT DEFAULT '',
    album_artist TEXT DEFAULT '',
    year INTEGER DEFAULT 0,
    cover_art_url TEXT DEFAULT '',
    cover_art_path TEXT DEFAULT '',
    output_format TEXT DEFAULT 'mp3',
    quality_profile TEXT DEFAULT 'balanced',
    output_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    retries INTEGER DEFAULT 0,
    progress REAL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    origin TEXT DEFAULT 'manual',
    origin_ref TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS acquisition (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    track_id INTEGER,
    source TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    completed_at TEXT DEFAULT (datetime('now')),
    file_path TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    organizer_result TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS library_content (
    digest TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provenance_backfill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT,
    completed_at TEXT,
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    bytes_processed INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_track_id INTEGER NOT NULL DEFAULT 0,
    last_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS synced_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_type TEXT DEFAULT 'spotify',   -- spotify | deezer
    source_ref TEXT DEFAULT '',           -- platform playlist id or URL
    url TEXT DEFAULT '',                  -- direct URL when pasted by hand
    auto_sync INTEGER DEFAULT 0,          -- 1 = sync on the timer, not just on demand
    sync_interval INTEGER DEFAULT 0,      -- 0 = use the global sync_interval
    quality_profile TEXT DEFAULT 'balanced',
    last_synced TEXT,
    last_track_ids TEXT DEFAULT '[]',     -- JSON array of source track ids from the last sync
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watched_artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_name TEXT NOT NULL UNIQUE,
    auto_download INTEGER DEFAULT 0,
    quality_profile TEXT DEFAULT 'balanced',
    last_checked TEXT,
    last_new_release TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album);
CREATE INDEX IF NOT EXISTS idx_tracks_album_artist ON tracks(album_artist);
-- idx_tracks_library_status is created by migration 11, not here:
-- SCHEMA runs before migrations, and on an upgraded database the
-- column does not exist yet, so indexing it here fails outright.
CREATE INDEX IF NOT EXISTS idx_acquisition_track ON acquisition(track_id);
CREATE INDEX IF NOT EXISTS idx_acquisition_sha ON acquisition(sha256);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

_db: aiosqlite.Connection | None = None


async def init_db():
    global _db
    _db = await aiosqlite.connect(str(config.DB_PATH))
    _db.row_factory = aiosqlite.Row
    for pragma in _PRAGMAS:
        try:
            await _db.execute(pragma)
        except Exception as e:
            logger.warning(f"Pragma failed ({pragma}): {e}")
    await _db.executescript(SCHEMA)
    await _db.commit()
    from .migrations import run_migrations
    applied = await run_migrations(_db)
    if applied:
        logger.info(f"Applied {applied} migration(s)")
    logger.info(f"Database initialized at {config.DB_PATH}")


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


@asynccontextmanager
async def get_db():
    global _db
    if _db is None:
        await init_db()
    yield _db


async def execute(sql: str, params: tuple = ()) -> aiosqlite.Cursor:
    async with _db_lock:
        async with get_db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor


async def executemany(sql: str, rows: list[tuple]) -> None:
    """Run one statement across many parameter sets, committing once.

    `execute` binds a single flat parameter tuple, so a batch has to go
    through here rather than being passed a list - which raises a binding
    error at runtime rather than at import. One commit for the whole batch
    matters for the content index, which writes dozens of rows per chunk.

    An empty list is a no-op, because sqlite3 rejects a zero-row
    executemany outright.
    """
    if not rows:
        return
    async with _db_lock:
        async with get_db() as db:
            await db.executemany(sql, rows)
            await db.commit()


async def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None
