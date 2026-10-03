"""Tests for the trustworthiness fixes: non-destructive migrations, the
loud tag-write contract, and a bounded event bus.
"""
import asyncio
import sqlite3

import pytest

from backend.errors import PathTraversalError, TagError
from backend.services.tagger import write_tags
from backend.validation import validate_path


class TestPathAncestryNotStringPrefix:
    def test_sibling_directory_with_shared_prefix_rejected(self, tmp_path):
        """/music/library-backup must not pass a /music/library check."""
        base = tmp_path / "library"
        base.mkdir()
        sibling = tmp_path / "library-backup"
        sibling.mkdir()
        target = sibling / "stolen.mp3"
        target.write_bytes(b"x")
        with pytest.raises(PathTraversalError):
            validate_path(str(target), base)

    def test_prefix_sibling_file_rejected(self, tmp_path):
        base = tmp_path / "lib"
        base.mkdir()
        target = tmp_path / "lib.txt"
        target.write_bytes(b"x")
        with pytest.raises(PathTraversalError):
            validate_path(str(target), base)

    def test_nested_child_still_allowed(self, tmp_path):
        base = tmp_path / "library"
        base.mkdir()
        ok = validate_path("Artist/Album/01 - Song.mp3", base)
        assert ok == base / "Artist" / "Album" / "01 - Song.mp3"

    def test_base_itself_allowed(self, tmp_path):
        base = tmp_path / "library"
        base.mkdir()
        assert validate_path(".", base) == base.resolve()

    def test_traversal_out_of_base_rejected(self, tmp_path):
        base = tmp_path / "library"
        base.mkdir()
        with pytest.raises(PathTraversalError):
            validate_path("../outside.mp3", base)


