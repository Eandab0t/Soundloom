"""The producers that actually feed Needs Attention.

Each one is a thin adapter: read the system that owns the state, turn its
problems into `AttentionItem`s. Nothing here writes to the library - a
producer only reports, and `attention.run_producers()` owns persistence.

Dedupe keys are namespaced by producer (`<producer>:<kind>:<id>`) so that
auto-resolution - which works per `source` column - can never close another
producer's item that happens to share a key.
"""
import logging
from pathlib import Path

from .. import config
from ..database import fetch_all
from .attention import AttentionItem, Severity, register_producer

logger = logging.getLogger(__name__)

# Item types. Free strings by design; the UI maps them to copy and routes.
TYPE_INTERRUPTED_OPERATION = "interrupted_operation"
TYPE_METADATA_REVIEW = "metadata_review"
TYPE_BLOCKED_DOWNLOAD = "blocked_download"


def _display_name(file_path: str | None) -> str:
    """Filename without its directory or extension, for display fallback."""
    if not file_path:
        return "Unknown track"
    return Path(file_path).stem or "Unknown track"


class InterruptedOperationProducer:
    """Download/file operations that stopped and need retry or discard.

    Bridges the operation journal into the attention layer: recovery records
    *what happened*, this surfaces *what the user has to decide about*.
    """

    name = "operations"

    async def scan(self) -> list[AttentionItem]:
        from .operations import OpState, list_operations

        out: list[AttentionItem] = []
        for state in (OpState.FAILED.value, OpState.ABANDONED.value):
            for op in await list_operations(state=state, limit=200):
                where = op.get("final_path") or op.get("staged_path") or ""
                label = op.get("title") or op.get("source_url") or "Untitled"
                artist = op.get("artist") or ""

                out.append(AttentionItem(
                    dedupe_key=f"{self.name}:op:{op['id']}",
                    type=TYPE_INTERRUPTED_OPERATION,
                    severity=(
                        Severity.HIGH if state == OpState.FAILED.value
                        else Severity.MEDIUM
                    ),
                    title=f"Interrupted: {label}",
                    description=(
                        f"{artist} - stopped at '{state}'. "
                        + (op.get("error") or "No further detail recorded.")
                    ),
                    entity_type="operation",
                    entity_id=op["id"],
                    action={
                        "kind": "operation",
                        "route": f"/operations/{op['id']}",
                        "label": "Review operation",
                        "retry_route": f"/api/operations/{op['id']}/retry",
                        "retry_label": "Retry",
                        "discard_route": f"/api/operations/{op['id']}/discard",
                        "discard_label": "Discard",
                        "where": where,
                    },
                ))
        return out


class MetadataReviewProducer:
    """Tracks whose fetched metadata was too weak to apply automatically.

    Only candidates *below* `fetch_confidence` are raised: the ones above it
    get applied on their own, so reporting them would be noise. The action
    points at the matching screen, where the user picks a candidate.
    """

    name = "metadata"

    async def scan(self) -> list[AttentionItem]:
        threshold = float(config.get("fetch_confidence", 0.7))
        rows = await fetch_all(
            "SELECT c.id, c.track_id, c.file_path, c.confidence, c.source, "
            "       t.title, t.artist "
            "FROM fetch_candidates c "
            "LEFT JOIN tracks t ON t.id = c.track_id "
            "WHERE c.status='pending' AND c.confidence < ? "
            "ORDER BY c.confidence ASC, c.id ASC",
            (threshold,),
        )

        out: list[AttentionItem] = []
        for row in rows:
            title = row.get("title") or _display_name(row.get("file_path"))
            artist = row.get("artist") or ""
            pct = round((row.get("confidence") or 0) * 100)
            out.append(AttentionItem(
                dedupe_key=f"{self.name}:candidate:{row['id']}",
                type=TYPE_METADATA_REVIEW,
                # A near-miss is still a miss; the further below the line, the
                # louder it should be.
                severity=(Severity.HIGH if pct < 50 else Severity.MEDIUM),
                title=f"Needs identification: {title}",
                description=(
                    f"{artist} - best {row.get('source') or 'source'} match "
                    f"scored {pct}%, under the {round(threshold * 100)}% threshold."
                ),
                entity_type="track",
                entity_id=row.get("track_id"),
                action={
                    "kind": "match",
                    "route": f"/identify?track_id={row.get('track_id')}",
                    "label": "Choose correct tags",
                },
            ))
        return out


