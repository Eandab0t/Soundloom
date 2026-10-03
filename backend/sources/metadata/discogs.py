"""Discogs fetcher - strongest for release-level metadata.

Discogs knows about vinyl and physical editions, so it fills gaps that
streaming services never carry (original year, pressing details). Needs a
free personal access token from discogs.com/settings/developers.
"""
import asyncio

import aiohttp

from .base import DEFAULT_TIMEOUT, FetchResult, MetadataFetcher, _to_int
from ...pipeline.models import TrackMetadata

SEARCH_URL = "https://api.discogs.com/database/search"


class DiscogsFetcher(MetadataFetcher):
    name = "discogs"
    description = "Release-level metadata including vinyl editions. Needs a free token."

    def __init__(self, settings: dict):
        super().__init__(settings)
        self._token = settings.get("discogs_token") or ""
        contact = settings.get("musicbrainz_email") or "none"
        self._headers = {
            "User-Agent": f"Soundloom/0.2.0 ( contact: {contact} )",
            "Accept": "application/json",
        }

    def available(self) -> bool:
        return bool(self._token)

    def unavailable_reason(self) -> str:
        if not self._token:
            return "No Discogs token set. Create a free one at discogs.com/settings/developers."
        return "Available."

    async def fetch(self, track: TrackMetadata, file_path: str = "") -> FetchResult:
        if not self._token:
            return FetchResult(matched=False, source=self.name,
                               notes=["No Discogs token configured."])
        if not track.album and not track.artist:
            return FetchResult(matched=False, source=self.name,
                               notes=["Need at least an artist or album to search Discogs."])

        params: dict[str, str] = {"type": "release", "token": self._token, "per_page": "5"}
        artist = track.primary_artist or track.display_artist or track.artist
        if artist:
            params["artist"] = artist
        if track.album:
            params["release_title"] = track.album
        elif track.title:
            params["track"] = track.title

        try:
            timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(SEARCH_URL, params=params, headers=self._headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return FetchResult(matched=False, source=self.name, notes=["Request timed out."])
        except aiohttp.ClientError as exc:
            return FetchResult(matched=False, source=self.name, notes=[f"Request failed: {exc}"])
        except ValueError as exc:
            return FetchResult(matched=False, source=self.name, notes=[f"Bad response: {exc}"])

        results = data.get("results") or []
        if not results:
            return FetchResult(matched=False, source=self.name, notes=["No matches."])

        # Discogs returns no normalized score, just a relevance order, so
        # rank position stands in for confidence.
        best = results[0]
        confidence = 0.85 if len(results) == 1 else 0.75

        metadata = _result_to_metadata(best)
        if metadata is None:
            return FetchResult(matched=False, source=self.name,
                               notes=["Result had no parseable title."])

        return FetchResult(
            matched=True,
            confidence=confidence,
            source=self.name,
            tags=metadata,
            notes=[f"Discogs release: {best.get('title', '?')}"],
        )


def _result_to_metadata(result: dict):
    raw_title = result.get("title")
    if not raw_title:
        return None

    artist = None
    album = None
    if " - " in raw_title:
        artist, album = raw_title.split(" - ", 1)
        artist = artist.strip() or None
        album = album.strip() or None
    else:
        album = raw_title.strip() or None

    genres = result.get("genre") or []
    return TrackMetadata(
        title="",
        artist=artist or "",
        primary_artist=artist or "",
        display_artist=artist or "",
        album_artist=artist or "",
        album=album or "",
        year=_to_int(result.get("year")) or 0,
        genre=genres[0] if genres else "",
    )


__all__ = ["DiscogsFetcher"]
