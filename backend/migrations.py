"""Database migration runner with version tracking.

Each migration is a named function that runs SQL to alter the schema.
Migrations are tracked in a schema_version table so they only run once.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger("soundloom.migrations")


@dataclass
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="add_artist_fields",
        sql="""
        ALTER TABLE tracks ADD COLUMN primary_artist TEXT DEFAULT '';
        ALTER TABLE tracks ADD COLUMN featured_artists TEXT DEFAULT '';
        ALTER TABLE tracks ADD COLUMN display_artist TEXT DEFAULT '';
        """,
    ),
    Migration(
        version=2,
        name="add_file_status",
        sql="ALTER TABLE tracks ADD COLUMN file_status TEXT DEFAULT 'present'",
    ),
    Migration(
        version=3,
        name="add_file_status_index",
        sql="CREATE INDEX IF NOT EXISTS idx_tracks_file_status ON tracks(file_status)",
    ),
    Migration(
        version=4,
        name="watcher_state",
        sql="""
        ALTER TABLE watched_artists ADD COLUMN deezer_artist_id TEXT DEFAULT '';
        ALTER TABLE watched_artists ADD COLUMN known_releases TEXT DEFAULT '[]';
        ALTER TABLE watched_artists ADD COLUMN last_error TEXT DEFAULT '';
        ALTER TABLE watched_artists ADD COLUMN last_enqueued INTEGER DEFAULT 0;
        """,
    ),
    Migration(
        version=5,
        name="fetch_candidates",
        sql="""
        CREATE TABLE IF NOT EXISTS fetch_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            file_path TEXT DEFAULT '',
            source TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            current_tags TEXT DEFAULT '{}',
            proposed_tags TEXT DEFAULT '{}',
            diff TEXT DEFAULT '{}',
            notes TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            applied_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fetch_candidates_status ON fetch_candidates(status);
        CREATE INDEX IF NOT EXISTS idx_fetch_candidates_track ON fetch_candidates(track_id);
        """,
    ),
    Migration(
        version=6,
        name="jobs_backoff_and_synced_playlists",
        sql="""
        ALTER TABLE jobs ADD COLUMN not_before TEXT;
        CREATE TABLE IF NOT EXISTS synced_playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_type TEXT DEFAULT 'spotify',
            source_ref TEXT DEFAULT '',
            url TEXT DEFAULT '',
            auto_sync INTEGER DEFAULT 0,
            sync_interval INTEGER DEFAULT 0,
            quality_profile TEXT DEFAULT 'balanced',
            last_synced TEXT,
            last_track_ids TEXT DEFAULT '[]',
            last_error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        """,
    ),
    Migration(
        version=7,
        name="playlist_hub",
        # Non-destructive: the pre-hub `playlists` table held
        # (id, name, created_at) and was never written to, but an ALTER-based
        # migration keeps any rows that do exist. The old version did
        # `DROP TABLE IF EXISTS playlists`, which destroyed playlist data on
        # upgrade - never use DROP here again.
        sql="""
        ALTER TABLE playlists ADD COLUMN description TEXT DEFAULT '';
        ALTER TABLE playlists ADD COLUMN origin TEXT DEFAULT 'manual';
        ALTER TABLE playlists ADD COLUMN origin_ref TEXT DEFAULT '';
        -- SQLite forbids a non-constant DEFAULT in ADD COLUMN, so the new
        -- column starts empty and is backfilled below.
        ALTER TABLE playlists ADD COLUMN updated_at TEXT DEFAULT '';
        UPDATE playlists SET updated_at = COALESCE(created_at, datetime('now')) WHERE updated_at IS NULL OR updated_at = '';
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            origin TEXT DEFAULT 'manual',
            origin_ref TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            title TEXT DEFAULT '',
            artist TEXT DEFAULT '',
            album TEXT DEFAULT '',
            duration REAL DEFAULT 0.0,
            path TEXT DEFAULT '',
            track_id INTEGER,
            ext_id TEXT DEFAULT '',
            ext_source TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks(playlist_id);
        """,
    ),
]


async def run_migrations(db) -> int:
    """Run all pending migrations. Returns number of migrations applied."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.commit()

    cursor = await db.execute("SELECT MAX(version) as v FROM schema_version")
    row = await cursor.fetchone()
    current_version = row[0] if row and row[0] else 0

    applied = 0
    pending = [m for m in MIGRATIONS if m.version > current_version]
    if pending:
        # Snapshot the library before touching schema. If a migration fails
        # halfway, this file is the way back.
        from .services.backup import backup_database
        from . import config as _config

        backed_up = backup_database(_config.DB_PATH)
        if backed_up:
            logger.info("Database backed up before migration: %s", backed_up)

    for m in pending:
        try:
            for statement in m.sql.strip().split(";"):
                statement = statement.strip()
                if statement:
                    try:
                        await db.execute(statement)
                    except Exception as e:
                        if "duplicate column name" in str(e):
                            logger.debug(f"Migration {m.version}: column already exists, skipping")
                        else:
                            raise
            await db.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (m.version, m.name),
            )
            await db.commit()
            logger.info(f"Migration {m.version}: {m.name} applied")
            applied += 1
        except Exception as e:
            logger.error(f"Migration {m.version} ({m.name}) failed: {e}")
            raise

    return applied
