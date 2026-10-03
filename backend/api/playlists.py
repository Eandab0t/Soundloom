"""Playlist hub endpoints — import, store, export, everywhere.

Playback-platform writes need an OAuth user token (Spotify and SoundCloud
both refuse app-only credentials for playlist writes), so connection state
and the OAuth callback live here too.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import config
from ..database import fetch_all, fetch_one, execute
from ..services import playlists as svc
from ..services import streaming_auth

router = APIRouter(prefix="/api/playlists", tags=["playlists"])
auth_router = APIRouter(prefix="/api/connections", tags=["connections"])
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Pydantic bodies
# --------------------------------------------------------------------------

class CreateBody(BaseModel):
    name: str
    description: str = ""


class ImportUrlBody(BaseModel):
    url: str
    name: str = ""


class AddTrackBody(BaseModel):
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    path: str = ""


class ExportBody(BaseModel):
    target: str             # m3u | csv | itunes_xml | json | spotify | soundcloud


class MatchBody(BaseModel):
    pass


class EnqueueBody(BaseModel):
    limit: int = 100


# --------------------------------------------------------------------------
# List / create / delete
# --------------------------------------------------------------------------

@router.get("")
async def list_playlists():
    rows = await fetch_all(
        "SELECT p.id, p.name, p.description, p.origin, p.origin_ref, p.created_at, "
        "p.updated_at, COUNT(pt.id) AS track_count, "
        "SUM(CASE WHEN pt.track_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_count "
        "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        "GROUP BY p.id ORDER BY p.updated_at DESC, p.id DESC"
    )
    for r in rows:
        r["track_count"] = r["track_count"] or 0
        r["matched_count"] = r["matched_count"] or 0
    return {"playlists": rows}


@router.post("")
async def create_playlist(body: CreateBody):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Playlist name is required")
    cursor = await execute(
        "INSERT INTO playlists (name, description, origin) VALUES (?, ?, 'manual')",
        (name, body.description or ""),
    )
    return {"id": cursor.lastrowid, "name": name}


@router.get("/{playlist_id}")
async def get_playlist(playlist_id: int):
    pl = await fetch_one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    if not pl:
        raise HTTPException(404, f"Playlist {playlist_id} not found")
    tracks = await fetch_all(
        "SELECT id, position, title, artist, album, duration, path, track_id, "
        "ext_id, ext_source FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
        (playlist_id,),
    )
    return {"playlist": pl, "tracks": tracks}


@router.delete("/{playlist_id}")
async def delete_playlist(playlist_id: int):
    await execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
    await execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return {"status": "deleted"}


@router.put("/{playlist_id}")
async def rename_playlist(playlist_id: int, body: CreateBody):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Playlist name is required")
    row = await fetch_one("SELECT id FROM playlists WHERE id=?", (playlist_id,))
    if not row:
        raise HTTPException(404, f"Playlist {playlist_id} not found")
    await execute(
        "UPDATE playlists SET name=?, description=?, updated_at=datetime('now') WHERE id=?",
        (name, body.description or "", playlist_id),
    )
    return {"status": "updated"}


@router.delete("/{playlist_id}/tracks/{row_id}")
async def remove_track(playlist_id: int, row_id: int):
    await execute(
        "DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", (row_id, playlist_id)
    )
    return {"status": "deleted"}


# --------------------------------------------------------------------------
# Import: from a platform URL, or an uploaded file
# --------------------------------------------------------------------------

@router.post("/import/url")
async def import_from_url(body: ImportUrlBody):
    """Import a public Spotify/Deezer playlist as a local playlist copy."""
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "Playlist URL is required")
    from ..validation import validate_url
    try:
        validate_url(url)
    except Exception:
        pass
    from ..services.sync import SpotifyClient, DeezerClient, parse_playlist_url
    try:
        source_type, source_ref = parse_playlist_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            if source_type == "spotify":
                settings = config.get_all()
                client = SpotifyClient(
                    session, settings.get("spotify_client_id", ""),
                    settings.get("spotify_client_secret", ""),
                )
            else:
                client = DeezerClient(session)
            detail = await client.playlist(source_ref)
            name = (body.name or "").strip() or (
                detail.get("title") or detail.get("name") or "Imported playlist"
            )
            raw = await client.playlist_tracks(source_ref)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Playlist import failed for %s: %s", url, e)
        raise HTTPException(502, f"Could not fetch that playlist: {e}")

    if not raw:
        raise HTTPException(422, "That playlist has no tracks (or it is not public).")

    cursor = await execute(
        "INSERT INTO playlists (name, origin, origin_ref) VALUES (?, ?, ?)",
        (name, f"{source_type}_url", source_ref),
    )
    pid = cursor.lastrowid
    for pos, t in enumerate(raw, 1):
        await execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist, album, "
            "duration, ext_id, ext_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, pos, t.get("title") or "", t.get("artist") or "",
             t.get("album") or "", float(t.get("duration") or 0.0),
             str(t.get("id") or ""), source_type),
        )
    await svc.refresh_local_matches(pid)
    counts = await fetch_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN track_id IS NOT NULL THEN 1 ELSE 0 END) AS m "
        "FROM playlist_tracks WHERE playlist_id=?", (pid,)
    )
    return {"id": pid, "name": name, "tracks": counts["n"] or 0,
            "matched": counts["m"] or 0, "origin": f"{source_type}_url"}


@router.post("/import/file/{playlist_id}")
async def import_file(
    playlist_id: int,
    request: Request,
    kind: str = "",
    name: str = "",
):
    """Append parsed tracks from an uploaded iTunes XML / M3U / CSV file.

    The frontend sends the raw file bytes as the request body (simplest with
    fetch(), no multipart plumbing); kind/name arrive as query params.
    """
    pl = await fetch_one("SELECT id, name FROM playlists WHERE id=?", (playlist_id,))
    if not pl:
        raise HTTPException(404, f"Playlist {playlist_id} not found")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "No file content received")
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "File too large (30 MB limit)")

    filename = (request.headers.get("X-Playlist-Filename") or "").lower()
    kind = (kind or "").strip().lower()
    if not kind:
        if filename.endswith((".xml",)):
            kind = "itunes"
        elif filename.endswith((".m3u", ".m3u8")):
            kind = "m3u"
        elif filename.endswith(".csv"):
            kind = "csv"
        else:
            head = raw[:512].lstrip().lower()
            if head.startswith(b"<?xml") or b"<plist" in head or b"<itunes" in head:
                kind = "itunes"
            elif head.startswith(b"#extm3u") or b"#extinf" in head:
                kind = "m3u"
            else:
                kind = "csv"

    try:
        if kind == "itunes":
            parsed = svc.parse_itunes_xml(raw)
        elif kind == "m3u":
            parsed = svc.parse_m3u(raw)
        elif kind == "csv":
            parsed = svc.parse_csv(raw)
        else:
            raise HTTPException(400, f"Unsupported playlist file type: {kind}")
    except ValueError as e:
        raise HTTPException(422, str(e))

    rename = (name or "").strip()
    if not rename or rename in svc.GENERIC_NAMES:
        rename = pl["name"]
    start = await fetch_one(
        "SELECT COALESCE(MAX(position), 0) AS p FROM playlist_tracks WHERE playlist_id=?",
        (playlist_id,),
    )
    pos = (start["p"] if start else 0) or 0
    for t in parsed["tracks"]:
        pos += 1
        await execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist, album, "
            "duration, path, ext_id, ext_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (playlist_id, pos, t["title"], t["artist"], t["album"],
             float(t.get("duration") or 0.0), t.get("path") or "", "", kind),
        )
    if rename and rename != pl["name"]:
        await execute("UPDATE playlists SET name=? WHERE id=?", (rename, playlist_id))
    await svc.refresh_local_matches(playlist_id)
    counts = await fetch_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN track_id IS NOT NULL THEN 1 ELSE 0 END) AS m "
        "FROM playlist_tracks WHERE playlist_id=?", (playlist_id,)
    )
    return {"playlist_id": playlist_id, "name": rename, "kind": kind,
            "added": len(parsed["tracks"]), "tracks": counts["n"] or 0,
            "matched": counts["m"] or 0}


@router.post("/{playlist_id}/tracks")
async def add_track(playlist_id: int, body: AddTrackBody):
    pl = await fetch_one("SELECT id FROM playlists WHERE id=?", (playlist_id,))
    if not pl:
        raise HTTPException(404, f"Playlist {playlist_id} not found")
    if not (body.title or "").strip() and not body.path.strip():
        raise HTTPException(400, "Track needs a title or a path")
    nxt = await fetch_one(
        "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM playlist_tracks WHERE playlist_id=?",
        (playlist_id,),
    )
    cursor = await execute(
        "INSERT INTO playlist_tracks (playlist_id, position, title, artist, album, "
        "duration, path, ext_id, ext_source) VALUES (?, ?, ?, ?, ?, ?, ?, '', 'manual')",
        (playlist_id, nxt["p"] if nxt else 1, body.title.strip(), body.artist.strip(),
         body.album.strip(), float(body.duration or 0.0), body.path.strip()),
    )
    await execute("UPDATE playlists SET updated_at=datetime('now') WHERE id=?", (playlist_id,))
    return {"id": cursor.lastrowid, "position": nxt["p"] if nxt else 1}


# --------------------------------------------------------------------------
# Match + enqueue
# --------------------------------------------------------------------------

@router.post("/{playlist_id}/match")
async def match_playlist(playlist_id: int, body: MatchBody | None = None):
    try:
        return await svc.refresh_local_matches(playlist_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/{playlist_id}/enqueue")
async def enqueue_playlist(playlist_id: int, body: EnqueueBody | None = None):
    try:
        return await svc.enqueue_missing(playlist_id, limit=(body.limit if body else 100) or 100)
    except ValueError as e:
        raise HTTPException(404, str(e))


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

@router.post("/{playlist_id}/export")
async def export_playlist(playlist_id: int, body: ExportBody):
    pl = await fetch_one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    if not pl:
        raise HTTPException(404, f"Playlist {playlist_id} not found")
    track_rows = await fetch_all(
        "SELECT pt.title, pt.artist, pt.album, pt.duration, pt.path, t.file_path "
        "FROM playlist_tracks pt LEFT JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id=? ORDER BY pt.position",
        (playlist_id,),
    )
    rows = [
        svc.make_row(
            title=t["title"], artist=t["artist"], album=t["album"],
            duration=t["duration"] or 0.0,
            path=(t["path"] or "") or (t["file_path"] or ""),
        )
        for t in track_rows
    ]
    target = (body.target or "").strip().lower()
    config._ensure_dirs()

    if target in ("m3u", "m3u8"):
        path = svc.write_m3u(pl["name"], rows, config.EXPORTS_DIR)
        return {"target": "m3u", "path": str(path), "tracks": len(rows)}
    if target == "csv":
        path = svc.write_csv(pl["name"], rows, config.EXPORTS_DIR)
        return {"target": "csv", "path": str(path), "tracks": len(rows)}
    if target in ("itunes_xml", "itunes", "xml"):
        path = svc.write_itunes_xml(pl["name"], rows, config.EXPORTS_DIR)
        return {"target": "itunes_xml", "path": str(path), "tracks": len(rows)}
    if target == "json":
        path = svc.write_json(pl["name"], rows, config.EXPORTS_DIR)
        return {"target": "json", "path": str(path), "tracks": len(rows)}
    if target == "spotify":
        try:
            result = await svc.create_spotify_playlist(
                pl["name"], rows, pl.get("description") or ""
            )
        except ValueError as e:
            code = 401 if "not connected" in str(e).lower() else 400
            raise HTTPException(code, str(e))
        return {"target": "spotify", **result}
    if target == "soundcloud":
        try:
            result = await svc.create_soundcloud_playlist(
                pl["name"], rows, pl.get("description") or ""
            )
        except ValueError as e:
            code = 401 if "not connected" in str(e).lower() else 400
            raise HTTPException(code, str(e))
        return {"target": "soundcloud", **result}
    raise HTTPException(400, f"Unknown export target: {target}")



# --------------------------------------------------------------------------
# Platform connections (OAuth user flow)
# --------------------------------------------------------------------------

class ConnectBody(BaseModel):
    platform: str         # spotify | soundcloud


@auth_router.post("/connect")
async def connect_start(body: ConnectBody):
    platform = (body.platform or "").strip().lower()
    if platform not in ("spotify", "soundcloud"):
        raise HTTPException(400, "platform must be spotify or soundcloud")
    if not streaming_auth.configured(platform):
        raise HTTPException(400, (
            f"{platform.capitalize()} app credentials are missing. Add the client id "
            f"and secret in Settings, then register the redirect URI shown there."
        ))
    try:
        info = streaming_auth.begin_auth(platform)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return info


@auth_router.get("/status")
async def connection_status():
    out = {}
    for platform in ("spotify", "soundcloud"):
        out[platform] = {
            "configured": streaming_auth.configured(platform),
            "connected": streaming_auth.connected(platform),
        }
    return out


class CallbackBody(BaseModel):
    code: str = ""
    state: str = ""


@auth_router.post("/callback")
async def callback_exchange(body: CallbackBody):
    """Complete an OAuth handshake (the callback page posts here)."""
    try:
        platform = streaming_auth.platform_for_state(body.state)
        store = await streaming_auth.complete_auth(platform, body.code, body.state)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "connected", "platform": platform}


@auth_router.post("/{platform}/disconnect")
async def disconnect(platform: str):
    if platform not in ("spotify", "soundcloud"):
        raise HTTPException(400, "platform must be spotify or soundcloud")
    streaming_auth.clear(platform)
    return {"status": "disconnected", "platform": platform}


@auth_router.get("/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """The platform redirects here; serve a tiny page that POSTs the code."""
    if error:
        return HTMLResponse(_CALLBACK_HTML.replace("__MSG__", f"Authorization failed: {error}"))
    if not code or not state:
        return HTMLResponse(_CALLBACK_HTML.replace("__MSG__", "Missing code/state in callback."))
    return HTMLResponse(
        _CALLBACK_HTML.replace("__CODE__", code).replace("__STATE__", state)
    )


_CALLBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Soundloom — connecting…</title>
<style>
  body { font-family: system-ui, sans-serif; background: #14120e; color: #f0e6d8;
         display: grid; place-items: center; min-height: 100vh; margin: 0; }
  .card { text-align: center; padding: 2rem 3rem; }
  h1 { font-size: 1.1rem; font-weight: 600; }
  p { color: #b8a88f; font-size: .9rem; }
  .ok { color: #ffb454; }
</style>
</head>
<body>
  <div class="card">
    <img src="/static/img/logo.svg" alt="" width="42" height="42">
    <h1 id="msg">Finishing connection…</h1>
    <p>You can close this tab once it says connected.</p>
  </div>
<script>
  const msg = document.getElementById('msg');
  async function run() {
    try {
      const r = await fetch('/api/connections/callback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: '__CODE__', state: '__STATE__' }),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok) {
        msg.textContent = 'Connected ✓ — ' + (j.platform || '') + '. You can close this tab.';
        msg.className = 'ok';
      } else {
        msg.textContent = 'Failed: ' + (j.detail || r.status);
      }
    } catch (e) {
      msg.textContent = 'Failed: ' + e.message;
    }
  }
  run();
</script>
</body>
</html>"""
