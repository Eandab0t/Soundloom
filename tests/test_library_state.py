"""Library state model: active/archived, acquisition provenance, and the
four-state reconciliation.

A track has two independent axes and conflating them is how a library ends
up lying:

    file_status    present | missing   -- is the bytes still there?
    library_status active | archived   -- is the user still collecting this?

Archiving is a database write and nothing else. No file moves, no row is
deleted, so unarchiving is always exact - including for a file that is
still missing, which is a perfectly legitimate state for an archived
record to be in.

`acquisition` is the durable half of the same idea. A log line rotates
away; a table does not. With a recorded SHA-256, "this file went missing"
upgrades from an inference to a measurement, and a file found elsewhere
can be *proven* to be the same one rather than merely similar.

The tests here cover, in order of how much damage a regression would do:

- migration adds the columns and backfills every existing row to 'active';
- archive/unarchive round-trips exactly and never touches the filesystem;
- re-indexing keeps the track's id and its archive state (the upsert fix);
- every download records a real SHA-256 and its job id;
- moved files are detected by content, and only by content.
"""
import hashlib

import pytest

from backend.database import close_db, execute, fetch_all, fetch_one, init_db
from backend.services import acquisition, scanner


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Throwaway DB + settings, with a real library folder on disk."""
    import backend.config as cfg
    library = tmp_path / "Music"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
    return library


def _write(path, data: bytes) -> str:
    """Create a real file with real content, and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _mk_track(file_path, status="present", **extra):
    cols = ["file_path", "title", "artist", "file_status"]
    vals = [str(file_path), "A Track", "An Artist", status]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    cur = await execute(
        f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(vals),
    )
    return cur.lastrowid


async def _row(track_id):
    return await fetch_one("SELECT * FROM tracks WHERE id=?", (track_id,))


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

class TestLibraryStateMigration:
    async def test_adds_columns_and_backfills_every_row_to_active(self, env):
        """An upgraded database gets the new columns, and every existing row
        is 'active'. Getting the backfill wrong would either hide the whole
        library or mark everything archived."""
        import sqlite3

        from backend.migrations import MIGRATIONS
        assert MIGRATIONS[-1].name == "library_state_and_acquisition"

        # Build a pre-migration database the way an upgrade would find it.
        legacy = sqlite3.connect(str(env.parent / "legacy.db"))
        legacy.execute("""
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                title TEXT DEFAULT '',
                artist TEXT DEFAULT '',
                album TEXT DEFAULT '',
                album_artist TEXT DEFAULT ''
            )""")
        legacy.executemany(
            "INSERT INTO tracks (file_path, title) VALUES (?, ?)",
            [("/m/a.mp3", "a"), ("/m/b.mp3", "b")],
        )
        legacy.commit()
        legacy.close()

        import backend.config as cfg
        cfg.DB_PATH = env.parent / "legacy.db"

        await init_db()
        try:
            cols = {c["name"] for c in await fetch_all("PRAGMA table_info(tracks)")}
            assert {"library_status", "archived_at", "archived_reason"} <= cols

            rows = await fetch_all("SELECT library_status FROM tracks")
            assert len(rows) == 2, "migration must not lose rows"
            assert all(r["library_status"] == "active" for r in rows)

            blank = await fetch_one(
                "SELECT COUNT(*) as c FROM tracks "
                "WHERE library_status IS NULL OR library_status=''")
            assert blank["c"] == 0
        finally:
            await close_db()

    async def test_acquisition_table_exists_and_is_queryable(self, env):
        await init_db()
        try:
            rows = await fetch_all("SELECT * FROM acquisition")
            assert rows == []
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

