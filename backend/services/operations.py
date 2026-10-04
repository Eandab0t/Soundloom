"""Crash-recoverable operation journal.

Every file-mutating operation (download, convert, tag, organize, index) is
recorded here as it advances:

    queued -> downloading -> downloaded -> validated -> tagged ->
    organized -> committed

`committed` means the file is in the library *and* SQLite knows about it.
Those two facts can disagree if the process dies in between - the file is
already in the library but no `tracks` row exists. That is the failure this
module exists to prevent, because it silently loses a track the user already
paid for.

Design rules:

- **The disk is the truth.** SQLite records intent; the filesystem records
  what actually happened. Reconciliation asks the disk first and rolls
  *forward* when the artifact is real. We never delete a file that made it
  into the library just because the database did not record it.
- **Transitions are explicit.** Advancing out of order is a programming error,
  not something to paper over - it means the checkpoint wiring is wrong.
- **Recovery is idempotent.** Running reconcile twice must be a no-op.
- **Resuming work that costs bandwidth requires consent.** Recovering a file
  already in the library is free and automatic; re-downloading an interrupted
  track is not, so that is opt-in via `auto_resume_operations`.
"""
import logging
from enum import Enum
from pathlib import Path

from .. import config
from ..database import execute, fetch_all, fetch_one
from ..errors import VividlyError

logger = logging.getLogger(__name__)


class OpState(Enum):
    """States an operation moves through. Mirrors the pipeline, not JobState.

    JobState has UI-facing states (paused, retrying, rate-limit backoff) that
    are not durability concerns; this enum only tracks "how far did the file
    actually get".
    """
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VALIDATED = "validated"
    TAGGED = "tagged"
    ORGANIZED = "organized"
    COMMITTED = "committed"
    # Terminal, non-happy outcomes.
    FAILED = "failed"
    ABANDONED = "abandoned"


# The forward pipeline, in order. Reconciliation and the retry endpoints both
# need to step outside this chain - a resumed file goes TAGGED -> ORGANIZED ->
# COMMITTED rather than teleporting, and a re-queue is a deliberate reset - so
# the table is derived from the chain plus the extra rules below instead of
# being hand-written per state (which is how committed -> queued slips in).
_FORWARD_ORDER = [
    OpState.QUEUED, OpState.DOWNLOADING, OpState.DOWNLOADED,
    OpState.VALIDATED, OpState.TAGGED, OpState.ORGANIZED, OpState.COMMITTED,
]
_FORWARD_STEPS = dict(zip(_FORWARD_ORDER, _FORWARD_ORDER[1:]))

# Outcome states any stage can fall into when something goes wrong.
_OUTCOMES = {OpState.FAILED, OpState.ABANDONED}


def _build_transitions() -> dict[OpState, set[OpState]]:
    table: dict[OpState, set[OpState]] = {}
    for state in _FORWARD_ORDER:
        allowed = set(_OUTCOMES)
        nxt = _FORWARD_STEPS.get(state)
        if nxt:
            allowed.add(nxt)
        if state is not OpState.COMMITTED:
            # An explicit retry or a startup re-queue resets to the front
            # rather than walking the chain backwards one step at a time.
            allowed.add(OpState.QUEUED)
        table[state] = allowed

    # Terminal outcomes. COMMITTED is final. A failed operation can still be
    # discarded (and discarding twice is a no-op, not an error).
    table[OpState.COMMITTED] = set()
    table[OpState.FAILED] = {OpState.QUEUED, OpState.ABANDONED}
    table[OpState.ABANDONED] = {OpState.QUEUED, OpState.ABANDONED}
    return table


LEGAL_TRANSITIONS: dict[OpState, set[OpState]] = _build_transitions()

TERMINAL_STATES = {OpState.COMMITTED, OpState.FAILED, OpState.ABANDONED}

# States where the artifact is already a complete, playable audio file sitting
# in the staging area. A crash here is recoverable without re-downloading.
RESUMABLE_STATES = {OpState.VALIDATED, OpState.TAGGED}


class OperationError(VividlyError):
    code = "operation_error"
    message = "Operation state error"


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _row_state(row: dict) -> OpState:
    return OpState(row["state"])


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

