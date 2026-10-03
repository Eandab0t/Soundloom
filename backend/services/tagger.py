"""Tag reading and writing via mutagen."""
import logging
from pathlib import Path
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCON, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture
from mutagen.oggvorbis import OggVorbis
import mutagen

from ..errors import TagError

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".wma"}


def _get_audio_type(path: str) -> str:
    return Path(path).suffix.lower()


async def read_tags(file_path: str) -> dict:
    p = Path(file_path)
    ext = p.suffix.lower()
    tags = {
        "file_path": str(p),
        "title": p.stem,
        "artist": "",
        "album_artist": "",
        "album": "",
        "track_number": 0,
        "disc_number": 0,
        "year": 0,
        "genre": "",
        "duration": 0.0,
        "file_size": p.stat().st_size if p.exists() else 0,
        "format": ext.lstrip("."),
        "bitrate": 0,
        "sample_rate": 0,
    }
    try:
        audio = mutagen.File(str(p), easy=True)
        if audio is None:
            return tags
        tags["title"] = (audio.get("title", [p.stem]))[0] if audio.get("title") else p.stem
        tags["artist"] = (audio.get("artist", [""]))[0] if audio.get("artist") else ""
        tags["album_artist"] = (audio.get("albumartist", [tags["artist"]]))[0] if audio.get("albumartist") else tags["artist"]
        tags["album"] = (audio.get("album", [""]))[0] if audio.get("album") else ""
        track_str = (audio.get("tracknumber", ["0"]))[0] if audio.get("tracknumber") else "0"
        tags["track_number"] = int(track_str.split("/")[0]) if track_str.split("/")[0].isdigit() else 0
        disc_str = (audio.get("discnumber", ["0"]))[0] if audio.get("discnumber") else "0"
        tags["disc_number"] = int(disc_str.split("/")[0]) if disc_str.split("/")[0].isdigit() else 0
        year_str = (audio.get("date", [""]))[0] if audio.get("date") else ""
        tags["year"] = int(year_str[:4]) if year_str[:4].isdigit() else 0
        tags["genre"] = (audio.get("genre", [""]))[0] if audio.get("genre") else ""
        tags["duration"] = audio.info.length if hasattr(audio, "info") else 0.0
        tags["bitrate"] = int(audio.info.bitrate / 1000) if hasattr(audio.info, "bitrate") else 0
        tags["sample_rate"] = audio.info.sample_rate if hasattr(audio.info, "sample_rate") else 0
    except Exception as e:
        logger.debug(f"Error reading tags from {p.name}: {e}")
    return tags


async def write_tags(file_path: str, tags: dict, cover_path: str = None) -> None:
    """Write tags to an audio file.

    Contract: returns normally on success, raises ``TagError`` on any
    failure. This used to return False, which several callers ignored - a
    failed write looked exactly like a successful one, leaving the DB and the
    file on disk disagreeing about the metadata. Failure is now always loud.
    """
    p = Path(file_path)
    ext = p.suffix.lower()
    try:
        if ext == ".mp3":
            ok = _write_id3(p, tags, cover_path)
        elif ext in (".m4a", ".aac"):
            ok = _write_mp4(p, tags, cover_path)
        elif ext == ".flac":
            ok = _write_flac(p, tags, cover_path)
        elif ext == ".ogg":
            ok = _write_ogg(p, tags, cover_path)
        else:
            raise TagError(
                f"Unsupported format for tag writing: {ext or p.name}",
                code="tag_unsupported_format",
            )
    except TagError:
        raise
    except Exception as e:
        logger.error(f"Error writing tags to {p.name}: {e}")
        raise TagError(f"Failed to write tags to {p.name}: {e}") from e

    if not ok:
        raise TagError(f"Tag write failed for {p.name}")


def _read_cover(path: str) -> bytes | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except Exception:
        return None