class TestArchive:
    async def test_archive_hides_the_row_but_keeps_it(self, env):
        await init_db()
        try:
            tid = await _mk_track(env / "a.mp3")
            await execute(
                "UPDATE tracks SET library_status='archived', archived_at='now', "
                "archived_reason='declined' WHERE id=?", (tid,))
            row = await _row(tid)
            assert row["library_status"] == "archived"
            assert row["archived_reason"] == "declined"
            assert row["title"] == "A Track", "metadata survives archiving"

            hidden = await fetch_all(
                "SELECT id FROM tracks WHERE library_status='active'")
            assert tid not in [r["id"] for r in hidden]
        finally:
            await close_db()

    async def test_archive_does_not_touch_the_file(self, env):
        """Archiving is a database state, not a filesystem action. If it
        deleted or moved the file, it would not be reversible."""
        await init_db()
        try:
            path = _write(env / "keep.mp3", b"audio bytes")
            tid = await _mk_track(path)
            await execute(
                "UPDATE tracks SET library_status='archived' WHERE id=?", (tid,))
            assert (env / "keep.mp3").exists()
            assert (env / "keep.mp3").read_bytes() == b"audio bytes"
        finally:
            await close_db()

    async def test_unarchive_restores_a_missing_file_without_inventing_it(self, env):
        """The interesting case: an archived record whose file is still gone.
        Restoring must not claim the file is back."""
        await init_db()
        try:
            tid = await _mk_track(env / "gone.mp3", status="missing",
                                  library_status="archived")
            await execute(
                "UPDATE tracks SET library_status='active', archived_at=NULL, "
                "archived_reason='' WHERE id=?", (tid,))
            row = await _row(tid)
            assert row["library_status"] == "active"
            assert row["archived_at"] is None
            assert row["file_status"] == "missing", \
                "the file really is gone; restoring must not lie about it"
        finally:
            await close_db()

    async def test_round_trip_is_exact(self, env):
        await init_db()
        try:
            path = _write(env / "rt.mp3", b"x")
            tid = await _mk_track(path, title="Round Trip")
            before = dict(await _row(tid))

            await execute(
                "UPDATE tracks SET library_status='archived', archived_at='t', "
                "archived_reason='why' WHERE id=?", (tid,))
            await execute(
                "UPDATE tracks SET library_status='active', archived_at=NULL, "
                "archived_reason='' WHERE id=?", (tid,))

            after = dict(await _row(tid))
            assert after == before, "archive/unarchive must change nothing else"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Indexing and provenance
# ---------------------------------------------------------------------------

class TestIndexingProvenance:
    async def test_index_file_records_a_real_sha256_and_job_id(self, env):
        await init_db()
        try:
            from backend.services import operations

            payload = b"the actual bytes of a track"
            path = _write(env / "Artist" / "Album" / "01 - Song.mp3", payload)

            tid = await operations.index_file(
                path, {"title": "Song", "artist": "Artist", "album": "Album"},
                source_url="ytsearch1:Artist Song", job_id=42,
            )
            assert tid

            rows = await fetch_all("SELECT * FROM acquisition WHERE track_id=?", (tid,))
            assert len(rows) == 1, "exactly one provenance row per acquisition"
            row = rows[0]
            assert row["sha256"] == _sha(payload), "hash of the real file contents"
            assert row["job_id"] == 42
            assert row["file_path"] == path
            assert row["file_size"] == len(payload)
            assert row["source_url"] == "ytsearch1:Artist Song"
            assert row["source"] == "ytsearch1"
        finally:
            await close_db()

    async def test_reindex_keeps_the_row_id_and_its_archive_state(self, env):
        """A re-index means the file came back, not that the user's mind
        changed. INSERT OR REPLACE would mint a new id (orphaning every
        acquisition row) and silently un-archive the track."""
        await init_db()
        try:
            from backend.services import operations

            path = _write(env / "again.mp3", b"one")
            first = await operations.index_file(path, {"title": "T"}, "src:a")
            await execute(
                "UPDATE tracks SET library_status='archived' WHERE id=?", (first,))

            second = await operations.index_file(path, {"title": "T"}, "src:a")
            assert second == first, "upsert must not change the primary key"

            row = await _row(first)
            assert row["library_status"] == "archived", \
                "re-indexing must not silently un-archive"

            acq = await fetch_all("SELECT track_id FROM acquisition")
            assert acq and all(a["track_id"] == first for a in acq), \
                "provenance must stay attached to the surviving row"
        finally:
            await close_db()

    async def test_unhashable_file_still_records_provenance(self, env):
        """Losing the hash is bad; losing the whole record would be worse."""
        await init_db()
        try:
            tid = await acquisition.record(
                track_id=999, file_path=str(env / "does-not-exist.mp3"),
                job_id=7, source="ytsearch1", sha256="", file_size=0,
            )
            assert tid
            rows = await fetch_all("SELECT * FROM acquisition WHERE track_id=999")
            assert len(rows) == 1
            assert rows[0]["job_id"] == 7
            assert rows[0]["sha256"] == ""
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------

