"""Library API endpoints."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..database import fetch_all, fetch_one, execute
from ..services.scanner import scan_folder, get_scan_status, reconcile_library
from ..services import acquisition, backfill, content_index

router = APIRouter(prefix="/api/library", tags=["library"])

ALLOWED_SORTS = {
    "title", "artist", "album", "album_artist", "year", "duration",
    "format", "created_at", "file_size",
}

AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma",
}


@router.get("/tracks/{track_id}/audio")
async def stream_track_audio(track_id: int):
    """The audio file itself, for the persistent player.

    FileResponse honours Range requests, which is what lets the player seek
    instead of waiting for a whole track to buffer.
    """
    row = await fetch_one(
        "SELECT file_path, file_status FROM tracks WHERE id = ?", (track_id,))
    if not row:
        raise HTTPException(404, "Track not found")

    path = Path(row["file_path"] or "")
    if row["file_status"] == "missing" or not path.is_file():
        raise HTTPException(404, "Audio file is missing from disk")

    # The path comes out of the database, not the request, so it is not
    # automatically safe: a stale or hand-edited row pointing at C:\ or a
    # network share would otherwise turn this into a read-any-file endpoint.
    # Serve only what lives under the configured library.
    try:
        resolved = path.resolve()
        root = Path(config.get("library_path", "")).resolve()
    except OSError:
        raise HTTPException(404, "Audio file is unreadable")
    if not root.is_dir() or not resolved.is_relative_to(root):
        raise HTTPException(403, "Audio file is outside the configured library")

    media_type = AUDIO_TYPES.get(resolved.suffix.lower(), "audio/mpeg")
    return FileResponse(resolved, media_type=media_type)


@router.get("/tracks/{track_id}/cover")
async def get_track_cover(track_id: int):
    """Cover art for one track — recorded, extracted on demand, or placeholder.

    Always returns an image (the bundled placeholder when the track has no
    art), so the UI can point plain <img> tags at this endpoint.
    """
    from ..services.covers import cover_response
    return await cover_response(track_id)


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
    library_status: str = Query("active"),
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
    if library_status != "all":
        where.append("library_status = ?")
        params.append(library_status)
    if status:
        where.append("file_status = ?")
        params.append(status)
    elif library_status == "active":
        # The active library is the one you can actually play, so a row
        # whose file is gone is left out of it. This exclusion deliberately
        # does not apply when asking for archived or for everything: a
        # missing file that was archived is exactly the row you would want
        # to see when reviewing what you archived.
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
async def list_artists(search: str = Query("", max_length=100)):
    """Distinct library artists with track counts.

    With ?search=, returns a prefix-first, case-insensitive page of matches
    ordered by track count — shaped for autocomplete dropdowns (the frontend
    caps at 8 suggestions). Ordered by count so the artist you most likely
    mean tops the list; empty libraries return [].
    """
    q = (search or "").strip()
    if q:
        like = f"%{q}%"
        return await fetch_all(
            """
            SELECT name, SUM(track_count) as track_count, MIN(cover_id) as cover_id FROM (
                SELECT COALESCE(NULLIF(primary_artist,''), artist) as name,
                       COUNT(*) as track_count, MIN(id) as cover_id
                FROM tracks WHERE file_status != 'missing'
                GROUP BY COALESCE(NULLIF(primary_artist,''), artist)
            )
            WHERE name != '' AND (name LIKE ? COLLATE NOCASE
                                  OR name LIKE ? COLLATE NOCASE)
            GROUP BY name
            ORDER BY track_count DESC, name ASC
            LIMIT 20
            """,
            (like, like + "%"),
        )
    return await fetch_all("""
        SELECT COALESCE(NULLIF(primary_artist,''), artist) as name,
               COUNT(*) as track_count, MIN(id) as cover_id
        FROM tracks
        WHERE file_status != 'missing' AND library_status != 'archived'
        GROUP BY name ORDER BY name COLLATE NOCASE ASC
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
        GROUP BY album, album_artist ORDER BY album COLLATE NOCASE ASC
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/stats")
async def library_stats():
    total = await fetch_one(
        "SELECT COUNT(*) as c FROM tracks "
        "WHERE file_status != 'missing' AND library_status != 'archived'"
    )
    artists = await fetch_one(
        "SELECT COUNT(DISTINCT COALESCE(NULLIF(primary_artist,''), artist)) as c "
        "FROM tracks "
        "WHERE file_status != 'missing' AND library_status != 'archived'"
    )
    albums = await fetch_one(
        "SELECT COUNT(DISTINCT album) as c FROM tracks "
        "WHERE album != '' AND file_status != 'missing' "
        "AND library_status != 'archived'"
    )
    size = await fetch_one(
        "SELECT COALESCE(SUM(file_size), 0) as c FROM tracks "
        "WHERE file_status != 'missing' AND library_status != 'archived'"
    )
    return {
        "total_tracks": total["c"] if total else 0,
        "total_artists": artists["c"] if artists else 0,
        "total_albums": albums["c"] if albums else 0,
        "total_size": size["c"] if size else 0,
    }