class BlockedMatchProducer:
    """Downloads the match gate refused, waiting on a human decision.

    A blocked job is *not* simply a failed job. It is a job that was right
    about everything except the one thing it was for: the gate compared what
    the user asked for against what the source returned and did not agree
    with the answer. That is a question for the user, not a retry.

    The decision routes live in `action` so the frontend renders whatever the
    backend decided is available, instead of hardcoding "if type is X show Y".

    Scoping to failed jobs is what keeps this honest. `_process_job` re-arms
    the gate on every attempt and reroutes to the next source on failure, so
    the gate can legitimately fire several times before a job gives up.
    Reporting on the moment it fires would raise an item for a job that then
    went on to succeed three seconds later. Only a job that actually ended
    blocked is worth someone's attention - and because a job that is no longer
    failed stops being reported, `run_producers()` retires the row by itself.
    """

    name = "matches"

    async def scan(self) -> list[AttentionItem]:
        from . import match_decisions

        rows = await fetch_all(
            "SELECT id, title, artist, album, error, match_block "
            "FROM jobs "
            # Terminal states only: 'failed' is a blocked job awaiting a
            # decision. 'cancelled' is what accept/reject/re-point leave
            # behind once the user has acted, so listing it here too would
            # immediately re-open the item the user just closed.
            "WHERE status='failed' AND match_block IS NOT NULL"
            "  AND match_block <> ''",
        )

        out: list[AttentionItem] = []
        for row in rows:
            block = _parse_block(row.get("match_block"))
            if not block:
                continue
            wanted = " - ".join(
                p for p in (block.get("wanted_artist", ""),
                            block.get("wanted_title", "")) if p
            ) or "the requested track"
            found = " - ".join(
                p for p in (block.get("candidate_artist", ""),
                            block.get("candidate_title", "")) if p
            ) or "something else"

            score = float(block.get("score", 0) or 0)
            threshold = float(block.get("threshold", 0) or 0)

            out.append(AttentionItem(
                dedupe_key=f"{self.name}:job:{row['id']}",
                type=TYPE_BLOCKED_DOWNLOAD,
                # The further under the line, the more certain the gate is that
                # this is wrong - which is exactly when a human should look.
                severity=(
                    Severity.HIGH if score < threshold * 0.6
                    else Severity.MEDIUM
                ),
                title=f"Match blocked: {wanted}",
                description=(
                    f"Scored {score}% against a {threshold:g}% threshold. "
                    f"The source returned \"{found}\". "
                    + (block.get("explanation") or "")
                ).strip(),
                entity_type="job",
                entity_id=row["id"],
                action={
                    "kind": "blocked_download",
                    "job_id": row["id"],
                    "score": score,
                    "threshold": threshold,
                    "wanted_artist": block.get("wanted_artist", ""),
                    "wanted_title": block.get("wanted_title", ""),
                    "candidate_artist": block.get("candidate_artist", ""),
                    "candidate_title": block.get("candidate_title", ""),
                    "explanation": block.get("explanation", ""),
                    "accept_route": f"/api/matches/{row['id']}/accept",
                    "reject_route": f"/api/matches/{row['id']}/reject",
                    "repoint_route": f"/api/matches/{row['id']}/repoint",
                },
            ))
        return out


def _parse_block(raw):
    """A job's match_block column, or None if it is absent or unreadable.

    Tolerant on purpose: a producer that raises on one malformed row would
    lose every other blocked download's attention item too.
    """
    if not raw:
        return None
    try:
        import json
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Skipping a job with an unreadable match_block")
        return None
    return parsed if isinstance(parsed, dict) else None


def register_default_producers() -> list:
    """Register the producers that ship with Soundloom.

    Called once at startup. New background systems add themselves here.
    """
    return [
        register_producer(InterruptedOperationProducer()),
        register_producer(MetadataReviewProducer()),
        register_producer(BlockedMatchProducer()),
    ]
