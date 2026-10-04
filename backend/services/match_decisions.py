"""Deciding what to do about a download the match gate blocked.

The gate in `downloader._process_job` refuses to download a candidate that
scores below `match_threshold`. That is the correct default, but on its own it
is a dead end: the job goes to `failed` and the only way forward is to open the
queue, read the error string and guess. This module is the other half - it turns
"the gate said no" into three answers the user can pick from the screen that
reported it.

    accept  - the candidate really is right; take it anyway
    reject  - the candidate is wrong; do not retry this job
    repoint - the *request* was wrong; search again for something else

Two design rules earn their keep here:

**An override is one-shot.** `jobs.match_override` is cleared the instant the
gate honours it. A persistent flag would look correct for the retry the user
asked for and then silently bypass every later attempt on that job too - and
the whole point of the gate is that it is not advisory.

**Re-point changes the request, not the gate.** Repointing rewrites the job's
intent and re-arms the gate at its normal threshold. It is the honest answer
when the user says "I asked for the wrong song"; accepting a 40% match is the
honest answer when they say "that *is* the song, the score is being pedantic".
Mixing them up is how a trust feature becomes a way to disable safety by
accident.

Every decision is written to `match_decisions` with the score and threshold that
were in force, so accepting a 58% match is auditable months later even after
the job row is gone.
"""
import json
import logging

from ..database import execute, fetch_all, fetch_one
from ..errors import VividlyError

logger = logging.getLogger(__name__)

# jobs.match_override values. Only ACCEPT exists as a real value; REJECTED and
# REPOINTED are written to *close out* a block so the gate does not see a stale
# candidate again.
OVERRIDE_ACCEPT = "accept"
OVERRIDE_REJECTED = "rejected"
OVERRIDE_REPOINTED = "repointed"

DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"
DECISION_REPOINT = "repoint"


class MatchDecisionError(VividlyError):
    code = "match_decision_error"
    message = "Could not apply that match decision"


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Recording the block
# ---------------------------------------------------------------------------

async def record_block(job_id: int, *, intent, candidate, score: float,
                       threshold: float, explanation: str,
                       source_url: str = "") -> dict:
    """Store the rejected candidate on the job so the UI can explain it.

    Called from the gate on the way to failing. Written before the raise so a
    crash between the two still leaves an explainable row.
    """
    block = {
        "wanted_artist": (getattr(intent, "artist", "") or "").strip(),
        "wanted_title": (getattr(intent, "title", "") or "").strip(),
        "candidate_artist": (getattr(candidate, "artist", "") or "").strip(),
        "candidate_title": (getattr(candidate, "title", "") or "").strip(),
        "candidate_url": getattr(candidate, "url", "") or source_url or "",
        "score": round(float(score), 1),
        "threshold": float(threshold),
        "explanation": explanation or "",
        "blocked_at": _utcnow_iso(),
    }
    await execute(
        "UPDATE jobs SET match_block=?, match_override='', updated_at=? WHERE id=?",
        (json.dumps(block), _utcnow_iso(), job_id),
    )
    logger.info(
        "Job %s blocked at %.1f%% (threshold %.0f%%): wanted '%s - %s', got '%s - %s'",
        job_id, score, threshold, block["wanted_artist"], block["wanted_title"],
        block["candidate_artist"], block["candidate_title"],
    )
    return block


async def consume_override(job_id: int) -> bool:
    """Claim a pending accept for this job, if there is one. Returns True if used.

    Read-and-clear in a single UPDATE, so the claim cannot race: two attempts
    on the same job can never both spend one user decision. `rowcount` is 1
    only for the caller that actually cleared the flag.

    Clearing on read is deliberate. If the flag survived, it would sit on the
    job forever and silently waive the gate on every future attempt - including
    attempts triggered by a source fallback or a rate-limit retry that the user
    never saw. One decision buys one download.
    """
    cursor = await execute(
        "UPDATE jobs SET match_override='', updated_at=? "
        "WHERE id=? AND match_override=?",
        (_utcnow_iso(), job_id, OVERRIDE_ACCEPT),
    )
    return bool(cursor.rowcount)