@router.get("/state")
async def library_state():
    """The counts line: All tracks / Available / Missing / Archived.

    The three buckets partition the library - a track is active-and-present,
    active-and-missing, or archived - so they always add up to the total.
    Counting a missing file that also happens to be archived would let one
    track appear twice and make the line lie by a few tracks, which is
    worse than not showing the number at all.
    """
    row = await fetch_one(
        """
        SELECT
            COUNT(*) as all_tracks,
            SUM(CASE WHEN library_status = 'active'
                      AND file_status = 'present' THEN 1 ELSE 0 END) as available,
            SUM(CASE WHEN library_status = 'active'
                      AND file_status = 'missing' THEN 1 ELSE 0 END) as missing,
            SUM(CASE WHEN library_status = 'archived' THEN 1 ELSE 0 END) as archived
        FROM tracks
        """
    )
    return {
        "all_tracks": row["all_tracks"] if row else 0,
        "available": (row["available"] if row else 0) or 0,
        "missing": (row["missing"] if row else 0) or 0,
        "archived": (row["archived"] if row else 0) or 0,
    }


@router.get("/reconcile")
async def reconcile(deep: bool = False):
    """Classify every track: present / moved / missing / never acquired.

    `deep=true` hashes every unclaimed file in the library to find moves by
    content. That measured at ~10 seconds for 2.7 GB, so it is opt-in: the
    cheap pass reports what it can and flags that moves were not checked,
    rather than quietly reporting a moved file as missing.
    """
    return await reconcile_library(deep=deep, include_archived=True)


@router.get("/tracks/{track_id}/provenance")
async def track_provenance(track_id: int):
    """Everything known about where this file came from.

    `never_acquired` is a real answer, not a failure to load: it means the
    acquisition table has no row for this track, which is exactly the
    situation a rotated log line could never reveal.
    """
    # `archived_reason` is written on archive and cleared on unarchive,
    # but until now no query selected it, so the one screen that should
    # explain an archived row had nothing to explain it with.
    track = await fetch_one(
        "SELECT id, file_path, file_status, library_status, file_size, "
        "       archived_reason "
        "FROM tracks WHERE id = ?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")

    history = await fetch_all(
        "SELECT * FROM acquisition WHERE track_id = ? ORDER BY id DESC",
        (track_id,),
    )
    operations = await fetch_all(
        "SELECT job_id, kind, state, source_url, final_path, error, created_at "
        "FROM operations WHERE track_id = ? ORDER BY id DESC",
        (track_id,),
    )
    return {
        "track": track,
        "acquisitions": history,
        "operations": operations,
        "never_acquired": not history,
    }


@router.post("/tracks/{track_id}/archive")
async def archive_track(track_id: int, data: dict | None = None):
    """Hide a track from active views. Keeps everything.

    Deliberately does not touch the filesystem and does not delete the row.
    The file stays where it is, the metadata and the acquisition history
    stay readable, and `archived_reason` records why - which is what makes
    the decision reviewable later instead of merely mysterious.
    """
    track = await fetch_one("SELECT id FROM tracks WHERE id = ?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")

    reason = str((data or {}).get("reason", ""))[:500]
    await execute(
        "UPDATE tracks SET library_status='archived', archived_at=?, "
        "archived_reason=?, updated_at=? WHERE id=?",
        (_now_iso(), reason, _now_iso(), track_id),
    )
    row = await fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
    return {"status": "archived", "track": row}


@router.post("/tracks/{track_id}/unarchive")
async def unarchive_track(track_id: int):
    """Restore an archived track. Always allowed, and always exact.

    Nothing was moved or deleted to archive a track, so there is nothing to
    undo and no state that can fail to come back - including when the file
    is still missing, which is a legitimate thing for an archived record to
    be. `file_status` is deliberately left alone rather than forced to
    'present', because the file really is still gone.
    """
    track = await fetch_one("SELECT id FROM tracks WHERE id = ?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")
    await execute(
        "UPDATE tracks SET library_status='active', archived_at=NULL, "
        "archived_reason='', updated_at=? WHERE id=?",
        (_now_iso(), track_id),
    )
    row = await fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
    return {"status": "active", "track": row}


