"""Startup filesystem verification - the cheap existence-only reconciliation.

The library rows and the disk drift apart for boring reasons: a folder gets
moved, a drive is not reconnected, a file is deleted in Explorer while
Soundloom is shut. Until a full rescan happens, the library claims files exist
when they do not, and every count on screen is wrong.

These tests pin the two things that make it safe to run unattended:

- it marks what is genuinely gone, and puts back what came back;
- when the library root is *unavailable* it marks **nothing at all**, because
  an unmounted drive and a deleted library look identical to a path check, and
  guessing wrong here empties the whole library on a technicality.
"""
import pytest

from backend.database import close_db, execute, fetch_all, init_db
from backend.services import scanner


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Throwaway DB + settings, with a real library folder on disk."""
    import backend.config as cfg
    library = tmp_path / "Music"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "verify.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
    return library


async def _mk_track(file_path, status="present"):
    cur = await execute(
        "INSERT INTO tracks (file_path, title, artist, file_status)"
        " VALUES (?, ?, ?, ?)",
        (str(file_path), "A Track", "An Artist", status),
    )
    return cur.lastrowid


async def _status(track_id):
    rows = await fetch_all("SELECT file_status FROM tracks WHERE id=?", (track_id,))
    return rows[0]["file_status"]


# ---------------------------------------------------------------------------
# Marking what is gone
# ---------------------------------------------------------------------------

class TestMarkingMissing:
    async def test_a_deleted_file_is_marked_missing(self, env):
        await init_db()
        try:
            # Never created on disk in the first place - the row points at a
            # path that is simply not there.
            gone = env / "gone.mp3"
            tid = await _mk_track(gone)

            result = await scanner.verify_filesystem()
            assert result["status"] == "complete"
            assert result["marked_missing"] == 1
            assert await _status(tid) == "missing"
        finally:
            await close_db()

    async def test_files_that_are_still_there_are_left_alone(self, env):
        await init_db()
        try:
            here = env / "here.mp3"
            here.write_bytes(b"\x00" * 16)
            tid = await _mk_track(here)

            result = await scanner.verify_filesystem()
            assert result["marked_missing"] == 0
            assert result["checked"] == 1
            assert await _status(tid) == "present"
        finally:
            await close_db()

    async def test_only_the_actually_gone_ones_are_marked(self, env):
        await init_db()
        try:
            a = env / "a.mp3"; a.write_bytes(b"x")
            b = env / "b.mp3"
            tid_a = await _mk_track(a)
            tid_b = await _mk_track(b)

            result = await scanner.verify_filesystem()
            assert result["marked_missing"] == 1
            assert result["checked"] == 2
            assert await _status(tid_a) == "present"
            assert await _status(tid_b) == "missing"
        finally:
            await close_db()

    async def test_a_directory_where_a_file_should_be_counts_as_missing(self, env):
        """`exists()` would be true for a directory. Only a real file counts."""
        await init_db()
        try:
            as_dir = env / "track.mp3"
            as_dir.mkdir()
            tid = await _mk_track(as_dir)

            await scanner.verify_filesystem()
            assert await _status(tid) == "missing"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Restoring what came back
# ---------------------------------------------------------------------------

class TestRestoringPresent:
    async def test_a_file_that_came_back_is_restored_to_present(self, env):
        """Marking missing must not be a one-way ratchet.

        Without this, a library on a removable drive goes permanently blank
        after one morning when the disk was not connected, with no way back
        except a full rescan.
        """
        await init_db()
        try:
            f = env / "returns.mp3"
            tid = await _mk_track(f, status="missing")
            f.write_bytes(b"x")

            result = await scanner.verify_filesystem()
            assert result["restored_present"] == 1
            assert result["marked_missing"] == 0
            assert await _status(tid) == "present"
        finally:
            await close_db()

    async def test_a_row_already_correct_is_not_rewritten(self, env):
        await init_db()
        try:
            f = env / "fine.mp3"
            f.write_bytes(b"x")
            tid = await _mk_track(f, status="present")

            result = await scanner.verify_filesystem()
            assert result["marked_missing"] == 0
            assert result["restored_present"] == 0
            assert await _status(tid) == "present"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The guard: an unavailable library root must change nothing
# ---------------------------------------------------------------------------

class TestUnavailableLibraryRoot:
    async def test_a_missing_root_marks_nothing(self, env):
        """The failure this exists to prevent: a drive that is not mounted must
        not look like a library that was deleted."""
        import shutil
        await init_db()
        try:
            tids = []
            for i in range(5):
                tids.append(await _mk_track(env / f"t{i}.mp3"))
            shutil.rmtree(env)

            result = await scanner.verify_filesystem()
            assert result["status"] == "skipped"
            assert "unavailable" in result["reason"]
            assert result["marked_missing"] == 0
            for tid in tids:
                assert await _status(tid) == "present", "a row was marked missing"
        finally:
            await close_db()

    async def test_a_root_that_is_a_file_not_a_folder_marks_nothing(self, env):
        await init_db()
        try:
            import backend.config as cfg
            not_a_dir = env.parent / "not-a-dir"
            not_a_dir.write_bytes(b"x")
            cfg._cache = {**cfg.DEFAULTS, "library_path": str(not_a_dir)}
            tid = await _mk_track(env / "t.mp3")

            result = await scanner.verify_filesystem()
            assert result["status"] == "skipped"
            assert result["marked_missing"] == 0
            assert await _status(tid) == "present"
        finally:
            await close_db()

    async def test_no_library_path_at_all_marks_nothing(self, env):
        await init_db()
        try:
            import backend.config as cfg
            cfg._cache = {**cfg.DEFAULTS, "library_path": ""}
            tid = await _mk_track(env / "t.mp3")

            result = await scanner.verify_filesystem()
            assert result["status"] == "skipped"
            assert result["marked_missing"] == 0
            assert await _status(tid) == "present"
        finally:
            await close_db()

    async def test_the_guard_wins_even_when_files_are_genuinely_gone(self, env):
        """Even with real deletions behind it, an unmounted volume is not
        evidence of anything - the whole root is in question, not one file."""
        import shutil
        await init_db()
        try:
            tid = await _mk_track(env / "deleted.mp3")
            shutil.rmtree(env)

            result = await scanner.verify_filesystem()
            assert result["status"] == "skipped"
            assert await _status(tid) == "present"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    async def test_an_empty_library_reports_nothing_to_do(self, env):
        await init_db()
        try:
            result = await scanner.verify_filesystem()
            assert result["checked"] == 0
            assert result["marked_missing"] == 0
        finally:
            await close_db()

    async def test_rows_with_no_path_are_skipped_not_crashed_on(self, env):
        await init_db()
        try:
            cur = await execute(
                "INSERT INTO tracks (file_path, title, artist, file_status)"
                " VALUES ('', 'No Path', 'Someone', 'present')"
            )
            result = await scanner.verify_filesystem()
            assert result["status"] == "complete"
            rows = await fetch_all("SELECT file_status FROM tracks WHERE id=?",
                                   (cur.lastrowid,))
            assert rows[0]["file_status"] == "present"
        finally:
            await close_db()

    async def test_more_missing_rows_than_sqlite_can_bind_still_work(self, env):
        """The chunking is not decoration - one giant IN (...) would raise."""
        await init_db()
        try:
            ids = []
            for i in range(1100):
                ids.append(await _mk_track(env / f"gone{i}.mp3"))
            result = await scanner.verify_filesystem()
            assert result["marked_missing"] == 1100
            rows = await fetch_all(
                "SELECT file_status FROM tracks WHERE file_status='missing'"
            )
            assert len(rows) == 1100
        finally:
            await close_db()

    async def test_a_second_run_is_idempotent(self, env):
        await init_db()
        try:
            await _mk_track(env / "gone.mp3")

            first = await scanner.verify_filesystem()
            second = await scanner.verify_filesystem()
            assert first["marked_missing"] == 1
            assert second["marked_missing"] == 0, "marked the same row twice"
        finally:
            await close_db()