async def get_block(job_id: int) -> dict | None:
    """The stored block for a job, or None if it has never been blocked."""
    row = await fetch_one("SELECT match_block FROM jobs WHERE id=?", (job_id,))
    if not row or not (row.get("match_block") or "").strip():
        return None
    try:
        return json.loads(row["match_block"])
    except (ValueError, TypeError):
        logger.warning("Job %s has an unreadable match_block; treating as none", job_id)
        return None


async def clear_override(job_id: int) -> None:
    """Drop an accept that was never spent.

    The gate clears the flag when it honours it, but an attempt can end before
    ever reaching the gate - a resolve error, say. If the job then dies, a
    waiver from a decision about a *candidate* would still be sitting on a dead
    job, and a later manual Retry would be waved through by a decision nobody
    gave for that attempt. Spending an accept on a job that never got as far
    as the match check has to mean it is gone.
    """
    await execute(
        "UPDATE jobs SET match_override='', updated_at=? "
        "WHERE id=? AND match_override=?",
        (_utcnow_iso(), job_id, OVERRIDE_ACCEPT),
    )


async def clear_block(job_id: int) -> None:
    """Forget the block once the user has acted on it."""
    await execute(
        "UPDATE jobs SET match_block='', updated_at=? WHERE id=?",
        (_utcnow_iso(), job_id),
    )


# ---------------------------------------------------------------------------
# Applying a decision
# ---------------------------------------------------------------------------

async def _record(job_id: int, decision: str, block: dict, note: str,
                  attention_item_id: int | None = None) -> int:
    cur = await execute(
        "INSERT INTO match_decisions (job_id, attention_item_id, decision,"
        " wanted_artist, wanted_title, candidate_artist, candidate_title,"
        " score, threshold, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id, attention_item_id, decision,
            block.get("wanted_artist", ""), block.get("wanted_title", ""),
            block.get("candidate_artist", ""), block.get("candidate_title", ""),
            float(block.get("score", 0) or 0), float(block.get("threshold", 0) or 0),
            note or "",
        ),
    )
    return cur.lastrowid


async def accept(job_id: int, note: str = "",
                 attention_item_id: int | None = None) -> dict:
    """Take the blocked candidate anyway and requeue the job.

    This is the only path that lets a below-threshold candidate reach the
    downloader, and it does so by setting a flag the gate consumes once.
    """
    block = await get_block(job_id)
    if block is None:
        raise MatchDecisionError(f"Job {job_id} has no blocked match to accept")

    decision_id = await _record(job_id, DECISION_ACCEPT, block, note,
                                attention_item_id)
    await execute(
        "UPDATE jobs SET match_override=?, match_block='', status='pending',"
        " error='', retries=0, not_before=NULL, updated_at=? WHERE id=?",
        (OVERRIDE_ACCEPT, _utcnow_iso(), job_id),
    )
    logger.info(
        "Job %s: user accepted a %.1f%% match (threshold %.0f%%)",
        job_id, float(block.get("score", 0) or 0), float(block.get("threshold", 0) or 0),
    )
    return {
        "status": "accepted",
        "job_id": job_id,
        "decision_id": decision_id,
        "score": block.get("score"),
        "threshold": block.get("threshold"),
    }


