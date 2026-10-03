"""Spotify fetcher via the Client Credentials flow.

Needs a free client id/secret. Only ever uses app-level credentials, never a
user session, so no listener data is involved. Spotify is authoritative about
canonical artist/title spellings and ISRCs, which makes it a good final
normaliser even though it carries no release dates.
"""
import asyncio
import base64

import aiohttp

from .base import DEFAULT_TIMEOUT, FetchResult, MetadataFetcher
from ...pipeline.models import TrackMetadata
from ...pipeline.normalize import parse_artists

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
# Cached token; Spotify tokens last an hour.
_TOKEN_TTL = 3300


class SpotifyFetcher(MetadataFetcher):
    name = "spotify"
    description = "Canonical spellings and ISRCs. Needs a free client id/secret."

    def __init__(self, settings: dict):
        super().__init__(settings)
        self._client_id = settings.get("spotify_client_id") or ""
        self._client_secret = settings.get("spotify_client_secret") or ""
        self._token = ""
        self._token_expiry = 0

    def available(self) -> bool:
        return bool(self._client_id) and bool(self._client_secret)

    def unavailable_reason(self) -> str:
        if not (self._client_id and self._client_secret):
            return "No Spotify credentials set. Create an app at developer.spotify.com."
        return "Available."

    async def _access_token(self, session: aiohttp.ClientSession) -> str:
        import time
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token

        raw = f"{self._client_id}:{self._client_secret}".encode()
        auth = base64.b64encode(raw).decode()
        try:
            async with session.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            raise RuntimeError(f"Spotify auth failed: {exc}") from exc

        self._token = payload.get("access_token", "")
        self._token_expiry = now + int(payload.get("expires_in", _TOKEN_TTL))
        return self._token

    async def fetch(self, track: TrackMetadata, file_path: str = "") -> FetchResult:
        if not self.available():
            return FetchResult(matched=False, source=self.name,
                               notes=["Spotify credentials not configured."])
        if not track.title:
            return FetchResult(matched=False, source=self.name,
                               notes=["Need a title to search Spotify."])

        artist = track.primary_artist or track.display_artist or track.artist
        query = f"track:{track.title}" + (f" artist:{artist}" if artist else "")

        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                token = await self._access_token(session)
                params = {"q": query, "type": "track", "limit": 5}
                async with session.get(
                    SEARCH_URL, params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return FetchResult(matched=False, source=self.name, notes=["Request timed out."])
        except RuntimeError as exc:
            return FetchResult(matched=False, source=self.name, notes=[str(exc)])
        except aiohttp.ClientError as exc:
            return FetchResult(matched=False, source=self.name, notes=[f"Request failed: {exc}"])
        except ValueError as exc:
            return FetchResult(matched=False, source=self.name, notes=[f"Bad response: {exc}"])

        items = ((data.get("tracks") or {}).get("items")) or []
        if not items:
            return FetchResult(matched=False, source=self.name, notes=["No matches."])

        best = items[0]
        return FetchResult(
            matched=True,
            confidence=0.9 if len(items) == 1 else 0.8,
            source=self.name,
            tags=_item_to_metadata(best),
            notes=[f"Spotify track: {best.get('name', '?')}"],
        )


def _item_to_metadata(item: dict) -> TrackMetadata:
    name = item.get("name") or ""
    artist_name = ((item.get("artists") or [{}])[0]).get("name") or ""
    parsed = parse_artists(artist_name)
    album = item.get("album") or {}

    return TrackMetadata(
        title=name,
        artist=artist_name,
        primary_artist=parsed["primary"],
        featured_artists=parsed["featured"],
        display_artist=parsed["display"],
        album_artist=parsed["primary"],
        album=album.get("name") or "",
        # Spotify gives a full date; take the year.
        year=int(str(album.get("release_date") or "")[:4] or 0) if album.get("release_date") else 0,
    )


__all__ = ["SpotifyFetcher"]
