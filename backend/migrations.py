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
    Migration(
        version=8,
        name="operations_journal",
        sql="""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            op_key TEXT UNIQUE,
            job_id INTEGER,
            kind TEXT DEFAULT 'download',
            state TEXT DEFAULT 'queued',
            title TEXT DEFAULT '',
            artist TEXT DEFAULT '',
            album TEXT DEFAULT '',
            album_artist TEXT DEFAULT '',
            track_number INTEGER DEFAULT 0,
            source_url TEXT DEFAULT '',
            staged_path TEXT DEFAULT '',
            final_path TEXT DEFAULT '',
            track_id INTEGER,
            error TEXT DEFAULT '',
            attempt INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state);
        CREATE INDEX IF NOT EXISTS idx_operations_job ON operations(job_id);
        """,
    ),
    Migration(
        version=9,
        name="attention_items",
        sql="""
        CREATE TABLE IF NOT EXISTS attention_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL DEFAULT 'generic',
            severity TEXT NOT NULL DEFAULT 'medium',
            title TEXT NOT NULL DEFAULT '',
            description TEXT DEFAULT '',
            entity_type TEXT DEFAULT '',
            entity_id INTEGER,
            source TEXT DEFAULT '',
            action TEXT DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            seen_count INTEGER NOT NULL DEFAULT 1,
            resolved_note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attention_status ON attention_items(status);
        CREATE INDEX IF NOT EXISTS idx_attention_type ON attention_items(type);
        CREATE INDEX IF NOT EXISTS idx_attention_source ON attention_items(source);
        """,
    ),
    Migration(
        version=10,
        name="interactive_match_decisions",
        # A blocked download has to be decidable *from the screen that
        # reported it*, not by editing a job row in a SQL console. These two
        # columns are that decision's memory on the job itself:
        #
        #   match_block    JSON describing the last rejected candidate, so a
        #                  producer can explain "wanted X, got Y" without
        #                   re-resolving the source (which may 404 by now).
        #   match_override one-shot 'accept', consumed the moment the gate
        #                  honours it. A persistent flag would silently
        #                  bypass every *future* attempt on this job too.
        #
        # match_decisions is the audit trail. The attention_items row records
        # that something was decided; this records what was decided, with the
        # score and threshold that were overridden, and it outlives the job -
        # "clear failed" deletes job rows, and the user's decision to accept a
        # 58% match is exactly the kind of thing that must not vanish.
        sql="""
        ALTER TABLE jobs ADD COLUMN match_block TEXT DEFAULT '';
        ALTER TABLE jobs ADD COLUMN match_override TEXT DEFAULT '';
        CREATE TABLE IF NOT EXISTS match_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            attention_item_id INTEGER,
            decision TEXT NOT NULL DEFAULT '',
            wanted_artist TEXT DEFAULT '',
            wanted_title TEXT DEFAULT '',
            candidate_artist TEXT DEFAULT '',
            candidate_title TEXT DEFAULT '',
            score REAL DEFAULT 0,
            threshold REAL DEFAULT 0,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_match_decisions_job
            ON match_decisions(job_id);
        CREATE INDEX IF NOT EXISTS idx_match_decisions_created
            ON match_decisions(created_at DESC);
        """,
    ),
    Migration(
        version=11,
        name="library_state_and_acquisition",
        # Two independent axes, previously conflated into one column.
        #
        # `file_status` answers "is the bytes still where we left them?" and is
        # owned by the filesystem checker. `library_status` answers "is the user
        # still collecting this?" and is owned by the user. Archive is the
        # second axis: it hides a record from active views while keeping the
        # row, its metadata, its original path and its provenance. It is a
        # database state and touches no file, which is what makes it reversible
        # in a way that deleting a row or a file never is.
        #
        # `acquisition` is the forensic record that turns the next disappearance
        # from an inference into an answer. A log line ("Job 42 complete") is
        # gone after rotation; this table records where the file came from, how
        # big it was, and its SHA-256 - so a file that turns up somewhere else
        # can be proven to be the same file rather than merely similar.
        sql="""
        ALTER TABLE tracks ADD COLUMN library_status TEXT DEFAULT 'active';
        ALTER TABLE tracks ADD COLUMN archived_at TEXT;
        ALTER TABLE tracks ADD COLUMN archived_reason TEXT DEFAULT '';
        UPDATE tracks SET library_status = 'active'
            WHERE library_status IS NULL OR library_status = '';
        CREATE INDEX IF NOT EXISTS idx_tracks_library_status
            ON tracks(library_status);
        CREATE INDEX IF NOT EXISTS idx_tracks_state
            ON tracks(library_status, file_status);
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
        CREATE INDEX IF NOT EXISTS idx_acquisition_track ON acquisition(track_id);
        CREATE INDEX IF NOT EXISTS idx_acquisition_sha ON acquisition(sha256);
        CREATE INDEX IF NOT EXISTS idx_acquisition_job ON acquisition(job_id);
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
        from . import config as _config

        # Resolve the destination from the backup module at call time instead
        # of letting backup_database fall back to its import-time constant, so
        # a caller that redirects backup_svc.DB_BACKUP_DIR (tests, portable
        # installs) gets its snapshot written where it asked.
        from .services import backup as _backup
        backed_up = _backup.backup_database(_config.DB_PATH, _backup.DB_BACKUP_DIR)
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
