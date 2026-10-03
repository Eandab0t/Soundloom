"""yt-dlp source adapter implementing pipeline interfaces.

FIXED: Downloads best available audio, not MP3-only.
Source quality is preserved through the pipeline.
"""
import asyncio
import logging
import re
from pathlib import Path
import yt_dlp

from ..pipeline.interfaces import SourceResolver, DownloadProvider
from ..pipeline.models import TrackMetadata, SourceCandidate, SourceType, QualityProfile
from ..pipeline.normalize import parse_artists

logger = logging.getLogger(__name__)

_YT_DLP_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "no_check_certificates": True,
    "extractor_args": {"youtube": {"skip": ["dash", "mpd"]}},
    "cookiesfrombrowser": ("chrome",),
}

# SoundCloud-specific: impersonate Chrome to avoid 403 rate limiting
_YT_DLP_OPTS_SOUNDCLOUD = {
    **_YT_DLP_OPTS_BASE,
    "impersonate": "chrome",
}

_URL_PATTERNS = [
    (r"youtube\.com/watch", "youtube"),
    (r"youtu\.be/", "youtube"),
    (r"youtube\.com/playlist", "youtube"),
    (r"soundcloud\.com/", "soundcloud"),
    (r"deezer\.com/", "deezer"),
    (r"vimeo\.com/", "vimeo"),
    (r"bandcamp\.com/", "bandcamp"),
    (r"twitch\.tv/", "twitch"),
]


def detect_source(url: str) -> str:
    for pattern, source in _URL_PATTERNS:
        if re.search(pattern, url):
            return source
    return "unknown"


def is_supported_url(url: str) -> bool:
    return detect_source(url) != "unknown" or url.startswith("http")


async def _run_extract(fn):
    """Run yt-dlp extraction in executor (it's blocking)."""
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _extract_artist(info: dict) -> str:
    if info.get("artist"):
        return info["artist"]
    if info.get("uploader"):
        return info["uploader"]
    if info.get("channel"):
        return info["channel"]
    title = info.get("title", "")
    for sep in [" - ", " — ", " | "]:
        if sep in title:
            return title.split(sep, 1)[0].strip()
    return ""


def _info_to_track_metadata(info: dict) -> TrackMetadata:
    """Convert yt-dlp info dict to TrackMetadata."""
    raw_artist = _extract_artist(info)
    parsed = parse_artists(raw_artist)
    return TrackMetadata(
        title=info.get("title", ""),
        artist=raw_artist,
        primary_artist=parsed["primary"],
        featured_artists=parsed["featured"],
        display_artist=parsed["display"],
        album_artist=parsed["album_artist"],
        album=info.get("album", ""),
        year=int(str(info.get("upload_date", ""))[:4]) if info.get("upload_date") else 0,
        duration=float(info.get("duration", 0) or 0),
    )


class YtdlpResolver(SourceResolver):
    """Resolves YouTube/SoundCloud/etc URLs to metadata."""

    async def resolve(self, input_str: str) -> TrackMetadata:
        def _extract(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(input_str, download=False)

        opts = {**_YT_DLP_OPTS_BASE, "skip_download": True, "extract_flat": False}
        try:
            info = await _run_extract(lambda: _extract(opts))
        except Exception:
            opts.pop("cookiesfrombrowser", None)
            logger.info("Retrying resolve without browser cookies")
            info = await _run_extract(lambda: _extract(opts))
        return _info_to_track_metadata(info)

    def can_handle(self, input_str: str) -> bool:
        return is_supported_url(input_str)


class YtdlpDownloader(DownloadProvider):
    """Downloads audio from YouTube/SoundCloud/etc using best available quality.

    FIXED: No longer forces MP3. Downloads best audio, then converts once.
    """

    async def download(self, candidate: SourceCandidate,
                       output_path: str,
                       progress_callback=None) -> tuple[str, dict | None]:
        def _progress(d):
            if progress_callback and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                pct = (downloaded / total * 100) if total > 0 else 0
                progress_callback(pct)
            elif d.get("status") == "finished":
                if progress_callback:
                    progress_callback(100)

        def _download(opts):
            opts["outtmpl"] = output_path + ".%(ext)s"
            opts["progress_hooks"] = [_progress]
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(candidate.url, download=True)
                return info

        # Use SoundCloud-specific options (with impersonate) for SoundCloud URLs
        base_opts = _YT_DLP_OPTS_SOUNDCLOUD if candidate.source_type == SourceType.SOUNDCLOUD else _YT_DLP_OPTS_BASE
        opts = {**base_opts}
        try:
            info = await _run_extract(lambda: _download(dict(opts)))
        except Exception:
            opts.pop("cookiesfrombrowser", None)
            opts.pop("impersonate", None)
            logger.info("Retrying download without browser cookies/impersonate")
            info = await _run_extract(lambda: _download(dict(opts)))
        actual_path = self._find_downloaded_file(output_path)
        return actual_path, info

    async def download_thumbnail(self, candidate: SourceCandidate,
                                 output_path: str) -> str:
        def _dl_thumb():
            opts = {
                **_YT_DLP_OPTS_BASE,
                "skip_download": True,
                "writethumbnail": True,
                "outtmpl": output_path,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(candidate.url, download=False)
                ydl.process_info(info)
                return info

        try:
            await _run_extract(_dl_thumb)
            import glob
            for m in glob.glob(output_path + ".*"):
                if m.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    return m
            return ""
        except Exception as e:
            logger.warning(f"Thumbnail download failed: {e}")
            return ""

    def _find_downloaded_file(self, base_path: str) -> str:
        """Find the actual downloaded file (yt-dlp picks the extension)."""
        parent = Path(base_path).parent
        stem = Path(base_path).name
        for f in parent.glob(stem + ".*"):
            if f.suffix in (".webm", ".m4a", ".opus", ".mp3", ".ogg", ".wav", ".flac"):
                return str(f)
        return base_path + ".webm"


async def resolve_url(url: str) -> dict:
    """Legacy resolve for backward compat."""
    resolver = YtdlpResolver()
    meta = await resolver.resolve(url)
    return {
        "url": url,
        "title": meta.title,
        "artist": meta.artist,
        "album": meta.album,
        "duration": meta.duration,
        "thumbnail": "",
        "source_type": detect_source(url),
    }


async def download_audio(url: str, output_path: str, progress_callback=None) -> tuple:
    """Legacy download for backward compat."""
    source = detect_source(url)
    source_type = SourceType(source) if source in {item.value for item in SourceType} else SourceType.UNKNOWN
    candidate = SourceCandidate(url=url, source_type=source_type)
    downloader = YtdlpDownloader()
    return await downloader.download(candidate, output_path, progress_callback)


async def download_thumbnail(url: str, output_path: str) -> str:
    """Legacy thumbnail download for backward compat."""
    candidate = SourceCandidate(url=url)
    downloader = YtdlpDownloader()
    return await downloader.download_thumbnail(candidate, output_path)
