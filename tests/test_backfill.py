"""The provenance backfill: resumable, pausable, and honest about what it
recorded.

The backfill exists to give pre-existing tracks a SHA-256, so that a
future disappearance can be *proven* rather than guessed at. Two things
follow from that, and both are pinned here.

**It must survive interruption.** A 10 second sweep that restarts from
zero on every crash is worse than no sweep, because it looks like it is
working. So progress is a database row, the resume cursor is read back
*from that row* rather than from memory, and the write is idempotent so a
re-run can never double-hash or duplicate.

**It must not overstate what it knows.** A backfilled row records
`source='backfill'` and a NULL `job_id`, because these files were not
acquired by a job - they were already in the library. A row that implied
otherwise would be a fabrication sitting in the one table whose entire
purpose is to be trustworthy.

The backfill never moves, renames or deletes anything, and there is no
test seam anywhere that would let it.
"""
import asyncio
import hashlib

import pytest

from backend.database import close_db, execute, fetch_all, fetch_one, init_db
from backend.services import backfill


@pytest.fixture
def env(monkeypatch, tmp_path):
    import backend.config as cfg
    library = tmp_path / "Music"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "backfill.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
    return library



@pytest.fixture(autouse=True)
async def _no_leaked_task():
    """No test may inherit a background run from the one before it.

    `_task` is module-level state, so without this a run left over from a
    previous test makes `start()` answer "already_running" for work that
    is not running - a failure that looks like a service bug and is not.
    """
    await backfill.shutdown()
    try:
        yield
    finally:
        await backfill.shutdown()

