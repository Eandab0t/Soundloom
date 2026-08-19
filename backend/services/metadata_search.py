"""Multi-source metadata search engine.

Searches YouTube, Spotify, MusicBrainz, and SoundCloud for track metadata
using async I/O for concurrent queries.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import yt_dlp

logger = logging.getLogger(__name__)

_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
_MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"
_USER_AGENT = "BigPickle/0.1 ( https://github.com/anomalyco )"
_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MetadataSearchResult:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    track_number: int = 0
    total_tracks: int = 0
    genre: str = ""
    duration: float = 0.0
    source: str = ""
    confidence: float = 0.0
    source_url: str = ""
    thumbnail_url: str = ""


# ---------------------------------------------------------------------------
# Spotify token cache
# ---------------------------------------------------------------------------

_spotify_token: str = ""
_spotify_token_expiry: float = 0.0


async def _get_spotify_token(
    session: aiohttp.ClientSession,
    client_id: str,
    client_secret: str,
) -> str:
    """Obtain or reuse a cached Spotify Client Credentials token."""
    global _spotify_token, _spotify_token_expiry  # noqa: PLW0603

    now = time.time()
    if _spotify_token and now < _spotify_token_expiry:
        return _spotify_token

    if not client_id or not client_secret:
        raise ValueError(
            "Spotify client_id and client_secret are required for authentication"
        )

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    async with session.post(
        _SPOTIFY_TOKEN_URL,
        data=data,
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    _spotify_token = payload["access_token"]
    _spotify_token_expiry = now + payload.get("expires_in", 3600) - 60
    return _spotify_token


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def _parse_youtube_entry(entry: dict[str, Any], confidence: float) -> MetadataSearchResult:
    """Convert a flat yt-dlp entry dict into a MetadataSearchResult."""
    title = entry.get("title", "")
    channel = entry.get("channel", entry.get("uploader", ""))
    duration = float(entry.get("duration") or 0)
    url = entry.get("url") or entry.get("webpage_url") or ""
    if entry.get("id") and not url:
        url = f"https://www.youtube.com/watch?v={entry['id']}"
    thumbnail = entry.get("thumbnail") or entry.get("thumbnails", [{}])[0].get("url", "") if entry.get("thumbnails") else ""

    return MetadataSearchResult(
        title=title,
        artist=channel,
        source="youtube",
        confidence=confidence,
        source_url=url,
        duration=duration,
        thumbnail_url=thumbnail,
    )


async def search_youtube(query: str, max_results: int = 5) -> list[MetadataSearchResult]:
    """Search YouTube via yt-dlp and return structured results."""
    search_term = f"ytsearch{max_results}:{query}"

    def _search() -> list[MetadataSearchResult]:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
        entries = info.get("entries") or []
        results: list[MetadataSearchResult] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            confidence = max(0.1, 0.9 - idx * 0.15)
            results.append(_parse_youtube_entry(entry, confidence))
        return results

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _search)
    except Exception:
        logger.exception("YouTube search failed for query: %s", query)
        return []


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

def _parse_spotify_track(track: dict[str, Any], confidence: float) -> MetadataSearchResult:
    """Convert a Spotify track object into a MetadataSearchResult."""
    title = track.get("name", "")
    artists = track.get("artists", [])
    artist_name = artists[0]["name"] if artists else ""
    album_info = track.get("album", {})
    album_name = album_info.get("name", "")
    album_artists = album_info.get("artists", [])
    album_artist_name = album_artists[0]["name"] if album_artists else ""
    release_date = album_info.get("release_date", "")
    year = release_date[:4] if release_date else ""
    track_number = track.get("track_number", 0)
    total_tracks = album_info.get("total_tracks", 0)
    duration_ms = track.get("duration_ms", 0)
    duration = duration_ms / 1000.0 if duration_ms else 0.0
    external_url = track.get("external_urls", {}).get("spotify", "")
    images = album_info.get("images", [])
    thumbnail = images[0]["url"] if images else ""
    genres: list[str] = []  # Spotify track search doesn't return genres directly

    return MetadataSearchResult(
        title=title,
        artist=artist_name,
        album=album_name,
        album_artist=album_artist_name,
        year=year,
        track_number=track_number,
        total_tracks=total_tracks,
        genre=", ".join(genres) if genres else "",
        duration=duration,
        source="spotify",
        confidence=confidence,
        source_url=external_url,
        thumbnail_url=thumbnail,
    )


async def search_spotify(
    query: str,
    client_id: str = "",
    client_secret: str = "",
    max_results: int = 5,
) -> list[MetadataSearchResult]:
    """Search Spotify via Web API using Client Credentials flow."""
    if not client_id or not client_secret:
        logger.warning("Spotify credentials not provided, skipping Spotify search")
        return []

    headers: dict[str, str] = {}
    try:
        async with aiohttp.ClientSession() as session:
            token = await _get_spotify_token(session, client_id, client_secret)
            headers["Authorization"] = f"Bearer {token}"

            params = {
                "q": query,
                "type": "track",
                "limit": str(max_results),
            }

            async with session.get(
                _SPOTIFY_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        tracks = data.get("tracks", {}).get("items", [])
        results: list[MetadataSearchResult] = []
        for idx, track in enumerate(tracks):
            confidence = max(0.1, 0.9 - idx * 0.15)
            results.append(_parse_spotify_track(track, confidence))
        return results

    except aiohttp.ClientError:
        logger.exception("Spotify API request failed for query: %s", query)
        return []
    except Exception:
        logger.exception("Spotify search failed for query: %s", query)
        return []


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------

def _parse_musicbrainz_recording(recording: dict[str, Any], confidence: float) -> MetadataSearchResult:
    """Convert a MusicBrainz recording object into a MetadataSearchResult."""
    title = recording.get("title", "")
    artist_credit = recording.get("artist-credit", [])
    artist_name = artist_credit[0]["name"] if artist_credit else ""
    releases = recording.get("releases", [])
    album_name = ""
    album_artist_name = ""
    year = ""
    track_number = 0
    total_tracks = 0
    if releases:
        release = releases[0]
        album_name = release.get("title", "")
        release_date = release.get("date", "")
        year = release_date[:4] if release_date else ""
        track_number = release.get("track-number", 0) or 0
        release_group = release.get("release-group", {})
        if release_group:
            total_tracks = release_group.get("primary-type-id", 0) or 0
        # MusicBrainz doesn't directly give album artist in recording search;
        # fall back to the recording artist
        album_artist_name = artist_name

    length = recording.get("length")
    duration = length / 1000.0 if length else 0.0
    mbid = recording.get("id", "")
    source_url = f"https://musicbrainz.org/recording/{mbid}" if mbid else ""

    return MetadataSearchResult(
        title=title,
        artist=artist_name,
        album=album_name,
        album_artist=album_artist_name,
        year=year,
        track_number=track_number,
        total_tracks=total_tracks,
        duration=duration,
        source="musicbrainz",
        confidence=confidence,
        source_url=source_url,
    )


async def search_musicbrainz(query: str, max_results: int = 5) -> list[MetadataSearchResult]:
    """Search MusicBrainz recordings (free, no API key required)."""
    params = {
        "query": query,
        "fmt": "json",
        "limit": str(max_results),
    }
    headers = {"User-Agent": _USER_AGENT}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _MUSICBRAINZ_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        recordings = data.get("recordings", [])
        results: list[MetadataSearchResult] = []
        for idx, recording in enumerate(recordings):
            confidence = max(0.1, 0.85 - idx * 0.15)
            results.append(_parse_musicbrainz_recording(recording, confidence))
        return results

    except aiohttp.ClientError:
        logger.exception("MusicBrainz API request failed for query: %s", query)
        return []
    except Exception:
        logger.exception("MusicBrainz search failed for query: %s", query)
        return []


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

def _parse_soundcloud_entry(entry: dict[str, Any], confidence: float) -> MetadataSearchResult:
    """Convert a flat yt-dlp SoundCloud entry into a MetadataSearchResult."""
    title = entry.get("title", "")
    channel = entry.get("channel", entry.get("uploader", ""))
    duration = float(entry.get("duration") or 0)
    url = entry.get("url") or entry.get("webpage_url") or ""
    if entry.get("id") and not url:
        url = f"https://soundcloud.com/user-xxx/{entry.get('url', entry.get('id', ''))}"
    thumbnail = entry.get("thumbnail") or ""

    return MetadataSearchResult(
        title=title,
        artist=channel,
        source="soundcloud",
        confidence=confidence,
        source_url=url,
        duration=duration,
        thumbnail_url=thumbnail,
    )


async def search_soundcloud(query: str, max_results: int = 5) -> list[MetadataSearchResult]:
    """Search SoundCloud via yt-dlp and return structured results."""
    search_term = f"scsearch{max_results}:{query}"

    def _search() -> list[MetadataSearchResult]:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
        entries = info.get("entries") or []
        results: list[MetadataSearchResult] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            confidence = max(0.1, 0.8 - idx * 0.15)
            results.append(_parse_soundcloud_entry(entry, confidence))
        return results

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _search)
    except Exception:
        logger.exception("SoundCloud search failed for query: %s", query)
        return []


# ---------------------------------------------------------------------------
# Unified search
# ---------------------------------------------------------------------------

_PROVIDER_MAP: dict[str, Any] = {
    "youtube": search_youtube,
    "spotify": search_spotify,
    "musicbrainz": search_musicbrainz,
    "soundcloud": search_soundcloud,
}


async def search_all(
    query: str,
    sources: list[str] | None = None,
    **kwargs: Any,
) -> list[MetadataSearchResult]:
    """Run enabled providers concurrently and return results sorted by confidence.

    Args:
        query: Search query string.
        sources: List of source names to enable. ``None`` means all sources.
        **kwargs: Extra keyword arguments forwarded to individual providers
            (e.g. ``client_id`` and ``client_secret`` for Spotify).

    Returns:
        Flat list of :class:`MetadataSearchResult` sorted by descending confidence.
    """
    enabled = sources or list(_PROVIDER_MAP.keys())
    tasks: list[asyncio.Task[list[MetadataSearchResult]]] = []

    for source in enabled:
        provider = _PROVIDER_MAP.get(source)
        if provider is None:
            logger.warning("Unknown search source: %s", source)
            continue

        # Only pass kwargs relevant to the provider
        import inspect

        sig = inspect.signature(provider)
        filtered_kwargs = {
            k: v for k, v in kwargs.items() if k in sig.parameters
        }
        tasks.append(asyncio.create_task(provider(query, **filtered_kwargs)))

    if not tasks:
        return []

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    all_results: list[MetadataSearchResult] = []
    for result in gathered:
        if isinstance(result, Exception):
            logger.warning("Search provider raised an exception: %s", result)
            continue
        if isinstance(result, list):
            all_results.extend(result)

    all_results.sort(key=lambda r: r.confidence, reverse=True)
    return all_results


async def search_for_track(
    title: str,
    artist: str = "",
    album: str = "",
    **kwargs: Any,
) -> list[MetadataSearchResult]:
    """Build a smart query from track metadata fields and search all sources.

    Query construction:
        - ``title + artist`` is preferred.
        - ``album`` is appended if provided.
    """
    parts: list[str] = []
    if title:
        parts.append(title)
    if artist:
        parts.append(artist)
    if album:
        parts.append(album)

    query = " ".join(parts).strip()
    if not query:
        return []

    return await search_all(query, **kwargs)