class TestMigrationDoesNotDestroyPlaylists:
    def _legacy_db(self, path):
        """A pre-hub database: playlists exists in its old 2-column shape.

        Only the table migration 7 touches is created here; every other table
        is created by the base SCHEMA on boot (CREATE TABLE IF NOT EXISTS),
        which is exactly how a real upgrade behaves.

        This mirrors the pre-hub shape recorded in git history: the old
        table had (id, name, created_at) and was never written to.
        """
        db = sqlite3.connect(str(path))
        db.executescript("""
            CREATE TABLE playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO playlists (name) VALUES ('My Road Trip');
            INSERT INTO playlists (name) VALUES ('Chill Vibes');
        """)
        db.commit()
        db.close()

    async def test_existing_playlists_survive_migration(self, tmp_path, monkeypatch):
        import backend.config as cfg
        from backend.database import init_db, close_db, execute, fetch_all

        db_path = tmp_path / "legacy.db"
        self._legacy_db(db_path)
        monkeypatch.setattr(cfg, "DB_PATH", db_path)

        try:
            await init_db()
            rows = await fetch_all("SELECT name FROM playlists ORDER BY name")
            assert [r["name"] for r in rows] == ["Chill Vibes", "My Road Trip"]
        finally:
            await close_db()

    async def test_migrated_playlists_get_new_columns(self, tmp_path, monkeypatch):
        import backend.config as cfg
        from backend.database import init_db, close_db, fetch_all

        db_path = tmp_path / "legacy2.db"
        self._legacy_db(db_path)
        monkeypatch.setattr(cfg, "DB_PATH", db_path)

        try:
            await init_db()
            row = await fetch_all("SELECT * FROM playlists LIMIT 1")
            for col in ("description", "origin", "origin_ref", "updated_at"):
                assert col in row[0], f"missing migrated column: {col}"
        finally:
            await close_db()

    async def test_playlist_tracks_table_created(self, tmp_path, monkeypatch):
        import backend.config as cfg
        from backend.database import init_db, close_db, execute, fetch_all

        db_path = tmp_path / "legacy3.db"
        self._legacy_db(db_path)
        monkeypatch.setattr(cfg, "DB_PATH", db_path)

        try:
            await init_db()
            await execute(
                "INSERT INTO playlist_tracks (playlist_id, position, title) VALUES (1, 1, 'T')"
            )
            rows = await fetch_all("SELECT title FROM playlist_tracks")
            assert rows[0]["title"] == "T"
        finally:
            await close_db()

    async def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Re-running init must not duplicate or destroy anything."""
        import backend.config as cfg
        from backend.database import init_db, close_db, fetch_all

        db_path = tmp_path / "legacy4.db"
        self._legacy_db(db_path)
        monkeypatch.setattr(cfg, "DB_PATH", db_path)

        try:
            await init_db()
            await close_db()
            await init_db()
            rows = await fetch_all("SELECT name FROM playlists")
            assert len(rows) == 2
        finally:
            await close_db()


class TestTagWriteFailureIsLoud:
    async def test_unsupported_extension_raises(self, tmp_path):
        bogus = tmp_path / "notes.txt"
        bogus.write_bytes(b"not audio")
        with pytest.raises(TagError):
            await write_tags(str(bogus), {"title": "X"})

    async def test_missing_file_raises(self, tmp_path):
        with pytest.raises(TagError):
            await write_tags(str(tmp_path / "ghost.mp3"), {"title": "X"})

    async def test_corrupt_audio_file_raises(self, tmp_path):
        corrupt = tmp_path / "broken.mp3"
        corrupt.write_bytes(b"\x00" * 512)  # not a real MP3 frame
        with pytest.raises(TagError):
            await write_tags(str(corrupt), {"title": "X", "artist": "Y"})

    async def test_error_carries_code(self, tmp_path):
        bogus = tmp_path / "x.txt"
        bogus.write_bytes(b"x")
        with pytest.raises(TagError) as exc:
            await write_tags(str(bogus), {"title": "X"})
        assert exc.value.code == "tag_unsupported_format"

    async def test_pipeline_editor_reports_failure(self, tmp_path):
        """apply_metadata_to_file must return False, not raise or lie."""
        from backend.pipeline.editor import apply_metadata_to_file
        from backend.pipeline.models import TrackMetadata

        bad = tmp_path / "song.txt"
        bad.write_bytes(b"nope")
        ok = await apply_metadata_to_file(
            str(bad), TrackMetadata(title="T", artist="A")
        )
        assert ok is False

    async def test_pipeline_editor_surfaces_write_error(self, tmp_path, monkeypatch):
        """A TagError from the write must not be reported as success."""
        import backend.services.tagger as tagger
        from backend.pipeline.editor import apply_metadata_to_file
        from backend.pipeline.models import TrackMetadata

        audio = tmp_path / "song.mp3"
        audio.write_bytes(b"\x00" * 512)

        async def boom(path, tags, cover_path=None):
            raise TagError("simulated write failure")

        # editor.py imports write_tags *inside* the function, so patch the
        # source module rather than editor's namespace.
        monkeypatch.setattr(tagger, "write_tags", boom)
        ok = await apply_metadata_to_file(
            str(audio), TrackMetadata(title="T", artist="A")
        )
        assert ok is False

    async def test_cleanup_result_reports_write_failure(self, tmp_path, monkeypatch):
        """A failed tag write must surface as success=False, not True."""
        from backend.pipeline import cleanup as cleanup_mod

        audio = tmp_path / "song.mp3"
        audio.write_bytes(b"\x00" * 512)

        async def fake_read(path):
            return {"title": "T", "artist": "A", "album": "Al"}

        async def boom(path, tags, cover_path=None):
            raise TagError("simulated write failure")

        monkeypatch.setattr(cleanup_mod, "read_tags", fake_read)
        monkeypatch.setattr(cleanup_mod, "write_tags", boom)

        # run_cleanup needs at least one rule to fire for a write to happen
        result = await cleanup_mod.auto_fix_file(str(audio), case_mode="title")
        assert result.success is False


class TestEventBusIsBounded:
    async def test_queue_has_a_max_size(self):
        from backend import events

        q = await events.subscribe()
        try:
            assert q.maxsize == events.MAX_QUEUE_SIZE
        finally:
            events.unsubscribe(q)

    async def test_slow_subscriber_is_dropped_not_unbounded(self):
        """Overflow drops the subscriber instead of growing forever."""
        from backend import events

        q = await events.subscribe()
        try:
            for i in range(events.MAX_QUEUE_SIZE + 50):
                await events.emit("noise", {"i": i})
            assert q not in events._subscribers, "overflowing subscriber must be dropped"
            assert q.qsize() <= events.MAX_QUEUE_SIZE
        finally:
            events._subscribers.clear()

    async def test_healthy_subscriber_keeps_receiving(self):
        from backend import events

        q = await events.subscribe()
        try:
            for i in range(10):
                await events.emit("tick", {"i": i})
                await q.get()  # drain like a real consumer
            assert q in events._subscribers
        finally:
            events.unsubscribe(q)

    async def test_one_slow_client_does_not_starve_others(self):
        """A healthy client keeps receiving even while a slow one backs up."""
        from backend import events

        slow = await events.subscribe()
        healthy = await events.subscribe()
        try:
            for i in range(events.MAX_QUEUE_SIZE + 10):
                await events.emit("tick", {"i": i})
                await healthy.get()  # healthy client drains as it goes
            assert slow not in events._subscribers, "slow client should be dropped"
            assert healthy in events._subscribers, "healthy client must survive"
            assert healthy.empty()
        finally:
            events._subscribers.clear()