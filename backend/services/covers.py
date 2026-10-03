"""Cover-art extraction and serving.

`cover_art_path` has been a column since day one, but nothing ever populated
it: downloads save art beside the audio when a provider supplies it, while
the thousands of tracks that predate the pipeline keep their art *embedded*
in the file tags. This module closes the loop:

  * ``extract_cover`` resolves art for one track, in priority order: an
    already-recorded path, the per-track cache in ``data/covers/``, a sibling
    ``cover.jpg``/``folder.jpg`` beside the audio (cheap, shared by the whole
    album), and finally the artwork *embedded* in the file's tags (mutagen
    APIC / MP4 covr / FLAC picture), written once to the cache. Returns ""
    when the file simply has no art — a normal outcome, not an error.
  * ``cover_response`` turns a track id into a FileResponse, falling back to
    the bundled placeholder so the UI can always point an <img> at it.

Extraction is blocking file work, so it runs in a thread pool to keep the
event loop responsive while a library grid warms its covers. Process-local
positive and negative caches stop repeated requests from re-reading files.
"""
import asyncio
import logging
from pathlib import Path

from fastapi.responses import FileResponse

from .. import config
from ..database import fetch_one, execute

logger = logging.getLogger(__name__)

PLACEHOLDER = config.FRONTEND_DIR / "img" / "logo.svg"

_SIBLING_COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "art.jpg")

_LOCK = asyncio.Lock()            # serialise extraction; work itself is threaded
_HAVE_ART: set[int] = set()       # track ids confirmed to have art this process
_NO_ART: set[int] = set()         # track ids confirmed artless this process

_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
}


def _media_type(path: str | Path) -> str:
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "image/jpeg")


def _sniff_media_type(blob: bytes, fallback: str = "image/jpeg") -> str:
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:3] == b"GIF":
        return "image/gif"
    return fallback


def extract_embedded(file_path: str) -> bytes | None:
    """Pull the largest embedded picture out of an audio file, or None.

    Read-only — never touches the audio bytes.
    """
    p = Path(file_path)
    if not p.exists():
        return None
    try:
        ext = p.suffix.lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                tags = ID3(str(p))
            except ID3NoHeaderError:
                return None
            apics = tags.getall("APIC")
            return max((a.data for a in apics), key=len, default=None)
        if ext in (".m4a", ".aac", ".mp4"):
            from mutagen.mp4 import MP4
            covers = MP4(str(p)).tags.get("covr") or []
            return max((bytes(c) for c in covers), key=len, default=None)
        if ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(str(p)).pictures or []
            return max((pic.data for pic in pics), key=len, default=None)
        if ext == ".ogg":
            import base64
            from mutagen.oggvorbis import OggVorbis
            from mutagen.flac import Picture
            blocks = OggVorbis(str(p)).get("metadata_block_picture") or []
            pics = []
            for b in blocks:
                try:
                    pics.append(Picture(base64.b64decode(b)).data)
                except Exception:
                    continue
            return max(pics, key=len, default=None)
    except Exception as e:
        logger.debug("No embedded art in %s: %s", file_path, e)
    return None


def find_sibling_cover(file_path: str) -> str | None:
    """Cover files often sit next to the audio; prefer those (cheap, shared)."""
    folder = Path(file_path).parent
    for name in _SIBLING_COVER_NAMES:
        candidate = folder / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return None


def _cached_path(track_id: int) -> Path:
    return Path(config.COVERS_DIR) / f"track_{track_id}.jpg"


async def extract_cover(track_id: int) -> str:
    """Resolve art for one track; returns the cover path ('' = no art found)."""
    if track_id in _NO_ART:
        return ""
    async with _LOCK:
        row = await fetch_one(
            "SELECT id, file_path, cover_art_path FROM tracks WHERE id=?", (track_id,)
        )
        if not row:
            return ""
        recorded = row["cover_art_path"] or ""
        if recorded and Path(recorded).is_file():
            _HAVE_ART.add(track_id)
            return recorded

        cached = _cached_path(track_id)
        if cached.is_file() and cached.stat().st_size > 0:
            if recorded != str(cached):
                await execute("UPDATE tracks SET cover_art_path=? WHERE id=?",
                              (str(cached), track_id))
            _HAVE_ART.add(track_id)
            return str(cached)

        def _work() -> str:
            # 1) a cover file beside the audio — no extraction needed
            sibling = find_sibling_cover(row["file_path"])
            if sibling:
                return sibling
            # 2) embedded art, written once to the cache
            blob = extract_embedded(row["file_path"])
            if not blob:
                return ""
            config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(blob)
            return str(cached)

        path = await asyncio.to_thread(_work)
        await execute("UPDATE tracks SET cover_art_path=? WHERE id=?", (path, track_id))
        if path:
            _HAVE_ART.add(track_id)
        else:
            _NO_ART.add(track_id)
        return path


async def cover_response(track_id: int) -> FileResponse:
    """FileResponse for a track's art — recorded, extracted, or placeholder."""
    path = await extract_cover(track_id)
    if path and Path(path).is_file():
        return FileResponse(path, media_type=_media_type(path))
    if PLACEHOLDER.is_file():
        return FileResponse(PLACEHOLDER, media_type="image/svg+xml")
    from fastapi import HTTPException
    raise HTTPException(404, "no cover available")
