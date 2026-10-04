"""Acquisition provenance - the durable record of where a file came from.

The problem this exists to solve: a log line is not a record. When a
library file disappears, `Job 42 complete` in a rotated log cannot say
where the file went, how big it was, or whether a similar-looking file
found later is actually the same one. Every conclusion drawn from that
gap is an inference, and inferences about deletions are usually wrong.

So every successful acquisition writes a row here before the job is
declared complete:

    job_id, track_id, source, source_url, completed_at,
    file_path, file_size, sha256, organizer_result

With that, reconciliation can answer four questions instead of guessing:

    present        acquired Oct 2, path unchanged, hash unchanged
    missing        acquired Sep 18, was ~/Music/INOHA/Cusp.flac,
                   last known size 43.2 MB, last known hash a1b2...
    moved          expected ~/Music/INOHA/Cusp.flac
                   discovered at ~/Music/Cusp.flac, same SHA-256
    never_acquired a job exists but no acquisition row was ever written

**On hashing.** A full SHA-256 of this library costs about ten seconds
(measured: 766 files, 2.7 GB, 258 MB/s). That is never acceptable at
startup, where the verifier's whole job is to be cheap. It is however
completely fine at acquisition, where a single file is already being
downloaded asynchronously and 40 MB costs ~160 ms. So the hash is taken
once, when the file arrives, and never recomputed on a schedule.

Recomputing later - to backfill old files, or to prove a file was moved -
is explicit and caller-driven, never something the user triggers by
accident. `hash_paths()` exists for that and is deliberately not called
from any startup path.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..database import fetch_all, execute

logger = logging.getLogger(__name__)

# 1 MiB reads: large enough that syscall overhead stops mattering, small
# enough that hashing a track never holds a meaningful amount of memory.
CHUNK_SIZE = 1 << 20

# The four answers reconciliation can give. These are stored as text
# rather than an enum so the values survive a round trip through JSON and
# can be compared in a test without importing this module.
STATE_PRESENT = "present"
STATE_MISSING = "missing"
STATE_MOVED = "moved"
STATE_NEVER_ACQUIRED = "never_acquired"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path) -> str:
    """Full SHA-256 of one file, hex. Blocking - call it off the event loop."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


async def sha256_async(path) -> str:
    """`sha256_file` without blocking the event loop."""
    return await asyncio.to_thread(sha256_file, path)


