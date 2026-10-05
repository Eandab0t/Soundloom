"""Provenance backfill: hash the existing library, resumably and in the
background.

The existing library predates the `acquisition` table, so its tracks have
no recorded SHA-256. Without one, "Locate" can only ask the user to point
at a file, because there is nothing to compare against. This fills those
rows in.

**Why this is a job and not a request.** A full sweep measured 10.4
seconds for 2.7 GB on this machine - fine as something a person asked
for, unacceptable as something an API call blocks on while the UI spins.
So it runs as a background task that yields between files, and every
counter is a database row rather than a Python variable.

**Why it is resumable.** Progress is written after each chunk, keyed by
`last_track_id`, and the cursor is read back *from the row* when a run
resumes - so it survives a process restart, which is the only restart
that matters. Two independent things make a crash harmless:

- the write is idempotent - a track that already has a SHA-256 is
  skipped, so re-running cannot double-hash or duplicate rows;
- the cursor means a restart does not re-walk finished work, even
  though the skip already makes that merely wasteful rather than wrong.

A run left in 'running' at startup means the process died - nothing else
leaves a row there - so `mark_interrupted()` relabels it rather than
silently restarting 2.7 GB of hashing nobody asked for twice.

**What it will not do.** It never moves, renames or deletes a file. It
reads bytes and writes a hash. Recovering a file that has already
disappeared is a decision for a person, made from evidence, not something
this job decides on its own.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ..database import execute, fetch_all, fetch_one
from . import acquisition

logger = logging.getLogger(__name__)

# Files hashed between progress writes. Small enough that a crash loses
# almost nothing, large enough that the write is not the bottleneck:
# hashing a 40 MB file takes ~160 ms, so a chunk is seconds of work.
CHUNK_SIZE = 25

# A row in one of these states still has work left, so "resume" means
# "continue the newest of these".
UNFINISHED = ("running", "paused", "interrupted")

_task: asyncio.Task | None = None
# Cooperative flags rather than task.cancel(): cancelling mid-hash would
# abandon a file without recording it, and the natural place to stop is
# between files, where what is on disk already matches the cursor.
_control = {"pause": False, "stop": False}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decorate(row) -> dict:
    """Shape a row for the UI, including the derived numbers it needs."""
    d = dict(row)
    total = d.get("bytes_total") or 0
    done = d.get("bytes_processed") or 0
    # Clamped: a progress bar that reads 111% is worse than one that stops
    # at 100, and the counter must never be able to contradict the total.
    d["percent"] = min(round((done / total) * 100, 1), 100.0) if total else 0.0
    # Four separate facts rather than one derived total. `remaining` is
    # computed against the snapshotted total, so it shrinks as work lands
    # and cannot be inflated by the denominator moving underneath it.
    d["processed"] = d.get("processed_files") or 0
    d["succeeded"] = d.get("succeeded_files") or 0
    d["failed"] = d.get("error_count") or 0
    d["remaining"] = max((d.get("total_files") or 0) - d["processed"], 0)
    d["running"] = (
        d.get("status") == "running"
        and _task is not None and not _task.done()
    )
    d["resumable"] = d.get("status") in UNFINISHED
    return d


async def status() -> dict | None:
    """The newest run, finished or not."""
    row = await fetch_one(
        "SELECT * FROM provenance_backfill ORDER BY id DESC LIMIT 1")
    return _decorate(row) if row else None


async def history(limit: int = 10) -> list[dict]:
    rows = await fetch_all(
        "SELECT * FROM provenance_backfill ORDER BY id DESC LIMIT ?", (limit,))
    return [_decorate(r) for r in rows]


async def pending() -> dict:
    """How much work a run would have, and how much is already done.

    Reported before anyone presses Run, because the difference between a
    task you choose to start and a surprise is whether you were told what
    it would cost first.
    """
    row = await fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM tracks
              WHERE file_path != '' AND file_status = 'present')
                AS present,
            (SELECT COUNT(*) FROM acquisition WHERE sha256 != '')
                AS hashed
        """
    )
    present = row["present"] if row else 0
    hashed = row["hashed"] if row else 0
    return {"present": present, "hashed": hashed,
            "remaining": max(present - hashed, 0)}


