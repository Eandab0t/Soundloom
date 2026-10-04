"""Deciding a download the match gate blocked.

Addressed by **job id**, not by attention item id, and that is deliberate.

The Needs Attention producer builds each row's action routes before the item
exists - `raise_item` assigns the id. So the producer knows the job and cannot
know the attention row. Keying these endpoints by job id means the route a
producer writes into `action` is the route the button can actually call, with
no translation step and no chance of the two ids drifting apart. (An earlier
version keyed them by item id and every button 404'd in the browser while the
endpoint tests passed, because they were handed the item id by hand.)

Acting on a decision also closes the attention row that asked for it, found by
`entity_id`. Leaving that to the next producer scan would keep a decided item
at the top of the user's list for up to a scan interval, still looking like it
needed them - so it is done here, in the same request.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import attention
from backend.services import match_decisions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/matches", tags=["matches"])

BLOCKED_TYPE = "blocked_download"


class _Note(BaseModel):
    """Optional note recorded with the decision."""
    note: str = ""


class _RePoint(BaseModel):
    """The corrected request. Either field alone is enough."""
    artist: str = ""
    title: str = ""
    album: str = ""
    note: str = ""


async def _close_attention_item(job_id: int, decision: str, result: dict) -> None:
    """Resolve the open attention row that was asking about this job."""
    from ..database import fetch_all
    rows = await fetch_all(
        "SELECT id FROM attention_items WHERE type=? AND entity_id=? AND status='open'",
        (BLOCKED_TYPE, job_id),
    )
    score = result.get("score", "?")
    threshold = result.get("threshold", "?")
    for row in rows:
        await attention.resolve_item(
            row["id"], f"{decision}: job {job_id} ({score}% vs {threshold}%)"
        )


async def _decide(job_id: int, apply, note: str, verb: str) -> dict:
    """Shared shape for the three decisions: act, then close the item."""
    try:
        result = await apply(job_id, note)
    except match_decisions.MatchDecisionError as e:
        raise HTTPException(409, e.message)
    try:
        await _close_attention_item(job_id, verb, result)
    except Exception as e:
        # The decision itself landed; failing to tidy the list must not tell
        # the user their decision did not happen.
        logger.exception("Decision %s applied to job %s but the attention "
                         "item was not closed: %s", verb, job_id, e)
    return result


@router.post("/{job_id}/accept")
async def accept_blocked_download(job_id: int, body: _Note | None = None):
    """Take this candidate anyway and download it.

    The only path past the gate for a below-threshold match, and it is spent
    on exactly one attempt.
    """
    return await _decide(job_id, match_decisions.accept,
                         (body.note if body else "") or "", "accepted")


@router.post("/{job_id}/reject")
async def reject_blocked_download(job_id: int, body: _Note | None = None):
    """Wrong candidate. Stop; do not retry this job."""
    return await _decide(job_id, match_decisions.reject,
                         (body.note if body else "") or "", "rejected")


@router.post("/{job_id}/repoint")
async def repoint_blocked_download(job_id: int, body: _RePoint):
    """The request was wrong. Correct it and search again, gate still armed."""
    return await _decide(
        job_id,
        lambda jid, note: match_decisions.repoint(
            jid, artist=body.artist, title=body.title,
            album=body.album, note=note,
        ),
        body.note, "repointed",
    )


@router.get("/decisions")
async def decision_history(job_id: int = None, limit: int = 50):
    """Every accept/reject/re-point, with the score it overrode."""
    try:
        return {"decisions": await match_decisions.history(job_id, limit)}
    except Exception as e:
        logger.exception("Failed to read match decision history: %s", e)
        raise HTTPException(500, f"Failed to read match decision history: {e}")
