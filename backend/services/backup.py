"""Tag backups taken before any destructive write.

Autofix and fetch-apply rewrite tags directly on the user's files. If
something goes wrong - a bad rule, a wrong metadata match, a truncated
file - there needs to be a way back. This writes the *pre-write* state of
every file about to be touched to a timestamped JSON or CSV file, which the
restore helper can replay.

Backups are tag-only, not copies of the audio. They are tiny and make the
un-undoable part (the file itself) recoverable, since the bytes are
untouched by a tag edit.
"""
import csv
import datetime
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Fields captured per track. Mirrors the tracks table columns that hold
# tag data, so a restore can put the file back as it was.
BACKUP_FIELDS = (
    "file_path", "title", "artist", "primary_artist", "featured_artists",
    "display_artist", "album_artist", "album", "track_number", "disc_number",
    "year", "genre", "duration", "cover_art_path", "source_url",
)

# The data directory (and backups under it) must stay user-writable, so it
# follows ROOT (the exe's folder in frozen builds), never the bundle.
from .. import config as _cfg
BACKUP_DIR = _cfg.DATA_DIR / "backups"


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_path(fmt: str = "json", directory: Optional[Path] = None) -> Path:
    base = Path(directory) if directory else BACKUP_DIR
    return base / f"tags_backup_{_timestamp()}.{fmt}"


def write_backup(records: Iterable[dict], fmt: str = "json",
                 directory: Optional[Path] = None) -> str:
    """Write a backup of tag rows and return the path written to.

    Returns "" when there is nothing to back up, so callers can treat it
    as a no-op without special-casing.
    """
    rows = [dict(r) for r in records]
    if not rows:
        return ""

    if fmt not in ("json", "csv"):
        raise ValueError(f"unsupported backup format: {fmt!r} (use 'json' or 'csv')")

    path = backup_path(fmt, directory)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        payload = {
            "created_at": datetime.datetime.now().isoformat(),
            "count": len(rows),
            "records": rows,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    else:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(BACKUP_FIELDS),
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in BACKUP_FIELDS})

    logger.info(f"Wrote tag backup of {len(rows)} file(s) to {path}")
    return str(path)


def backup_tracks(track_rows: Iterable[dict], settings: dict) -> str:
    """Back up tracks honouring the backup_before_write setting."""
    if not settings.get("backup_before_write", True):
        return ""
    fmt = settings.get("backup_format", "json")
    try:
        return write_backup(track_rows, fmt)
    except OSError as e:
        # Never let a backup failure silently block the write; surface it
        # loudly but let the caller decide.
        logger.error(f"Tag backup failed: {e}")
        return ""


def list_backups(directory: Optional[Path] = None) -> list[dict]:
    """Most recent first."""
    base = Path(directory) if directory else BACKUP_DIR
    if not base.exists():
        return []
    out = []
    for p in sorted(base.glob("tags_backup_*"), reverse=True):
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "path": str(p),
            "size": stat.st_size,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return out


def read_backup(path: str) -> list[dict]:
    """Load records out of a backup file for preview or restore."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such backup: {path}")

    if p.suffix == ".csv":
        with open(p, "r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    with open(p, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        return payload.get("records", [])
    return payload if isinstance(payload, list) else []


__all__ = [
    "BACKUP_FIELDS",
    "backup_path",
    "write_backup",
    "backup_tracks",
    "list_backups",
    "read_backup",
]