async def begin_operation(op_key: str, *, job_id: int | None = None,
                          kind: str = "download", title: str = "",
                          artist: str = "", album: str = "",
                          album_artist: str = "", track_number: int = 0,
                          source_url: str = "") -> int:
    """Start (or reattach to) an operation. Idempotent on `op_key`.

    Returns the operation id. Calling this twice for the same key returns the
    same row rather than creating a duplicate, so a retried job does not fork
    into two competing operations.
    """
    existing = await fetch_one("SELECT id FROM operations WHERE op_key=?", (op_key,))
    if existing:
        return existing["id"]

    cursor = await execute(
        "INSERT INTO operations (op_key, job_id, kind, state, title, artist, album, "
        "album_artist, track_number, source_url, attempt, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (op_key, job_id, kind, OpState.QUEUED.value, title, artist, album,
         album_artist, track_number, source_url, _utcnow_iso(), _utcnow_iso()),
    )
    op_id = cursor.lastrowid
    logger.info("Operation %s started (key=%s, job=%s)", op_id, op_key, job_id)
    return op_id


async def advance(op_id: int, state: OpState, **fields) -> dict | None:
    """Move an operation to `state`, recording any path/track fields.

    `staged_path` and `final_path` are tracked separately because they answer
    different questions during recovery: where is the work-in-progress, and
    where did the finished file land.
    """
    row = await fetch_one("SELECT * FROM operations WHERE id=?", (op_id,))
    if not row:
        logger.warning("advance() on unknown operation %s", op_id)
        return None

    current = _row_state(row)
    if state is not current and state not in LEGAL_TRANSITIONS.get(current, set()):
        raise OperationError(
            f"Illegal operation transition {current.value} -> {state.value} "
            f"(op {op_id}); allowed: "
            f"{sorted(s.value for s in LEGAL_TRANSITIONS.get(current, set()))}"
        )

    allowed = {"job_id", "title", "artist", "album", "album_artist",
               "track_number", "source_url",
               "staged_path", "final_path", "track_id", "error", "attempt"}
    unknown = set(fields) - allowed
    if unknown:
        raise OperationError(f"Unknown operation fields: {sorted(unknown)}")

    updates = {"state": state.value, "updated_at": _utcnow_iso()}
    for key, value in fields.items():
        updates[key] = value
    set_parts = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [op_id]
    await execute(f"UPDATE operations SET {set_parts} WHERE id=?", tuple(values))
    return await fetch_one("SELECT * FROM operations WHERE id=?", (op_id,))