async def reject(job_id: int, note: str = "",
                 attention_item_id: int | None = None) -> dict:
    """Stop here: the candidate is wrong and the job should not run again.

    Note there is no `match_override` written. The job simply goes to
    `cancelled`, which no worker picks up, so rejecting is the state that
    cannot accidentally download.
    """
    block = await get_block(job_id)
    if block is None:
        raise MatchDecisionError(f"Job {job_id} has no blocked match to reject")

    decision_id = await _record(job_id, DECISION_REJECT, block, note,
                                attention_item_id)
    await execute(
        "UPDATE jobs SET status='cancelled', match_override=?, match_block='',"
        " error='Rejected by user: match did not match the request',"
        " updated_at=? WHERE id=?",
        (OVERRIDE_REJECTED, _utcnow_iso(), job_id),
    )
    logger.info("Job %s: user rejected the blocked match", job_id)
    return {
        "status": "rejected",
        "job_id": job_id,
        "decision_id": decision_id,
        "score": block.get("score"),
        "threshold": block.get("threshold"),
    }


async def repoint(job_id: int, *, artist: str = "", title: str = "",
                  album: str = "", note: str = "",
                  attention_item_id: int | None = None) -> dict:
    """The request was wrong. Correct it and search again, gate still armed.

    No override is written. This is the whole difference from `accept`: a
    re-point goes back through the matcher at the normal threshold, so a wrong
    correction blocks again rather than being waved through.
    """
    block = await get_block(job_id)
    if block is None:
        raise MatchDecisionError(f"Job {job_id} has no blocked match to re-point")

    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist and not title:
        raise MatchDecisionError(
            "A re-point needs an artist or a title - otherwise the search "
            "would be no more specific than the request that just failed"
        )

    job = await fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise MatchDecisionError(f"Job {job_id} not found")

    # Fall back to what was wanted before, so a user who fixes only the title
    # does not silently lose the artist.
    new_artist = artist or (block.get("wanted_artist") or job.get("artist") or "")
    new_title = title or (block.get("wanted_title") or job.get("title") or "")

    decision_id = await _record(job_id, DECISION_REPOINT, block,
                                (note or f"re-pointed to {new_artist} - {new_title}"),
                                attention_item_id)

    # Rebuild the search URL from the corrected intent so the source is asked
    # the new question. A direct URL has no query to correct, so it stays put
    # and only the intent changes - the gate still judges it.
    from . import source_registry

    source_url = job.get("source_url") or ""
    if source_registry.is_search_job_url(source_url):
        # Stay on the source that produced this block, so a re-point is a
        # better question to the same place rather than a reroute. Falls back
        # to the head of source_priority if that source has since been removed
        # from settings.
        order = source_registry.normalize_priority(
            _settings().get("source_priority")
        )
        current = source_registry.search_source_of(source_url)
        key = current if current in order else (order[0] if order else "youtube")
        source_url = source_registry.build_search_job_url(
            key, new_artist, new_title
        )

    await execute(
        "UPDATE jobs SET title=?, artist=?, album=?, source_url=?, query=?,"
        " match_override=?, match_block='', status='pending', error='',"
        " retries=0, not_before=NULL, updated_at=? WHERE id=?",
        (new_title, new_artist, (album or job.get("album") or ""), source_url,
         f"{new_artist} {new_title}".strip(),
         OVERRIDE_REPOINTED, _utcnow_iso(), job_id),
    )
    logger.info(
        "Job %s: user re-pointed '%s - %s' to '%s - %s' (gate still armed)",
        job_id, block.get("wanted_artist"), block.get("wanted_title"),
        new_artist, new_title,
    )
    return {
        "status": "repointed",
        "job_id": job_id,
        "decision_id": decision_id,
        "artist": new_artist,
        "title": new_title,
        "source_url": source_url,
        # Reported so every decision's audit note can name the score it was
        # made against, not just accept and reject.
        "score": block.get("score"),
        "threshold": block.get("threshold"),
    }


def _settings() -> dict:
    from .. import config
    return config.get_all()


async def history(job_id: int | None = None, limit: int = 50) -> list[dict]:
    """Recorded decisions, newest first. The audit trail for an override."""
    sql = "SELECT * FROM match_decisions"
    params: list = []
    if job_id is not None:
        sql += " WHERE job_id=?"
        params.append(job_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return await fetch_all(sql, tuple(params))
