"""MusicBrainz metadata fetcher.

MusicBrainz's web service is free and needs no API key, only a descriptive
User-Agent so they can contact us if the client misbehaves. We search the
``recording`` endpoint with whatever artist/title/album we already have and
turn the best-scoring hit into a TrackMetadata.

Their etiquette asks for no more than ~1 request/second, hence the delay.
"""
import asyncio

import aiohttp

from .base import API_DELAY, DEFAULT_TIMEOUT, FetchResult, MetadataFetcher, _to_int
from ...pipeline.models import TrackMetadata

SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"

# Lucene characters that would otherwise break the query.
# NB: no space - escaping whitespace breaks the phrase match.
_ESCAPE = set('+-&&||!(){}[]^"~*?:\\/')


class MusicBrainzFetcher(MetadataFetcher):
    name = "musicbrainz"
    description = "Free, no API key. Matches on existing artist/title/album tags."

    def __init__(self, settings: dict):
        super().__init__(settings)
        contact = settings.get("musicbrainz_email") or "no-contact-provided@example.com"
        self._headers = {
            "User-Agent": f"Soundloom/0.2.0 ( {contact} )",
            "Accept": "application/json",
        }

    async def fetch(self, track: TrackMetadata, file_path: str = "") -> FetchResult:
        if not track.title and not track.album:
            return FetchResult(
                matched=False, source=self.name,
                notes=["Need at least a title or album to search MusicBrainz."],
            )

        query = _build_query(track)
        params = {"query": query, "fmt": "json", "limit": 5}
        try:
            await asyncio.sleep(API_DELAY)
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

        recordings = data.get("recordings") or []
        if not recordings:
            return FetchResult(matched=False, source=self.name, notes=["No matches."])

        best = max(recordings, key=lambda r: r.get("score", 0))
        confidence = min(1.0, max(0.0, best.get("score", 0) / 100.0))

        return FetchResult(
            matched=True,
            confidence=confidence,
            source=self.name,
            tags=_recording_to_metadata(best),
            notes=[f"MusicBrainz score {best.get('score', 0)}/100"],
        )


def _build_query(track: TrackMetadata) -> str:
    parts: list[str] = []
    if track.title:
        parts.append(f'recording:"{_escape(track.title)}"')
    artist = track.primary_artist or track.display_artist or track.artist
    if artist:
        parts.append(f'artist:"{_escape(artist)}"')
    if track.album:
        parts.append(f'release:"{_escape(track.album)}"')
    return " AND ".join(parts) if parts else '"unknown"'


def _escape(value: str) -> str:
    out = []
    for ch in value:
        if ch in _ESCAPE:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _recording_to_metadata(recording: dict) -> TrackMetadata:
    title = recording.get("title")
    credit = recording.get("artist-credit") or []
    artist = credit[0].get("name") if credit else None

    album = None
    year = None
    track_no = None
    total_tracks = None

    releases = recording.get("releases") or []
    if releases:
        # Prefer the earliest release - closest to the "original".
        best_release = min(releases, key=lambda r: r.get("date") or "9999")
        album = best_release.get("title")
        date = best_release.get("date")
        if date:
            year = _to_int(date.split("-", 1)[0])
        for medium in best_release.get("media") or []:
            total_tracks = medium.get("track-count") or total_tracks
            for t in medium.get("tracks") or []:
                rec = t.get("recording", {}) or {}
                if rec.get("id") == recording.get("id") or t.get("title") == title:
                    track_no = _to_int(t.get("number") or t.get("position"))
                    break
            if track_no is not None:
                break

    return TrackMetadata(
        title=title or "",
        artist=artist or "",
        primary_artist=artist or "",
        display_artist=artist or "",
        album_artist=artist or "",
        album=album or "",
        track_number=track_no or 0,
        year=year or 0,
    )


__all__ = ["MusicBrainzFetcher"]
