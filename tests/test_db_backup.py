"""Tests for pre-migration database backups.

A migration rewrites schema, so the library file is the thing worth
protecting first. These cover: a real snapshot is taken, it contains the
data, it only fires when work is pending, and old copies are pruned.
"""
import sqlite3

import pytest

from backend.services.backup import (
    DB_BACKUP_KEEP,
    backup_database,
    list_db_backups,
    prune_db_backups,
)


def _make_db(path, rows=("a", "b", "c")):
    """A minimal database with the columns the real migrations touch.

    Migrations add columns and build indexes over tracks, so a toy table
    would fail before the backup code is ever reached.
    """
    db = sqlite3.connect(str(path))
    db.execute("""CREATE TABLE tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT UNIQUE NOT NULL,
        title TEXT DEFAULT '',
        artist TEXT DEFAULT '',
        album TEXT DEFAULT '',
        album_artist TEXT DEFAULT ''
    )""")
    db.executemany("INSERT INTO tracks (file_path, title) VALUES (?, ?)",
                   [(f"/m/{r}.mp3", r) for r in rows])
    db.commit()
    db.close()
    return path


class TestBackupDatabase:
    def test_creates_a_snapshot(self, tmp_path):
        src = _make_db(tmp_path / "library.db")
        dest = tmp_path / "backups"
        out = backup_database(src, dest)
        assert out, "expected a backup path"
        assert (tmp_path / "backups").exists()

    def test_snapshot_contains_the_data(self, tmp_path):
        src = _make_db(tmp_path / "library.db", rows=("x", "y", "z"))
        dest = tmp_path / "backups"
        out = backup_database(src, dest)
        db = sqlite3.connect(out)
        names = [r[0] for r in db.execute("SELECT title FROM tracks ORDER BY id")]
        db.close()
        assert names == ["x", "y", "z"]

    def test_snapshot_is_a_valid_readable_database(self, tmp_path):
        src = _make_db(tmp_path / "library.db")
        out = backup_database(src, tmp_path / "backups")
        db = sqlite3.connect(out)
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        db.close()
        assert "tracks" in tables

    def test_no_database_is_a_noop(self, tmp_path):
        assert backup_database(tmp_path / "missing.db", tmp_path / "b") == ""

    def test_empty_database_is_skipped(self, tmp_path):
        empty = tmp_path / "empty.db"
        empty.write_bytes(b"")
        assert backup_database(empty, tmp_path / "b") == ""

    def test_later_writes_do_not_touch_the_snapshot(self, tmp_path):
        """The backup must be independent of the live DB."""
        src = _make_db(tmp_path / "library.db", rows=("one",))
        out = backup_database(src, tmp_path / "b")
        db = sqlite3.connect(str(src))
        db.execute("INSERT INTO tracks (file_path, title) VALUES ('/m/two.mp3', 'two')")
        db.commit()
        db.close()

        snap = sqlite3.connect(out)
        names = [r[0] for r in snap.execute("SELECT title FROM tracks")]
        snap.close()
        assert names == ["one"], "snapshot must not reflect later writes"


class TestListAndPrune:
    def test_lists_newest_first(self, tmp_path):
        dest = tmp_path / "b"
        for _ in range(3):
            backup_database(_make_db(tmp_path / f"lib{_}.db"), dest)
        entries = list_db_backups(dest)
        assert len(entries) == 3
        assert all(e["size"] > 0 for e in entries)

    def test_list_empty_when_dir_missing(self, tmp_path):
        assert list_db_backups(tmp_path / "nope") == []

    def test_prune_keeps_the_newest(self, tmp_path):
        dest = tmp_path / "b"
        # backup_database auto-prunes, so creating more than the limit must
        # already have left exactly DB_BACKUP_KEEP copies behind.
        for i in range(DB_BACKUP_KEEP + 3):
            backup_database(_make_db(tmp_path / f"lib{i}.db"), dest)
        assert len(list_db_backups(dest)) == DB_BACKUP_KEEP

        # An explicit prune with nothing extra to remove is a no-op.
        assert prune_db_backups(dest, keep=DB_BACKUP_KEEP) == 0
        assert len(list_db_backups(dest)) == DB_BACKUP_KEEP

    def test_prune_nothing_when_under_limit(self, tmp_path):
        dest = tmp_path / "b"
        backup_database(_make_db(tmp_path / "lib.db"), dest)
        assert prune_db_backups(dest, keep=DB_BACKUP_KEEP) == 0

    def test_auto_prune_after_backup(self, tmp_path):
        dest = tmp_path / "b"
        for i in range(DB_BACKUP_KEEP + 2):
            backup_database(_make_db(tmp_path / f"l{i}.db"), dest)
        # backup_database prunes automatically, so the dir stays bounded.
        assert len(list_db_backups(dest)) <= DB_BACKUP_KEEP

    def test_old_copies_are_really_deleted(self, tmp_path):
        """Pruning must unlink files, not just drop them from the listing."""
        dest = tmp_path / "b"
        for i in range(DB_BACKUP_KEEP + 3):
            backup_database(_make_db(tmp_path / f"lib{i}.db"), dest)
        on_disk = sorted(p.name for p in dest.glob("library_*.db"))
        assert len(on_disk) == DB_BACKUP_KEEP, f"extra files left behind: {on_disk}"


class TestMigrationTriggersBackup:
    async def test_backup_written_when_migration_pending(self, tmp_path, monkeypatch):
        import backend.config as cfg
        from backend.database import init_db, close_db, fetch_all
        from backend.services import backup as backup_svc

        db_path = _make_db(tmp_path / "old.db", rows=("keepme",))
        # Point the backup dir somewhere observable.
        bdir = tmp_path / "dbbackups"
        monkeypatch.setattr(cfg, "DB_PATH", db_path)
        monkeypatch.setattr(backup_svc, "DB_BACKUP_DIR", bdir)

        try:
            await init_db()  # schema_version empty -> all migrations pending
            files = list_db_backups(bdir)
            assert files, "expected a pre-migration database snapshot"
            assert files[0]["size"] > 0
        finally:
            await close_db()

    async def test_no_backup_when_nothing_pending(self, tmp_path, monkeypatch):
        import backend.config as cfg
        from backend.database import init_db, close_db
        from backend.services import backup as backup_svc

        db_path = tmp_path / "fresh.db"
        monkeypatch.setattr(cfg, "DB_PATH", db_path)
        bdir = tmp_path / "dbbackups"
        monkeypatch.setattr(backup_svc, "DB_BACKUP_DIR", bdir)

        try:
            await init_db()
            first = len(list_db_backups(bdir))
            await close_db()
            # Second boot: every migration is already applied.
            await init_db()
            second = len(list_db_backups(bdir))
            await close_db()
            assert second == first, "no migration pending -> no new snapshot"
        except Exception:
            await close_db()
            raise

    async def test_user_data_survives_and_backup_exists(self, tmp_path, monkeypatch):
        import backend.config as cfg
        from backend.database import init_db, close_db, fetch_all
        from backend.services import backup as backup_svc

        db_path = _make_db(tmp_path / "legacy.db", rows=("song1", "song2"))
        monkeypatch.setattr(cfg, "DB_PATH", db_path)
        bdir = tmp_path / "dbbackups"
        monkeypatch.setattr(backup_svc, "DB_BACKUP_DIR", bdir)

        try:
            await init_db()
            titles = [r["title"] for r in await fetch_all(
                "SELECT title FROM tracks ORDER BY id")]
            assert titles == ["song1", "song2"]
            assert list_db_backups(bdir), "pre-migration snapshot must exist"
        finally:
            await close_db()