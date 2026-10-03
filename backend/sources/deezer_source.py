"""Deezer source adapter using deemix CLI and Deezer public API.

Downloads from Deezer via the bambanah/deemix fork (TypeScript).
Metadata resolved via Deezer's public API (no auth needed for basic info).
"""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import aiohttp

from ..pipeline.interfaces import SourceResolver, DownloadProvider
from ..pipeline.models import TrackMetadata, SourceCandidate, SourceType
from ..pipeline.normalize import parse_artists

logger = logging.getLogger(__name__)

_DEEZER_URL_RE = re.compile(
    r"deezer\.com/(?:\w+/)?(?:track|album|playlist|artist)/(\d+)"
)
_DEEZER_API = "https://api.deezer.com"


def detect_deezer(url: str) -> str | None:
    """Return Deezer entity type ('track', 'album', 'playlist', 'artist') or None."""
    m = _DEEZER_URL_RE.search(url)
    if m:
        # Re-check which group matched
        if "/track/" in url:
            return "track"
        if "/album/" in url:
            return "album"
        if "/playlist/" in url:
            return "playlist"
        if "/artist/" in url:
            return "artist"
    return None


def is_deezer_url(url: str) -> bool:
    return detect_deezer(url) is not None


async def _deezer_api_get(endpoint: str) -> dict:
    """Fetch from Deezer public API."""
    url = f"{_DEEZER_API}{endpoint}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _resolve_deezer_track(track_id: int) -> TrackMetadata:
    """Resolve a single Deezer track to TrackMetadata."""
    data = await _deezer_api_get(f"/track/{track_id}")
    if data.get("error"):
        raise ValueError(f"Deezer API error: {data['error']}")

    raw_artist = data.get("artist", {}).get("name", "")
    parsed = parse_artists(raw_artist)

    return TrackMetadata(
        title=data.get("title", ""),
        artist=raw_artist,
        primary_artist=parsed["primary"],
        featured_artists=parsed["featured"],
        display_artist=parsed["display"],
        album_artist=parsed.get("album", {}).get("artist", {}).get("name", raw_artist),
        album=data.get("album", {}).get("title", ""),
        track_number=data.get("track_position", 0),
        disc_number=data.get("disk_number", 1),
        year=int(data.get("release_date", "")[:4]) if data.get("release_date") else 0,
        genre="",  # Deezer API doesn't return genre in track endpoint
        duration=float(data.get("duration", 0) or 0),
    )


async def _resolve_deezer_album(album_id: int) -> TrackMetadata:
    """Resolve a Deezer album to TrackMetadata (first track as representative)."""
    data = await _deezer_api_get(f"/album/{album_id}")
    if data.get("error"):
        raise ValueError(f"Deezer API error: {data['error']}")

    raw_artist = data.get("artist", {}).get("name", "")
    parsed = parse_artists(raw_artist)

    return TrackMetadata(
        title=data.get("title", ""),
        artist=raw_artist,
        primary_artist=parsed["primary"],
        featured_artists=parsed["featured"],
        display_artist=parsed["display"],
        album_artist=raw_artist,
        album=data.get("title", ""),
        year=int(data.get("release_date", "")[:4]) if data.get("release_date") else 0,
        duration=float(data.get("duration", 0) or 0),
    )


class DeezerResolver(SourceResolver):
    """Resolves Deezer URLs to metadata via public API."""

    async def resolve(self, input_str: str) -> TrackMetadata:
        entity_type = detect_deezer(input_str)
        m = _DEEZER_URL_RE.search(input_str)
        if not m:
            raise ValueError(f"Not a valid Deezer URL: {input_str}")

        entity_id = int(m.group(1))

        if entity_type == "track":
            return await _resolve_deezer_track(entity_id)
        elif entity_type == "album":
            return await _resolve_deezer_album(entity_id)
        else:
            # For playlist/artist, extract first track
            data = await _deezer_api_get(f"/{entity_type}/{entity_id}")
            if data.get("error"):
                raise ValueError(f"Deezer API error: {data['error']}")
            # Try to get first track
            tracks = data.get("tracks", {}).get("data", [])
            if tracks:
                return await _resolve_deezer_track(tracks[0]["id"])
            raise ValueError(f"No tracks found in Deezer {entity_type}: {input_str}")

    def can_handle(self, input_str: str) -> bool:
        return is_deezer_url(input_str)


