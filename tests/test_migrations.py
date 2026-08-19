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