def _write_id3(p: Path, tags: dict, cover_path: str = None) -> bool:
    try:
        audio = MP3(str(p))
    except ID3NoHeaderError:
        audio = MP3(str(p))
        audio.add_tags()
    if not audio.tags:
        audio.add_tags()
    t = audio.tags
    if tags.get("title"):
        t["TIT2"] = TIT2(encoding=3, text=[tags["title"]])
    if tags.get("artist"):
        t["TPE1"] = TPE1(encoding=3, text=[tags["artist"]])
    if tags.get("album_artist"):
        t["TPE2"] = TPE2(encoding=3, text=[tags["album_artist"]])
    if tags.get("album"):
        t["TALB"] = TALB(encoding=3, text=[tags["album"]])
    if tags.get("track_number"):
        t["TRCK"] = TRCK(encoding=3, text=[str(tags["track_number"])])
    if tags.get("disc_number"):
        t["TPOS"] = mutagen.id3.TPOS(encoding=3, text=[str(tags["disc_number"])])
    if tags.get("year"):
        t["TDRC"] = TDRC(encoding=3, text=[str(tags["year"])])
    if tags.get("genre"):
        t["TCON"] = TCON(encoding=3, text=[tags["genre"]])
    if cover_path:
        cover_data = _read_cover(cover_path)
        if cover_data:
            t["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data)
    audio.save()
    return True


def _write_mp4(p: Path, tags: dict, cover_path: str = None) -> bool:
    audio = MP4(str(p))
    if tags.get("title"):
        audio["\xa9nam"] = [tags["title"]]
    if tags.get("artist"):
        audio["\xa9ART"] = [tags["artist"]]
    if tags.get("album_artist"):
        audio["aART"] = [tags["album_artist"]]
    if tags.get("album"):
        audio["\xa9alb"] = [tags["album"]]
    if tags.get("track_number"):
        audio["trkn"] = [(tags["track_number"], 0)]
    if tags.get("disc_number"):
        audio["disk"] = [(tags["disc_number"], 0)]
    if tags.get("year"):
        audio["\xa9day"] = [str(tags["year"])]
    if tags.get("genre"):
        audio["\xa9gen"] = [tags["genre"]]
    if cover_path:
        cover_data = _read_cover(cover_path)
        if cover_data:
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    return True


def _write_flac(p: Path, tags: dict, cover_path: str = None) -> bool:
    audio = FLAC(str(p))
    if tags.get("title"):
        audio["title"] = tags["title"]
    if tags.get("artist"):
        audio["artist"] = tags["artist"]
    if tags.get("album_artist"):
        audio["albumartist"] = tags["album_artist"]
    if tags.get("album"):
        audio["album"] = tags["album"]
    if tags.get("track_number"):
        audio["tracknumber"] = str(tags["track_number"])
    if tags.get("disc_number"):
        audio["discnumber"] = str(tags["disc_number"])
    if tags.get("year"):
        audio["date"] = str(tags["year"])
    if tags.get("genre"):
        audio["genre"] = tags["genre"]
    if cover_path:
        cover_data = _read_cover(cover_path)
        if cover_data:
            pic = Picture()
            pic.data = cover_data
            pic.type = 3
            pic.mime = "image/jpeg"
            audio.clear_pictures()
            audio.add_picture(pic)
    audio.save()
    return True


def _write_ogg(p: Path, tags: dict, cover_path: str = None) -> bool:
    audio = OggVorbis(str(p))
    if tags.get("title"):
        audio["title"] = tags["title"]
    if tags.get("artist"):
        audio["artist"] = tags["artist"]
    if tags.get("album_artist"):
        audio["albumartist"] = tags["album_artist"]
    if tags.get("album"):
        audio["album"] = tags["album"]
    if tags.get("track_number"):
        audio["tracknumber"] = str(tags["track_number"])
    if tags.get("disc_number"):
        audio["discnumber"] = str(tags["disc_number"])
    if tags.get("year"):
        audio["date"] = str(tags["year"])
    if tags.get("genre"):
        audio["genre"] = tags["genre"]
    audio.save()
    return True
