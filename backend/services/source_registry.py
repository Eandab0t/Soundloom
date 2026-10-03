"""Audio source registry - which search sources exist, and in what order.

Soundloom never downloads audio from streaming platforms; audio always comes
from one of the search-based downloaders:

  * ``youtube``     - ``ytsearch1:`` via yt-dlp (the historical default)
  * ``deezer``      - Deezer public search API -> best track -> deemix download
  * ``soundcloud``  - ``scsearch1:`` via yt-dlp

``source_priority`` (a settings list of source keys) decides which search
source a sync/watch/artist-title job uses first, and ``_fallback_chain`` in
the downloader walks the remaining sources when one fails. Direct URL jobs
are pinned to their source and never rerouted.

Deezer search needs no credentials; YouTube and SoundCloud need none either
(though YouTube hits bot checks more often, which is exactly why the order
is user-configurable).
"""
import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

DEEZER_API = "https://api.deezer.com"

# key -> human label for the settings UI. Order here is only the default.
SOURCES = {
    "youtube": "YouTube (yt-dlp search)",
    "deezer": "Deezer (search + deemix)",
    "soundcloud": "SoundCloud (yt-dlp search)",
}
DEFAULT_PRIORITY = ["youtube", "deezer", "soundcloud"]


def normalize_priority(raw, valid=None) -> list[str]:
    """Coerce user/settings input into a clean, complete priority list.

    Unknown keys are dropped, missing known keys are appended in default
    order, duplicates collapse, and an empty/garbage value resets to the
    default. Always returns every known source exactly once.
    """
    valid = valid if valid is not None else set(SOURCES)
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_PRIORITY)
    out: list[str] = []
    for item in raw:
        if item in valid and item not in out:
            out.append(item)
    out.extend(s for s in DEFAULT_PRIORITY if s not in out)
    return out


def search_prefix_for(source_key: str) -> str:
    """The yt-dlp search prefix for a source ('deezer' is handled upstream)."""
    return {"youtube": "ytsearch1:", "soundcloud": "scsearch1:"}.get(source_key, "ytsearch1:")


def build_search_job_url(source_key: str, artist: str, title: str) -> str:
    """The job `source_url` for 'artist title' on the given search source.

    Deezer jobs use a ``dzsearch:`` scheme (resolved to a real track URL at
    download time); yt-dlp sources use their native search prefixes.
    """
    artist = (artist or "").split(",")[0].strip()
    query = f"{artist} {title or ''}".strip()
    if source_key == "deezer":
        return f"dzsearch:{quote(query)}"
    return f"{search_prefix_for(source_key)}{quote(query)}"


def is_search_job_url(url: str) -> bool:
    """True when a job source_url is a search (not a pinned direct URL)."""
    return url.startswith(("ytsearch1:", "scsearch1:", "dzsearch:"))


def search_source_of(url: str) -> str | None:
    """Which source a search job URL targets ('dzsearch:' -> 'deezer')."""
    if url.startswith("dzsearch:"):
        return "deezer"
    if url.startswith("ytsearch1:"):
        return "youtube"
    if url.startswith("scsearch1:"):
        return "soundcloud"
    return None


async def deezer_search_track(artist: str, title: str) -> str | None:
    """Best Deezer track URL for 'artist title', or None when nothing matches.

    Scores candidates by exact-ish artist + title containment, then duration
    proximity, then rank - the same spirit as the pipeline matcher, cheap
    edition. Returns a deezer.com/track/ URL that the existing
    DeezerResolver + DeezerDownloader already know how to handle.
    """
    query = f"{artist} {title}".strip()
    if not query:
        return None
    url = f"{DEEZER_API}/search?q={quote(query)}&limit=10"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("Deezer search failed for %r: %s", query, e)
        return None

    results = data.get("data") or []
    if not results:
        return None

    def _score(item: dict) -> float:
        t_artist = (item.get("artist") or {}).get("name", "").lower()
        t_title = (item.get("title") or "").lower()
        score = 0.0
        if artist.lower() in t_artist or t_artist in artist.lower():
            score += 2.0
        if title.lower() in t_title or t_title in title.lower():
            score += 2.0
        score += max(0.0, 1.0 - (item.get("rank", 0) or 0) / 1_000_000.0)
        return score

    best = max(results, key=_score)
    track_id = best.get("id")
    return f"https://www.deezer.com/track/{track_id}" if track_id else None