def _respond(action: str, run: dict | None) -> dict:
    """A control action, and the run state, as two separate things.

    These used to share one key. `{"status": "nothing_to_resume", **row}`
    evaluated to the *row's* status, because a later key wins in a dict
    literal - so every control call reported the previous run's outcome
    instead of what it had just done.
    """
    return {"action": action, "run": run}


async def start(resume: bool = False) -> dict:
    """Begin a backfill, or continue an unfinished one.

    Refuses to start a second concurrent run: two loops writing the same
    rows would duplicate work and corrupt the counters, and a progress bar
    can only honestly represent one.
    """
    global _task

    if _task is not None and not _task.done():
        return _respond("already_running", await status())

    if resume:
        row = await fetch_one(
            "SELECT * FROM provenance_backfill WHERE status IN (?,?,?) "
            "ORDER BY id DESC LIMIT 1", UNFINISHED)
        if not row:
            return _respond("nothing_to_resume", await status())
        await execute(
            "UPDATE provenance_backfill SET status='running', error='', "
            "updated_at=? WHERE id=?", (_utcnow_iso(), row["id"]))
        _control["pause"] = False
        _control["stop"] = False
        _task = asyncio.create_task(_run(row["id"]))
        logger.info("Resuming provenance backfill run %s from track %s",
                    row["id"], row["last_track_id"])
        return _respond("resumed", await status())

    # Snapshot the denominator ONCE, here, where the job is created.
    #
    # Every later read of total_files trusts this value. Recomputing it
    # mid-job is what let a resume report a smaller total than the run it
    # was continuing, so the numbers stopped describing one job.
    work_rows = await fetch_all(_WORK_SQL)
    total_files, total_bytes = await asyncio.to_thread(
        _measure_total_blocking, work_rows)
    cur = await execute(
        "INSERT INTO provenance_backfill (status, started_at, total_files, "
        "bytes_total, updated_at) VALUES ('running', ?, ?, ?, ?)",
        (_utcnow_iso(), total_files, total_bytes, _utcnow_iso()))
    _control["pause"] = False
    _control["stop"] = False
    _task = asyncio.create_task(_run(cur.lastrowid))
    return _respond("started", await status())


async def pause() -> dict:
    if _task is None or _task.done():
        return _respond("not_running", await status())
    _control["pause"] = True
    return _respond("pausing", await status())


async def resume() -> dict:
    return await start(resume=True)


async def cancel() -> dict:
    """Stop the run. Whatever was hashed stays hashed: that work is real,
    and a partial backfill is strictly better than none."""
    if _task is None or _task.done():
        return _respond("not_running", await status())
    _control["stop"] = True
    _control["pause"] = False
    return _respond("cancelling", await status())


async def mark_interrupted() -> int:
    """Relabel runs the process died in the middle of.

    Called at startup. Without this a crashed run keeps claiming to be
    'running' forever, and the panel offers Resume for a task that is not
    running - worse than an honest 'interrupted'.
    """
    cur = await execute(
        "UPDATE provenance_backfill SET status='interrupted', updated_at=? "
        "WHERE status='running'", (_utcnow_iso(),))
    n = cur.rowcount or 0
    if n:
        logger.info("Marked %d interrupted provenance backfill run(s)", n)
    return n


# The work list, for counting. Deliberately does NOT sum file_size:
# `tracks.file_size` is a scan-time snapshot and drifts, and using it as
# the denominator made the progress bar run to 111% against real bytes.
# The total is stat-ed from disk instead, in _measure_total.
_WORK_SQL = """
    SELECT t.id AS id, t.file_path AS file_path,
           COALESCE(t.file_size, 0) AS file_size
      FROM tracks t
     WHERE t.file_path != ''
       AND t.file_status = 'present'
       AND NOT EXISTS (
             SELECT 1 FROM acquisition a
              WHERE a.track_id = t.id AND a.sha256 != ''
       )
     ORDER BY t.id
"""

