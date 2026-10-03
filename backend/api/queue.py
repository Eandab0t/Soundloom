"""Queue / download job endpoints."""
import asyncio
from fastapi import APIRouter, HTTPException, Query
from ..database import fetch_all, fetch_one, execute

router = APIRouter(prefix="/api/queue", tags=["queue"])


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
    allowed = {"status", "output_format", "quality_profile", "progress", "error", "title", "artist", "output_path"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    await execute(f"UPDATE jobs SET {set_clause}, updated_at=datetime('now') WHERE id=?", tuple(list(updates.values()) + [job_id]))
    return {"status": "updated"}


@router.delete("/{job_id}")
async def delete_job(job_id: int):
    await execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"status": "deleted"}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    # Clearing not_before matters: a job parked in rate-limit backoff would
    # otherwise be re-parked indefinitely even after a manual retry.
    await execute(
        "UPDATE jobs SET status='pending', error='', not_before=NULL, retries=retries+1 WHERE id=?",
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
