"""Artist watch poller - detects new releases and queues them for download.

Started as a background task from `backend.main.lifespan`. Each cycle:

  1. Pick the watched artists whose `last_checked` is older than
     `watched_check_interval` (skipped entirely while disabled).
  2. Resolve the stored artist name to a Deezer artist id, cached on the row
     so we only pay for the search once.
  3. List every release - album, EP and single alike - newest first,
     paginating because Deezer caps a page at 100 entries.
  4. Diff those release ids against `known_releases` to find what is new.
  5. Pull the tracklist of each new release and enqueue any track that is
     not already in the library.

Release detection is deliberately generous: singles count, not just albums.
The `watch_backfill_days` setting caps only the *first* check for an artist so
that following somebody with 100+ releases does not enqueue their entire back
catalogue on day one.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from .. import config
from ..database import fetch_all, fetch_one, execute
from ..events import emit
from ..pipeline.normalize import normalize_text

logger = logging.getLogger(__name__)

DEEZER_API = "https://api.deezer.com"
DEEZER_TRACK_URL = "https://www.deezer.com/track/{id}"
DEEZER_ALBUM_URL = "https://www.deezer.com/album/{id}"

# Release types that count as a real release. Everything Deezer files a
# single under is included on purpose - the user asked for "anything new".
_RELEASE_TYPES = {"album", "single", "ep", "compilation"}

# Deezer refuses `limit` above 100.
_PAGE_SIZE = 100
_MAX_PAGES = 10  # hard ceiling so a malformed `total` cannot loop forever

_API_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Politeness delay between outbound Deezer calls, in seconds.
_API_DELAY = 0.25
# Cap on tracks enqueued per artist per cycle. A surprise 60-track deluxe
# reissue should not silently fill the queue with 60 jobs.
_MAX_TRACKS_PER_ARTIST_PER_CYCLE = 50

# Upper bound on remembered release ids per artist, so the column cannot grow
# without limit. Deezer returns releases newest-first, so trimming the oldest
# is always safe: a forgotten release can never reappear later.
_MAX_KNOWN_RELEASES = 1000

_task: asyncio.Task | None = None
_stop = asyncio.Event()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Deezer API
# --------------------------------------------------------------------------

class DeezerWatchClient:
    """Thin Deezer client scoped to a single polling session.

    Holds one aiohttp session for the whole cycle so connections are reused,
    and serialises every call behind a small delay to stay polite.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def get(self, endpoint: str) -> dict:
        await asyncio.sleep(_API_DELAY)
        url = f"{DEEZER_API}{endpoint}"
        async with self._session.get(url, timeout=_API_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def search_artist(self, name: str) -> dict | None:
        """Find the Deezer artist that best matches `name`."""
        from urllib.parse import quote

        data = await self.get(f"/search/artist?q={quote(name)}&limit=10")
        candidates = data.get("data") or []
        if not candidates:
            return None

        target = normalize_text(name)
        # Exact normalised name wins; otherwise the biggest fanbase is the
        # best guess, which is right far more often than the first result.
        exact = [c for c in candidates if normalize_text(c.get("name", "")) == target]
        pool = exact or candidates
        return max(pool, key=lambda c: c.get("nb_fan", 0))

    async def releases(self, artist_id: int) -> list[dict]:
        """Every release for an artist, newest first, fully paginated."""
        out: list[dict] = []
        index = 0
        total = None
        for _ in range(_MAX_PAGES):
            data = await self.get(f"/artist/{artist_id}/albums?index={index}&limit={_PAGE_SIZE}")
            items = data.get("data") or []
            if total is None:
                total = data.get("total", len(items))
            out.extend(items)
            index += _PAGE_SIZE
            if not items or index >= total:
                break
        return out

    async def album(self, release_id: int) -> dict:
        """Full release detail. The list endpoint has no artist field, so this
        is where we find out who the release is actually by."""
        return await self.get(f"/album/{release_id}")

    def album_tracks(self, detail: dict) -> list[dict]:
        return (detail.get("tracks") or {}).get("data") or []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _release_key(release: dict) -> str:
    return str(release.get("id") or "")


def _parse_release_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _load_known(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _is_wanted(release: dict) -> bool:
    if release.get("available") is False:
        return False
    rtype = (release.get("record_type") or "album").lower()
    return rtype in _RELEASE_TYPES


async def _in_library(url: str, title: str, artist: str) -> bool:
    """True when we already hold this track, by URL or by normalised name."""
    row = await fetch_one("SELECT id FROM tracks WHERE source_url=? LIMIT 1", (url,))
    if row:
        return True

    t = normalize_text(title)
    a = normalize_text(artist)
    if not t:
        return False
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
            if not a or a in have:
                return True
    return False


async def _already_queued(url: str) -> bool:
    row = await fetch_one(
        "SELECT id FROM jobs WHERE source_url=? AND status NOT IN ('complete','failed','cancelled') LIMIT 1",
        (url,),
    )
    return row is not None


async def _enqueue(url: str, title: str, artist: str, album: str,
                   quality: str, output_format: str) -> int | None:
    """Insert a pending job. Returns the job id, or None if already queued."""
    if await _already_queued(url):
        return None
    cursor = await execute(
        """
        INSERT INTO jobs (source_url, source_type, title, artist, album,
                          output_format, quality_profile, status)
        VALUES (?, 'deezer', ?, ?, ?, ?, ?, 'pending')
        """,
        (url, title, artist, album, output_format, quality),
    )
    return cursor.lastrowid


# --------------------------------------------------------------------------
# Core per-artist check
# --------------------------------------------------------------------------

async def check_artist(client: DeezerWatchClient, row: dict, settings: dict) -> int:
    """Check one watched artist. Returns the number of tracks enqueued."""
    watch_id = row["id"]
    name = row["artist_name"]
    known = _load_known(row.get("known_releases"))
    first_run = not known
    quality = row.get("quality_profile") or settings.get("default_quality", "balanced")
    output_format = settings.get("default_format", "mp3")

    artist_id = row.get("deezer_artist_id") or ""
    if not artist_id:
        found = await client.search_artist(name)
        if not found:
            raise LookupError(f"No Deezer artist matches '{name}'")
        artist_id = str(found["id"])
        await execute(
            "UPDATE watched_artists SET deezer_artist_id=?, last_error='' WHERE id=?",
            (artist_id, watch_id),
        )
        logger.info(f"Watch '{name}' resolved to Deezer artist {artist_id} ({found.get('name')})")

    releases = [r for r in await client.releases(int(artist_id)) if _is_wanted(r)]
    all_seen = [k for k in (_release_key(r) for r in releases) if k]

    if first_run:
        # Backfill guard: on the first pass we only enqueue releases inside
        # the backfill window, but we still record the *whole* catalogue as
        # seen so day two starts from "newest first" instead of re-queueing
        # the artist's entire back discography.
        days = int(settings.get("watch_backfill_days", 30))
        cutoff = _utcnow() - timedelta(days=days)
        candidates = []
        for r in releases:
            when = _parse_release_date(r.get("release_date"))
            if when is None or when >= cutoff:
                candidates.append(r)
        logger.info(
            f"Watch '{name}' first check: {len(candidates)} of {len(releases)} "
            f"releases inside the {days}-day backfill window"
        )
    else:
        previous = set(known)
        candidates = [r for r in releases if _release_key(r) not in previous]

    enqueued = 0
    newest_date = row.get("last_new_release") or ""

    if candidates:
        for release in candidates:
            if enqueued >= _MAX_TRACKS_PER_ARTIST_PER_CYCLE:
                logger.info(f"Watch '{name}': per-cycle cap reached, deferring the rest")
                break

            key = _release_key(release)
            if not key:
                continue

            try:
                detail = await client.album(int(key))
            except Exception as e:
                logger.warning(f"Watch '{name}': could not fetch release {release.get('title')!r}: {e}")
                continue

            # Deezer lists a release under *every* credited contributor, so
            # the album list is full of other people's records. Only act when
            # the watched artist is the primary artist of the release.
            primary = (detail.get("artist") or {}).get("id")
            if primary is not None and str(primary) != str(artist_id):
                logger.debug(
                    f"Watch '{name}': skipping {release.get('title')!r} "
                    f"(primary artist is {(detail.get('artist') or {}).get('name')!r})"
                )
                continue

            album_title = detail.get("title") or release.get("title") or ""
            for track in client.album_tracks(detail):
                if enqueued >= _MAX_TRACKS_PER_ARTIST_PER_CYCLE:
                    break
                track_id = track.get("id")
                if not track_id:
                    continue
                url = DEEZER_TRACK_URL.format(id=track_id)
                title = track.get("title") or ""
                artist = ((track.get("artist") or {}).get("name")) or name

                if await _in_library(url, title, artist):
                    logger.debug(f"Watch '{name}': already have {title!r}, skipping")
                    continue
                if await _enqueue(url, title, artist, album_title, quality, output_format):
                    enqueued += 1
                    logger.info(f"Watch '{name}': queued {title!r} from {album_title!r}")

            when = _parse_release_date(detail.get("release_date") or release.get("release_date"))
            if when and when.isoformat() > newest_date:
                newest_date = when.isoformat()

    # Remember everything currently visible, not just what we acted on, or the
    # next pass would rediscover the same untouched releases.
    merged = list(dict.fromkeys(all_seen + known))[:_MAX_KNOWN_RELEASES]

    await execute(
        """
        UPDATE watched_artists
           SET last_checked = ?,
               known_releases = ?,
               last_new_release = ?,
               last_enqueued = ?,
               last_error = ''
         WHERE id = ?
        """,
        (_iso(_utcnow()), json.dumps(merged), newest_date, enqueued, watch_id),
    )

    if enqueued:
        await emit("watch_update", {
            "watch_id": watch_id,
            "artist": name,
            "enqueued": enqueued,
            "releases": [r.get("title") for r in candidates][:10],
        })
        logger.info(f"Watch '{name}': enqueued {enqueued} new track(s)")
    return enqueued


# --------------------------------------------------------------------------
# Poller lifecycle
# --------------------------------------------------------------------------

async def poll_once(force: bool = False) -> int:
    """Run one polling pass. Returns the total number of tracks enqueued."""
    settings = config.get_all()
    if not settings.get("watch_enabled", True):
        logger.debug("Watcher disabled via settings")
        return 0

    interval = int(settings.get("watched_check_interval", 3600))
    rows = await fetch_all("SELECT * FROM watched_artists ORDER BY artist_name ASC")
    if not rows:
        return 0

    now = _utcnow()
    due = []
    for row in rows:
        if force:
            due.append(row)
            continue
        last = row.get("last_checked")
        if not last:
            due.append(row)
            continue
        try:
            when = datetime.fromisoformat(last)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            due.append(row)
            continue
        if (now - when) >= timedelta(seconds=interval):
            due.append(row)

    if not due:
        logger.debug("Watcher: no artists due for a check")
        return 0

    logger.info(f"Watcher: checking {len(due)} watched artist(s)")
    total = 0
    connector = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:
        client = DeezerWatchClient(session)
        for row in due:
            try:
                total += await check_artist(client, row, settings)
            except Exception as e:
                logger.warning(f"Watcher: check failed for '{row.get('artist_name')}': {e}")
                await execute(
                    "UPDATE watched_artists SET last_checked=?, last_error=? WHERE id=?",
                    (_iso(_utcnow()), str(e)[:200], row["id"]),
                )
                try:
                    await emit("watch_update", {
                        "watch_id": row["id"],
                        "artist": row.get("artist_name"),
                        "error": str(e)[:200],
                    })
                except Exception:
                    pass
    return total


async def _watcher_loop():
    while not _stop.is_set():
        try:
            await poll_once()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.error("Watcher loop error", exc_info=True)

        interval = int(config.get("watched_check_interval", 3600))
        logger.info(f"Watcher: next check in {interval}s")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=max(interval, 30))
        except asyncio.TimeoutError:
            continue


async def start_watcher():
    global _task
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_watcher_loop())
    logger.info("Artist watcher started")


async def stop_watcher():
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    logger.info("Artist watcher stopped")
