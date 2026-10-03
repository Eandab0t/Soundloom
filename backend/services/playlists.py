"""The playlist hub — import, store and re-export track lists.

One normalized home for playlists regardless of where they came from:

  Import        parse into playlist_tracks rows
  ----          -------------------------------
  Spotify       paste a playlist/album URL (public playlists)
  Deezer        paste a playlist URL (no auth at all)
  iTunes XML    the Library.xml iTunes/Music exports (File > Library >
                Export Library, or Export Playlist)
  M3U/M3U8      the interchange format every player understands
  CSV           any CSV with title/artist columns (Excel, Sheets, "/csv" exports)
  manual        created from your library in the UI

  Export        write out
  ------        ---------
  M3U/M3U8      playlist.m3u(8) — #EXTM3U header, absolute paths
  CSV           title,artist,album,duration_sec,local_path
  iTunes XML    a playlist-only Library-style XML other iTunes/Music
                installs can import
  JSON          lossless Soundloom backup
  Spotify       creates the playlist on the user's account via OAuth
                (playlist-modify scope)
  SoundCloud    creates a set via OAuth 2.1; needs a registered app and
                resolves rows to SoundCloud track ids by search

Local matching (linking rows to `tracks` ids) happens on demand and is
recomputed after scans, so imports stay stable even when the file is not in
the library yet.
"""
import csv
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp

from .. import config
from ..database import fetch_all, fetch_one, execute
from ..pipeline.normalize import normalize_text
from . import source_registry
from . import streaming_auth

logger = logging.getLogger(__name__)

SPOTIFY_API = "https://api.spotify.com/v1"
SOUNDCLOUD_API = "https://api.soundcloud.com"

_SYNC_TIMEOUT = aiohttp.ClientTimeout(total=30)

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".wav", ".wma", ".alac", ".aiff"}

# Fallback names parsers use when the file itself carries no title. These
# must never override a playlist name the user (or the filename) chose.
GENERIC_NAMES = {"M3U Import", "CSV Import", "iTunes Import"}


def _sanitize(name: str) -> str:
    from ..validation import sanitize_filename
    return sanitize_filename(name) or "playlist"


# --------------------------------------------------------------------------
# Rows: the normalized track reference
# --------------------------------------------------------------------------

def make_row(title: str = "", artist: str = "", album: str = "",
             duration: float = 0.0, path: str = "",
             ext_id: str = "", ext_source: str = "") -> dict:
    return {
        "title": (title or "").strip(),
        "artist": (artist or "").strip(),
        "album": (album or "").strip(),
        "duration": float(duration or 0.0),
        "path": (path or "").strip(),
        "ext_id": (ext_id or "").strip(),
        "ext_source": (ext_source or "").strip(),
    }


# --------------------------------------------------------------------------
# Import parsers (all offline formats are parsed locally, no network)
# --------------------------------------------------------------------------

