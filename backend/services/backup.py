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


# --- Database backups -------------------------------------------------------
# Tag backups are tiny and only exist while a write is in flight. A schema
# migration rewrites table structure, so the thing worth protecting first is
# the whole library.db file. These are binary copies kept under backups/db/.

DB_BACKUP_DIR = BACKUP_DIR / "db"
# Keep the most recent pre-migration copies; older ones are pruned so a
# long-lived install does not accumulate full copies of the library.
DB_BACKUP_KEEP = 5


def db_backup_path(directory: Optional[Path] = None) -> Path:
    """A unique path for a DB snapshot.

    Several backups can legitimately be taken within one second (tests, a
    retry loop, a fast migration), so the seconds-only timestamp is made
    unique with a counter suffix rather than silently overwriting an earlier
    snapshot.
    """
    base = Path(directory) if directory else DB_BACKUP_DIR
    stamp = _timestamp()
    path = base / f"library_{stamp}.db"
    n = 1
    while path.exists():
        path = base / f"library_{stamp}-{n}.db"
        n += 1
    return path


def backup_database(db_path: str | Path, directory: Optional[Path] = None) -> str:
    """Copy the SQLite file so a failed migration can be rolled back.

    Uses SQLite's own backup API rather than shutil.copy: it is safe to call
    on a WAL database while other connections exist, and it produces a
    consistent snapshot instead of a torn file.

    Returns the path written, or "" when there is no database yet (a brand
    new install has nothing to protect).
    """
    src = Path(db_path)
    if not src.exists() or src.stat().st_size == 0:
        return ""

    path = db_backup_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import sqlite3

        # NB: a `with sqlite3.connect(...)` block commits but does NOT close,
        # and on Windows an open handle blocks deletion - which would make
        # pruning silently fail forever. Close explicitly.
        source = sqlite3.connect(str(src))
        dest = sqlite3.connect(str(path))
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
    except Exception as exc:
        # A missing backup must never stop the app from starting; log and
        # let the migration proceed.
        logger.warning("Could not back up %s before migration: %s", src.name, exc)
        return ""

    prune_db_backups(directory)
    return str(path)


def list_db_backups(directory: Optional[Path] = None) -> list[dict]:
    """Newest-first metadata for each stored database backup."""
    base = Path(directory) if directory else DB_BACKUP_DIR
    if not base.exists():
        return []
    out = []
    for p in sorted(base.glob("library_*.db"), key=lambda f: f.stat().st_mtime,
                    reverse=True):
        stat = p.stat()
        out.append({
            "path": str(p),
            "name": p.name,
            "size": stat.st_size,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return out


def prune_db_backups(directory: Optional[Path] = None, keep: int = DB_BACKUP_KEEP) -> int:
    """Delete all but the `keep` newest database backups. Returns removed."""
    backups = list_db_backups(directory)
    removed = 0
    for entry in backups[keep:]:
        try:
            Path(entry["path"]).unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Could not prune old DB backup %s: %s", entry["name"], exc)
        except Exception as exc:
            logger.warning("Could not prune old DB backup %s: %s", entry["name"], exc)
    return removed


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