async def get_operation(op_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM operations WHERE id=?", (op_id,))


async def find_by_key(op_key: str) -> dict | None:
    return await fetch_one("SELECT * FROM operations WHERE op_key=?", (op_key,))


async def list_operations(state: str | None = None, limit: int = 100) -> list[dict]:
    if state:
        return await fetch_all(
            "SELECT * FROM operations WHERE state=? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (state, limit),
        )
    return await fetch_all(
        "SELECT * FROM operations ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
    )


async def interrupted_operations() -> list[dict]:
    """Non-terminal operations - the set that reconciliation must judge."""
    # Build the values once. The placeholders and the bound parameters have to
    # line up positionally, and assuming two iterations of a set agree is not
    # something to rely on for the query that decides what recovery does.
    terminal = sorted(s.value for s in TERMINAL_STATES)
    placeholders = ",".join("?" for _ in terminal)
    return await fetch_all(
        f"SELECT * FROM operations WHERE state NOT IN ({placeholders}) "
        "ORDER BY id ASC",
        tuple(terminal),
    )


async def owned_staged_paths() -> set[str]:
    """Staging paths still claimed by a live operation.

    `reclaim_orphans()` used to delete everything under data/tmp on every
    startup, which threw away artifacts a resume could still have used. It now
    consults this so in-flight work survives a restart.
    """
    rows = await interrupted_operations()
    out = set()
    for row in rows:
        if row.get("staged_path"):
            out.add(str(Path(row["staged_path"])))
    return out


# ---------------------------------------------------------------------------
# Library indexing (shared with the downloader)
# ---------------------------------------------------------------------------

async def index_file(file_path: str, metadata: dict, source_url: str = "") -> int:
    """Insert a finished file into `tracks`. Returns the track id.

    Shared with the download pipeline so a crash-recovered file is indexed
    exactly like a live one - same tags, same provenance.
    """
    from ..pipeline.normalize import parse_artists
    from .tagger import read_tags

    try:
        tags = await read_tags(file_path)
    except Exception:
        tags = {}

    artist = metadata.get("artist", "")
    parsed = parse_artists(artist)
    path = Path(file_path)
    file_size = path.stat().st_size if path.exists() else 0

    def pick(field: str, fallback):
        """Prefer a real tag value; treat empty as absent.

        A file can reach the library with no embedded tags (the tag write is
        best-effort) or with empty ones. Falling back on an empty string would
        index the track with a blank artist and lose it from artist listings
        even though the journal knows the answer.
        """
        value = tags.get(field)
        return value if value not in (None, "") else fallback

    cursor = await execute("""
        INSERT OR REPLACE INTO tracks
        (file_path, title, artist, primary_artist, featured_artists, display_artist,
         album_artist, album, track_number, disc_number,
         year, genre, duration, file_size, format, bitrate, sample_rate,
         cover_art_path, source_url, source_type, file_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'present')
    """, (
        file_path,
        pick("title", metadata.get("title", "")),
        pick("artist", metadata.get("artist", "")),
        parsed["primary"],
        ",".join(parsed["featured"]),
        parsed["display"],
        pick("album_artist", parsed["album_artist"]),
        pick("album", metadata.get("album", "")),
        pick("track_number", metadata.get("track_number", 0)),
        pick("disc_number", metadata.get("disc_number", 0)),
        pick("year", metadata.get("year", 0)),
        pick("genre", metadata.get("genre", "")),
        pick("duration", metadata.get("duration", 0)),
        file_size,
        path.suffix.lstrip("."),
        tags.get("bitrate", 0),
        tags.get("sample_rate", 0),
        tags.get("cover_art_path", ""),
        source_url,
        "download",
    ))
    return cursor.lastrowid


async def _already_indexed(file_path: str) -> dict | None:
    return await fetch_one("SELECT id FROM tracks WHERE file_path=?", (file_path,))


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _op_metadata(row: dict, staged_path: str = "") -> dict:
    """Metadata needed to place a recovered file exactly where it belongs.

    The default folder template keys on album_artist/album/track_number/format,
    so anything missing here would land the resumed file under "Unknown".
    """
    return {
        "title": row.get("title") or "",
        "artist": row.get("artist") or "",
        "album": row.get("album") or "",
        "album_artist": row.get("album_artist") or row.get("artist") or "",
        "track_number": row.get("track_number") or 0,
        "format": Path(staged_path).suffix.lstrip(".") if staged_path else "",
    }


async def _commit_from_disk(row: dict, final_path: str, note: str) -> dict:
    """The file is in the library - make SQLite agree, then mark committed.

    This is the roll-forward that recovers tracks the old code lost. Safe to
    run repeatedly: if the track row already exists we adopt its id instead of
    inserting a second copy.
    """
    existing = await _already_indexed(final_path)
    if existing:
        track_id = existing["id"]
    else:
        track_id = await index_file(final_path, _op_metadata(row),
                                    row.get("source_url") or "")

    # A resumed file arrives from TAGGED/VALIDATED, but the artifact really is
    # in the library now - record that intermediate truth before closing it
    # out, rather than letting the state machine reject the shortcut.
    if _row_state(row) is not OpState.ORGANIZED:
        await advance(row["id"], OpState.ORGANIZED,
                      final_path=final_path, staged_path="")
        row = await fetch_one("SELECT * FROM operations WHERE id=?", (row["id"],))

    updated = await advance(
        row["id"], OpState.COMMITTED,
        final_path=final_path, staged_path="", track_id=track_id, error="",
    )
    await _complete_job(row)
    logger.info("Recovered operation %s: %s (%s)", row["id"], final_path, note)
    return updated


async def _complete_job(row: dict) -> None:
    """The work landed, so the job is no longer in flight.

    Without this a job crashed at 'indexing' stays stuck in the queue view
    forever even though recovery has already finished what it was doing.
    """
    job_id = row.get("job_id")
    if not job_id:
        return
    job = await fetch_one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    if not job:
        return
    from ..pipeline.models import JobState
    if job["status"] == JobState.COMPLETE.value:
        return
    await execute(
        "UPDATE jobs SET status=?, progress=100, output_path=?, error='', updated_at=? WHERE id=?",
        (JobState.COMPLETE.value, row.get("final_path") or "", _utcnow_iso(), job_id),
    )


async def _resume_from_staged(row: dict, staged_path: str) -> dict | None:
    """Finish an operation whose audio file survived in the staging area.

    Only attempted for states where the staged file is already a complete,
    converted audio file. Raw downloads are re-fetched instead: a partially
    written download that looks finished on disk cannot be trusted.
    """
    settings = config.get_all()
    library_path = settings.get("library_path") or str(Path.home() / "Music")
    folder_template = settings.get(
        "folder_template", "{album_artist}\\{album}\\{track_number} - {title}.{format}"
    )
    duplicate_policy = settings.get("duplicate_policy", "keep_separate")

    from .organizer import organize_file

    try:
        final_path = await organize_file(
            staged_path, _op_metadata(row, staged_path),
            library_path=library_path,
            folder_template=folder_template,
            duplicate_policy=duplicate_policy,
        )
    except Exception as e:
        logger.warning("Operation %s: could not organize %s: %s", row["id"], staged_path, e)
        return await advance(row["id"], OpState.FAILED,
                             error=f"Interrupted; resume failed during organize: {e}")

    return await _commit_from_disk(row, final_path, "resumed from staging area")


async def reconcile() -> dict:
    """Reconcile every interrupted operation against the filesystem.

    Runs at startup, before the download worker picks up new work. Returns a
    summary of what was recovered, retried and abandoned.
    """
    rows = await interrupted_operations()
    summary = {"recovered": 0, "resumed": 0, "retried": 0,
               "abandoned": 0, "examined": len(rows)}
    if not rows:
        return summary

    auto_resume = bool(config.get("auto_resume_operations", False))

    for row in rows:
        state = _row_state(row)
        final_path = (row.get("final_path") or "").strip()
        staged_path = (row.get("staged_path") or "").strip()

        final_exists = bool(final_path) and Path(final_path).exists()
        staged_exists = bool(staged_path) and Path(staged_path).exists()

        try:
            # The file reached the library. Whatever SQLite thought, the
            # artifact is real - index it and close the operation.
            if final_exists:
                await _commit_from_disk(row, final_path, f"crashed in '{state.value}'")
                summary["recovered"] += 1
                continue

            # We recorded a final path but the file is gone. The move never
            # completed, or something outside the app removed it.
            if state is OpState.ORGANIZED:
                await advance(row["id"], OpState.FAILED,
                              error="Interrupted after organize; library file is missing")
                summary["abandoned"] += 1
                continue

            # A converted file survived in staging - finish it without
            # re-downloading.
            if staged_exists and state in RESUMABLE_STATES:
                result = await _resume_from_staged(row, staged_path)
                if result and _row_state(result) is OpState.COMMITTED:
                    summary["resumed"] += 1
                else:
                    summary["abandoned"] += 1
                continue

            # Nothing usable on disk. Re-queue the owning job if the user opted
            # in; otherwise leave it recorded so it can be retried deliberately.
            message = f"Interrupted during '{state.value}'; no usable file on disk"
            if auto_resume and row.get("job_id"):
                requeued = await _requeue_job(row)
                # staged_path is dropped: a re-queued job re-downloads from
                # scratch, so the old partial artifact is dead weight.
                await advance(row["id"], OpState.QUEUED, error=message, staged_path="")
                if requeued:
                    summary["retried"] += 1
                    continue
            await advance(row["id"], OpState.FAILED, error=message)
            summary["abandoned"] += 1

        except Exception as e:
            # Reconciliation must never abort startup - one bad row should not
            # stop the app from coming up.
            logger.exception("Reconcile failed for operation %s: %s", row["id"], e)
            summary["abandoned"] += 1

    if summary["examined"]:
        logger.info("Reconciled %s interrupted operation(s): %s",
                    summary["examined"], summary)
    return summary


async def _requeue_job(row: dict) -> bool:
    """Put the owning job back in the queue for a clean retry."""
    job_id = row.get("job_id")
    if not job_id:
        return False
    job = await fetch_one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    if not job:
        return False
    from ..pipeline.models import JobState
    if job["status"] in (JobState.COMPLETE.value, JobState.CANCELLED.value):
        return False
    await execute(
        "UPDATE jobs SET status=?, updated_at=?, not_before=NULL WHERE id=?",
        (JobState.PENDING.value, _utcnow_iso(), job_id),
    )
    logger.info("Requeued job %s for retry after an interrupted operation", job_id)
    return True


async def retry_operation(op_id: int) -> dict | None:
    """Explicitly re-queue a failed/abandoned operation and its job."""
    row = await get_operation(op_id)
    if not row:
        return None
    if _row_state(row) in TERMINAL_STATES - {OpState.COMMITTED}:
        await _requeue_job(row)
    await advance(row["id"], OpState.QUEUED, error="", attempt=(row.get("attempt") or 0) + 1)
    return await get_operation(op_id)


async def discard_operation(op_id: int) -> dict | None:
    """Mark an operation resolved without retrying, and drop its staged file."""
    row = await get_operation(op_id)
    if not row:
        return None
    staged_path = (row.get("staged_path") or "").strip()
    if staged_path:
        try:
            Path(staged_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Could not remove staged file %s: %s", staged_path, e)
    await advance(row["id"], OpState.ABANDONED, staged_path="")
    return await get_operation(op_id)
