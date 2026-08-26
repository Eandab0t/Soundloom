"""Library API endpoints."""
from fastapi import APIRouter, HTTPException, Query
from ..database import fetch_all, fetch_one, execute
from ..services.scanner import scan_folder, get_scan_status

router = APIRouter(prefix="/api/library", tags=["library"])

ALLOWED_SORTS = {
    "title", "artist", "album", "album_artist", "year", "duration",
    "format", "created_at", "file_size",
}


@router.get("/scan")
async def trigger_scan():
    import asyncio
    asyncio.create_task(scan_folder())
    return {"status": "started"}


@router.get("/scan/status")
async def scan_status():
    return await get_scan_status()


@router.get("/tracks")
async def list_tracks(
    sort: str = Query("title"),
    order: str = Query("ASC"),
    search: str = "",
    artist: str = "",
    album: str = "",
    format: str = "",
    status: str = "",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    where = []
    params = []
    if search:
        where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s])
    if artist:
        where.append("artist = ?")
        params.append(artist)
    if album:
        where.append("album = ?")
        params.append(album)
    if format:
        where.append("format = ?")
        params.append(format)
    if status:
        where.append("file_status = ?")
        params.append(status)
    else:
        where.append("file_status != 'missing'")

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    safe_sort = sort if sort in ALLOWED_SORTS else "title"
    safe_order = "DESC" if order.upper() == "DESC" else "ASC"

    count_row = await fetch_one(
        f"SELECT COUNT(*) as count FROM tracks{where_clause}", tuple(params)
    )
    total = count_row["count"] if count_row else 0

    rows = await fetch_all(
        f"SELECT * FROM tracks{where_clause} ORDER BY {safe_sort} {safe_order} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return {"tracks": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/tracks/{track_id}")
async def get_track(track_id: int):
    row = await fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
    if not row:
        raise HTTPException(404, "Track not found")
    return row


@router.put("/tracks/{track_id}")
async def update_track(track_id: int, data: dict):
    allowed = {
        "title", "artist", "album_artist", "primary_artist", "display_artist",
        "album", "track_number", "disc_number", "year", "genre",
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields to update")

    track = await fetch_one("SELECT file_path FROM tracks WHERE id=?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")

    file_path = track["file_path"]
    from pathlib import Path
    if Path(file_path).exists():
        from ..services.tagger import write_tags
        tag_data = {}
        row = await fetch_one("SELECT * FROM tracks WHERE id=?", (track_id,))
        if row:
            for k in allowed:
                tag_data[k] = updates.get(k, row.get(k))
        try:
            await write_tags(file_path, tag_data)
        except Exception as e:
            raise HTTPException(500, f"Failed to write tags to file: {e}")

    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [track_id]
    await execute(
        f"UPDATE tracks SET {set_clause}, updated_at=datetime('now') WHERE id=?",
        tuple(vals),
    )
    return {"status": "updated"}


@router.delete("/tracks/{track_id}")
async def delete_track(track_id: int):
    await execute("DELETE FROM tracks WHERE id=?", (track_id,))
    return {"status": "deleted"}


@router.get("/artists")
async def list_artists():
    return await fetch_all("""
        SELECT COALESCE(NULLIF(primary_artist,''), artist) as name,
               COUNT(*) as track_count
        FROM tracks WHERE file_status != 'missing'
        GROUP BY name ORDER BY name ASC
    """)


@router.get("/artists/{artist_name}")
async def get_artist_tracks(artist_name: str):
    return await fetch_all(
        """SELECT * FROM tracks
           WHERE (primary_artist = ? OR artist = ?) AND file_status != 'missing'
           ORDER BY album ASC, track_number ASC""",
        (artist_name, artist_name),
    )


@router.get("/albums")
async def list_albums():
    return await fetch_all("""
        SELECT album as name, album_artist as artist, year,
               COUNT(*) as track_count, MIN(id) as id
        FROM tracks WHERE album != '' AND file_status != 'missing'
        GROUP BY album, album_artist ORDER BY album ASC
    """)


@router.get("/albums/{album_id}")
async def get_album_tracks(album_id: int):
    return await fetch_all(
        """SELECT * FROM tracks
           WHERE album = (SELECT album FROM tracks WHERE id=?)
           AND file_status != 'missing'
           ORDER BY disc_number ASC, track_number ASC""",
        (album_id,),
    )


@router.get("/stats")
async def library_stats():
    total = await fetch_one(
        "SELECT COUNT(*) as c FROM tracks WHERE file_status != 'missing'"
    )
    artists = await fetch_one(
        "SELECT COUNT(DISTINCT COALESCE(NULLIF(primary_artist,''), artist)) as c "
        "FROM tracks WHERE file_status != 'missing'"
    )
    albums = await fetch_one(
        "SELECT COUNT(DISTINCT album) as c FROM tracks "
        "WHERE album != '' AND file_status != 'missing'"
    )
    size = await fetch_one(
        "SELECT COALESCE(SUM(file_size), 0) as c FROM tracks WHERE file_status != 'missing'"
    )
    return {
        "total_tracks": total["c"] if total else 0,
        "total_artists": artists["c"] if artists else 0,
        "total_albums": albums["c"] if albums else 0,
        "total_size": size["c"] if size else 0,
    }