def parse_itunes_xml(data: bytes | str) -> dict:
    """Parse an iTunes/Music Library.xml (or a single exported playlist).

    Returns {"name": ..., "tracks": [row, ...]}.

    Handles Apple's plist dialect: <dict><key>Tracks</key><dict>...</dict>
    with per-track keys like Name, Artist, Album, Total Time, Location
    (a file:// URL, possibly percent-encoded and prefixed with a volume).
    """
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"Not a valid iTunes XML file: {e}")

    def _plist_dict(node) -> dict:
        out = {}
        if node is None:
            return out
        children = list(node)
        for i in range(0, len(children) - 1, 2):
            key = children[i]
            val = children[i + 1]
            if key.tag == "key":
                out[key.text or ""] = val
        return out

    top = _plist_dict(root if root.tag == "dict" else (root.find("dict") if root.tag == "plist" else None))

    # Only the Tracks dict (skip Playlists' duplicate copies of the same data).
    tracks_node = top.get("Tracks")
    if tracks_node is None:
        raise ValueError("iTunes XML has no Tracks dictionary — export the library or a playlist.")

    # Playlist name: when exporting a single playlist, it is under Playlists.
    name = "iTunes Import"
    playlists_node = top.get("Playlists")
    if playlists_node is not None and list(playlists_node):
        first = _plist_dict(list(playlists_node)[0])
        pname = first.get("Name")
        if pname is not None and (pname.text or "").strip():
            name = pname.text.strip()

    def _loc_to_path(loc: str) -> str:
        from urllib.parse import unquote, urlparse
        try:
            parsed = urlparse(loc)
            path = unquote(parsed.path) if parsed.scheme else loc
        except ValueError:
            path = loc
        # Windows iTunes writes file://localhost/C:/... or file:///C:/...
        path = re.sub(r"^/([A-Za-z]:)/", r"\1/", path)
        return path

    rows: list[dict] = []
    for track_dict in list(tracks_node):
        t = _plist_dict(track_dict)
        title = (t.get("Name").text or "") if t.get("Name") is not None else ""
        artist = (t.get("Artist").text or "") if t.get("Artist") is not None else ""
        album = (t.get("Album").text or "") if t.get("Album") is not None else ""
        total_time = (t.get("Total Time").text or "0") if t.get("Total Time") is not None else "0"
        location = (t.get("Location").text or "") if t.get("Location") is not None else ""
        if not title and not location:
            continue
        try:
            duration = int(total_time) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0
        rows.append(make_row(
            title=title, artist=artist, album=album, duration=duration,
            path=_loc_to_path(location),
        ))
    if not rows:
        raise ValueError("iTunes XML contained no usable tracks.")
    return {"name": name, "tracks": rows}


def parse_m3u(data: bytes | str, name_hint: str = "") -> dict:
    """Parse an M3U/M3U8 playlist. Extended info (#EXTINF) is used when present.

    Relative paths resolve against the M3U file's own folder when known
    (the caller passes base_dir).
    """
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    text = text.lstrip("\ufeff")
    lines = [ln.strip() for ln in text.splitlines()]
    rows: list[dict] = []
    pending: dict = {}
    for ln in lines:
        if not ln or ln.startswith("#PLAYLIST:"):
            if ln.startswith("#PLAYLIST:") and not name_hint:
                name_hint = ln.split(":", 1)[1].strip()
            continue
        if ln.startswith("#EXTINF:"):
            meta = ln.split(":", 1)[1]
            dur_str, _, rest = meta.partition(",")
            rest = rest.strip()
            artist, _, title = rest.partition(" - ")
            if not title:  # some writers put "Artist - Title" without spacing
                artist, _, title = rest.partition("-")
            try:
                pending = {"duration": float(dur_str or 0), "artist": artist.strip(),
                           "title": title.strip()}
            except ValueError:
                pending = {"duration": 0.0, "artist": artist.strip(), "title": title.strip()}
            continue
        if ln.startswith("#"):
            continue
        # A path line closes the pending EXTINF.
        path = ln
        if pending:
            rows.append(make_row(
                title=pending.get("title") or Path(path).stem,
                artist=pending.get("artist", ""),
                duration=pending.get("duration", 0.0),
                path=path,
            ))
            pending = {}
        else:
            rows.append(make_row(title=Path(path).stem, path=path))
    if not rows:
        raise ValueError("M3U file contained no tracks.")
    return {"name": name_hint or "M3U Import", "tracks": rows}


