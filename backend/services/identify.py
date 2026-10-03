"""Identify: propose better metadata for library tracks, for review.

The download pipeline can only tag files it just fetched. This is the other
half - finding correct tags for files already on disk, including ones whose
current tags are wrong or absent.

Flow:

    scan   read candidates from the library, look each one up, store the
           result in `fetch_candidates` with its confidence
    review the user compares old vs new in the UI and applies or rejects
    apply  write tags to the file, update the tracks row, and mark done

Nothing is written to any audio file during `scan`. `apply` is the only
destructive step, and it backs up first.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .. import config
from ..database import execute, fetch_all, fetch_one
from ..events import emit
from ..pipeline.models import TrackMetadata
from ..sources.metadata import FetchResult, describe_methods, get_fetcher, pick_fetcher

logger = logging.getLogger(__name__)

# Fields a fetcher may contribute. `file_path` and identity columns are
# never touched - this is about tags only.
TAG_FIELDS = (
    "title", "artist", "primary_artist", "featured_artists", "display_artist",
    "album_artist", "album", "track_number", "year", "genre",
)

_scan_lock = asyncio.Lock()


def _row_to_metadata(row: dict) -> TrackMetadata:
    return TrackMetadata(
        title=row.get("title") or "",
        artist=row.get("artist") or "",
        primary_artist=row.get("primary_artist") or "",
        featured_artists=[a for a in (row.get("featured_artists") or "").split(",") if a],
        display_artist=row.get("display_artist") or "",
        album_artist=row.get("album_artist") or "",
        album=row.get("album") or "",
        track_number=int(row.get("track_number") or 0),
        year=int(row.get("year") or 0),
        genre=row.get("genre") or "",
        duration=float(row.get("duration") or 0),
    )


def _current_tags(row: dict) -> dict:
    return {f: row.get(f) for f in TAG_FIELDS}


def _diff(old: dict, new: dict) -> dict:
    """Only the fields that actually differ, for compact display.

    Fields the fetcher left empty are omitted: apply never writes an empty
    value, so showing "genre: Music -> (blank)" in the review list would
    advertise a change that does not happen.
    """
    out = {}
    for key in TAG_FIELDS:
        before = old.get(key) or ""
        after = new.get(key) or ""
        if not after:
            continue
        if str(before) != str(after):
            out[key] = {"before": before, "after": after}
    return out


async def _pick_candidate_tracks(settings: dict, limit: int,
                                scope: str = "auto") -> list[dict]:
    """Choose which library rows to look up.

    `auto` targets the tracks most likely to be wrong: those missing tags
    first. `all` samples the whole library so wrong tags can be fixed too.
    """
    if scope == "all":
        return await fetch_all(
            "SELECT * FROM tracks WHERE file_status != 'missing' "
            "ORDER BY id ASC LIMIT ?",
            (limit,),
        )

    rows = await fetch_all(
        """
        SELECT * FROM tracks
         WHERE file_status != 'missing'
           AND (COALESCE(title,'') = '' OR COALESCE(artist,'') = ''
                OR COALESCE(album,'')  = '' OR COALESCE(album_artist,'') = '')
         ORDER BY COALESCE(album_artist, artist, title) ASC
         LIMIT ?
        """,
        (limit,),
    )
    if rows:
        return rows

    # Nothing untagged - fall back to a sample so the user can still use
    # this to correct wrong tags, not just missing ones.
    return await fetch_all(
        "SELECT * FROM tracks WHERE file_status != 'missing' "
        "ORDER BY RANDOM() LIMIT ?",
        (limit,),
    )


def methods_status(settings: Optional[dict] = None) -> list[dict]:
    return describe_methods(settings or config.get_all())


async def scan(limit: int = 25, scope: str = "auto",
               method: str = "") -> dict:
    """Look up candidate tracks and store proposals for review.

    Returns a summary. Never writes to audio files.
    """
    settings = config.get_all()
    limit = max(1, min(int(limit or 25), 200))
    min_confidence = float(settings.get("fetch_confidence", 0.7))
    preferred = method or settings.get("fetch_method", "")

    fetcher = pick_fetcher(settings, preferred)
    if not fetcher.available():
        raise RuntimeError(
            f"No metadata source is usable: {fetcher.unavailable_reason()}"
        )

    async with _scan_lock:
        rows = await _pick_candidate_tracks(settings, limit, scope)
        logger.info(f"Identify: looking up {len(rows)} track(s) via {fetcher.name}")

        matched = 0
        unmatched = 0
        for row in rows:
            existing = await fetch_one(
                "SELECT id FROM fetch_candidates WHERE track_id=? AND status='pending'",
                (row["id"],),
            )
            if existing:
                continue

            track = _row_to_metadata(row)
            try:
                result: FetchResult = await fetcher.fetch(track, row.get("file_path", ""))
            except Exception as exc:
                logger.warning(f"Identify: lookup failed for track {row['id']}: {exc}")
                result = FetchResult(matched=False, source=fetcher.name,
                                     notes=[f"Lookup error: {exc}"])

            if not result.matched or not result.tags:
                unmatched += 1
                continue

            if result.confidence < min_confidence:
                unmatched += 1
                continue

            new_tags = {f: getattr(result.tags, f, "") for f in TAG_FIELDS}
            diff = _diff(_current_tags(row), new_tags)
            if not diff:
                unmatched += 1
                continue

            await execute(
                """
                INSERT INTO fetch_candidates
                    (track_id, file_path, source, confidence, current_tags,
                     proposed_tags, diff, notes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (row["id"], row.get("file_path", ""), result.source,
                 result.confidence,
                 json.dumps(_current_tags(row), ensure_ascii=False),
                 json.dumps(new_tags, ensure_ascii=False),
                 json.dumps(diff, ensure_ascii=False),
                 " | ".join(result.notes)),
            )
            matched += 1

    await emit("identify_update", {"scanned": len(rows), "matched": matched})
    return {
        "scanned": len(rows),
        "matched": matched,
        "unmatched": unmatched,
        "method": fetcher.name,
    }