class DeezerDownloader(DownloadProvider):
    """Downloads from Deezer via deemix CLI.

    Calls `deemix -p <dir> -b <bitrate> <url>` and waits for completion.
    Output files are named by deemix templates (artist - title.flac/mp3).
    """

    def __init__(self, bitrate: str = "128", fallback_bitrate: str = "128"):
        self.bitrate = bitrate
        self.fallback_bitrate = fallback_bitrate

    def _resolve_executable(self) -> str:
        """Locate the deemix binary.

        On Windows CreateProcess does not apply PATHEXT, so a bare "deemix"
        raises FileNotFoundError even when deemix is installed - npm only
        writes the extension-less shell shim and deemix.cmd. shutil.which
        does apply PATHEXT, so resolve through it first.
        """
        override = os.environ.get("DEEMIX_BIN")
        for name in ([override] if override else []) + ["deemix"]:
            found = shutil.which(name)
            if found:
                return found
        raise RuntimeError(
            "deemix not found on PATH. Install it with "
            "`npm install -g deemix` (or `npm i -g deemix@latest`), or point "
            "the DEEMIX_BIN environment variable at the executable."
        )

    async def _run_deemix(self, exe: str, dl_dir: Path, bitrate: str, url: str) -> str:
        """Run deemix once and return its combined output."""
        cmd = [exe, "-p", str(dl_dir), "-b", bitrate, url]
        logger.info(f"Running: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"deemix failed (exit {proc.returncode}): "
                f"{(stderr.decode(errors='replace') or stdout.decode(errors='replace') or '').strip()}"
            )
        return (stdout.decode(errors="replace") or "") + (stderr.decode(errors="replace") or "")

    async def download(self, candidate: SourceCandidate,
                       output_path: str,
                       progress_callback=None) -> tuple[str, dict | None]:
        """Download via deemix CLI. Returns (actual_file_path, info)."""
        dl_dir = Path(output_path).parent
        dl_dir.mkdir(parents=True, exist_ok=True)
        exe = self._resolve_executable()

        if progress_callback:
            progress_callback(5)

        # 320 and flac need a premium ARL. deemix does not report that as a
        # failure - it exits 0 and still prints "Download complete" while
        # writing nothing - so probe the directory after each attempt and
        # drop to 128, the only tier that streams anonymously.
        attempts = [self.bitrate]
        if self.fallback_bitrate and self.fallback_bitrate != self.bitrate:
            attempts.append(self.fallback_bitrate)

        output = ""
        used = self.bitrate
        for i, bitrate in enumerate(attempts):
            output = await self._run_deemix(exe, dl_dir, bitrate, candidate.url)
            found = self._find_downloaded_file(dl_dir)
            if found:
                used = bitrate
                break
            if i + 1 < len(attempts):
                logger.warning(
                    f"deemix produced no file at {bitrate} (usually a missing/premium "
                    f"ARL cookie); retrying at {attempts[i + 1]}"
                )

        actual_path = self._find_downloaded_file(dl_dir)
        if not actual_path:
            raise RuntimeError(
                "deemix reported success but no audio file appeared at any "
                f"bitrate ({', '.join(attempts)}). The link is likely region-locked, "
                "DRM-protected, or deemix has no valid ARL cookie - run `deemix login`."
            )

        if progress_callback:
            progress_callback(90)

        if used != self.bitrate:
            logger.warning(
                f"Fell back from {self.bitrate} to {used} for {candidate.url} "
                f"(no premium ARL configured)"
            )

        return actual_path, {
            "provider": "deemix",
            "bitrate": used,
            "requested_bitrate": self.bitrate,
            "fell_back": used != self.bitrate,
            "stdout": output.strip()[-2000:],
        }

    async def download_thumbnail(self, candidate: SourceCandidate,
                                 output_path: str) -> str:
        """Download cover art from Deezer API."""
        entity_type = detect_deezer(candidate.url)
        m = _DEEZER_URL_RE.search(candidate.url)
        if not m:
            return ""

        entity_id = int(m.group(1))
        try:
            if entity_type == "track":
                data = await _deezer_api_get(f"/track/{entity_id}")
                cover_url = data.get("album", {}).get("cover_xl") or \
                            data.get("album", {}).get("cover_big") or \
                            data.get("album", {}).get("cover_medium", "")
            else:
                data = await _deezer_api_get(f"/{entity_type}/{entity_id}")
                cover_url = data.get("cover_xl") or \
                            data.get("cover_big") or \
                            data.get("cover_medium", "")

            if not cover_url:
                return ""

            # Download cover image
            thumb_path = output_path + ".jpg"
            async with aiohttp.ClientSession() as session:
                async with session.get(cover_url) as resp:
                    resp.raise_for_status()
                    with open(thumb_path, "wb") as f:
                        f.write(await resp.read())
            return thumb_path
        except Exception as e:
            logger.warning(f"Thumbnail download failed: {e}")
            return ""

    def _find_downloaded_file(self, dl_dir: Path) -> str | None:
        """Find the audio file deemix just wrote, or None if there is none."""
        audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
        candidates = [
            f for f in dl_dir.iterdir()
            if f.is_file() and f.suffix.lower() in audio_exts
        ]
        if not candidates:
            return None
        # Newest first - deemix just wrote it.
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(candidates[0])
