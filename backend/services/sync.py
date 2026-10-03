"""Playlist sync - the Soundiiz-style feature.

A synced playlist is a pointer at a playlist on a streaming platform plus a
memory of what we already downloaded from it. Each sync pass:

  1. Fetch the platform playlist's track list.
  2. For every track, build a search URL on an audio source.
  3. Skip tracks already in the library or already queued.
  4. Enqueue the rest, respecting per-playlist and per-pass caps.

Diffing by the platform's track ids (last_track_ids) means a sync is
idempotent: running it twice in a row enqueues nothing the second time, and
re-running after the user cleared the queue does not re-download anything.

Spotify uses the Client Credentials flow the same way the identify backend
does - app-level auth only, never a user session. Deezer playlists need no
auth at all. Audio always comes from the existing yt-dlp / deemix adapters;
nothing here downloads audio from a streaming platform.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp

from .. import config
from ..database import fetch_all, fetch_one, execute
from ..events import emit
from ..pipeline.normalize import normalize_text
from . import source_registry

logger = logging.getLogger(__name__)

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
DEEZER_API = "https://api.deezer.com"

_SYNC_TIMEOUT = aiohttp.ClientTimeout(total=30)

_task: asyncio.Task | None = None
_stop = asyncio.Event()

# How many tracks a single sync pass may enqueue across all auto playlists,
# so a first sync of a 500-track playlist cannot flood the queue.
_MAX_ENQUEUES_PER_PASS = 150


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Platform clients
# --------------------------------------------------------------------------

class SpotifyClient:
    """App-auth Spotify client (Client Credentials). Playlist reading only."""

    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str):
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""
        self._token_expiry = 0.0

    def _configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def _access_token(self) -> str:
        import time
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        if not self._configured():
            raise RuntimeError(
                "No Spotify credentials configured. Add spotify_client_id / "
                "spotify_client_secret in Settings (developer.spotify.com, free)."
            )
        import base64
        raw = f"{self._client_id}:{self._client_secret}".encode()
        auth = base64.b64encode(raw).decode()
        async with self._session.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        self._token = payload.get("access_token", "")
        self._token_expiry = now + int(payload.get("expires_in", 3300))
        return self._token

    async def get_json(self, url: str) -> dict:
        token = await self._access_token()
        async with self._session.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=_SYNC_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def playlist(self, playlist_id: str) -> dict:
        url = f"{SPOTIFY_API}/playlists/{playlist_id}?fields=name,tracks(total)"
        return await self.get_json(url)

    async def playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Every track on the playlist, following pagination."""
        out: list[dict] = []
        url = (
            f"{SPOTIFY_API}/playlists/{playlist_id}/tracks"
            "?fields=next,items(track(id,name,artists,duration_ms,album(name)))"
            "&limit=100&offset=0"
        )
        while url:
            data = await self.get_json(url)
            items = data.get("items") or []
            for item in items:
                track = item.get("track") or {}
                if track.get("id"):
                    out.append({
                        "id": str(track["id"]),
                        "title": track.get("name") or "",
                        "artist": ", ".join(
                            a.get("name", "") for a in (track.get("artists") or [])[:2]
                        ),
                        "duration": (track.get("duration_ms") or 0) / 1000.0,
                        "album": ((track.get("album") or {}).get("name")) or "",
                    })
            url = data.get("next")
            if len(out) >= 1000:  # sanity ceiling
                break
        return out