def _write(path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _mk_track(file_path, status="present", size=0):
    cur = await execute(
        "INSERT INTO tracks (file_path, title, file_status, file_size) "
        "VALUES (?, ?, ?, ?)",
        (str(file_path), "T", status, size))
    return cur.lastrowid


async def _library(env, count=4):
    """`count` real files on disk, each with a track row pointing at it."""
    made = []
    for i in range(count):
        payload = f"audio bytes number {i}".encode()
        path = _write(env / f"t{i}.mp3", payload)
        tid = await _mk_track(path, size=len(payload))
        made.append((tid, path, payload))
    return made


# ---------------------------------------------------------------------------
# The ordinary run
# ---------------------------------------------------------------------------

class TestBackfillRun:
    async def test_hashes_every_present_file(self, env):
        await init_db()
        try:
            made = await _library(env, 4)
            await backfill.start()
            await backfill.wait_idle()

            rows = await fetch_all(
                "SELECT * FROM acquisition ORDER BY track_id")
            assert len(rows) == 4
            by_track = {r["track_id"]: r for r in rows}
            for tid, path, payload in made:
                assert by_track[tid]["sha256"] == _sha(payload)
                assert by_track[tid]["file_size"] == len(payload)
                assert by_track[tid]["file_path"] == path
        finally:
            await close_db()

    async def test_run_completes_and_records_progress(self, env):
        await init_db()
        try:
            await _library(env, 3)
            await backfill.start()
            await backfill.wait_idle()

            run = await backfill.status()
            assert run["status"] == "complete"
            assert run["total_files"] == 3
            assert run["processed_files"] == 3
            assert run["bytes_processed"] == run["bytes_total"] > 0
            assert run["percent"] == 100.0
            assert run["error_count"] == 0
            assert run["completed_at"], "a completed run gets a completion time"
            assert run["running"] is False
        finally:
            await close_db()

    async def test_records_backfill_provenance_not_an_invented_job(self, env):
        """These files were already in the library. Claiming a job id or a
        download source would put a fabrication in the one table that has
        to be trustworthy."""
        await init_db()
        try:
            await _library(env, 1)
            await backfill.start()
            await backfill.wait_idle()

            row = (await fetch_all("SELECT * FROM acquisition"))[0]
            assert row["source"] == "backfill"
            assert row["job_id"] is None
            assert row["organizer_result"] == "backfill"
        finally:
            await close_db()

    async def test_skips_missing_files_but_still_records_the_gap(self, env):
        """A track whose file cannot be read still gets a row - without a
        hash. That is the honest record: the library believes the file
        exists, and its content could not be identified."""
        await init_db()
        try:
            tid = await _mk_track(env / "not-there.mp3", size=123)
            await backfill.start()
            await backfill.wait_idle()

            rows = await fetch_all("SELECT * FROM acquisition WHERE track_id=?",
                                   (tid,))
            assert len(rows) == 1
            assert rows[0]["sha256"] == ""
            assert rows[0]["file_size"] == 123
            assert "unreadable" in rows[0]["organizer_result"]

            run = await backfill.status()
            assert run["error_count"] == 1
        finally:
            await close_db()

    async def test_does_not_touch_missing_tracks(self, env):
        """A missing file cannot be hashed. Pretending otherwise by writing
        an empty hash would make it look identified."""
        await init_db()
        try:
            tid = await _mk_track(env / "gone.mp3", status="missing")
            await backfill.start()
            await backfill.wait_idle()

            rows = await fetch_all("SELECT * FROM acquisition WHERE track_id=?",
                                   (tid,))
            assert rows == []
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Idempotence and resume
# ---------------------------------------------------------------------------

class TestIdempotence:
    async def test_running_twice_does_not_duplicate(self, env):
        await init_db()
        try:
            await _library(env, 3)
            await backfill.start()
            await backfill.wait_idle()
            await backfill.start()
            await backfill.wait_idle()

            assert len(await fetch_all("SELECT * FROM acquisition")) == 3
            runs = await backfill.history()
            assert len(runs) == 2, "both runs are kept as an audit trail"
            assert runs[0]["processed_files"] == 0, \
                "the second run had nothing left to do"
        finally:
            await close_db()

    async def test_resume_continues_from_the_persisted_cursor(self, env):
        """The cursor has to come back from the row. A cursor kept in a
        module-level dict would resume correctly in the same process and
        silently restart from zero after the restart this feature is for."""
        await init_db()
        try:
            made = await _library(env, 5)
            # Simulate a run that died after two files.
            await execute(
                "INSERT INTO provenance_backfill "
                " (status, started_at, total_files, processed_files, "
                "  bytes_total, bytes_processed, last_track_id) "
                "VALUES ('interrupted', datetime('now'), 5, 2, 100, 40, ?)",
                (made[1][0],))
            for tid, _path, payload in made[:2]:
                await execute(
                    "INSERT INTO acquisition (track_id, file_path, sha256, "
                    "source, file_size) VALUES (?,?,?,'backfill',?)",
                    (tid, str(env / f"t{made.index((tid, _path, payload))}.mp3"),
                     _sha(payload), len(payload)))

            result = await backfill.start(resume=True)
            assert result["action"] == "resumed"
            await backfill.wait_idle()

            run = await backfill.status()
            assert run["status"] == "complete"
            assert run["processed_files"] == 5
            assert run["last_track_id"] == made[-1][0]

            rows = await fetch_all("SELECT * FROM acquisition")
            assert len(rows) == 5
            digests = {r["sha256"] for r in rows}
            assert digests == {_sha(p) for _tid, _p, p in made}
        finally:
            await close_db()

    async def test_nothing_to_resume_is_reported_not_guessed(self, env):
        await init_db()
        try:
            await _library(env, 1)
            await backfill.start()
            await backfill.wait_idle()

            out = await backfill.start(resume=True)
            assert out["action"] == "nothing_to_resume"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Pause, cancel, crash
# ---------------------------------------------------------------------------

class TestControl:
    async def test_cancel_keeps_the_work_already_done(self, env):
        await init_db()
        try:
            made = await _library(env, 40)
            # One file per chunk, so the loop is stoppable mid-run.
            monkey = backfill.CHUNK_SIZE
            backfill.CHUNK_SIZE = 1
            try:
                await backfill.start()
                # Let a few files land, then stop.
                for _ in range(30):
                    await asyncio.sleep(0)
                    run = await backfill.status()
                    if (run["processed_files"] or 0) >= 2:
                        break
                await backfill.cancel()
                await backfill.wait_idle()
            finally:
                backfill.CHUNK_SIZE = monkey

            run = await backfill.status()
            assert run["status"] == "cancelled"
            assert not run["completed_at"], \
                "a cancelled run has not finished; stamping one would lie"

            hashed = await fetch_one(
                "SELECT COUNT(*) c FROM acquisition WHERE sha256 != ''")
            assert hashed["c"] >= 2, "finished work survives a cancel"
            assert hashed["c"] < len(made), "and it really did stop early"
        finally:
            await close_db()

    async def test_pause_stops_and_offers_resume(self, env):
        await init_db()
        try:
            await _library(env, 40)
            monkey = backfill.CHUNK_SIZE
            backfill.CHUNK_SIZE = 1
            try:
                await backfill.start()
                for _ in range(30):
                    await asyncio.sleep(0)
                    run = await backfill.status()
                    if (run["processed_files"] or 0) >= 2:
                        break
                await backfill.pause()
                await backfill.wait_idle()
            finally:
                backfill.CHUNK_SIZE = monkey

            run = await backfill.status()
            assert run["status"] == "paused"
            assert run["resumable"] is True
            assert run["running"] is False

            out = await backfill.start(resume=True)
            assert out["action"] == "resumed"
            await backfill.wait_idle()
            run = await backfill.status()
            assert run["status"] == "complete"
        finally:
            await close_db()

    async def test_mark_interrupted_relabels_a_dead_run(self, env):
        """Only a process death leaves a row in 'running'. Leaving it there
        would make the panel claim a task is in flight that is not, and
        offer Resume for nothing."""
        await init_db()
        try:
            cur = await execute(
                "INSERT INTO provenance_backfill (status, processed_files) "
                "VALUES ('running', 5)")
            assert (await backfill.status())["status"] == "running"

            n = await backfill.mark_interrupted()
            assert n == 1
            run = await backfill.status()
            assert run["status"] == "interrupted"
            assert run["processed_files"] == 5, "counters survive the relabel"
            assert run["resumable"] is True
        finally:
            await close_db()

    async def test_mark_interrupted_leaves_finished_runs_alone(self, env):
        await init_db()
        try:
            await execute(
                "INSERT INTO provenance_backfill (status) VALUES ('complete')")
            await execute(
                "INSERT INTO provenance_backfill (status) VALUES ('cancelled')")
            assert await backfill.mark_interrupted() == 0
            runs = await backfill.history()
            assert {r["status"] for r in runs} == {"complete", "cancelled"}
        finally:
            await close_db()

    async def test_a_second_concurrent_run_is_refused(self, env):
        """Two loops writing the same rows would duplicate work and
        corrupt the counters, and one progress bar cannot honestly show
        two."""
        await init_db()
        try:
            await _library(env, 60)
            monkey = backfill.CHUNK_SIZE
            backfill.CHUNK_SIZE = 1
            try:
                await backfill.start()
                second = await backfill.start()
                assert second["action"] == "already_running"
                await backfill.cancel()
                await backfill.wait_idle()
            finally:
                backfill.CHUNK_SIZE = monkey
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestResumeCoherence:
    async def test_resumed_run_never_processes_more_than_its_total(self, env):
        """A resume measures only what is still un-hashed, but
        `processed_files` carries the work done before the crash. Reporting
        the remainder as the total gave 757/482 on the real library.

        The total has to be cumulative, or the bar runs backwards after any
        interruption - which is precisely when someone is watching it.
        """
        await init_db()
        try:
            made = await _library(env, 5)

            # A run that died with two files done and a cursor past them.
            await execute(
                "INSERT INTO provenance_backfill "
                " (status, total_files, processed_files, bytes_total, "
                "  bytes_processed, last_track_id) "
                "VALUES ('interrupted', 5, 2, 100, 40, ?)",
                (made[1][0],))
            for tid, path, payload in made[:2]:
                await execute(
                    "INSERT INTO acquisition (track_id, file_path, sha256, "
                    "source, file_size) VALUES (?,?,?,'backfill',?)",
                    (tid, path, _sha(payload), len(payload)))

            await backfill.start(resume=True)
            await backfill.wait_idle()

            run = await backfill.status()
            assert run["processed_files"] <= run["total_files"], \
                f"processed {run['processed_files']} of total {run['total_files']}"
            assert run["total_files"] == 5, "the total is the whole job"
            assert run["processed_files"] == 5
            assert run["percent"] == 100.0
            assert run["bytes_processed"] <= run["bytes_total"]

            rows = await fetch_all("SELECT * FROM acquisition")
            assert len(rows) == 5
            assert all(r["sha256"] for r in rows)
        finally:
            await close_db()


class TestProgressHonesty:
    async def test_percent_never_exceeds_one_hundred(self, env):
        """The counter and the total can disagree - a file can change size
        between being measured and being hashed - and a bar that runs to
        111% is worse than one that stops at 100."""
        await init_db()
        try:
            await _library(env, 2)
            await backfill.start()
            await backfill.wait_idle()

            run = await backfill.status()
            # Force the disagreement the real library produced.
            await execute(
                "UPDATE provenance_backfill SET bytes_total = ? WHERE id = ?",
                (1, run["id"]))
            run = await backfill.status()
            assert run["percent"] == 100.0
            assert run["bytes_processed"] > run["bytes_total"]
        finally:
            await close_db()

    async def test_total_is_measured_from_disk_not_the_database(self, env):
        """`tracks.file_size` is a scan-time snapshot and drifts. Using it
        as the denominator for work measured against real bytes is what
        made the bar overrun."""
        await init_db()
        try:
            payload = b"x" * 5000
            path = _write(env / "drifted.mp3", payload)
            # Record a deliberately wrong size, as a stale scan would.
            tid = await _mk_track(path, size=7)

            await backfill.start()
            await backfill.wait_idle()

            run = await backfill.status()
            assert run["bytes_total"] == 5000, \
                "the total comes from the file, not from file_size"
            assert run["bytes_processed"] == 5000
            assert run["percent"] == 100.0

            row = (await fetch_all(
                "SELECT * FROM acquisition WHERE track_id=?", (tid,)))[0]
            assert row["file_size"] == 5000
        finally:
            await close_db()

    async def test_percent_is_zero_before_any_work(self, env):
        await init_db()
        try:
            await _library(env, 2)
            await execute(
                "INSERT INTO provenance_backfill (status, bytes_total, "
                "bytes_processed, total_files) VALUES ('running', 0, 0, 0)")
            run = await backfill.status()
            assert run["percent"] == 0.0
        finally:
            await close_db()


class TestReporting:
    async def test_pending_is_knowable_before_anything_runs(self, env):
        await init_db()
        try:
            await _library(env, 3)
            await _mk_track(env / "gone.mp3", status="missing")

            p = await backfill.pending()
            assert p["present"] == 3
            assert p["hashed"] == 0
            assert p["remaining"] == 3

            await backfill.start()
            await backfill.wait_idle()
            p = await backfill.pending()
            assert p["hashed"] == 3
            assert p["remaining"] == 0
        finally:
            await close_db()

    async def test_no_runs_yet_reports_none_rather_than_erroring(self, env):
        await init_db()
        try:
            assert await backfill.status() is None
            assert await backfill.history() == []
        finally:
            await close_db()

    async def test_hashed_tracks_are_left_alone(self, env):
        """A track that already has a recorded hash must not be re-hashed
        or given a second row: that would be duplicate evidence for one
        file, and the audit trail has to stay readable."""
        await init_db()
        try:
            payload = b"already hashed"
            path = _write(env / "known.mp3", payload)
            tid = await _mk_track(path, size=len(payload))
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, source, "
                "job_id) VALUES (?,?,?,'ytsearch1',42)",
                (tid, path, _sha(payload)))

            await backfill.start()
            await backfill.wait_idle()

            rows = await fetch_all("SELECT * FROM acquisition WHERE track_id=?",
                                   (tid,))
            assert len(rows) == 1
            assert rows[0]["source"] == "ytsearch1", \
                "the real acquisition record survives the backfill"
            assert rows[0]["job_id"] == 42
        finally:
            await close_db()