_CHUNK_SQL = """
    SELECT t.id AS id, t.file_path AS file_path,
           COALESCE(t.file_size, 0) AS file_size,
           t.source_type AS source_type, t.source_url AS source_url
      FROM tracks t
     WHERE t.id > ?
       AND t.file_path != ''
       AND t.file_status = 'present'
       AND NOT EXISTS (
             SELECT 1 FROM acquisition a
              WHERE a.track_id = t.id AND a.sha256 != ''
       )
     ORDER BY t.id
     LIMIT ?
"""


def _measure_total_blocking(rows) -> tuple[int, int]:
    """(file_count, real_bytes) for the work list, measured on disk."""
    total = 0
    for row in rows:
        try:
            total += os.path.getsize(row["file_path"])
        except OSError:
            total += row["file_size"] or 0
    return len(rows), total


async def _run(row_id: int) -> None:
    global _task
    try:
        row = await fetch_one(
            "SELECT last_track_id, processed_files, bytes_processed, "
            "       total_files, bytes_total "
            "  FROM provenance_backfill WHERE id=?", (row_id,))
        # Resume position, read from the row rather than from memory: this
        # is the only cursor that survives the restart the feature exists
        # for.
        cursor = row["last_track_id"] if row else 0

        # The denominator is read, never rebuilt. See start().
        total_files = (row["total_files"] if row else 0) or 0
        bytes_total = (row["bytes_total"] if row else 0) or 0
        if not total_files:
            # Only reachable for a row created before this fix existed,
            # which has no snapshot to inherit. Give it one and stop.
            work_rows = await fetch_all(_WORK_SQL)
            total_files, bytes_total = await asyncio.to_thread(
                _measure_total_blocking, work_rows)
            await execute(
                "UPDATE provenance_backfill SET total_files=?, bytes_total=?, "
                "updated_at=? WHERE id=?",
                (total_files, bytes_total, _utcnow_iso(), row_id))

        remaining_files = len(await fetch_all(_WORK_SQL))

        if remaining_files == 0:
            # No file this job promised to cover is still unhashed, so the
            # job is finished even if its own counter has not caught up -
            # which happens when the last files were hashed between a crash
            # and this restart. Reconcile, or a completed run sits at
            # 753/766 for ever, which is exactly the lie the freeze was
            # meant to remove.
            await _reconcile_complete(row_id, total_files, bytes_total)
            await _finish(row_id, "complete")
            await _index_unclaimed()
            return

        while True:
            if _control["stop"]:
                await _finish(row_id, "cancelled", stamp_completion=False)
                return
            if _control["pause"]:
                await _finish(row_id, "paused", stamp_completion=False)
                return

            rows = await fetch_all(_CHUNK_SQL, (cursor, CHUNK_SIZE))
            if not rows:
                # The second way a run runs dry. Same reconciliation as
                # above, because the invariant is about reaching
                # 'complete', not about which loop got there.
                await _reconcile_complete(row_id, total_files, bytes_total)
                await _finish(row_id, "complete")
                return

            done_files = 0
            done_bytes = 0
            errors = 0
            last_path = ""

            for track in rows:
                if _control["stop"] or _control["pause"]:
                    break
                cursor = track["id"]
                last_path = track["file_path"]
                size, errored = await _hash_one(track)
                done_files += 1
                done_bytes += size
                if errored:
                    errors += 1
                # Hashing already runs in a worker thread; this yield is
                # what keeps the API answering while a 10 second sweep is
                # in flight.
                await asyncio.sleep(0)

            await execute(
                """
                UPDATE provenance_backfill
                   SET processed_files = processed_files + ?,
                       bytes_processed = bytes_processed + ?,
                       error_count = error_count + ?,
                       succeeded_files = succeeded_files + ?,
                       last_track_id = ?, last_path = ?, updated_at = ?
                 WHERE id = ?
                """,
                (done_files, done_bytes, errors, done_files - errors,
                 cursor, last_path, _utcnow_iso(), row_id))

    except asyncio.CancelledError:
        await _finish(row_id, "cancelled", stamp_completion=False)
        raise
    except Exception as e:  # noqa: BLE001 - the row is the record
        logger.error("Provenance backfill failed: %s", e, exc_info=True)
        await execute(
            "UPDATE provenance_backfill SET status='failed', error=?, "
            "updated_at=? WHERE id=?",
            (str(e)[:500], _utcnow_iso(), row_id))
        _task = None


