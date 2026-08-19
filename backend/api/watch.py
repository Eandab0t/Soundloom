"""Watch / artist follow endpoints."""
from fastapi import APIRouter, HTTPException
from ..database import fetch_all, fetch_one, execute

router = APIRouter(prefix="/api/watch", tags=["watch"])


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
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        await execute(f"UPDATE watched_artists SET {set_clause} WHERE id=?", tuple(list(updates.values()) + [watch_id]))
    return {"status": "updated"}


@router.delete("/{watch_id}")
async def delete_watched(watch_id: int):
    await execute("DELETE FROM watched_artists WHERE id=?", (watch_id,))
    return {"status": "deleted"}


@router.post("/{watch_id}/check")
async def check_watched(watch_id: int):
    return {"status": "checked", "new_releases": 0}


@router.post("/check-all")
async def check_all_watched():
    return {"status": "checked", "artists": 0, "new_releases": 0}
