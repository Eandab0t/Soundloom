"""Queue / download job endpoints."""
from fastapi import APIRouter, HTTPException, Query
from ..database import fetch_all, fetch_one, execute
from ..pipeline.models import JobState

router = APIRouter(prefix="/api/queue", tags=["queue"])

# Statuses the API may set. 'backoff' is a UI-facing extension of JobState.
_VALID_STATUSES = {state.value for state in JobState} | {"backoff"}

# Terminal states can be removed immediately. Active jobs are cancelled
# cooperatively so the worker can stop before it commits a file.
_TERMINAL_STATUSES = {
    JobState.COMPLETE.value, JobState.FAILED.value, JobState.CANCELLED.value,
}
_FINALIZING_STATUSES = {JobState.ORGANIZING.value, JobState.INDEXING.value}
_CANCELLABLE_STATUSES = _VALID_STATUSES - _TERMINAL_STATUSES - _FINALIZING_STATUSES
_RETRYABLE_STATUSES = {
    JobState.FAILED.value, JobState.CANCELLED.value, JobState.PAUSED.value,
    "backoff",
}


@router.get("")
async def list_jobs(status: str = Query(None)):
    if status == "active":
        rows = await fetch_all("SELECT * FROM jobs WHERE status NOT IN ('complete', 'failed', 'cancelled') ORDER BY created_at DESC LIMIT 100")
    elif status:
        rows = await fetch_all("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT 100", (status,))
    else:
        rows = await fetch_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")
    return {"jobs": rows}


@router.post("")
async def add_job(data: dict):
    url = data.get("url", "").strip()
    query = data.get("query", "").strip()
    source_url = url or query
    if not source_url:
        raise HTTPException(400, "URL or query is required")

    existing = await fetch_one(
        "SELECT id FROM jobs WHERE source_url=? AND status IN ('pending','resolving','matching','downloading','converting','tagging','organizing')",
        (source_url,),
    )
    if existing:
        raise HTTPException(409, f"Already queued (job #{existing['id']})")

    cursor = await execute("""
        INSERT INTO jobs (source_url, source_type, query, output_format, quality_profile, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (
        source_url, data.get("source_type", "auto"), query,
        data.get("format", data.get("output_format", "mp3")),
        data.get("quality", data.get("quality_profile", "balanced"))
    ))
    return {"id": cursor.lastrowid, "status": "added"}


@router.put("/{job_id}")
async def update_job(job_id: int, data: dict):
    job = await fetch_one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Job not found")

    allowed = {"status", "output_format", "quality_profile", "progress", "error", "title", "artist", "output_path"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields")

    if "progress" in updates:
        try:
            progress = float(updates["progress"])
        except (TypeError, ValueError):
            raise HTTPException(400, "progress must be a number")
        if not 0 <= progress <= 100:
            raise HTTPException(400, "progress must be between 0 and 100")
        updates["progress"] = progress

    if "status" in updates:
        target = updates["status"]
        if target not in _VALID_STATUSES:
            raise HTTPException(400, f"Unknown job status '{target}'")
        current = job["status"]
        if target != current:
            if target == JobState.CANCELLED.value:
                if current not in _CANCELLABLE_STATUSES:
                    raise HTTPException(
                        409, f"Cannot cancel a job in '{current}' state"
                    )
                updates.setdefault("error", "Cancelled by user")
                updates["progress"] = 0
            elif target == JobState.PENDING.value:
                if current not in _RETRYABLE_STATUSES:
                    raise HTTPException(
                        409, f"Cannot requeue a job in '{current}' state; use retry"
                    )
                updates.setdefault("error", "")
                updates["not_before"] = None
            else:
                # Worker-managed states are advanced by the pipeline, not by
                # clients; accepting them here would let a lost update move a
                # running job backwards.
                raise HTTPException(
                    409, f"Status '{target}' is managed by the download worker"
                )

    set_clause = ", ".join(f"{k}=?" for k in updates)
    await execute(
        f"UPDATE jobs SET {set_clause}, updated_at=datetime('now') WHERE id=?",
        tuple(list(updates.values()) + [job_id]),
    )
    return {"status": "updated"}


@router.delete("/{job_id}")
async def delete_job(job_id: int):
    job = await fetch_one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Job not found")

    status = job["status"]
    if status in _FINALIZING_STATUSES:
        # The file is already on its way into the library. Cancelling now
        # could leave a file on disk with no database row, so the request is
        # rejected rather than accepted and ignored.
        raise HTTPException(
            409, f"Job is finalizing ('{status}') and can no longer be cancelled"
        )

    if status in _TERMINAL_STATUSES:
        await execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return {"status": "deleted"}

    # Cooperative cancellation: the worker checks this before each stage that
    # mutates files and abandons the operation without touching the library.
    await execute(
        "UPDATE jobs SET status=?, error=?, progress=0, updated_at=datetime('now') WHERE id=?",
        (JobState.CANCELLED.value, "Cancelled by user", job_id),
    )
    return {"status": "cancelled"}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    job = await fetch_one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in _RETRYABLE_STATUSES:
        raise HTTPException(
            409, f"Job in '{job['status']}' state cannot be retried"
        )
    # Clearing not_before matters: a job parked in rate-limit backoff would
    # otherwise be re-parked indefinitely even after a manual retry. Manual
    # retries also earn a fresh retry budget; keeping the old count would make
    # a job that already exhausted max_retries fail again on the first error.
    await execute(
        "UPDATE jobs SET status='pending', error='', not_before=NULL, "
        "retries=0, progress=0, updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    return {"status": "retried"}


@router.post("/clear-completed")
async def clear_completed():
    await execute("DELETE FROM jobs WHERE status='complete'")
    return {"status": "cleared"}


@router.post("/clear-failed")
async def clear_failed():
    await execute("DELETE FROM jobs WHERE status='failed'")
    return {"status": "cleared"}


@router.get("/stats")
async def queue_stats():
    active = await fetch_one("SELECT COUNT(*) as c FROM jobs WHERE status NOT IN ('pending', 'complete', 'failed', 'cancelled')")
    pending = await fetch_one("SELECT COUNT(*) as c FROM jobs WHERE status='pending'")
    completed = await fetch_one("SELECT COUNT(*) as c FROM jobs WHERE status='complete'")
    failed = await fetch_one("SELECT COUNT(*) as c FROM jobs WHERE status='failed'")
    return {
        "active": active["c"] if active else 0,
        "pending": pending["c"] if pending else 0,
        "completed": completed["c"] if completed else 0,
        "failed": failed["c"] if failed else 0,
    }