class DeezerClient:
    """Deezer public API client. Playlist reading needs no auth."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def get_json(self, url: str) -> dict:
        async with self._session.get(url, timeout=_SYNC_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def playlist(self, playlist_id: str) -> dict:
        return await self.get_json(f"{DEEZER_API}/playlist/{playlist_id}")

    async def playlist_tracks(self, playlist_id: str) -> list[dict]:
        out: list[dict] = []
        index = 0
        total = None
        for _ in range(20):  # 20 pages x 100 = 2000 tracks, plenty
            data = await self.get_json(
                f"{DEEZER_API}/playlist/{playlist_id}/tracks?index={index}&limit=100"
            )
            items = data.get("data") or []
            if total is None:
                total = data.get("total", len(items))
            for track in items:
                if track.get("id"):
                    out.append({
                        "id": str(track["id"]),
                        "title": track.get("title") or "",
                        "artist": (track.get("artist") or {}).get("name", ""),
                        "duration": float(track.get("duration") or 0.0),
                        "album": (track.get("album") or {}).get("title", ""),
                    })
            index += 100
            if not items or index >= total:
                break
        return out


# --------------------------------------------------------------------------
# URL parsing and source mapping
# --------------------------------------------------------------------------

def parse_playlist_url(url: str) -> tuple[str, str]:
    """Return (source_type, playlist_id) or raise ValueError.

    Accepts:
      https://open.spotify.com/playlist/<id>...
      https://open.spotify.com/album/<id>...  (read as a track list)
      spotify:playlist:<id>
      https://www.deezer.com/playlist/<id>
      https://www.deezer.com/playlist/<id>?...
      a bare 22-char Spotify id or numeric Deezer id (source chosen by shape)
    """
    u = (url or "").strip()
    if not u:
        raise ValueError("Playlist URL is required")

    lowered = u.lower()
    if "spotify" in lowered:
        if ":" in u and "spotify:" in lowered:
            parts = u.split(":")
            pid = parts[-1].split("?")[0]
            return "spotify", pid
        for marker in ("/playlist/", "/album/"):
            if marker in lowered:
                pid = u.split(marker, 1)[1].split("?")[0].split("/")[0]
                if pid:
                    return "spotify", pid
        raise ValueError("Spotify URL must contain /playlist/ or /album/")

    if "deezer" in lowered or "deezer." in lowered:
        if "/playlist/" in lowered:
            pid = u.split("/playlist/", 1)[1].split("?")[0].split("/")[0]
            if pid:
                return "deezer", pid
        raise ValueError("Deezer URL must contain /playlist/")

    # Bare ids: Spotify ids are base62 and 22 chars; Deezer ids are numeric.
    bare = u.split("?")[0].strip()
    if bare.isdigit():
        return "deezer", bare
    if 18 <= len(bare) <= 30 and bare.isalnum():
        return "spotify", bare
    raise ValueError(
        "Unsupported playlist URL. Use open.spotify.com or deezer.com playlist links."
    )


def _primary_search_source(settings: dict | None = None) -> str:
    """The first enabled search source from the user's priority list."""
    settings = settings if settings is not None else config.get_all()
    order = source_registry.normalize_priority(settings.get("source_priority"))
    return order[0] if order else "youtube"


def _search_url(track: dict, source_type: str) -> str:
    """Build the search job URL for a platform track on the top-priority source.

    The user's `source_priority` decides which source that is (the
    downloader's fallback chain walks the rest when one fails). The
    pipeline's matcher then gates the result against match_threshold, so a
    bad search result fails the job instead of landing the wrong song in the
    library.
    """
    artist = (track.get("artist") or "").split(",")[0].strip()
    return source_registry.build_search_job_url(
        _primary_search_source(), artist, track.get("title") or ""
    )


def _fallback_search_url(track: dict) -> str:
    """The second source in the priority list, for manual fallback work."""
    settings = config.get_all()
    order = source_registry.normalize_priority(settings.get("source_priority"))
    artist = (track.get("artist") or "").split(",")[0].strip()
    return source_registry.build_search_job_url(
        order[1] if len(order) > 1 else "soundcloud", artist, track.get("title") or ""
    )


# --------------------------------------------------------------------------
# Library / queue dedup
# --------------------------------------------------------------------------

async def _in_library(track: dict) -> bool:
    row = await fetch_one(
        "SELECT id FROM tracks WHERE source_url != '' AND source_url=? LIMIT 1",
        (track["id"],),
    )
    if row:
        return True
    t = normalize_text(track.get("title") or "")
    if not t:
        return False
    artist = normalize_text((track.get("artist") or "").split(",")[0])
    rows = await fetch_all(
        "SELECT title, artist, primary_artist, display_artist FROM tracks "
        "WHERE file_status != 'missing' AND title IS NOT NULL"
    )
    for r in rows:
        if normalize_text(r.get("title") or "") == t:
            have = {
                normalize_text(r.get(k) or "")
                for k in ("artist", "primary_artist", "display_artist")
            }
            if not artist or artist in have:
                return True
    return False


async def _already_queued(track: dict) -> bool:
    """True when an unfinished job already targets this track.

    Sync jobs are search-URL based (ytsearch1:...), so match on the normalised
    'artist title' text appearing in any queued job's source_url or query.
    """
    marker = normalize_text(f"{track.get('artist', '')} {track.get('title', '')}")
    if not marker:
        return False
    rows = await fetch_all(
        "SELECT source_url, query FROM jobs "
        "WHERE status NOT IN ('complete','failed','cancelled')"
    )
    for r in rows:
        haystack = normalize_text(f"{r.get('source_url') or ''} {r.get('query') or ''}")
        if marker in haystack:
            return True
    return False


def _load_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    return {str(x) for x in parsed} if isinstance(parsed, list) else set()


# --------------------------------------------------------------------------
# Sync core
# --------------------------------------------------------------------------

