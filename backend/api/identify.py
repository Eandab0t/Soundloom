"""Identify endpoints: propose better tags for library tracks."""
import logging

from fastapi import APIRouter, HTTPException, Query

from ..services import identify

router = APIRouter(prefix="/api/identify", tags=["identify"])
logger = logging.getLogger(__name__)


@router.get("/methods")
async def methods():
    """Which lookup backends are usable right now, and why not if not."""
    return {"methods": identify.methods_status()}


@router.post("/scan")
async def scan(data: dict = None):
    """Look up tracks and queue proposals for review. Writes nothing to disk."""
    data = data or {}
    try:
        return await identify.scan(
            limit=data.get("limit", 25),
            scope=data.get("scope", "auto"),
            method=data.get("method", ""),
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error(f"Identify scan failed: {exc}", exc_info=True)
        raise HTTPException(500, f"Scan failed: {exc}")


@router.get("/candidates")
async def candidates(status: str = Query("pending"), limit: int = Query(100)):
    rows = await identify.list_candidates(status, limit)
    return {"candidates": rows, "count": len(rows)}


@router.post("/{candidate_id}/apply")
async def apply_one(candidate_id: int):
    """Write an approved proposal to the file. Backs up first."""
    try:
        return await identify.apply_candidate(candidate_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.error(f"Identify apply failed for {candidate_id}: {exc}", exc_info=True)
        raise HTTPException(500, f"Apply failed: {exc}")


@router.post("/{candidate_id}/reject")
async def reject_one(candidate_id: int):
    return await identify.reject_candidate(candidate_id)


@router.post("/apply-all")
async def apply_all(data: dict = None):
    data = data or {}
    return await identify.apply_all(limit=data.get("limit", 100))


@router.post("/clear")
async def clear(status: str = Query("all")):
    """Drop candidates that are not pending, keeping the review queue clean."""
    from ..database import execute
    await execute("DELETE FROM fetch_candidates WHERE status != 'pending'")
    return {"status": "cleared"}
