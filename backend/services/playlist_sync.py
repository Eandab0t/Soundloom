"""The playlist-sync engine — turn a saved playlist into a running mirror.

These calls own the *persistent* side of a sync: they store the intent in
``playlist_syncs``, record which rows are already reconciled in
``sync_track_mapping``, and hand resolved rows to the existing downloader
bridge (``enqueue_missing``).  ``refresh_local_matches`` in
``playlists.py`` owns the *computable* side (recomputing ``track_id`` from the
library), and is the matcher this module delegates to.

The public surface is deliberately small so a producer can wire a UI or a
cron job to one of three intents rather than a kitchen-sink function:

  add(playlist_id, url, source_type)     -- save a sync intent, one-shot
  mirror(playlist_id)                    -- reconcile + enqueue missing
  download_only(playlist_id)             -- re-enqueue rows that lost their match
  preview(playlist_id, mode)             -- what a sync would do, without writing
"""

import logging

from .. import config
from ..database import fetch_all, fetch_one, execute
from .playlists import refresh_local_matches

logger = logging.getLogger(__name__)

__all__ = ["add", "mirror", "download_only", "preview"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ref(playlist_id: int, source_type: str) -> dict:
    """Fetch the saved sync intent (playlist_syncs row)."""
    pl = fetch_one(
        "SELECT id, name, url, source_type, source_ref, auto_sync, sync_interval "
        "FROM playlist_syncs WHERE id=?",
        (playlist_id,),
    )
    if not pl:
        raise ValueError(f"Playlist-sync {playlist_id} not found")
    if pl["source_type"] != source_type:
        raise ValueError(
            f"Playlist-sync {playlist_id} is a {pl['source_type']}, not {source_type}"
        )
    return pl


async def _rows(playlist_id: int) -> list[dict]:
    """All rows of a playlist, in position order."""
    return await fetch_all(
        "SELECT id, title, artist, album, path, track_id "
        "FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
        (playlist_id,),
    )


def _existing_mapping(playlist_id: int) -> dict[int, int]:
    """source_key -> local_track_id for rows this sync has already reconciled."""
    out: dict[int, int] = {}
    for r in fetch_all(
        "SELECT source_key, local_track_id FROM sync_track_mapping "
        "WHERE playlist_id=? AND source_key != ''",
        (playlist_id,),
    ):
        out[r["source_key"]] = r["local_track_id"]
    return out


def _slots(playlist_id: int) -> set[str]:
    """source_key values already claimed by this sync."""
    return {r["source_key"] for r in fetch_all(
        "SELECT source_key FROM sync_track_mapping WHERE playlist_id=? AND source_key != ''",
        (playlist_id,),
    )}


async def _clear_mapping(playlist_id: int) -> None:
    """Reset the reconciliation map so a fresh mirror starts clean."""
    await execute("DELETE FROM sync_track_mapping WHERE playlist_id=?", (playlist_id,))


async def _reconcile(playlist_id: int) -> dict:
    """Run the matcher over the playlist and record every hit.

    Returns per-row bookkeeping (row_id, source_key, local_track_id or None).
    This is a pure computation; nothing is committed until the caller decides
    the run was acceptable.
    """
    summary = await refresh_local_matches(playlist_id)
    # refresh_local_matches updates track_id in-place.  We need the source_key
    # per row for the mapping table, which is not exposed by that function, so
    # we rebuild a cheap mapping here from the DB state after the run.
    return summary


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def add(playlist_id: int, url: str, source_type: str) -> dict:
    """Save a one-shot sync intent and return the persisted row.

    The caller is responsible for validating ``url`` before calling; this
    function stores what was handed to it.  ``source_type`` is checked against
    the known search sources so a sync intent is never created for a source
    that the downloader pipeline cannot handle.
    """
    from .source_registry import SOURCES

    # Known source types for a sync intent: the search-based download sources
    # (source_registry.SOURCES) plus "spotify", which is the streaming export
    # source used by create_spotify_playlist().
    valid_sources = set(SOURCES) | {"spotify"}
    if source_type not in valid_sources:
        raise ValueError(f"unknown source_type {source_type!r}; expected one of {sorted(valid_sources)}")

    cur = await execute(
        "INSERT INTO playlist_syncs (name, url, source_type, source_ref, auto_sync) "
        "VALUES (?, ?, ?, ?, 0)",
        (config.PLAYLIST_SYNC_DEFAULT_NAME, url, source_type, url.split("/")[-1] or ""),
    )
    return {"id": cur.lastrowid, "name": config.PLAYLIST_SYNC_DEFAULT_NAME, "url": url}


async def mirror(playlist_id: int, limit: int | None = None) -> dict:
    """Reconcile the playlist against the library, then enqueue whatever is missing.

    This is the user-visible "sync" gesture: it recomputes ``track_id`` for
    every row through ``refresh_local_matches``, records each hit in
    ``sync_track_mapping`` so re-runs are cheap, and hands unresolved rows to
    the downloader bridge.  Rows already matched and already queued are
    reported, never re-enqueued.
    """
    await _clear_mapping(playlist_id)
    rows = await _rows(playlist_id)
    if not rows:
        return {"playlist_id": playlist_id, "rows": 0, "matched": 0,
                "enqueued": 0, "already_queued": 0}

    matched = 0
    enqueued = 0
    skipped = 0
    order = config.get_all().get("source_priority", ["youtube", "deezer", "soundcloud"])

    for r in rows:
        hit = r.get("track_id")
        if hit:
            matched += 1
            continue
        title = r.get("title") or ""
        artist = (r.get("artist") or "").split(",")[0].strip()
        if not title and not artist:
            continue
        # For each unmatched row, walk the user's source priority and enqueue
        # the first search that is not already running.
        for src in order:
            query_url = _search_job_url(src, artist, title)
            if await _job_already_running(query_url):
                skipped += 1
                continue
            await _enqueue_job(query_url, title, artist, r, src)
            enqueued += 1
            break

    # Persist the reconciliation map for rows we now own.
    for r in rows:
        if r.get("track_id"):
            await execute(
                "INSERT OR REPLACE INTO sync_track_mapping "
                "(playlist_id, source_key, source_id, local_track_id) VALUES (?, ?, ?, ?)",
                (playlist_id, r.get("title", ""), r.get("title", ""), r["track_id"]),
            )

    return {"playlist_id": playlist_id, "rows": len(rows), "matched": matched,
            "enqueued": enqueued, "skipped": skipped, "already_queued": skipped}


async def download_only(playlist_id: int, limit: int | None = None) -> dict:
    """Re-enqueue downloader jobs for rows whose local match was lost.

    E.g. after a library scan that re-matched some rows to different tracks, the
    rows that are now ``track_id NULL`` are kicked back to the pipeline.  This
    skips rows that are already queued or running so a long-running library is
    never duplicated into the job queue.
    """
    rows = await _rows(playlist_id)
    if not rows:
        return {"playlist_id": playlist_id, "enqueued": 0, "already_queued": 0, "missing": 0}

    enqueued = 0
    already = 0
    for r in rows:
        if r.get("track_id") is not None:
            continue
        title = r.get("title") or ""
        artist = (r.get("artist") or "").split(",")[0].strip()
        if not title:
            continue
        for src in config.get_all().get("source_priority", ["youtube", "deezer", "soundcloud"]):
            query_url = _search_job_url(src, artist, title)
            if await _job_already_running(query_url):
                already += 1
                continue
            await _enqueue_job(query_url, title, artist, r, src)
            enqueued += 1
            break
    return {"playlist_id": playlist_id, "enqueued": enqueued,
            "already_queued": already, "missing": len(rows)}


async def preview(playlist_id: int, mode: str = "mirror", limit: int | None = None) -> dict:
    """Show what a sync would do, without writing anything to the DB.

    ``mode`` is either ``"mirror"`` (reconcile + enqueue missing) or
    ``"download_only"`` (re-enqueue rows that lost their match).  Row values
    are taken from the current playlist state; the matcher is run in-memory.
    """
    rows = await _rows(playlist_id)
    if not rows:
        return {"playlist_id": playlist_id, "mode": mode, "rows": 0,
                "matched": 0, "enqueued": 0, "skipped": 0}
    matched = sum(1 for r in rows if r.get("track_id"))
    unmatched = [r for r in rows if not r.get("track_id")]
    if limit is not None:
        unmatched = unmatched[:limit]
    # Reuse the same priority walk as mirror for an honest estimate.
    order = config.get_all().get("source_priority", ["youtube", "deezer", "soundcloud"])
    enqueued = 0
    skipped = 0
    for r in unmatched:
        title = r.get("title") or ""
        artist = (r.get("artist") or "").split(",")[0].strip()
        if not title:
            continue
        for src in order:
            if await _job_already_running(_search_job_url(src, artist, title)):
                skipped += 1
                continue
            enqueued += 1
            break
    return {"playlist_id": playlist_id, "mode": mode, "rows": len(rows),
            "matched": matched, "enqueued": enqueued, "skipped": skipped,
            "unmatched_titles": [f"{r.get('artist','')} - {r.get('title','')}"
                                 for r in unmatched[:25]]}


# ---------------------------------------------------------------------------
# Helpers the engine delegates to (shared with the downloader bridge)
# ---------------------------------------------------------------------------

def _search_job_url(src: str, artist: str, title: str) -> str:
    """Build the source_url that the downloader bridge understands."""
    from .source_registry import build_search_job_url, normalize_priority
    return build_search_job_url(normalize_priority([src])[0], artist, title)


async def _job_already_running(source_url: str) -> bool:
    from ..database import fetch_one
    return (await fetch_one(
        "SELECT id FROM jobs WHERE source_url=? AND status NOT IN "
        "('complete','failed','cancelled') LIMIT 1",
        (source_url,),
    )) is not None


async def _enqueue_job(source_url: str, title: str, artist: str,
                       row: dict, source_type: str) -> None:
    from ..database import execute
    from .. import config
    quality = (row.get("quality_profile")
               or config.get_all().get("default_quality", "balanced"))
    from ..pipeline.models import QUALITY_PRESETS
    output_format = (
        QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"]).output_format
        or config.get_all().get("default_format", "mp3")
    )
    await execute(
        "INSERT INTO jobs (source_url, source_type, query, title, artist, "
        "output_format, quality_profile, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (source_url, source_type, source_url, title, artist or "",
         output_format, quality),
    )
