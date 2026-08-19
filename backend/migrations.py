"""Database migration runner with version tracking.

Each migration is a named function that runs SQL to alter the schema.
Migrations are tracked in a schema_version table so they only run once.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger("bigpickle.migrations")


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
    for m in MIGRATIONS:
        if m.version > current_version:
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
