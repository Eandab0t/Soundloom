"""SQLite database management via aiosqlite."""
import asyncio
import aiosqlite
import logging
from contextlib import asynccontextmanager
from . import config

logger = logging.getLogger(__name__)

_db_lock = asyncio.Lock()

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
    created_at TEXT DEFAULT (datetime('now')),
    track_ids TEXT DEFAULT '[]'
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
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

_db: aiosqlite.Connection | None = None


async def init_db():
    global _db
    _db = await aiosqlite.connect(str(config.DB_PATH))
    _db.row_factory = aiosqlite.Row
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