class TestClassify:
    def test_file_in_place_is_present(self):
        state = acquisition.classify(
            on_disk=True, expected_path="/m/a.mp3", sha256="abc", has_acquisition=True)
        assert state["state"] == acquisition.STATE_PRESENT

    def test_missing_with_provenance_is_missing(self):
        state = acquisition.classify(
            on_disk=False, expected_path="/m/a.mp3", sha256="abc",
            has_acquisition=True, last_known_size=1024)
        assert state["state"] == acquisition.STATE_MISSING
        assert state["last_known_size"] == 1024

    def test_job_but_no_acquisition_is_never_acquired(self):
        state = acquisition.classify(
            on_disk=False, expected_path="/m/a.mp3",
            has_acquisition=False, has_job=True)
        assert state["state"] == acquisition.STATE_NEVER_ACQUIRED

    def test_a_found_file_outranks_missing(self):
        """Moved and missing look identical on disk. Reporting a moved file
        as missing is the specific wrong answer this model exists to avoid,
        because the fix is opposite: repoint the row, do not re-download."""
        state = acquisition.classify(
            on_disk=False, expected_path="/m/Artist/a.mp3",
            discovered_path="/m/a.mp3", sha256="abc", has_acquisition=True)
        assert state["state"] == acquisition.STATE_MOVED
        assert state["expected_path"] == "/m/Artist/a.mp3"
        assert state["discovered_path"] == "/m/a.mp3"

    def test_a_discovery_without_a_hash_is_not_a_move(self):
        state = acquisition.classify(
            on_disk=False, discovered_path="/m/a.mp3", sha256="",
            has_acquisition=True)
        assert state["state"] == acquisition.STATE_MISSING


class TestMatchMoved:
    def test_matches_only_on_identical_digest(self):
        index = {"deadbeef": ["/m/wherever/a.mp3"]}
        found = acquisition.match_moved(
            [{"track_id": 1, "expected_path": "/m/Artist/a.mp3",
              "sha256": "deadbeef"}], index)
        assert len(found) == 1
        assert found[0]["discovered_path"] == "/m/wherever/a.mp3"
        assert found[0]["expected_path"] == "/m/Artist/a.mp3"

    def test_no_match_without_a_recorded_hash(self):
        index = {"deadbeef": ["/m/a.mp3"]}
        found = acquisition.match_moved(
            [{"track_id": 1, "expected_path": "/m/Artist/a.mp3",
              "sha256": ""}], index)
        assert found == [], "a track with no hash can never be proven moved"

    def test_the_same_path_is_missing_not_moved(self):
        index = {"deadbeef": ["/m/Artist/a.mp3"]}
        found = acquisition.match_moved(
            [{"track_id": 1, "expected_path": "/m/Artist/a.mp3",
              "sha256": "deadbeef"}], index)
        assert found == [], "a file at its own path is missing, not relocated"

    def test_a_different_digest_does_not_match(self):
        index = {"cafebabe": ["/m/someone-elses-song.mp3"]}
        found = acquisition.match_moved(
            [{"track_id": 1, "expected_path": "/m/a.mp3",
              "sha256": "deadbeef"}], index)
        assert found == [], "content must be identical, not merely present"


# ---------------------------------------------------------------------------
# Reconciliation end to end, against real files
# ---------------------------------------------------------------------------

