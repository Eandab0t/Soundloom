"""Tests for backend.migrations — schema versioning and migration runner."""
import pytest
import aiosqlite
from backend.migrations import run_migrations, MIGRATIONS
from backend.database import SCHEMA


@pytest.fixture
async def db_with_schema(tmp_path):
    """Create a database with the base schema but no migrations applied."""
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    yield db
    await db.close()


async def _apply_migrations_upto(db, upto_version: int):
    """Manually apply migrations 1..upto_version and record them in schema_version.

    Simulates a database created by an older version of Soundloom, so we can
    test that upgrading from a partial schema to the current version works.
    """
    await db.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL,
        applied_at TEXT DEFAULT (datetime('now'))
    )""")
    for m in MIGRATIONS:
        if m.version > upto_version:
            break
        for statement in m.sql.strip().split(";"):
            statement = statement.strip()
            if statement:
                try:
                    await db.execute(statement)
                except Exception:
                    pass  # columns/tables may already exist from base SCHEMA
        await db.execute(
            "INSERT OR IGNORE INTO schema_version (version, name) VALUES (?, ?)",
            (m.version, m.name),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_run_migrations_on_fresh_db(db_with_schema):
    db = db_with_schema
    applied = await run_migrations(db)
    assert applied == len(MIGRATIONS)

    cursor = await db.execute("SELECT COUNT(*) as c FROM schema_version")
    row = await cursor.fetchone()
    assert row[0] == len(MIGRATIONS)


@pytest.mark.asyncio
async def test_run_migrations_idempotent(db_with_schema):
    db = db_with_schema
    await run_migrations(db)
    applied = await run_migrations(db)
    assert applied == 0


@pytest.mark.asyncio
async def test_migrations_add_expected_columns(db_with_schema):
    db = db_with_schema
    await run_migrations(db)

    cursor = await db.execute("PRAGMA table_info(tracks)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "primary_artist" in cols
    assert "featured_artists" in cols
    assert "display_artist" in cols
    assert "file_status" in cols


@pytest.mark.asyncio
async def test_migration_version_tracking(db_with_schema):
    db = db_with_schema
    await run_migrations(db)

    cursor = await db.execute("SELECT version, name FROM schema_version ORDER BY version")
    rows = await cursor.fetchall()
    assert len(rows) == len(MIGRATIONS)
    for row in rows:
        assert row[0] > 0
        assert row[1] != ""


@pytest.mark.asyncio
async def test_migrations_creates_schema_version_table(db_with_schema):
    db = db_with_schema
    await run_migrations(db)

    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    row = await cursor.fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Upgrade from older (partial) schemas — simulates existing user databases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMigrationUpgrade:
    async def test_upgrade_from_migration_8(self, tmp_path):
        """Simulate a DB at migration 8 (operations journal), upgrade to latest."""
        db_path = tmp_path / "upgrade.db"
        db = await aiosqlite.connect(str(db_path))
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await _apply_migrations_upto(db, 8)

        applied = await run_migrations(db)
        assert applied == len(MIGRATIONS) - 8

        # Verify key tables exist after upgrade.
        for table in ("operations", "synced_playlists", "playlist_syncs",
                      "sync_track_mapping", "sync_runs", "library_content",
                      "file_observation", "canary"):
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            assert await cursor.fetchone() is not None, f"missing table: {table}"

        await db.close()

    async def test_upgrade_from_migration_12(self, tmp_path):
        """Simulate a DB at migration 12 (backfill progress), upgrade to latest."""
        db_path = tmp_path / "upgrade2.db"
        db = await aiosqlite.connect(str(db_path))
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await _apply_migrations_upto(db, 12)

        applied = await run_migrations(db)
        assert applied == len(MIGRATIONS) - 12

        await db.close()

    async def test_upgrade_is_idempotent(self, tmp_path):
        """Running migrations twice after an upgrade applies nothing the second time."""
        db_path = tmp_path / "upgrade3.db"
        db = await aiosqlite.connect(str(db_path))
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await _apply_migrations_upto(db, 5)

        await run_migrations(db)
        applied = await run_migrations(db)
        assert applied == 0

        await db.close()
