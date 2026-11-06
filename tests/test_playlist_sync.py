"""Tests for backend.services.playlist_sync — the persistent sync engine.

These tests drive the real DB-backed surface (``add`` / ``mirror`` /
``download_only`` / ``preview``) with a throwaway SQLite file, so nothing
persistent is touched.  The matcher itself (``refresh_local_matches``) is
tested in ``tests/test_playlists_service.py``.
"""

import sqlite3
from pathlib import Path

import pytest

from backend import config
from backend import database as db
from backend.services import playlist_sync as sync


# ---------------------------------------------------------------------------
# Shared test DB: a fresh SQLite file, pointed at by the process-wide DB
# ---------------------------------------------------------------------------

@pytest.fixture()
async def sync_db(tmp_path, monkeypatch):
    """Point the process-wide DB at a fresh file and run migrations once.

    The database is closed and the path restored between tests so each test
    starts from a clean schema.
    """
    original = config.DB_PATH
    path = tmp_path / "sync_test.db"
    monkeypatch.setattr(config, "DB_PATH", path)

    # Initialise the DB now (this runs migrations + creates the tables).
    import asyncio
    await db.init_db()

    yield

    # Close the connection so the next test starts clean.
    if db._db is not None:
        await db._db.close()
        db._db = None

    monkeypatch.setattr(config, "DB_PATH", original)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

class TestAdd:
    async def test_add_creates_row(self, sync_db):
        result = await sync.add(999, "https://open.spotify.com/playlist/37i9dQZF1DXcBWIG", "spotify")
        assert result["id"] > 0
        assert result["name"] == "Soundloom Sync"
        assert result["url"] == "https://open.spotify.com/playlist/37i9dQZF1DXcBWIG"

        # Row is persisted in playlist_syncs.
        row = await db.fetch_one("SELECT * FROM playlist_syncs WHERE id=?", (result["id"],))
        assert row is not None
        assert row["source_type"] == "spotify"
        assert row["url"] == "https://open.spotify.com/playlist/37i9dQZF1DXcBWIG"

    async def test_add_rejects_wrong_source_type(self, sync_db):
        # spotify is a valid source_type; this call succeeds.
        result = await sync.add(1, "https://example.com/playlist", "spotify")
        assert result["id"] > 0
        with pytest.raises(ValueError):
            await sync.add(1, "https://example.com/playlist", "")


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------

class TestPreview:
    async def test_preview_empty_playlist(self, sync_db):
        out = await sync.preview(5)
        assert out["rows"] == 0
        assert out["matched"] == 0
        assert out["enqueued"] == 0

    async def test_preview_reports_matched_and_unmatched(self, sync_db):
        pl = await sync.add(1, "https://example.com/playlist", "spotify")
        pid = pl["id"]

        # Seed one matching row (track_id set) and one unmatched row.
        # The playlist_tracks rows reference playlists.id, so create a playlist
        # row and use its id (different from the sync intent's id).
        await db.execute(
            "INSERT INTO playlists (name, description, origin, origin_ref, created_at, updated_at) "
            "VALUES (?, '', 'manual', '', datetime('now'), datetime('now'))",
            ("Test Playlist",),
        )
        pl_row = await db.fetch_one("SELECT id FROM playlists ORDER BY id DESC LIMIT 1")
        assert pl_row is not None
        playlist_id = pl_row["id"]

        await db.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist, track_id) "
            "VALUES (?, 1, 'Already Known', 'Artist', 42)",
            (playlist_id,),
        )
        await db.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist) "
            "VALUES (?, 2, 'Unknown Song', 'Unknown Artist')",
            (playlist_id,),
        )

        out = await sync.preview(playlist_id)
        assert out["rows"] == 2
        assert out["matched"] == 1
        assert out["enqueued"] == 1
        assert out["unmatched_titles"] == ["Unknown Artist - Unknown Song"]


# ---------------------------------------------------------------------------
# mirror
# ---------------------------------------------------------------------------

class TestMirror:
    async def test_mirror_enqueues_unmatched_rows(self, sync_db):
        # A sync intent (playlist_syncs) is separate from a playlist (playlists).
        # The playlist_tracks rows reference playlists.id, so create both.
        sp = await sync.add(1, "https://example.com/playlist", "spotify")
        await db.execute(
            "INSERT INTO playlists (name, description, origin, origin_ref, created_at, updated_at) "
            "VALUES (?, '', 'manual', '', datetime('now'), datetime('now'))",
            ("Test Playlist",),
        )
        pl = await db.fetch_one("SELECT id FROM playlists ORDER BY id DESC LIMIT 1")
        assert pl is not None
        pid = pl["id"]
        await db.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist) "
            "VALUES (?, 1, 'Rock Song', 'Rock Band')",
            (pid,),
        )
        out = await sync.mirror(pid)
        assert out["rows"] == 1
        assert out["matched"] == 0
        assert out["enqueued"] >= 1

        jobs = await db.fetch_all("SELECT * FROM jobs WHERE source_url LIKE '%rock%' LIMIT 1")
        assert len(jobs) == 1

    async def test_mirror_skips_already_matched(self, sync_db):
        # A sync intent (playlist_syncs) is separate from a playlist (playlists).
        # The playlist_tracks rows reference playlists.id, so create both.
        await sync.add(1, "https://example.com/playlist", "spotify")
        await db.execute(
            "INSERT INTO playlists (name, description, origin, origin_ref, created_at, updated_at) "
            "VALUES (?, '', 'manual', '', datetime('now'), datetime('now'))",
            ("Test Playlist",),
        )
        pl = await db.fetch_one("SELECT id FROM playlists ORDER BY id DESC LIMIT 1")
        assert pl is not None
        pid = pl["id"]
        await db.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist, track_id) "
            "VALUES (?, 1, 'Known', 'Artist', 42)",
            (pid,),
        )
        out = await sync.mirror(pid)
        assert out["matched"] == 1
        assert out["enqueued"] == 0
        assert await db.fetch_all("SELECT * FROM jobs") == []

    async def test_mirror_works_with_non_empty_name(self, sync_db):
        # This exercises the sync_tables path (the DB has a playlist_syncs row).
        await sync.add(1, "https://example.com/playlist", "spotify")
        await db.execute(
            "INSERT INTO playlists (name, description, origin, origin_ref, created_at, updated_at) "
            "VALUES (?, '', 'manual', '', datetime('now'), datetime('now'))",
            ("Test Playlist",),
        )
        pl = await db.fetch_one("SELECT id FROM playlists ORDER BY id DESC LIMIT 1")
        assert pl is not None
        pid = pl["id"]
        await db.execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist) "
            "VALUES (?, 1, 'X', 'Y')",
            (pid,),
        )
        out = await sync.mirror(pid)
        assert out["rows"] == 1
        assert out["enqueued"] == 1