async def _hash_one(track) -> tuple[int, bool]:
    """Hash one track and record it. Returns (bytes, errored).

    The row is written with source='backfill' rather than the track's own
    source_type. These files were not acquired by a job - they were
    already in the library - and conflating the two would let a backfilled
    row later be mistaken for evidence of where a file came from.
    `job_id` stays NULL for the same reason: there is no job, and inventing
    one would be a lie nothing downstream could catch.
    """
    path = Path(track["file_path"])
    try:
        if not path.is_file():
            raise FileNotFoundError(track["file_path"])
        size = path.stat().st_size
        digest = await acquisition.sha256_async(path)
    except (OSError, ValueError) as e:
        logger.debug("Backfill skipped %s: %s", track["file_path"], e)
        # A track whose file cannot be read still gets a row, without a
        # hash. That is the honest record: the file exists to the library
        # but its content could not be identified.
        await acquisition.record(
            track_id=track["id"], file_path=str(path),
            source="backfill", source_url=track["source_url"] or "",
            file_size=track["file_size"] or 0, sha256="",
            organizer_result="backfill: unreadable",
        )
        try:
            return path.stat().st_size, True
        except OSError:
            return track["file_size"] or 0, True

    await acquisition.record(
        track_id=track["id"], file_path=str(path),
        source="backfill", source_url=track["source_url"] or "",
        file_size=size, sha256=digest,
        organizer_result="backfill",
    )
    return size, False


async def _index_unclaimed() -> None:
    """Start a content-index rebuild now the library's provenance exists.

    A move can only be proven by comparing a missing track's hash against
    the rest of the library, and that comparison needs the unclaimed side
    hashed too. That becomes worth doing exactly when the tracked side
    finishes, so it is triggered from here rather than run on a timer.

    Failures are logged, never raised: the backfill's own result is
    already recorded, and a failed index refresh is not a reason to
    report the hashing as failed.
    """
    try:
        from . import content_index
        await content_index.rebuild()
    except Exception as e:  # noqa: BLE001
        logger.warning("Content index rebuild could not start: %s", e)


async def _reconcile_complete(row_id: int, total_files: int,
                              bytes_total: int) -> None:
    """Settle the counters on the way to 'complete'.

    A run can finish with its own counter behind the total: files hashed
    between a crash and the restart stop counting as this run's work, so
    the counter never catches up. Left alone, a *completed* job reports
    753/766 - a finished task that looks unfinished, which is worse than
    the drift it replaced.

    Reconciling is honest here precisely because completion means "every
    file this job promised to cover now has a hash". Whoever hashed the
    stragglers, the job's promise is kept, and the total it promised
    against is the total it reports.
    """
    await execute(
        "UPDATE provenance_backfill SET processed_files=?, "
        "bytes_processed=?, "
        "succeeded_files=MAX(total_files - error_count, 0), "
        "updated_at=? WHERE id=?",
        (total_files, bytes_total, _utcnow_iso(), row_id))


async def _finish(row_id: int, status: str, stamp_completion: bool = True) -> None:
    """Close out a run.

    `completed_at` is stamped only on a genuine completion - a paused or
    cancelled run has not finished, and writing a completion time for it
    would make "when did this finish?" answerable with a lie.
    """
    global _task
    if stamp_completion:
        await execute(
            "UPDATE provenance_backfill SET status=?, completed_at=?, "
            "updated_at=? WHERE id=?",
            (status, _utcnow_iso(), _utcnow_iso(), row_id))
    else:
        await execute(
            "UPDATE provenance_backfill SET status=?, updated_at=? WHERE id=?",
            (status, _utcnow_iso(), row_id))
    _task = None


async def shutdown() -> None:
    """Stop any running sweep and forget it.

    Without this the module-level `_task` outlives the thing it was doing:
    a run that ended any way other than `_finish` leaves the global
    pointing at a finished task, and `start()` then refuses every future
    run as "already_running" - wedged permanently, with no way to clear it
    short of a restart.
    """
    global _task
    task, _task = _task, None
    _control["pause"] = False
    _control["stop"] = True
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: B014
        pass


async def wait_idle(timeout: float = 60.0) -> None:
    """Test seam: wait for the background task to finish."""
    task = _task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout)
