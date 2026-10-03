"""AcoustID fetcher - identifies a track from its audio, not its tags.

This is the only method that can recover metadata for a completely untagged
or mislabelled file, which is exactly the case the download pipeline cannot
help with. Two steps:

1. Run Chromaprint's ``fpcalc`` over the file to get a fingerprint + duration.
2. POST that to the AcoustID v2 lookup API with a free API key.

``fpcalc`` is an optional system dependency. When it is missing this fetcher
reports itself unavailable with an explanation instead of raising, and the
caller falls back to a tag-based backend.
"""
import asyncio
import json
import shutil
import subprocess

import aiohttp

from .base import DEFAULT_TIMEOUT, FetchResult, MetadataFetcher
from ...pipeline.models import TrackMetadata

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
FPCALC_TIMEOUT = 30


class FingerprintError(RuntimeError):
    pass


class AcoustIDFetcher(MetadataFetcher):
    name = "acoustid"
    description = "Identifies by audio fingerprint. Needs a free key and the fpcalc binary."

    def __init__(self, settings: dict):
        super().__init__(settings)
        self._api_key = settings.get("acoustid_api_key") or ""
        self._fpcalc = shutil.which("fpcalc")

    def available(self) -> bool:
        return bool(self._api_key) and bool(self._fpcalc)

    def unavailable_reason(self) -> str:
        if not self._fpcalc:
            return ("fpcalc (Chromaprint) is not installed. Download it from "
                    "acoustid.org/chromaprint and put it on PATH.")
        if not self._api_key:
            return "No AcoustID API key set. Get a free one at acoustid.org/api-key."
        return "Available."

    async def fetch(self, track: TrackMetadata, file_path: str = "") -> FetchResult:
        if not self._api_key:
            return FetchResult(matched=False, source=self.name,
                               notes=["No AcoustID API key configured."])
        if not self._fpcalc:
            return FetchResult(matched=False, source=self.name,
                               notes=["fpcalc (Chromaprint) not found on PATH."])
        if not file_path:
            return FetchResult(matched=False, source=self.name,
                               notes=["Fingerprinting needs a file path."])

        try:
            duration, fingerprint = await self._fingerprint(file_path)
        except FingerprintError as exc:
            return FetchResult(matched=False, source=self.name, notes=[str(exc)])

        payload = {
            "client": self._api_key,
            "duration": duration,
            "fingerprint": fingerprint,
            "meta": "recordings+releasegroups+compress",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(LOOKUP_URL, data=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return FetchResult(matched=False, source=self.name, notes=["Request timed out."])
        except aiohttp.ClientError as exc:
            return FetchResult(matched=False, source=self.name, notes=[f"Request failed: {exc}"])
        except ValueError as exc:
            return FetchResult(matched=False, source=self.name, notes=[f"Bad response: {exc}"])

        if data.get("status") != "ok":
            message = (data.get("error") or {}).get("message", "AcoustID error")
            return FetchResult(matched=False, source=self.name, notes=[message])

        results = data.get("results") or []
        if not results:
            return FetchResult(matched=False, source=self.name, notes=["No fingerprint match."])

        best = max(results, key=lambda r: r.get("score", 0))
        recordings = best.get("recordings") or []
        if not recordings:
            return FetchResult(
                matched=False, source=self.name,
                notes=["Fingerprint matched but no recording metadata attached."],
            )

        confidence = min(1.0, max(0.0, best.get("score", 0.0)))
        return FetchResult(
            matched=True,
            confidence=confidence,
            source=self.name,
            tags=_recording_to_metadata(recordings[0]),
            notes=[f"AcoustID fingerprint score {confidence:.2f}"],
        )

    async def _fingerprint(self, path: str) -> tuple[int, str]:
        """Run fpcalc off the event loop and return (duration, fingerprint)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run_fpcalc, path)

    def _run_fpcalc(self, path: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                [self._fpcalc, "-json", path],
                capture_output=True, text=True,
                timeout=FPCALC_TIMEOUT, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FingerprintError(f"fpcalc failed to run: {exc}") from exc

        if proc.returncode != 0:
            raise FingerprintError(f"fpcalc exited {proc.returncode}: {proc.stderr.strip()}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise FingerprintError(f"Could not parse fpcalc output: {exc}") from exc

        duration = payload.get("duration")
        fingerprint = payload.get("fingerprint")
        if duration is None or fingerprint is None:
            raise FingerprintError("fpcalc output missing duration/fingerprint.")
        return int(round(duration)), fingerprint


def _recording_to_metadata(recording: dict) -> TrackMetadata:
    artists = recording.get("artists") or []
    artist = artists[0].get("name") if artists else None
    title = recording.get("title")

    album = None
    groups = recording.get("releasegroups") or []
    if groups:
        # Prefer an Album type group over singles/compilations.
        best = next((g for g in groups if g.get("type") == "Album"), groups[0])
        album = best.get("title")

    return TrackMetadata(
        title=title or "",
        artist=artist or "",
        primary_artist=artist or "",
        display_artist=artist or "",
        album_artist=artist or "",
        album=album or "",
    )


__all__ = ["AcoustIDFetcher", "FingerprintError"]