@router.post("/tracks/{track_id}/locate")
async def locate_track(track_id: int, data: dict):
    """Point a missing track at the file that was found for it.

    Only `tracks.file_path` changes. The acquisition row keeps the path the
    file arrived at, so the pair (acquisition path, current path) is the
    evidence that a move happened, and both survive the correction.
    """
    track = await fetch_one(
        "SELECT id, file_path, file_status FROM tracks WHERE id = ?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")

    new_path = str((data or {}).get("path") or "").strip()
    if not new_path:
        raise HTTPException(400, "A path is required")

    candidate = Path(new_path)
    if not candidate.is_file():
        raise HTTPException(400, f"Not a file: {new_path}")

    # The path now names a real file, so it has to be a path this app is
    # allowed to serve. Same rule as the audio route, for the same reason.
    try:
        resolved = candidate.resolve()
        root = Path(config.get("library_path", "")).resolve()
    except OSError:
        raise HTTPException(400, "Path could not be resolved")
    if not root.is_dir() or not resolved.is_relative_to(root):
        raise HTTPException(403, "File is outside the configured library")

    existing = await fetch_one(
        "SELECT id FROM tracks WHERE file_path = ? AND id != ?",
        (str(resolved), track_id),
    )
    if existing:
        raise HTTPException(409, "Another track already points at that file")

    try:
        size = resolved.stat().st_size
    except OSError:
        size = 0
    await execute(
        "UPDATE tracks SET file_path=?, file_status='present', file_size=?, "
        "updated_at=? WHERE id=?",
        (str(resolved), size, _now_iso(), track_id),
    )
    row = await fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
    return {
        "status": "located",
        "track": row,
        "previous_path": track["file_path"],
    }


@router.get("/moves")
async def moved_suggestions():
    """Missing tracks whose exact content is indexed elsewhere.

    Every row here is proof, not a lead: only an identical SHA-256 can
    match. A missing track with no recorded hash cannot match and is
    absent from the result - which is the honest outcome for the whole
    pre-backfill library, and the reason the Missing screen also keeps a
    manual Locate.
    """
    return {"moves": await content_index.find_moves()}


@router.get("/content-index")
async def content_index_state():
    return await content_index.status()


@router.post("/content-index/rebuild")
async def content_index_rebuild():
    return await content_index.rebuild()


@router.get("/provenance/backfill")
async def backfill_state():
    """What a backfill would cost, and how the last one went.

    Split deliberately: `pending` is knowable without running anything,
    so the panel can say "766 files, 2.7 GB" before the user commits,
    rather than after.
    """
    return {
        "pending": await backfill.pending(),
        "run": await backfill.status(),
        "history": await backfill.history(),
    }


@router.post("/provenance/backfill/start")
async def backfill_start(resume: bool = False):
    """Start, or continue, hashing the library. Returns at once."""
    return await backfill.start(resume=resume)


@router.post("/provenance/backfill/pause")
async def backfill_pause():
    return await backfill.pause()


@router.post("/provenance/backfill/resume")
async def backfill_resume():
    return await backfill.resume()


@router.post("/provenance/backfill/cancel")
async def backfill_cancel():
    return await backfill.cancel()


@router.post("/tracks/{track_id}/find")
async def find_moved_track(track_id: int):
    """Search the library for this track by content, not by name.

    A name is not evidence - two different recordings share a title and an
    artist field every day. SHA-256 is: an identical digest means the same
    bytes. So this offers only exact matches and says so when there are
    none, rather than offering the nearest thing and calling it a find.
    """
    track = await fetch_one(
        "SELECT id, file_path FROM tracks WHERE id = ?", (track_id,))
    if not track:
        raise HTTPException(404, "Track not found")

    acq = await acquisition.latest_for_track(track_id)
    if not acq or not acq.get("sha256"):
        return {
            "status": "unknown",
            "reason": "no recorded SHA-256 for this track, so its content "
                      "cannot be identified; use Locate with a path instead",
            "candidates": [],
        }

    claimed = {
        str(Path(r["file_path"]).resolve()).casefold()
        for r in await fetch_all("SELECT file_path FROM tracks")
    }
    root = Path(config.get("library_path") or "")

    from ..services.tagger import AUDIO_EXTENSIONS

    def _unclaimed() -> list[str]:
        if not root.is_dir():
            return []
        out = []
        for fp in root.rglob("*"):
            if not fp.is_file() or fp.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                key = str(fp.resolve()).casefold()
            except OSError:
                continue
            if key not in claimed:
                out.append(str(fp))
        return out

    candidates = await acquisition.hash_paths(await asyncio.to_thread(_unclaimed))
    matches = acquisition.match_moved(
        [{"track_id": track_id, "expected_path": track["file_path"],
          "sha256": acq["sha256"]}],
        candidates,
    )
    return {
        "status": "found" if matches else "not_found",
        "sha256": acq["sha256"],
        "candidates": matches,
        "hashed_files": sum(len(v) for v in candidates.values()),
    }
