"""Queue lifecycle: existence checks, cancellation and retry rules."""

import pytest
from fastapi import HTTPException

from backend import config
from backend import database as db
from backend.api import queue as api
from backend.pipeline.models import JobState
from backend.services.downloader import JobCancelled, _ensure_not_cancelled


@pytest.fixture()
async def queue_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "queue.db")
    if db._db is not None:
        await db._db.close()
    db._db = None
    await db.init_db()
    yield db
    await db.close_db()


async def _job(status="pending", **fields):
    columns = {"status": status, "source_url": "https://example.com/x"}
    columns.update(fields)
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    cursor = await db.execute(
        f"INSERT INTO jobs ({names}) VALUES ({marks})", tuple(columns.values())
    )
    return cursor.lastrowid


async def _row(job_id):
    return await db.fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))


class TestDelete:
    async def test_missing_job_is_404(self, queue_db):
        with pytest.raises(HTTPException) as exc:
            await api.delete_job(999)
        assert exc.value.status_code == 404

    async def test_completed_job_is_removed(self, queue_db):
        job_id = await _job(JobState.COMPLETE.value)
        assert await api.delete_job(job_id) == {"status": "deleted"}
        assert await _row(job_id) is None

    async def test_active_job_is_cancelled_not_deleted(self, queue_db):
        job_id = await _job(JobState.DOWNLOADING.value, progress=40)
        assert await api.delete_job(job_id) == {"status": "cancelled"}
        row = await _row(job_id)
        assert row is not None
        assert row["status"] == JobState.CANCELLED.value
        assert row["error"] == "Cancelled by user"
        assert row["progress"] == 0

    async def test_finalizing_job_cannot_be_cancelled(self, queue_db):
        job_id = await _job(JobState.ORGANIZING.value)
        with pytest.raises(HTTPException) as exc:
            await api.delete_job(job_id)
        assert exc.value.status_code == 409
        assert (await _row(job_id))["status"] == JobState.ORGANIZING.value


class TestUpdate:
    async def test_missing_job_is_404(self, queue_db):
        with pytest.raises(HTTPException) as exc:
            await api.update_job(999, {"title": "x"})
        assert exc.value.status_code == 404

    async def test_unknown_status_is_rejected(self, queue_db):
        job_id = await _job()
        with pytest.raises(HTTPException) as exc:
            await api.update_job(job_id, {"status": "banana"})
        assert exc.value.status_code == 400

    async def test_worker_state_cannot_be_set_by_client(self, queue_db):
        job_id = await _job()
        with pytest.raises(HTTPException) as exc:
            await api.update_job(job_id, {"status": JobState.DOWNLOADING.value})
        assert exc.value.status_code == 409

    async def test_cancel_transition_is_allowed(self, queue_db):
        job_id = await _job()
        await api.update_job(job_id, {"status": JobState.CANCELLED.value})
        row = await _row(job_id)
        assert row["status"] == JobState.CANCELLED.value
        assert row["error"] == "Cancelled by user"

    async def test_invalid_progress_is_rejected(self, queue_db):
        job_id = await _job()
        for value in ("nope", -1, 101):
            with pytest.raises(HTTPException) as exc:
                await api.update_job(job_id, {"progress": value})
            assert exc.value.status_code == 400

    async def test_metadata_update_still_works(self, queue_db):
        job_id = await _job()
        await api.update_job(job_id, {"title": "New", "progress": 12.5})
        row = await _row(job_id)
        assert row["title"] == "New"
        assert row["progress"] == 12.5


class TestRetry:
    async def test_missing_job_is_404(self, queue_db):
        with pytest.raises(HTTPException) as exc:
            await api.retry_job(999)
        assert exc.value.status_code == 404

    async def test_active_job_cannot_be_retried(self, queue_db):
        job_id = await _job(JobState.DOWNLOADING.value)
        with pytest.raises(HTTPException) as exc:
            await api.retry_job(job_id)
        assert exc.value.status_code == 409

    async def test_failed_job_gets_a_fresh_budget(self, queue_db):
        job_id = await _job(JobState.FAILED.value, retries=3,
                            error="boom", not_before="2099-01-01T00:00:00+00:00")
        assert await api.retry_job(job_id) == {"status": "retried"}
        row = await _row(job_id)
        assert row["status"] == JobState.PENDING.value
        assert row["retries"] == 0
        assert row["error"] == ""
        assert row["not_before"] is None


class TestCancellationCheckpoint:
    async def test_cancelled_job_stops_the_pipeline(self, queue_db):
        job_id = await _job(JobState.CANCELLED.value)
        with pytest.raises(JobCancelled):
            await _ensure_not_cancelled(job_id)

    async def test_deleted_job_stops_the_pipeline(self, queue_db):
        with pytest.raises(JobCancelled):
            await _ensure_not_cancelled(12345)
