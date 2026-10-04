"""Tests for crash-recoverable file operations.

The bug these exist to prevent: a crash between the atomic move into the
library and the SQLite INSERT left a real, paid-for file in the user's
library that Soundloom did not know about. Reconcile() rolls that forward.

Covers the state machine, journal idempotency, every crash point in the
pipeline, and that recovery never runs twice into a duplicate.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.errors import VividlyError
from backend.services import operations as ops
from backend.services.operations import OpState


@pytest.fixture
def op_env(monkeypatch, tmp_path):
    """Throwaway DB + settings + library dir."""
    import backend.config as cfg
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "ops.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {
        **cfg.DEFAULTS,
        "library_path": str(library),
        "auto_resume_operations": False,
    })
    return SimpleNamespace(cfg=cfg, LIBRARY=library)


def _audio(path, size=2048):
    """A stand-in audio file. Tag reads fail and fall back to op metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path


async def _mk_job(url="https://example.test/x", status="downloading"):
    from backend.database import execute
    cur = await execute(
        "INSERT INTO jobs (source_url, status) VALUES (?, ?)", (url, status))
    return cur.lastrowid


async def _tracks():
    from backend.database import fetch_all
    return await fetch_all("SELECT * FROM tracks")


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

class TestStateMachine:
    async def test_walks_the_full_happy_path(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            for state in (OpState.DOWNLOADING, OpState.DOWNLOADED,
                          OpState.VALIDATED, OpState.TAGGED,
                          OpState.ORGANIZED, OpState.COMMITTED):
                row = await ops.advance(op_id, state)
                assert row["state"] == state.value
            assert row["state"] == "committed"
        finally:
            await close_db()

    async def test_rejects_skipping_states(self, op_env):
        """queued -> committed bypasses the whole pipeline; that is a bug."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            op_id = await ops.begin_operation("job:1")
            with pytest.raises(VividlyError):
                await ops.advance(op_id, OpState.COMMITTED)
        finally:
            await close_db()

    async def test_cannot_leave_a_terminal_state(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            op_id = await ops.begin_operation("job:1")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED)
            with pytest.raises(VividlyError):
                await ops.advance(op_id, OpState.COMMITTED)
        finally:
            await close_db()

    async def test_rejects_unknown_fields(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            op_id = await ops.begin_operation("job:1")
            with pytest.raises(VividlyError):
                await ops.advance(op_id, OpState.DOWNLOADING, bogus_field=1)
        finally:
            await close_db()

    async def test_advance_on_unknown_operation_is_none(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            assert await ops.advance(9999, OpState.DOWNLOADING) is None
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Journal idempotency
# --------------------------------------------------------------------------

class TestJournal:
    async def test_same_key_returns_the_same_operation(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            a = await ops.begin_operation("job:7", title="A")
            b = await ops.begin_operation("job:7", title="B")
            assert a == b
            rows = await ops.list_operations()
            assert len(rows) == 1
        finally:
            await close_db()

    async def test_interrupted_excludes_terminal(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            live = await ops.begin_operation("job:1")
            done = await ops.begin_operation("job:2")
            for state in (OpState.DOWNLOADING, OpState.DOWNLOADED,
                          OpState.VALIDATED, OpState.TAGGED,
                          OpState.ORGANIZED, OpState.COMMITTED):
                await ops.advance(done, state)
            ids = [r["id"] for r in await ops.interrupted_operations()]
            assert ids == [live]
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Reconciliation - the crash-recovery behaviour
# --------------------------------------------------------------------------

class TestReconcile:
    async def test_indexes_a_file_that_reached_the_library(self, op_env):
        """The headline case: file moved into the library, process died
        before the INSERT. The track must not be lost."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            final = _audio(op_env.LIBRARY / "The Cardigans" / "Lovefool.mp3")
            op_id = await ops.begin_operation(
                "job:1", title="Lovefool", artist="The Cardigans",
                album="Gran Turismo")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED)
            await ops.advance(op_id, OpState.VALIDATED)
            await ops.advance(op_id, OpState.TAGGED)
            await ops.advance(op_id, OpState.ORGANIZED, final_path=str(final))

            assert await _tracks() == []          # crash: never indexed

            summary = await ops.reconcile()

            assert summary["recovered"] == 1
            tracks = await _tracks()
            assert len(tracks) == 1
            assert tracks[0]["file_path"] == str(final)
            row = await ops.get_operation(op_id)
            assert row["state"] == "committed"
            assert row["track_id"] == tracks[0]["id"]
        finally:
            await close_db()

    async def test_reconcile_is_idempotent(self, op_env):
        """Running recovery twice must not create a second track row."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            final = _audio(op_env.LIBRARY / "Lovefool.mp3")
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED)
            await ops.advance(op_id, OpState.VALIDATED)
            await ops.advance(op_id, OpState.TAGGED)
            await ops.advance(op_id, OpState.ORGANIZED, final_path=str(final))

            await ops.reconcile()
            second = await ops.reconcile()        # already terminal: no-op

            assert second["examined"] == 0
            assert len(await _tracks()) == 1
        finally:
            await close_db()

    async def test_resumes_a_converted_file_left_in_staging(self, op_env, tmp_path):
        """Crash after tagging: the audio file is complete, so finish the
        operation instead of making the user download it again."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            staged = _audio(tmp_path / "tmp" / "conv_abc123.mp3")
            op_id = await ops.begin_operation(
                "job:1", title="Lovefool", artist="The Cardigans",
                album="Gran Turismo", album_artist="The Cardigans",
                track_number=3)
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED)
            await ops.advance(op_id, OpState.VALIDATED, staged_path=str(staged))
            await ops.advance(op_id, OpState.TAGGED, staged_path=str(staged))

            summary = await ops.reconcile()

            assert summary["resumed"] == 1
            assert not staged.exists()             # moved, not copied
            row = await ops.get_operation(op_id)
            assert row["state"] == "committed"
            assert str(op_env.LIBRARY) in row["final_path"]
            # Placed under the real album folder, not "Unknown".
            assert "The Cardigans" in row["final_path"]
            assert "Gran Turismo" in row["final_path"]
            assert len(await _tracks()) == 1
        finally:
            await close_db()

    async def test_missing_library_file_is_a_failure_not_a_recovery(self, op_env):
        """Organized but the file is gone: nothing to recover, say so."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED)
            await ops.advance(op_id, OpState.VALIDATED)
            await ops.advance(op_id, OpState.TAGGED)
            await ops.advance(op_id, OpState.ORGANIZED,
                              final_path=str(op_env.LIBRARY / "gone.mp3"))

            summary = await ops.reconcile()

            assert summary["recovered"] == 0
            assert summary["abandoned"] == 1
            row = await ops.get_operation(op_id)
            assert row["state"] == "failed"
            assert "missing" in row["error"]
            assert await _tracks() == []
        finally:
            await close_db()

    async def test_partial_download_is_not_resumed(self, op_env, tmp_path):
        """A raw download may be truncated. Never treat it as complete."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            staged = _audio(tmp_path / "tmp" / "dl_partial.webm", size=10)
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED, staged_path=str(staged))

            summary = await ops.reconcile()

            assert summary["resumed"] == 0
            row = await ops.get_operation(op_id)
            assert row["state"] == "failed"
            assert "no usable file" in row["error"]
        finally:
            await close_db()

    async def test_interrupted_download_is_recorded_not_silently_dropped(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            job_id = await _mk_job(status="downloading")
            op_id = await ops.begin_operation(f"job:{job_id}", job_id=job_id,
                                              title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)

            summary = await ops.reconcile()

            assert summary["retried"] == 0        # not opted in
            row = await ops.get_operation(op_id)
            assert row["state"] == "failed"
            assert "downloading" in row["error"]
            from backend.database import fetch_one
            job = await fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
            assert job["status"] == "downloading"  # untouched
        finally:
            await close_db()

    async def test_auto_resume_requeues_the_job(self, op_env):
        """With consent, an interrupted download is re-queued for a retry."""
        import backend.config as cfg
        from backend.database import init_db, close_db, fetch_one
        cfg._cache = {**cfg._cache, "auto_resume_operations": True}
        await init_db()
        try:
            job_id = await _mk_job(status="downloading")
            op_id = await ops.begin_operation(f"job:{job_id}", job_id=job_id)
            await ops.advance(op_id, OpState.DOWNLOADING)

            summary = await ops.reconcile()

            assert summary["retried"] == 1
            row = await ops.get_operation(op_id)
            assert row["state"] == "queued"
            job = await fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
            assert job["status"] == "pending"
        finally:
            await close_db()

    async def test_completed_jobs_are_never_requeued(self, op_env):
        import backend.config as cfg
        from backend.database import init_db, close_db, fetch_one
        cfg._cache = {**cfg._cache, "auto_resume_operations": True}
        await init_db()
        try:
            job_id = await _mk_job(status="complete")
            op_id = await ops.begin_operation(f"job:{job_id}", job_id=job_id)
            await ops.advance(op_id, OpState.DOWNLOADING)

            await ops.reconcile()

            job = await fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
            assert job["status"] == "complete"
        finally:
            await close_db()

    async def test_nothing_to_do_is_a_no_op(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            summary = await ops.reconcile()
            assert summary["examined"] == 0
            assert summary["recovered"] == 0
        finally:
            await close_db()

    async def test_one_bad_row_does_not_abort_the_whole_run(self, op_env, monkeypatch):
        """A recovery bug must never stop the app from starting."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            good = _audio(op_env.LIBRARY / "ok.mp3")
            op_good = await ops.begin_operation("job:1", title="Good")
            await ops.advance(op_good, OpState.DOWNLOADING)
            await ops.advance(op_good, OpState.DOWNLOADED)
            await ops.advance(op_good, OpState.VALIDATED)
            await ops.advance(op_good, OpState.TAGGED)
            await ops.advance(op_good, OpState.ORGANIZED, final_path=str(good))

            _audio(op_env.LIBRARY / "bad.mp3")
            op_bad = await ops.begin_operation("job:2", title="Bad")
            await ops.advance(op_bad, OpState.DOWNLOADING)
            await ops.advance(op_bad, OpState.DOWNLOADED)
            await ops.advance(op_bad, OpState.VALIDATED)
            await ops.advance(op_bad, OpState.TAGGED)
            await ops.advance(op_bad, OpState.ORGANIZED,
                              final_path=str(op_env.LIBRARY / "bad.mp3"))

            original = ops._commit_from_disk
            calls = {"n": 0}

            async def flaky(row, final_path, note):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated crash during recovery")
                return await original(row, final_path, note)

            monkeypatch.setattr(ops, "_commit_from_disk", flaky)

            summary = await ops.reconcile()

            assert summary["recovered"] == 1     # the healthy one still landed
            assert summary["abandoned"] >= 1
            assert len(await _tracks()) == 1
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Retry / discard
# --------------------------------------------------------------------------

class TestResolution:
    async def test_retry_requeues_operation_and_job(self, op_env):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            job_id = await _mk_job(status="failed")
            op_id = await ops.begin_operation(f"job:{job_id}", job_id=job_id)
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED, error="boom")

            row = await ops.retry_operation(op_id)

            assert row["state"] == "queued"
            assert row["error"] == ""
            assert row["attempt"] == 2
            job = await fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
            assert job["status"] == "pending"
        finally:
            await close_db()

    async def test_discard_removes_the_staged_file(self, op_env, tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            staged = _audio(tmp_path / "tmp" / "conv_x.mp3")
            op_id = await ops.begin_operation("job:1")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED, staged_path=str(staged))

            row = await ops.discard_operation(op_id)

            assert row["state"] == "abandoned"
            assert not staged.exists()
        finally:
            await close_db()

    async def test_unknown_operation_returns_none(self, op_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            assert await ops.retry_operation(4242) is None
            assert await ops.discard_operation(4242) is None
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Temp sweep respects ownership
# --------------------------------------------------------------------------

class TestReclaimOrphans:
    async def test_keeps_artifacts_a_live_operation_owns(self, op_env, tmp_path, monkeypatch):
        from backend.database import init_db, close_db
        import backend.config as cfg
        monkeypatch.setattr(cfg, "ROOT", tmp_path)
        await init_db()
        try:
            owned_dir = tmp_path / "data" / "tmp" / "conv_keep"
            owned_dir.mkdir(parents=True)
            (owned_dir / "song.mp3").write_bytes(b"\x00" * 512)
            orphan_dir = tmp_path / "data" / "tmp" / "conv_drop"
            orphan_dir.mkdir(parents=True)
            (orphan_dir / "song.mp3").write_bytes(b"\x00" * 512)

            op_id = await ops.begin_operation("job:1")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED,
                              staged_path=str(owned_dir / "song.mp3"))
            await ops.advance(op_id, OpState.VALIDATED,
                              staged_path=str(owned_dir / "song.mp3"))

            from backend.services.downloader import reclaim_orphans
            await reclaim_orphans()

            assert (owned_dir / "song.mp3").exists()
            assert not orphan_dir.exists()
        finally:
            await close_db()

    async def test_journal_failure_blocks_the_sweep(self, op_env, tmp_path, monkeypatch):
        """Deleting is irreversible - if ownership is unreadable, delete nothing."""
        from backend.database import init_db, close_db
        import backend.config as cfg
        monkeypatch.setattr(cfg, "ROOT", tmp_path)
        await init_db()
        try:
            keep = tmp_path / "data" / "tmp" / "conv_keep"
            keep.mkdir(parents=True)
            (keep / "song.mp3").write_bytes(b"\x00" * 512)

            async def boom():
                raise RuntimeError("journal unavailable")

            monkeypatch.setattr(ops, "owned_staged_paths", boom)

            from backend.services.downloader import reclaim_orphans
            removed = await reclaim_orphans()

            assert removed == 0
            assert (keep / "song.mp3").exists()
        finally:
            await close_db()


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------

class TestOperationsApi:
    async def test_lists_and_filters(self, op_env):
        from fastapi import HTTPException
        from backend.database import init_db, close_db
        import backend.api.operations as api
        await init_db()
        try:
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)

            listed = await api.list_ops()
            assert [o["id"] for o in listed["operations"]] == [op_id]

            filtered = await api.list_ops(state="downloading")
            assert len(filtered["operations"]) == 1

            interrupted = await api.list_interrupted()
            assert len(interrupted["operations"]) == 1
        finally:
            await close_db()

    async def test_get_returns_404_for_unknown(self, op_env):
        from fastapi import HTTPException
        from backend.database import init_db, close_db
        import backend.api.operations as api
        await init_db()
        try:
            with pytest.raises(HTTPException) as exc:
                await api.get_op(999)
            assert exc.value.status_code == 404
        finally:
            await close_db()

    async def test_retry_and_discard_endpoints(self, op_env, tmp_path):
        from backend.database import init_db, close_db
        import backend.api.operations as api
        await init_db()
        try:
            staged = _audio(tmp_path / "tmp" / "conv_y.mp3")
            op_id = await ops.begin_operation("job:1")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED, staged_path=str(staged))

            retried = await api.retry_op(op_id)
            assert retried["state"] == "queued"

            discarded = await api.discard_op(op_id)
            assert discarded["state"] == "abandoned"
            assert not staged.exists()
        finally:
            await close_db()

    async def test_reconcile_endpoint(self, op_env):
        from backend.database import init_db, close_db
        import backend.api.operations as api
        await init_db()
        try:
            final = _audio(op_env.LIBRARY / "x.mp3")
            op_id = await ops.begin_operation("job:1", title="X")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.DOWNLOADED)
            await ops.advance(op_id, OpState.VALIDATED)
            await ops.advance(op_id, OpState.TAGGED)
            await ops.advance(op_id, OpState.ORGANIZED, final_path=str(final))

            summary = await api.run_reconcile()
            assert summary["recovered"] == 1
        finally:
            await close_db()

# --------------------------------------------------------------------------
# Regression: recovery must produce a usable library row
# --------------------------------------------------------------------------

class TestRecoveredTrackQuality:
    async def test_empty_tags_do_not_blank_the_metadata(self, op_env):
        """A file with no embedded tags must not be indexed with a blank
        artist - the journal knows the values."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            final = _audio(op_env.LIBRARY / "untagged.mp3")
            op_id = await ops.begin_operation(
                "job:1", title="Lovefool", artist="The Cardigans",
                album="Gran Turismo", album_artist="The Cardigans")
            for state in (OpState.DOWNLOADING, OpState.DOWNLOADED,
                          OpState.VALIDATED, OpState.TAGGED,
                          OpState.ORGANIZED):
                await ops.advance(op_id, state, **(
                    {"final_path": str(final)} if state is OpState.ORGANIZED else {}))

            await ops.reconcile()

            track = (await _tracks())[0]
            assert track["artist"] == "The Cardigans"
            assert track["primary_artist"] == "The Cardigans"
            assert track["album"] == "Gran Turismo"
        finally:
            await close_db()

    async def test_recovery_completes_the_stuck_job(self, op_env):
        """A job crashed at 'indexing' must not stay stuck forever."""
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            final = _audio(op_env.LIBRARY / "x.mp3")
            job_id = await _mk_job(status="indexing")
            op_id = await ops.begin_operation(f"job:{job_id}", job_id=job_id,
                                              title="Lovefool")
            for state in (OpState.DOWNLOADING, OpState.DOWNLOADED,
                          OpState.VALIDATED, OpState.TAGGED):
                await ops.advance(op_id, state)
            await ops.advance(op_id, OpState.ORGANIZED, final_path=str(final))

            await ops.reconcile()

            job = await fetch_one(
                "SELECT status, output_path FROM jobs WHERE id=?", (job_id,))
            assert job["status"] == "complete"
            assert job["output_path"] == str(final)
        finally:
            await close_db()