async def record(
    *,
    track_id: int,
    file_path: str,
    job_id: int | None = None,
    source: str = "",
    source_url: str = "",
    file_size: int = 0,
    sha256: str = "",
    organizer_result: str = "",
    completed_at: str | None = None,
) -> int:
    """Write one acquisition row. Returns its id.

    Failures are logged, never raised: provenance is a record of what
    happened, and losing it is bad, but failing the download because the
    record could not be written would turn a completed file into a lost
    one. The job still completes and the library still has the track.
    """
    try:
        if not file_size:
            try:
                file_size = Path(file_path).stat().st_size
            except OSError:
                file_size = 0
        if not sha256:
            try:
                sha256 = await sha256_async(file_path)
            except OSError as e:
                # A file we cannot read is still worth a provenance row -
                # the path, size and timestamp are the part that matters.
                logger.warning("Could not hash %s: %s", file_path, e)
                sha256 = ""
        cursor = await execute(
            """
            INSERT INTO acquisition
                (job_id, track_id, source, source_url, completed_at,
                 file_path, file_size, sha256, organizer_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                track_id,
                source or "",
                source_url or "",
                completed_at or _utcnow_iso(),
                file_path or "",
                int(file_size or 0),
                sha256 or "",
                organizer_result or "",
            ),
        )
        return cursor.lastrowid
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("Could not record acquisition for track %s: %s", track_id, e)
        return 0


async def latest_for_track(track_id: int) -> dict | None:
    rows = await fetch_all(
        """
        SELECT * FROM acquisition
         WHERE track_id = ?
         ORDER BY COALESCE(completed_at, '') DESC, id DESC
         LIMIT 1
        """,
        (track_id,),
    )
    return rows[0] if rows else None


async def latest_by_track(track_ids: list[int]) -> dict[int, dict]:
    """Latest acquisition per track, for many tracks at once.

    A correlated subquery rather than one query per track: the Missing
    screen renders a whole page of rows and the per-track version turns
    that into hundreds of round trips.
    """
    if not track_ids:
        return {}
    placeholders = ",".join("?" * len(track_ids))
    rows = await fetch_all(
        f"""
        SELECT a.* FROM acquisition a
         WHERE a.track_id IN ({placeholders})
           AND a.id = (
                SELECT MAX(b.id) FROM acquisition b
                 WHERE b.track_id = a.track_id
           )
        """,
        tuple(track_ids),
    )
    return {row["track_id"]: row for row in rows}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    *,
    on_disk: bool,
    expected_path: str = "",
    discovered_path: str = "",
    sha256: str = "",
    last_known_size: int = 0,
    has_acquisition: bool = False,
    has_job: bool = False,
) -> dict:
    """Decide what a track's state actually is. Pure function, no I/O.

    Returns a dict with `state` plus whatever that state knows. The four
    states are distinct questions, not a severity ranking:

      present         the bytes are at the expected path
      moved           the bytes are gone from the expected path but a file
                      with the identical SHA-256 was found elsewhere
      missing         the bytes are gone and nothing was found
      never_acquired  there was a job, but no successful acquisition to
                      say what the file was supposed to be

    `moved` outranks `missing` deliberately. They look identical on disk
    - both mean "not at file_path" - but they call for opposite responses:
    a moved file needs its row repointed, a missing one needs archiving or
    re-fetching. Reporting both as "missing" is what made the earlier
    investigation guess instead of measure.
    """
    detail = {
        "expected_path": expected_path or "",
        "discovered_path": discovered_path or "",
        "sha256": sha256 or "",
        "last_known_size": int(last_known_size or 0),
        "has_acquisition": bool(has_acquisition),
        "has_job": bool(has_job),
    }

    if on_disk:
        return {"state": STATE_PRESENT, **detail}

    if discovered_path and sha256:
        return {"state": STATE_MOVED, **detail}

    if has_acquisition:
        return {"state": STATE_MISSING, **detail}

    if has_job:
        return {"state": STATE_NEVER_ACQUIRED, **detail}

    # Nothing on disk, no provenance, no job: a scan that indexed a file
    # which is since gone. Present as missing, with nothing to say beyond it.
    return {"state": STATE_MISSING, **detail}


# ---------------------------------------------------------------------------
# Content hashing for 'moved' detection
# ---------------------------------------------------------------------------


def hash_paths_blocking(paths: list[str]) -> dict[str, list[str]]:
    """Map SHA-256 -> paths for every readable file. Blocking.

    Explicitly expensive: this reads every byte of every file passed in
    (roughly 10 seconds per 2.7 GB on this machine) and is never called
    from a startup path. It exists so 'Locate file' can search by content
    when a user asks it to, not so the app can afford to guess.
    """
    index: dict[str, list[str]] = {}
    for path in paths:
        try:
            index.setdefault(sha256_file(path), []).append(str(path))
        except OSError as e:
            logger.debug("Skipping unhashable file %s: %s", path, e)
    return index


async def hash_paths(paths: list[str]) -> dict[str, list[str]]:
    return await asyncio.to_thread(hash_paths_blocking, paths)


def match_moved(
    missing: list[dict],
    index: dict[str, list[str]],
) -> list[dict]:
    """Pair missing tracks with identically-hashed files found elsewhere.

    `missing` is a list of dicts carrying `track_id`, `expected_path`,
    `sha256`. Returns only the pairs that actually matched - a hash match
    is proof of identity, so there is no fuzzy threshold here and nothing
    is guessed. A file claimed by no track row at all is what belongs in
    `index`; that is the caller's job to arrange, because "unclaimed" is
    a question about the library, not about a single track.
    """
    found: list[dict] = []
    for track in missing:
        digest = track.get("sha256") or ""
        if not digest:
            continue
        candidates = index.get(digest)
        if not candidates:
            continue
        for path in candidates:
            if path.casefold() == (track.get("expected_path") or "").casefold():
                continue  # same path: that is 'missing', not 'moved'
            found.append({
                "track_id": track.get("track_id"),
                "expected_path": track.get("expected_path") or "",
                "discovered_path": path,
                "sha256": digest,
            })
            break
    return found
