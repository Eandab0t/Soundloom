"""Playlist sync endpoints (Soundiiz-style)."""
import logging

from fastapi import APIRouter, HTTPException, Query

from .. import config
from ..database import fetch_all, fetch_one, execute
from ..validation import validate_url
from ..services.sync import parse_playlist_url, sync_playlist, poll_once

router = APIRouter(prefix="/api/sync", tags=["sync"])
logger = logging.getLogger(__name__)


@router.get("")
async def list_synced():
    rows = await fetch_all(
        "SELECT id, name, source_type, source_ref, url, auto_sync, sync_interval, "
        "quality_profile, last_synced, last_error, created_at "
        "FROM synced_playlists ORDER BY created_at DESC"
    )
    return {"playlists": rows}


@router.post("")
async def add_synced(data: dict):
    url = (data.get("url") or data.get("source_ref") or "").strip()
    if not url:
        raise HTTPException(400, "Playlist URL is required")
    try:
        validate_url(url)
    except Exception:
        # validate_url may reject platform URLs by design; fall through to
        # parse_playlist_url which understands Spotify/Deezer shapes.
        pass

    try:
        source_type, source_ref = parse_playlist_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    source_ref = source_ref or url
    quality = data.get("quality_profile", "balanced")

    existing = await fetch_one(
        "SELECT id FROM synced_playlists WHERE source_ref=? AND source_type=?",
        (source_ref, source_type),
    )
    if existing:
        raise HTTPException(409, f"Already syncing this playlist (#{existing['id']})")

    cursor = await execute(
        "INSERT INTO synced_playlists (name, source_type, source_ref, url, "
        "auto_sync, sync_interval, quality_profile) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            data.get("name") or f"{source_type} playlist {source_ref[:12]}",
            source_type,
            source_ref,
            url,
            1 if data.get("auto_sync", True) else 0,
            int(data.get("sync_interval") or 0),
            quality,
        ),
    )
    pid = cursor.lastrowid

    enqueued = 0
    if data.get("sync_now", True):
        try:
            result = await sync_playlist(pid)
            enqueued = result["enqueued"]
        except Exception as e:
            logger.warning(f"Initial sync failed for playlist {pid}: {e}")

    return {
        "id": pid,
        "source_type": source_type,
        "source_ref": source_ref,
        "enqueued": enqueued,
    }


@router.delete("/{playlist_id}")
async def remove_synced(playlist_id: int):
    await execute("DELETE FROM synced_playlists WHERE id=?", (playlist_id,))
    return {"status": "deleted"}


@router.put("/{playlist_id}")
async def update_synced(playlist_id: int, data: dict):
    allowed = {"auto_sync", "sync_interval", "quality_profile", "name"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return {"status": "updated"}
    set_clause = ", ".join(f"{k}=?" for k in updates)
    await execute(
        f"UPDATE synced_playlists SET {set_clause} WHERE id=?",
        tuple(list(updates.values()) + [playlist_id]),
    )
    return {"status": "updated"}


@router.post("/{playlist_id}/sync")
async def sync_now(playlist_id: int):
    try:
        result = await sync_playlist(playlist_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Sync failed: {e}")
    return result


@router.post("/sync-all")
async def sync_all():
    rows = await fetch_all("SELECT id FROM synced_playlists")
    results = []
    for row in rows:
        try:
            results.append(await sync_playlist(row["id"]))
        except Exception as e:
            results.append({"playlist_id": row["id"], "error": str(e)})
    return {"results": results}
