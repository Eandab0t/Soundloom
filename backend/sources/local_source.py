"""Local-filesystem source adapter.

`SourceType.LOCAL` existed in the enum and the pipeline already special-cased
it (the downloader skips the thumbnail fetch for it), but nothing ever
produced it: `detect_source()` in ytdlp_source matches only remote URL
patterns and returns "unknown" for anything else, so a filesystem path fell
through to YtdlpResolver and yt-dlp failed to extract it.

This module is the missing half. It makes a path a first-class source, which
is what lets an acquisition be run end to end without credentials and
without touching the network, down exactly the route a remote download takes:
resolve -> match gate -> fetch -> convert -> tag -> organize -> index.

The fetch is a copy, never a move and never a link. The source file belongs
to whoever handed us the path, and a hard link would let a later write on the
library side mutate their original.
"""
import asyncio
import logging
import shutil
from pathlib import Path

from ..pipeline.interfaces import SourceResolver, DownloadProvider
from ..pipeline.models import TrackMetadata, SourceType, SourceCandidate
from ..pipeline.normalize import parse_artists
from ..services.tagger import read_tags

logger = logging.getLogger(__name__)

# Only formats the converter and tagger actually handle. A path with any
# other suffix is not a track, and treating it as one would push junk
# through ffmpeg.
_AUDIO_SUFFIXES = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg",
    ".opus", ".wav", ".wma", ".aiff", ".aif",
}

_REMOTE_PREFIXES = ("http://", "https://", "ftp://")


def detect_local(input_str: str) -> str | None:
    """Return the path if this input is a readable local audio file.

    Returns None for anything remote or unreadable, so callers can use it as
    a plain "is this local?" test and fall through otherwise.

    The remote-prefix guard is not redundant with `is_file()`: a bare path
    check on a forward-slash-normalising filesystem can be surprising about
    what it resolves, and an http URL must never be handed to the copier.
    """
    if not input_str:
        return None
    candidate = input_str.strip()
    if not candidate or candidate.lower().startswith(_REMOTE_PREFIXES):
        return None
    try:
        path = Path(candidate)
        if not path.is_file():
            return None
        if path.suffix.lower() not in _AUDIO_SUFFIXES:
            return None
    except OSError:
        return None
    return str(path)


def is_local_path(input_str: str) -> bool:
    return detect_local(input_str) is not None


async def metadata_for(path: str) -> TrackMetadata:
    """Read a local file's own tags into the pipeline's metadata shape."""
    tags = await read_tags(path)
    parsed = parse_artists(tags.get("artist") or "")
    return TrackMetadata(
        title=tags.get("title") or "",
        artist=tags.get("artist") or "",
        primary_artist=parsed["primary"],
        featured_artists=parsed["featured"],
        display_artist=parsed["display"],
        album_artist=tags.get("album_artist") or parsed["album_artist"],
        album=tags.get("album") or "",
        track_number=int(tags.get("track_number") or 0),
        disc_number=int(tags.get("disc_number") or 0),
        year=int(tags.get("year") or 0),
        genre=tags.get("genre") or "",
        duration=float(tags.get("duration") or 0.0),
    )


class LocalResolver(SourceResolver):
    """Resolves a local audio file to metadata by reading its own tags.

    There is no remote service to ask, so the file is the authority. An
    untagged file still resolves, with the filename standing in as the title,
    because refusing it would make untagged local audio unusable rather than
    merely untidy.
    """

    async def resolve(self, input_str: str) -> TrackMetadata:
        path = detect_local(input_str)
        if path is None:
            raise ValueError(f"Not a readable local audio file: {input_str!r}")
        return await metadata_for(path)

    def can_handle(self, input_str: str) -> bool:
        return is_local_path(input_str)


class LocalDownloader(DownloadProvider):
    """Fetches a local source by copying it into the job's staging area.

    Copy rather than move: the staged copy is what the converter and the
    organizer operate on, and `_process_job` deletes the whole staging tree
    when the job ends. A move would take the user's file away with it.
    """

    async def download(self, candidate: SourceCandidate,
                       output_path: str,
                       progress_callback=None) -> tuple[str, dict | None]:
        source = detect_local(candidate.url)
        if source is None:
            raise FileNotFoundError(f"Local source is gone: {candidate.url!r}")
        src = Path(source)
        dest = Path(output_path + src.suffix.lower())

        await asyncio.get_running_loop().run_in_executor(
            None, lambda: shutil.copy2(src, dest),
        )
        if progress_callback:
            # A copy is not streamed, so there is no partial state to report;
            # the caller expects a terminal 100 either way.
            progress_callback(100)
        logger.info("Local fetch: %s -> %s (%d bytes)",
                    src, dest, dest.stat().st_size)
        return str(dest), None

    async def download_thumbnail(self, candidate: SourceCandidate,
                                 output_path: str) -> str:
        raise NotImplementedError(
            "Local sources are not fetched for artwork; the pipeline skips "
            "this call for SourceType.LOCAL, and any artwork comes from the "
            "file's own embedded tags."
        )