async def list_candidates(status: str = "pending", limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM fetch_candidates"
    params: tuple = ()
    if status and status != "all":
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY confidence DESC, id ASC LIMIT ?"
    params = params + (limit,)

    rows = await fetch_all(sql, params)
    for r in rows:
        for key in ("current_tags", "proposed_tags", "diff"):
            try:
                r[key] = json.loads(r.get(key) or "{}")
            except (ValueError, TypeError):
                r[key] = {}
    return rows


async def apply_candidate(candidate_id: int) -> dict:
    """Write one approved proposal to disk. This is the destructive step."""
    from .backup import backup_tracks
    from .tagger import read_tags, write_tags

    row = await fetch_one("SELECT * FROM fetch_candidates WHERE id=?", (candidate_id,))
    if not row:
        raise LookupError(f"No such candidate: {candidate_id}")
    if row["status"] == "applied":
        return {"id": candidate_id, "status": "already_applied"}

    try:
        proposed = json.loads(row.get("proposed_tags") or "{}")
    except (ValueError, TypeError):
        proposed = {}

    file_path = row.get("file_path") or ""
    settings = config.get_all()
    overwrite = settings.get("fetch_overwrite", False)

    tags = await read_tags(file_path)
    changes: dict[str, Any] = {}
    for key, value in proposed.items():
        if not value:
            continue
        if not overwrite and tags.get(key):
            continue  # keep what is already there
        if str(tags.get(key) or "") != str(value):
            changes[key] = value

    if not changes:
        await execute(
            "UPDATE fetch_candidates SET status='skipped' WHERE id=?", (candidate_id,)
        )
        return {"id": candidate_id, "status": "skipped", "reason": "nothing to change"}

    # Back up before touching the file.
    track_row = await fetch_one("SELECT * FROM tracks WHERE id=?", (row["track_id"],))
    backup = backup_tracks([track_row] if track_row else [], settings)

    merged = {**tags, **changes}
    await write_tags(file_path, merged, cover_path=tags.get("cover_art_path") or None)

    assignments = ", ".join(f"{k}=?" for k in changes)
    await execute(
        f"UPDATE tracks SET {assignments}, updated_at=datetime('now') WHERE id=?",
        tuple(list(changes.values()) + [row["track_id"]]),
    )
    await execute(
        "UPDATE fetch_candidates SET status='applied', applied_at=datetime('now') WHERE id=?",
        (candidate_id,),
    )
    await emit("library_change", {"action": "updated", "track_id": row["track_id"]})
    logger.info(f"Identify: applied candidate {candidate_id} to {file_path} ({len(changes)} field(s))")
    return {
        "id": candidate_id,
        "status": "applied",
        "fields": list(changes),
        "backup": backup,
        "file_path": file_path,
    }


async def reject_candidate(candidate_id: int) -> dict:
    await execute(
        "UPDATE fetch_candidates SET status='rejected' WHERE id=?", (candidate_id,)
    )
    return {"id": candidate_id, "status": "rejected"}


async def apply_all(limit: int = 100) -> dict:
    """Apply every pending candidate above the confidence threshold."""
    settings = config.get_all()
    threshold = float(settings.get("fetch_confidence", 0.7))
    rows = await list_candidates("pending", limit)

    applied, skipped, failed = 0, 0, 0
    for row in rows:
        if (row.get("confidence") or 0) < threshold:
            skipped += 1
            continue
        try:
            result = await apply_candidate(row["id"])
            if result["status"] == "applied":
                applied += 1
            else:
                skipped += 1
        except Exception as exc:
            failed += 1
            logger.warning(f"Identify: apply failed for candidate {row['id']}: {exc}")

    return {"applied": applied, "skipped": skipped, "failed": failed, "threshold": threshold}


__all__ = [
    "TAG_FIELDS",
    "methods_status",
    "scan",
    "list_candidates",
    "apply_candidate",
    "apply_all",
    "reject_candidate",
]
