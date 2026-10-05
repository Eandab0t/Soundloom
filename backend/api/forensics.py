"""Forensic observation and canary endpoints.

Read-mostly by design. The only write is `POST /observe`, and it writes to
the observation log rather than to disk - the point of watching a library
is to not be the thing that changes it.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import forensics

router = APIRouter(prefix="/api/forensics", tags=["forensics"])


class ObserveRequest(BaseModel):
    force: bool = False


class CanaryRequest(BaseModel):
    track_id: int
    expected_path: str
    label: str = ""
    job_id: str = ""


@router.get("/summary")
async def summary():
    """How much has been observed, and how it broke down."""
    return await forensics.summary()


@router.post("/observe")
async def observe(body: ObserveRequest | None = None):
    """Append a row for every transition since the last observation.

    `force` re-records unchanged states, which is what a canary check
    wants: there the timestamp is the finding.
    """
    return await forensics.observe(force=bool(body and body.force))


@router.post("/poll")
async def poll():
    """Run one debounced pass over known-provenance files now.

    The background observer does this on a timer. This is the same
    pass, on demand, so an operator can see what it currently believes
    without waiting for the interval or restarting the app.
    """
    return await forensics.poll()


@router.get("/timeline/{track_id}")
async def timeline(track_id: int, limit: int = 200):
    """Every recorded transition for one track, newest first."""
    return {"track_id": track_id,
            "observations": await forensics.timeline(track_id, limit)}


@router.get("/canaries")
async def canaries():
    return {"canaries": await forensics.list_canaries()}


@router.post("/canaries")
async def register(body: CanaryRequest):
    """Record a specimen: a file whose bytes are measured now, so that its
    later absence means something."""
    try:
        return await forensics.register_canary(
            track_id=body.track_id, expected_path=body.expected_path,
            label=body.label, job_id=body.job_id)
    except FileNotFoundError:
        raise HTTPException(404, "That file is not on disk, so there is "
                                  "nothing to record a canary for.")


@router.post("/canaries/{canary_id}/verify")
async def verify(canary_id: int):
    """Re-check one specimen by digest and record what changed."""
    try:
        return await forensics.verify_canary(canary_id)
    except LookupError:
        raise HTTPException(404, "No such canary")
