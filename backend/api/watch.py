"""Watch / artist follow endpoints."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from ..database import fetch_all, execute, fetch_one

router = APIRouter(prefix="/api/watch", tags=["watch"])
logger = logging.getLogger(__name__)


@router.get("")
async def list_watched():
    return await fetch_all("SELECT * FROM watched_artists ORDER BY artist_name ASC")


@router.post("")
async def add_watched(data: dict):
    name = data.get("artist_name", "").strip()
    if not name:
        raise HTTPException(400, "artist_name required")
    try:
        await execute(
            "INSERT INTO watched_artists (artist_name, auto_download, quality_profile) VALUES (?, ?, ?)",
            (name, data.get("auto_download", 0), data.get("quality_profile", "balanced"))
        )
        return {"status": "added"}
    except Exception:
        return {"status": "already_watched"}


@router.put("/{watch_id}")
async def update_watched(watch_id: int, data: dict):
    allowed = {"auto_download", "quality_profile"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return {"status": "updated"}

    # Turning auto-download on should pick up anything released since the last
    # check, so forget what we have already seen. The backfill window in the
    # poller keeps this bounded on the next pass.
    if "auto_download" in updates and updates["auto_download"]:
        await execute(
            "UPDATE watched_artists SET known_releases='[]' WHERE id=?", (watch_id,)
        )
        logger.info(f"Watch {watch_id}: auto-download enabled, will re-scan releases")

    set_clause = ", ".join(f"{k}=?" for k in updates)
    await execute(
        f"UPDATE watched_artists SET {set_clause} WHERE id=?",
        tuple(list(updates.values()) + [watch_id]),
    )
    return {"status": "updated"}


@router.delete("/{watch_id}")
async def delete_watched(watch_id: int):
    await execute("DELETE FROM watched_artists WHERE id=?", (watch_id,))
    return {"status": "deleted"}


@router.post("/check")
async def check_now():
    """Run a polling pass immediately instead of waiting for the timer."""
    from ..services.watcher import poll_once

    try:
        enqueued = await poll_once(force=True)
    except Exception as e:
        logger.error(f"Manual watch check failed: {e}", exc_info=True)
        raise HTTPException(500, f"Watch check failed: {e}")
    return {"status": "checked", "enqueued": enqueued}


@router.post("/{watch_id}/check")
async def check_one(watch_id: int):
    """Re-check a single artist without touching the others."""
    from ..services.watcher import check_artist, DeezerWatchClient
    from .. import config
    import aiohttp

    row = await fetch_one("SELECT * FROM watched_artists WHERE id=?", (watch_id,))
    if not row:
        raise HTTPException(404, "Watch entry not found")

    settings = config.get_all()
    try:
        async with aiohttp.ClientSession() as session:
            client = DeezerWatchClient(session)
            enqueued = await check_artist(client, row, settings)
    except Exception as e:
        logger.warning(f"Manual check failed for watch {watch_id}: {e}")
        raise HTTPException(500, f"Check failed: {e}")
    return {"status": "checked", "enqueued": enqueued}