def parse_csv(data: bytes | str, name_hint: str = "") -> dict:
    """Parse a CSV with headers (case-insensitive): title/name, artist, album,
    duration (seconds or mm:ss), and optionally path/location."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is empty.")
    fields = {f.strip().lower().strip('"'): f for f in reader.fieldnames}

    def col(*names: str):
        for n in names:
            if n in fields:
                return fields[n]
        return None

    tcol = col("title", "name", "track", "song")
    acol = col("artist", "artist name", "artists")
    if not tcol:
        raise ValueError(
            "CSV needs a 'title' column (artist/album/duration are optional)."
        )
    hcol = col("duration", "duration_sec", "duration_seconds", "length", "time", "total_time")
    pcol = col("path", "location", "file", "filename")
    alcol = col("album", "album name")
    rows: list[dict] = []
    for rec in reader:
        title = (rec.get(tcol) or "").strip()
        if not title:
            continue
        dur_raw = (rec.get(hcol) or "").strip() if hcol else ""
        duration = 0.0
        if dur_raw:
            try:
                if ":" in dur_raw:
                    parts = [float(p) for p in dur_raw.split(":")]
                    for p in parts:
                        duration = duration * 60 + p
                else:
                    duration = float(dur_raw)
            except ValueError:
                duration = 0.0
        rows.append(make_row(
            title=title,
            artist=((rec.get(acol) or "").strip() if acol else ""),
            album=((rec.get(alcol) or "").strip() if alcol else ""),
            duration=duration,
            path=((rec.get(pcol) or "").strip() if pcol else ""),
        ))
    if not rows:
        raise ValueError("CSV contained no usable track rows.")
    return {"name": name_hint or "CSV Import", "tracks": rows}


# --------------------------------------------------------------------------
# Export writers
# --------------------------------------------------------------------------

def write_m3u(name: str, rows: list[dict], base_dir: Path) -> Path:
    """Absolute-path M3U8 (UTF-8) so any player can open it."""
    out_lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for r in rows:
        dur = int(r.get("duration") or 0)
        label = f"{r.get('artist', '')} - {r.get('title', '')}".strip(" -")
        out_lines.append(f"#EXTINF:{dur},{label}")
        out_lines.append(r.get("path") or f"{r.get('artist', '')} - {r.get('title', '')}.mp3")
    target = base_dir / f"{_sanitize(name)}.m3u8"
    counter = 2
    while target.exists():
        target = base_dir / f"{_sanitize(name)} ({counter}).m3u8"
        counter += 1
    target.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return target


def write_csv(name: str, rows: list[dict], base_dir: Path) -> Path:
    target = base_dir / f"{_sanitize(name)}.csv"
    counter = 2
    while target.exists():
        target = base_dir / f"{_sanitize(name)} ({counter}).csv"
        counter += 1
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["title", "artist", "album", "duration_sec", "local_path"])
    for r in rows:
        writer.writerow([r.get("title", ""), r.get("artist", ""), r.get("album", ""),
                         int(r.get("duration") or 0), r.get("path", "")])
    target.write_text(buf.getvalue(), encoding="utf-8-sig")
    return target


def write_itunes_xml(name: str, rows: list[dict], base_dir: Path) -> Path:
    """A playlist-only iTunes-style XML. Music/iTunes can import these via
    File > Library > Import Playlist."""
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _plist_str(s: str) -> str:
        return f"<string>{esc(s)}</string>"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Major Version</key><integer>1</integer>",
        "  <key>Minor Version</key><integer>1</integer>",
        "  <key>Application Version</key><string>Soundloom</string>",
        "  <key>Playlists Count</key><integer>1</integer>",
        "  <key>Tracks</key><dict>",
    ]
    track_keys = []
    tracks_dict_lines = []
    for i, r in enumerate(rows, 1):
        key = str(i)
        track_keys.append(key)
        total_ms = int((r.get("duration") or 0) * 1000)
        tracks_dict_lines.append(f"    <key>{key}</key><dict>")
        tracks_dict_lines.append(f"      <key>Track ID</key><integer>{i}</integer>")
        tracks_dict_lines.append(f"      <key>Name</key>{_plist_str(r.get('title', ''))}")
        if r.get("artist"):
            tracks_dict_lines.append(f"      <key>Artist</key>{_plist_str(r['artist'])}")
        if r.get("album"):
            tracks_dict_lines.append(f"      <key>Album</key>{_plist_str(r['album'])}")
        tracks_dict_lines.append(f"      <key>Total Time</key><integer>{total_ms}</integer>")
        tracks_dict_lines.append(f"      <key>Track Type</key><string>File</string>")
        if r.get("path"):
            from urllib.parse import quote
            # file://localhost/<path> is the shape iTunes itself writes; the
            # reader strips the leading slash from Windows drive paths.
            tracks_dict_lines.append(
                f"      <key>Location</key><string>file://localhost/{quote(r['path']).lstrip('/')}</string>"
            )
        tracks_dict_lines.append("    </dict>")
    lines.extend(tracks_dict_lines)
    lines.append("  </dict>")
    lines.append("  <key>Playlists</key><array>")
    lines.append("    <dict>")
    lines.append("      <key>Name</key>" + _plist_str(name))
    lines.append("      <key>All Items</key><true/>")
    lines.append("      <key>Playlist Items</key><array>")
    for key in track_keys:
        lines.append(f"        <dict><key>Track ID</key><integer>{key}</integer></dict>")
    lines.append("      </array>")
    lines.append("    </dict>")
    lines.append("  </array>")
    lines.append("</dict>")
    lines.append("</plist>")

    target = base_dir / f"{_sanitize(name)}.xml"
    counter = 2
    while target.exists():
        target = base_dir / f"{_sanitize(name)} ({counter}).xml"
        counter += 1
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_json(name: str, rows: list[dict], base_dir: Path) -> Path:
    target = base_dir / f"{_sanitize(name)}.json"
    counter = 2
    while target.exists():
        target = base_dir / f"{_sanitize(name)} ({counter}).json"
        counter += 1
    payload = {"name": name, "tracks": [
        {k: r.get(k, "") for k in ("title", "artist", "album", "duration", "path", "ext_id", "ext_source")}
        for r in rows
    ]}
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# Local library matching
# --------------------------------------------------------------------------

async def refresh_local_matches(playlist_id: int) -> dict:
    """Recompute track_id for every row from the local library.

    Rows match on exact normalized 'artist title' first, then title-only when
    unique, then by absolute file path. Files flagged missing in the library
    are ignored on purpose — a missing file must not count as owned, or the
    downloader would skip a track the user cannot actually play.
    """
    pl = await fetch_one("SELECT id FROM playlists WHERE id=?", (playlist_id,))
    if not pl:
        raise ValueError(f"Playlist {playlist_id} not found")
    rows = await fetch_all(
        "SELECT id, title, artist, path FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
        (playlist_id,),
    )
    lib = await fetch_all(
        "SELECT id, file_path, title, artist, primary_artist, display_artist "
        "FROM tracks WHERE file_status != 'missing'"
    )
    by_path = {Path(l["file_path"]).resolve().as_posix().lower(): l for l in lib}
    by_artist_title: dict[str, list] = {}
    by_title: dict[str, list] = {}
    for l in lib:
        t = normalize_text(l.get("title") or "")
        if t:
            by_title.setdefault(t, []).append(l)
            for a in (l.get("artist"), l.get("primary_artist"), l.get("display_artist")):
                at = normalize_text(f"{a or ''} {t}")
                if at:
                    by_artist_title.setdefault(at, []).append(l)

    matched = 0
    for row in rows:
        hit = None
        p = (row.get("path") or "").strip()
        if p:
            try:
                hit = by_path.get(Path(p).resolve().as_posix().lower())
            except (OSError, ValueError):
                hit = None
        if hit is None:
            at = normalize_text(f"{row.get('artist', '').split(',')[0]} {row.get('title', '')}")
            if at and len(by_artist_title.get(at, [])) == 1:
                hit = by_artist_title[at][0]
        if hit is None:
            tl = by_title.get(normalize_text(row.get("title", "")), [])
            if len(tl) == 1:
                hit = tl[0]
        new_id = hit["id"] if hit else None
        if new_id != row.get("track_id"):
            await execute(
                "UPDATE playlist_tracks SET track_id=? WHERE id=?", (new_id, row["id"])
            )
        if hit:
            matched += 1
    summary = {"playlist_id": playlist_id, "rows": len(rows), "matched": matched}
    await execute(
        "UPDATE playlists SET updated_at=datetime('now') WHERE id=?", (playlist_id,)
    )
    return summary


# --------------------------------------------------------------------------
# Streaming-platform export (Spotify / SoundCloud)
# --------------------------------------------------------------------------

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def create_spotify_playlist(name: str, rows: list[dict], description: str = "") -> dict:
    """Create a playlist on the connected user's Spotify account.

    Uses the OAuth user token (playlist-modify scope). Tracks are searched
    (order preserved, up to Spotify's 100-track URI add limit per request);
    misses are reported, never invented.
    """
    if not rows:
        raise ValueError("Nothing to export — the playlist is empty.")
    token = await streaming_auth.get_access_token("spotify")
    user_id = await _spotify_me(token)
    created = await _spotify_create_playlist(name, token, user_id, description)

    playlist_id = created.get("id", "")
    uris: list[str] = []
    added = missing = 0
    missing_titles: list[str] = []
    snapshot = ""
    async with aiohttp.ClientSession() as session:
        for r in rows:
            uri = await _spotify_search_uri(session, token, r)
            if uri:
                uris.append(uri)
                added += 1
            else:
                missing += 1
                missing_titles.append(f"{r.get('artist', '')} - {r.get('title', '')}".strip(" -"))
            if len(uris) >= 100:
                snapshot = await _spotify_add(session, token, playlist_id, uris)
                uris = []
        if uris:
            snapshot = await _spotify_add(session, token, playlist_id, uris)
    return {
        "url": created.get("external_urls", {}).get("spotify", ""),
        "platform_id": playlist_id,
        "added": added,
        "missing": missing,
        "missing_titles": missing_titles[:25],
        "snapshot": snapshot,
    }


async def _spotify_create_playlist(name: str, token: str, user_id: str, description: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{SPOTIFY_API}/users/{user_id}/playlists",
            headers=_auth_headers(token),
            json={"name": name, "description": description or "Exported from Soundloom",
                  "public": False},
            timeout=_SYNC_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                raise ValueError(f"Spotify playlist creation failed ({resp.status}): {(await resp.text())[:200]}")
            return await resp.json(content_type=None)


async def _spotify_me(token: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{SPOTIFY_API}/me", headers={"Authorization": f"Bearer {token}"},
                               timeout=_SYNC_TIMEOUT) as resp:
            if resp.status >= 400:
                raise ValueError(f"Spotify /me failed ({resp.status}). Reconnect from the Playlists tab.")
            data = await resp.json(content_type=None)
    return data.get("id", "")


async def _spotify_search_uri(session, token: str, row: dict) -> str:
    q = f"track:{row.get('title', '')} artist:{(row.get('artist') or '').split(',')[0]}"
    try:
        async with session.get(
            f"{SPOTIFY_API}/search",
            params={"q": q, "type": "track", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_SYNC_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                return ""
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("Spotify search failed for %r: %s", row.get("title"), e)
        return ""
    items = ((data.get("tracks") or {}).get("items")) or []
    return items[0].get("uri", "") if items else ""


async def _spotify_add(session, token: str, playlist_id: str, uris: list[str]) -> str:
    async with session.post(
        f"{SPOTIFY_API}/playlists/{playlist_id}/tracks",
        headers=_auth_headers(token),
        json={"uris": uris},
        timeout=_SYNC_TIMEOUT,
    ) as resp:
        if resp.status >= 400:
            raise ValueError(f"Adding tracks failed ({resp.status}): {(await resp.text())[:200]}")
        data = await resp.json(content_type=None)
    return data.get("snapshot_id", "")


async def create_soundcloud_playlist(name: str, rows: list[dict], description: str = "") -> dict:
    """Create a SoundCloud set from the rows, resolving each by search.

    The SoundCloud API has no 'add by name' — sets reference track ids, so
    every row is resolved with /search/tracks first. Rows that resolve to
    nothing are reported, never invented.
    """
    if not rows:
        raise ValueError("Nothing to export — the playlist is empty.")
    token = await streaming_auth.get_access_token("soundcloud")
    async with aiohttp.ClientSession() as session:
        track_ids: list[int] = []
        missing_titles: list[str] = []
        for r in rows:
            tid = await _soundcloud_search_id(session, token, r)
            if tid:
                track_ids.append(tid)
            else:
                missing_titles.append(f"{r.get('artist', '')} - {r.get('title', '')}".strip(" -"))
        async with session.post(
            f"{SOUNDCLOUD_API}/playlists",
            headers=_auth_headers(token),
            json={"playlist": {
                "title": name,
                "description": description or "Exported from Soundloom",
                "sharing": "private",
                "tracks": [{"id": t} for t in track_ids],
            }},
            timeout=_SYNC_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                raise ValueError(f"SoundCloud playlist creation failed ({resp.status}): {(await resp.text())[:200]}")
            created = await resp.json(content_type=None)
    return {
        "url": created.get("permalink_url", ""),
        "platform_id": str(created.get("id", "")),
        "added": len(track_ids),
        "missing": len(missing_titles),
        "missing_titles": missing_titles[:25],
    }


async def _soundcloud_search_id(session, token: str, row: dict) -> int | None:
    q = f"{(row.get('artist') or '').split(',')[0]} {row.get('title', '')}".strip()
    if not q:
        return None
    try:
        async with session.get(
            f"{SOUNDCLOUD_API}/search/tracks",
            params={"q": q, "limit": 5},
            headers=_auth_headers(token),
            timeout=_SYNC_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                return None
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("SoundCloud search failed for %r: %s", q, e)
        return None
    for item in data or []:
        if item.get("id"):
            return int(item["id"])
    return None


# --------------------------------------------------------------------------
# Enqueue-to-downloader bridge
# --------------------------------------------------------------------------

async def enqueue_missing(playlist_id: int, limit: int = 100) -> dict:
    """Queue downloader jobs for rows with no local match, in playlist order.

    Uses the existing source-priority search scheme (ytsearch1:/dzsearch:/
    scsearch1:), so downloads land through the same pipeline — tags, covers,
    organizer — as everything else. Rows that are already queued are skipped.
    """
    pl = await fetch_one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    if not pl:
        raise ValueError(f"Playlist {playlist_id} not found")
    rows = await fetch_all(
        "SELECT * FROM playlist_tracks WHERE playlist_id=? AND track_id IS NULL "
        "ORDER BY position",
        (playlist_id,),
    )
    settings = config.get_all()
    order = source_registry.normalize_priority(settings.get("source_priority"))
    search_source = order[0] if order else "youtube"

    enqueued = 0
    skipped_queued = 0
    for r in rows[:limit]:
        title = r.get("title") or ""
        artist = (r.get("artist") or "").split(",")[0].strip()
        if not title and not artist:
            continue
        query_url = source_registry.build_search_job_url(search_source, artist, title)
        existing = await fetch_one(
            "SELECT id FROM jobs WHERE source_url=? AND status NOT IN "
            "('complete','failed','cancelled') LIMIT 1",
            (query_url,),
        )
        if existing:
            skipped_queued += 1
            continue
        await execute(
            "INSERT INTO jobs (source_url, source_type, query, title, artist, "
            "output_format, quality_profile, status) VALUES (?, 'youtube', ?, ?, ?, "
            "'mp3', 'balanced', 'pending')",
            (query_url, title, title, r.get("artist") or ""),
        )
        enqueued += 1
    return {
        "playlist_id": playlist_id,
        "enqueued": enqueued,
        "already_queued": skipped_queued,
        "unmatched_total": len(rows),
    }