class TestReconcileLibrary:
    async def test_splits_the_library_into_its_four_states(self, env):
        await init_db()
        try:
            here = _write(env / "here.mp3", b"i am here")
            tid_here = await _mk_track(here)
            tid_gone = await _mk_track(env / "gone.mp3", status="missing")
            tid_never = await _mk_track(env / "never.mp3", status="missing")
            await execute(
                "INSERT INTO operations (op_key, job_id, kind, state, track_id) "
                "VALUES ('job:1', 1, 'download', 'complete', ?)", (tid_never,))

            result = await scanner.reconcile_library(deep=False)

            assert result["status"] == "complete"
            counts = result["counts"]
            assert counts[acquisition.STATE_PRESENT] == 1
            assert counts[acquisition.STATE_MISSING] == 1
            assert counts[acquisition.STATE_NEVER_ACQUIRED] == 1
            assert counts[acquisition.STATE_MOVED] == 0

            by_id = {t["id"]: t for t in result["tracks"]}
            assert by_id[tid_here]["state"] == acquisition.STATE_PRESENT
            assert by_id[tid_gone]["state"] == acquisition.STATE_MISSING
            assert by_id[tid_never]["state"] == acquisition.STATE_NEVER_ACQUIRED
            assert "job 1" in by_id[tid_never]["detail"]
        finally:
            await close_db()

    async def test_detects_a_genuinely_moved_file_by_content(self, env):
        """The case that motivated all of this: a download that was moved
        somewhere else in the library. Proven by hashing real files."""
        await init_db()
        try:
            payload = b"distinctive content for this particular recording"
            original = env / "Artist" / "Album" / "01 - Song.mp3"
            _write(original, payload)

            tid = await _mk_track(original)
            await acquisition.record(
                track_id=tid, file_path=str(original), job_id=9,
                source="ytsearch1", sha256=_sha(payload), file_size=len(payload),
            )

            # The folder is reorganised; the file now sits at the root.
            original.unlink()
            original.parent.rmdir()
            original.parent.parent.rmdir()
            _write(env / "01 - Song.mp3", payload)

            cheap = await scanner.reconcile_library(deep=False)
            assert cheap["counts"][acquisition.STATE_MISSING] == 1, \
                "without hashing there is no evidence of a move"
            assert cheap["moved_checked"] is False

            deep = await scanner.reconcile_library(deep=True)
            assert deep["counts"][acquisition.STATE_MOVED] == 1
            assert deep["counts"][acquisition.STATE_MISSING] == 0

            moved = [t for t in deep["tracks"]
                     if t["state"] == acquisition.STATE_MOVED][0]
            assert moved["id"] == tid
            assert moved["expected_path"] == str(original)
            assert moved["discovered_path"] == str(env / "01 - Song.mp3")
            assert moved["sha256"] == _sha(payload)
        finally:
            await close_db()

    async def test_different_content_at_the_new_path_is_not_a_move(self, env):
        """Someone else's track landed where ours used to be. That is a
        different problem, and it must not be reported as our file moving."""
        await init_db()
        try:
            original = env / "Album" / "song.mp3"
            _write(original, b"our recording")
            tid = await _mk_track(original)
            await acquisition.record(
                track_id=tid, file_path=str(original),
                sha256=_sha(b"our recording"), file_size=14,
            )
            original.unlink()
            original.parent.rmdir()
            _write(env / "song.mp3", b"a completely different recording")

            deep = await scanner.reconcile_library(deep=True)
            assert deep["counts"][acquisition.STATE_MOVED] == 0
            assert deep["counts"][acquisition.STATE_MISSING] == 1
        finally:
            await close_db()

    async def test_archived_rows_are_included_when_asked(self, env):
        await init_db()
        try:
            tid = await _mk_track(env / "a.mp3", status="missing",
                                  library_status="archived")
            with_archived = await scanner.reconcile_library(include_archived=True)
            without = await scanner.reconcile_library(include_archived=False)

            assert tid in [t["id"] for t in with_archived["tracks"]]
            assert tid not in [t["id"] for t in without["tracks"]]
        finally:
            await close_db()

    async def test_an_unavailable_root_reports_nothing_missing(self, env, monkeypatch):
        """An unmounted drive and a deleted library look identical to a path
        check. Claiming the whole library vanished would be a lie."""
        await init_db()
        try:
            for name in ("a.mp3", "b.mp3"):
                _write(env / name, b"data")
                await _mk_track(env / name)
            monkeypatch.setenv("SOUNDBLOOM_TEST", "1")
            import backend.config as cfg
            cfg._cache = {**cfg.DEFAULTS,
                          "library_path": str(env.parent / "not-mounted")}

            result = await scanner.reconcile_library(deep=False)
            assert result["status"] == "skipped"
            assert result["counts"] == {}
            assert result["tracks"] == []
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Hashing behaviour
# ---------------------------------------------------------------------------

class TestHashing:
    async def test_sha256_matches_hashlib(self, env):
        payload = b"x" * (1024 * 1024 + 7)  # spans several chunk boundaries
        path = _write(env / "big.mp3", payload)
        assert await acquisition.sha256_async(path) == _sha(payload)

    async def test_index_maps_digest_to_every_file_with_that_content(self, env):
        a = _write(env / "a.mp3", b"same")
        b = _write(env / "b.mp3", b"same")
        c = _write(env / "c.mp3", b"different")
        index = await acquisition.hash_paths([a, b, c])

        assert set(index[_sha(b"same")]) == {a, b}
        assert index[_sha(b"different")] == [c]

    async def test_unreadable_files_are_skipped_not_fatal(self, env):
        good = _write(env / "good.mp3", b"data")
        index = await acquisition.hash_paths([str(env / "missing.mp3"), good])
        assert list(index.values()) == [[good]]
