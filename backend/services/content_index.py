"""Content index: cached digests for library files no track row claims.

This is what makes an automatic moved-file suggestion affordable.

A move can only be *proven* by comparing a missing track's own SHA-256
against the rest of the library, and that comparison needs the other side
hashed. On this machine, hashing the ~831 unclaimed files in a flat
library takes about 4 seconds - acceptable once, unacceptable on every
page load. So the digests are cached in `library_content`, and after one
pass "is this missing track's file sitting somewhere else?" is a join
rather than a sweep.

**The index is deliberately conservative.** It only ever contains files
that no track claims, so a file already accounted for can never be
offered as "the one that moved". And `find_moves()` returns a
suggestion, never an action: the caller decides whether to repoint a
row. Nothing here moves, renames or deletes anything - the closest this
module comes to a file is opening it read-only to hash it.

Reindexing is idempotent (UNIQUE on file_path) and skips files already
in the table with the same size, so a rescan after one file appears
costs almost nothing.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..database import executemany, fetch_all, fetch_one
from . import acquisition

logger = logging.getLogger(__name__)

CHUNK_SIZE = 25

_task: asyncio.Task | None = None
_control = {"stop": False}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def status() -> dict:
    row = await fetch_one(
        "SELECT COUNT(*) AS indexed, COALESCE(SUM(file_size), 0) AS bytes "
        "  FROM library_content")
    claimed = await fetch_one(
        "SELECT COUNT(*) AS c FROM tracks WHERE file_status='present'")
    unclaimed = await _unclaimed_paths()
    return {
        "indexed": row["indexed"] if row else 0,
        "indexed_bytes": row["bytes"] if row else 0,
        "unclaimed": len(unclaimed),
        "stale": max(len(unclaimed) - (row["indexed"] if row else 0), 0),
        "running": _task is not None and not _task.done(),
    }


def _unclaimed_blocking(root: Path, claimed: set[str]) -> list[str]:
    """Audio files under the library that no present track row claims."""
    from .tagger import AUDIO_EXTENSIONS

    out = []
    for fp in root.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            key = str(fp.resolve()).casefold()
        except OSError:
            continue
        if key not in claimed:
            out.append(str(fp))
    return out


async def _unclaimed_paths() -> list[str]:
    from .. import config

    folder = config.get("library_path")
    if not folder:
        return []
    root = Path(folder)
    if not root.is_dir():
        return []
    rows = await fetch_all(
        "SELECT file_path FROM tracks WHERE file_status='present'")
    claimed = set()
    for r in rows:
        try:
            claimed.add(str(Path(r["file_path"]).resolve()).casefold())
        except OSError:
            continue
    return await asyncio.to_thread(_unclaimed_blocking, root, claimed)


async def rebuild() -> dict:
    """Re-index unclaimed files in the background. Returns immediately.

    Idempotent, and cheap when little has changed: a file already indexed
    at the same size is skipped, so a rescan after a single download costs
    a stat rather than a full read.
    """
    global _task
    if _task is not None and not _task.done():
        return {"action": "already_running", **(await status())}
    _control["stop"] = False
    _task = asyncio.create_task(_run())
    return {"action": "started", **(await status())}


async def _run() -> None:
    global _task
    try:
        from .. import config

        paths = await _unclaimed_paths()
        known = {
            r["file_path"]: r["file_size"]
            for r in await fetch_all("SELECT file_path, file_size FROM library_content")
        }

        done = 0
        for i in range(0, len(paths), CHUNK_SIZE):
            if _control["stop"]:
                logger.info("Content index rebuild cancelled after %d file(s)", done)
                _task = None
                return
            chunk = paths[i:i + CHUNK_SIZE]
            rows = []
            for path in chunk:
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    continue
                if known.get(path) == size:
                    continue  # already indexed, unchanged
                digest = await acquisition.sha256_async(path)
                rows.append((digest, path, size))
                done += 1
            if rows:
                await executemany(
                    "INSERT INTO library_content (digest, file_path, file_size,"
                    " indexed_at) VALUES (?, ?, ?, ?) "
                    " ON CONFLICT(file_path) DO UPDATE SET digest=excluded.digest,"
                    " file_size=excluded.file_size, indexed_at=excluded.indexed_at",
                    [(d, p, s, _utcnow_iso()) for d, p, s in rows],
                )
            await asyncio.sleep(0)

        logger.info("Content index rebuilt: %d file(s) hashed, %d unclaimed",
                    done, len(paths))
        _task = None
    except asyncio.CancelledError:
        _task = None
        raise
    except Exception as e:  # noqa: BLE001 - never take the app down for this
        logger.warning("Content index rebuild failed: %s", e, exc_info=True)
        _task = None


async def find_moves() -> list[dict]:
    """Missing tracks whose exact content exists somewhere else.

    A join, not a search: only an identical SHA-256 can appear here, so
    every row returned is proof rather than a lead. A missing track with
    no recorded hash cannot match, and is absent from the result rather
    than guessed at.

    **Two places a file can turn up, and the distinction matters.** A file
    can be sitting unclaimed, which `library_content` holds. Or it can
    have been indexed as a track of its own - which is what the scanner
    does to anything new inside the library, and so is what actually
    happens when a file is tidied out of its Album folder. That second
    case used to be excluded outright, which meant the most ordinary kind
    of move was the one kind never reported.

    Both are proof, so both are reported. `relinkable` says whether the
    path is free: pointing a row at an unclaimed file is safe, while
    pointing two rows at one file would make a duplicate look like a
    repair. That is a judgement for the person, not this function.
    """
    unclaimed = await fetch_all(
        """
        SELECT t.id AS track_id, t.title AS title, t.artist AS artist,
               t.file_path AS expected_path, a.sha256 AS sha256,
               c.file_path AS discovered_path, c.file_size AS file_size
          FROM tracks t
          JOIN acquisition a
            ON a.track_id = t.id AND a.sha256 != ''
                 AND a.id = (SELECT MAX(b.id) FROM acquisition b
                              WHERE b.track_id = t.id)
          JOIN library_content c
            ON c.digest = a.sha256
         WHERE t.file_status = 'missing'
           AND c.file_path != t.file_path
           AND NOT EXISTS (SELECT 1 FROM tracks x
                            WHERE x.file_path = c.file_path)
         ORDER BY t.artist, t.title
        """
    )

    claimed = await fetch_all(
        """
        SELECT t.id AS track_id, t.title AS title, t.artist AS artist,
               t.file_path AS expected_path, a.sha256 AS sha256,
               x.file_path AS discovered_path,
               COALESCE(x.file_size, 0) AS file_size,
               x.id AS claimed_by_track_id, x.title AS claimed_by_title
          FROM tracks t
          JOIN acquisition a
            ON a.track_id = t.id AND a.sha256 != ''
                 AND a.id = (SELECT MAX(b.id) FROM acquisition b
                              WHERE b.track_id = t.id)
          JOIN acquisition a2
            ON a2.sha256 = a.sha256 AND a2.track_id != t.id
                 AND a2.id = (SELECT MAX(b2.id) FROM acquisition b2
                               WHERE b2.track_id = a2.track_id)
          JOIN tracks x
            ON x.id = a2.track_id
         WHERE t.file_status = 'missing'
           AND x.file_status = 'present'
           AND x.file_path != t.file_path
         ORDER BY t.artist, t.title
        """
    )

    out: list[dict] = []
    for r in unclaimed:
        d = dict(r)
        d["relinkable"] = True
        d["claimed_by_track_id"] = None
        d["claimed_by_title"] = None
        out.append(d)
    for r in claimed:
        d = dict(r)
        d["relinkable"] = False
        out.append(d)
    out.sort(key=lambda d: (d["artist"] or "", d["title"] or ""))
    return out


async def shutdown() -> None:
    global _task
    task, _task = _task, None
    _control["stop"] = True
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: B014
        pass


async def wait_idle(timeout: float = 60.0) -> None:
    """Test seam."""
    task = _task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout)