async def sync_playlist(playlist_id: int) -> dict:
    """Sync one synced_playlists row. Returns a summary dict."""
    row = await fetch_one("SELECT * FROM synced_playlists WHERE id=?", (playlist_id,))
    if not row:
        raise ValueError(f"Synced playlist {playlist_id} not found")

    settings = config.get_all()
    max_tracks = int(settings.get("sync_max_tracks", 200))
    search_source = _primary_search_source(settings)
    source_type = row["source_type"]
    source_ref = row["source_ref"] or row["url"]

    enqueued = skipped_owned = skipped_queued = 0
    new_track_ids: list[str] = []
    errors: list[str] = []

    try:
        async with aiohttp.ClientSession() as session:
            if source_type == "spotify":
                client = SpotifyClient(
                    session,
                    settings.get("spotify_client_id", ""),
                    settings.get("spotify_client_secret", ""),
                )
            else:
                client = DeezerClient(session)

            detail = await client.playlist(source_ref)
            # Deezer calls it `title`, Spotify `name`.
            playlist_name = detail.get("title") or detail.get("name") or row["name"]
            tracks = await client.playlist_tracks(source_ref)

        known = _load_ids(row["last_track_ids"])
        pass_budget = _MAX_ENQUEUES_PER_PASS

        for track in tracks:
            tid = track["id"]
            if tid in known:
                # Already considered on a previous pass; only record new ids.
                new_track_ids.append(tid)
                continue
            new_track_ids.append(tid)

            if len(new_track_ids) - len(known) > max_tracks:
                errors.append(f"Stopped: more than {max_tracks} new tracks this pass")
                break

            if enqueued + skipped_owned + skipped_queued >= max_tracks:
                errors.append(f"Reached per-playlist cap of {max_tracks}")
                break

            if await _in_library(track):
                skipped_owned += 1
                continue
            if await _already_queued(track):
                skipped_queued += 1
                continue
            if pass_budget <= 0:
                errors.append("Pass enqueue budget reached; remaining tracks next pass")
                break

            query_url = source_registry.build_search_job_url(
                search_source, (track.get("artist") or "").split(",")[0].strip(),
                track.get("title") or "",
            )
            existing = await fetch_one(
                "SELECT id FROM jobs WHERE source_url=? AND status NOT IN ('complete','failed','cancelled') LIMIT 1",
                (query_url,),
            )
            if existing:
                skipped_queued += 1
                continue

            await execute(
                "INSERT INTO jobs (source_url, source_type, query, title, artist, "
                "output_format, quality_profile, status) VALUES (?, 'youtube', ?, ?, ?, 'mp3', ?, 'pending')",
                (
                    query_url,
                    track.get("title") or "",
                    track.get("title") or "",
                    track.get("artist") or "",
                    row["quality_profile"] or "balanced",
                ),
            )
            enqueued += 1
            pass_budget -= 1

        await execute(
            "UPDATE synced_playlists SET last_synced=?, last_track_ids=?, last_error='', "
            "name=COALESCE(NULLIF(?, ''), name) WHERE id=?",
            (_iso(_utcnow()), json.dumps(new_track_ids[-2000:]), playlist_name, playlist_id),
        )
        await emit("sync_update", {
            "playlist_id": playlist_id,
            "name": playlist_name,
            "enqueued": enqueued,
            "owned": skipped_owned,
            "already_queued": skipped_queued,
            "errors": errors,
        })
        logger.info(
            f"Sync '{playlist_name}': {enqueued} enqueued, {skipped_owned} owned, "
            f"{skipped_queued} already queued"
        )
        return {
            "playlist_id": playlist_id,
            "name": playlist_name,
            "tracks": len(tracks),
            "enqueued": enqueued,
            "owned": skipped_owned,
            "already_queued": skipped_queued,
            "errors": errors,
        }

    except Exception as e:
        logger.error(f"Sync of playlist {playlist_id} failed: {e}", exc_info=True)
        await execute(
            "UPDATE synced_playlists SET last_error=?, last_synced=? WHERE id=?",
            (str(e)[:300], _iso(_utcnow()), playlist_id),
        )
        await emit("sync_update", {"playlist_id": playlist_id, "error": str(e)})
        raise


async def sync_due() -> list[int]:
    """Sync every auto_sync playlist whose interval has elapsed."""
    rows = await fetch_all("SELECT * FROM synced_playlists WHERE auto_sync=1")
    now = _utcnow()
    due: list[int] = []
    for row in rows:
        interval = row["sync_interval"] or int(config.get("sync_interval", 21600))
        last = row["last_synced"]
        if not last:
            due.append(row["id"])
            continue
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            due.append(row["id"])
            continue
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if now - last_dt >= timedelta(seconds=interval):
            due.append(row["id"])
    return due


async def poll_once(force: bool = False) -> int:
    """One scheduler pass. Returns total tracks enqueued."""
    due = await sync_due()
    if not due and not force:
        return 0
    total = 0
    for pid in due:
        try:
            result = await sync_playlist(pid)
            total += result["enqueued"]
        except Exception as e:
            logger.warning(f"Scheduled sync of playlist {pid} failed: {e}")
    return total


async def _sync_loop():
    while not _stop.is_set():
        try:
            await poll_once()
        except Exception as e:
            logger.error(f"Sync loop error: {e}", exc_info=True)
        try:
            await asyncio.sleep(int(config.get("sync_interval", 21600)))
        except asyncio.CancelledError:
            break


async def start_sync_loop():
    global _task, _stop
    _stop.clear()
    _task = asyncio.create_task(_sync_loop())
    logger.info("Playlist sync scheduler started")


async def stop_sync_loop():
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("Playlist sync scheduler stopped")
