"""Library scanner with reconciliation.

Scan flow:
    1. Build fingerprint set for all audio files on disk
    2. Diff against DB: detect added, missing, moved files
    3. Insert new files, mark missing files, update moved files
"""
import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path

from ..database import fetch_one, fetch_all, execute
from .. import config
from .tagger import read_tags, AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)

_scan_state = {"scanning": False, "total": 0, "processed": 0,
               "added": 0, "updated": 0, "missing": 0, "errors": 0}


async def get_scan_status() -> dict:
    return dict(_scan_state)


async def scan_folder(folder_path: str | None = None) -> dict:
    global _scan_state
    if _scan_state["scanning"]:
        return {"status": "already_scanning", **_scan_state}

    folder = folder_path or config.get("library_path")
    if not folder or not Path(folder).exists():
        return {"status": "error", "message": f"Folder not found: {folder}"}

    _scan_state = {"scanning": True, "total": 0, "processed": 0,
                   "added": 0, "updated": 0, "missing": 0, "errors": 0}
    logger.info(f"Starting scan of {folder}")

    _scan_state["total"] = await _scan_and_reconcile(folder)

    _scan_state["scanning"] = False
    result = {"status": "complete", **_scan_state}
    logger.info(
        f"Scan complete: {_scan_state['added']} added, "
        f"{_scan_state['updated']} updated, "
        f"{_scan_state['missing']} missing, "
        f"{_scan_state['errors']} errors"
    )
    return result


async def _scan_and_reconcile(folder: str) -> int:
    """Scan folder and reconcile with database. Returns total files found."""
    audio_files = []
    for fp in Path(folder).rglob("*"):
        if fp.is_file() and fp.suffix.lower() in AUDIO_EXTENSIONS:
            audio_files.append(fp)

    total = len(audio_files)
    _scan_state["total"] = total

    disk_paths = {str(fp.resolve()).casefold(): fp for fp in audio_files}

    db_rows = await fetch_all("SELECT id, file_path FROM tracks")
    db_paths = {}
    for row in db_rows:
        db_paths[str(Path(row["file_path"]).resolve()).casefold()] = (row["id"], row["file_path"])

    missing = set(db_paths.keys()) - set(disk_paths)
    for m in missing:
        await execute(
            "UPDATE tracks SET file_status='missing', updated_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), db_paths[m][0]),
        )
        _scan_state["missing"] += 1

    for fp in audio_files:
        try:
            tags = await read_tags(str(fp))
            _scan_state["processed"] += 1
        except Exception as e:
            _scan_state["errors"] += 1
            logger.warning(f"Error reading {fp.name}: {e}")
            continue

        file_size = fp.stat().st_size if fp.exists() else 0

        normalized_path = str(fp.resolve()).casefold()
        if normalized_path in db_paths:
            await execute("""
                UPDATE tracks SET title=?, artist=?, album_artist=?, album=?,
                track_number=?, disc_number=?, year=?, genre=?, duration=?,
                file_size=?, format=?, bitrate=?, sample_rate=?,
                file_status='present', updated_at=?
                WHERE file_path=?
            """, (
                tags["title"], tags["artist"], tags["album_artist"], tags["album"],
                tags["track_number"], tags["disc_number"], tags["year"], tags["genre"],
                tags["duration"], file_size, tags["format"], tags["bitrate"],
                tags["sample_rate"], datetime.utcnow().isoformat(), db_paths[normalized_path][1],
            ))
            _scan_state["updated"] += 1
        else:
            await execute("""
                INSERT INTO tracks (file_path, title, artist, album_artist, album,
                track_number, disc_number, year, genre, duration, file_size, format,
                bitrate, sample_rate, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(fp), tags["title"], tags["artist"], tags["album_artist"],
                tags["album"], tags["track_number"], tags["disc_number"],
                tags["year"], tags["genre"], tags["duration"], file_size,
                tags["format"], tags["bitrate"], tags["sample_rate"],
                datetime.utcnow().isoformat(), datetime.utcnow().isoformat(),
            ))
            _scan_state["added"] += 1

        if _scan_state["processed"] % 50 == 0:
            await asyncio.sleep(0)

    return total


async def scan_single_file(file_path: str) -> dict:
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    existing = await fetch_one("SELECT id FROM tracks WHERE file_path = ?", (str(p),))
    tags = await read_tags(file_path)
    file_size = p.stat().st_size if p.exists() else 0

    if existing:
        await execute("""
            UPDATE tracks SET title=?, artist=?, album_artist=?, album=?,
            track_number=?, disc_number=?, year=?, genre=?, duration=?,
            file_size=?, format=?, bitrate=?, sample_rate=?,
            file_status='present', updated_at=?
            WHERE file_path=?
        """, (
            tags["title"], tags["artist"], tags["album_artist"], tags["album"],
            tags["track_number"], tags["disc_number"], tags["year"], tags["genre"],
            tags["duration"], file_size, tags["format"], tags["bitrate"],
            tags["sample_rate"], datetime.utcnow().isoformat(), str(p),
        ))
    else:
        await execute("""
            INSERT INTO tracks (file_path, title, artist, album_artist, album,
            track_number, disc_number, year, genre, duration, file_size, format,
            bitrate, sample_rate, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(p), tags["title"], tags["artist"], tags["album_artist"],
            tags["album"], tags["track_number"], tags["disc_number"],
            tags["year"], tags["genre"], tags["duration"], file_size,
            tags["format"], tags["bitrate"], tags["sample_rate"],
            datetime.utcnow().isoformat(), datetime.utcnow().isoformat(),
        ))
    return tags
