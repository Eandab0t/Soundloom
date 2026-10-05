"""Forensics: an append-only log of what the filesystem actually did.

The provenance work answers "what is this file". This answers "what
happened to it", which is the question the missing-files investigation kept
hitting and could not get past.

**Why an observation log rather than a status column.** `file_status` is a
snapshot: it says a file is missing *now*, and when it changes the previous
value is gone. That is enough to run the app and useless for an
investigation, where the entire question is the sequence and the timing -
created, organised, renamed, missing, reappeared. So every *transition* is
appended to `file_observation` and the snapshot is left to do what
snapshots are for.

Rows are written only when something changes. An observation table that
grew by one row per track per scan would be a second copy of the tracks
table, and the transitions that matter would be buried in the noise.

**What this will not do.** It never moves, renames, restores or deletes
anything. It reads a directory and writes to a database. An investigation
tool that changes the thing it is investigating is not an investigation
tool, and the one behaviour under suspicion here - something reorganising
files - is precisely what this must not do while it is being watched.

**On canaries.** A canary is a file acquired through the real pipeline into
a dedicated directory, then re-verified by digest at intervals. Its value
is not that it tests whether acquisition works - that is already known -
but that its lifecycle is a known, labelled specimen. When one vanishes,
the log already holds the transitions leading up to it.
"""
import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ..database import execute, fetch_all, fetch_one

logger = logging.getLogger(__name__)

# The transitions worth writing down. Anything not in here is noise.
PRESENT = "present"
MISSING = "missing"
MOVED = "moved"
REAPPEARED = "reappeared"
CREATED = "created"
RENAMED = "renamed"

TRANSITIONS = (PRESENT, MISSING, MOVED, REAPPEARED, CREATED, RENAMED)

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_blocking(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


async def _record(track_id: int, path: str, state: str,
                  detail: str = "", sha256: str = "",
                  file_size: int = 0) -> None:
    await execute(
        """
        INSERT INTO file_observation
            (track_id, observed_at, file_path, file_size, sha256,
             state, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (track_id, _utcnow_iso(), path, file_size, sha256, state,
         detail[:300]),
    )


async def _last_state(track_id: int) -> str:
    row = await fetch_one(
        "SELECT state FROM file_observation WHERE track_id = ? "
        "  ORDER BY id DESC LIMIT 1", (track_id,))
    return row["state"] if row else ""


def _locate_digest_blocking(root: Path, digest: str) -> str:
    """Find a file with these exact bytes, by looking.

    Recorded digests are consulted first because they are free, but they
    are not sufficient: an index only knows what has been indexed, and the
    question here is precisely whether something has moved since. So the
    fallback is to sweep the tree and hash what is there.
    """
    from .tagger import AUDIO_EXTENSIONS

    for fp in root.rglob("*"):
        try:
            if not fp.is_file() or fp.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
        except OSError:
            continue
        try:
            found, _ = _sha256_blocking(fp)
        except OSError:
            continue
        if found == digest:
            return str(fp)
    return ""


def _probe_blocking(entries: list[tuple[int, str]]) -> list[dict]:
    """Read the filesystem once, off the event loop.

    Returns one verdict per track: what is true now, not what was true
    last time. Comparing those is the caller's job, because "changed" is
    only meaningful against the previous observation.
    """
    out = []
    for track_id, raw in entries:
        path = Path(raw)
        if not path.is_file():
            out.append({"track_id": track_id, "path": str(path),
                        "exists": False, "sha256": "", "file_size": 0})
            continue
        try:
            digest, size = _sha256_blocking(path)
        except OSError as e:
            out.append({"track_id": track_id, "path": str(path),
                        "exists": False, "sha256": "", "file_size": 0,
                        "detail": f"unreadable: {e}"})
            continue
        out.append({"track_id": track_id, "path": str(path),
                    "exists": True, "sha256": digest, "file_size": size})
    return out


async def observe(force: bool = False) -> dict:
    """Walk the library and append a row for every transition.

    Idempotent by construction: a track whose state has not changed
    produces nothing, so calling this every scan costs one stat per file
    and no writes. `force` re-records the current state even when
    unchanged, which is what a canary verification wants - the point there
    is the timestamp, not the change.
    """
    rows = await fetch_all(
        "SELECT id, file_path FROM tracks "
        " WHERE file_path != '' AND library_status != 'archived' "
        " ORDER BY id")
    entries = [(r["id"], r["file_path"]) for r in rows]

    probed = await asyncio.to_thread(_probe_blocking, entries)

    # The most recent state per track, so "changed" is measured against
    # the previous observation rather than against the row before it in
    # some arbitrary order.
    previous: dict[int, str] = {}
    if rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        hist = await fetch_all(
            f"SELECT track_id, state FROM file_observation "
            f"WHERE track_id IN ({marks}) ORDER BY id", ids)
        for h in hist:
            previous[h["track_id"]] = h["state"]

    written = 0
    for p in probed:
        now = PRESENT if p["exists"] else MISSING
        was = previous.get(p["track_id"])
        if was == now and not force:
            continue
        state = now
        detail = ""
        if was is None:
            state = PRESENT if now == PRESENT else MISSING
            detail = "first observation"
        elif was != now:
            # Reappeared and vanished are distinguished from a plain
            # change because "it came back" and "it went away" are the two
            # observations this whole table exists to make legible.
            state = REAPPEARED if now == PRESENT else MISSING
            detail = f"{was} -> {now}"
        await _record(p["track_id"], p["path"], state, detail,
                      p["sha256"], p["file_size"])
        written += 1

    return {"tracks": len(entries), "transitions": written}


# ---------------------------------------------------------------------------
# Continuous observation of known-provenance files
# ---------------------------------------------------------------------------
#
# `observe()` above answers "what is true now" for the whole library, once,
# when asked. That reconstructs a disappearance after the fact but cannot say
# when it happened, because nothing was looking.
#
# What follows looks only at files whose bytes Soundloom already holds a
# recorded digest for - a canary, an acquisition row, or a content-index
# entry. That is a deliberate limit, not an optimisation. A watcher over
# every file in a music library is a general-purpose filesystem monitor, it
# is expensive, and it would fill `file_observation` with rows about files
# Soundloom has no stake in. A recorded digest is also the only thing that
# can decide "is this the same file", so it is the only case where an
# answer is possible at all.
#
# Debouncing is the other half. A file that is briefly unreadable - a copy in
# progress, an indexer holding it open, a cloud client rewriting it - looks
# exactly like a file that just disappeared. Believing that first reading
# would fill the log with absences that never happened and destroy the
# signal. So a verdict has to repeat before it is recorded:
# DEBOUNCE_POLLS consecutive identical readings, and one contradicting
# reading resets the count.
#
# Nothing here moves, restores or deletes anything. It reads paths and
# appends rows to the observation log.


# Consecutive identical readings required before a transition is written.
# Two is the smallest number that survives a single bad read, which is the
# common failure here.
DEBOUNCE_POLLS = 2

# Default seconds between polls.
OBSERVER_INTERVAL = 60.0

# Ceiling on hashes per poll. The common case costs one stat, but a library
# being rewritten all at once must not turn into an unbounded read.
MAX_HASHES_PER_POLL = 64

_observer_task: asyncio.Task | None = None


class _Debouncer:
    """Requires a verdict to repeat before it is written down.

    Its own type because "only believe what repeats" is the entire safety
    argument for this observer, and a named type says that where a bare
    integer comparison does not.
    """

    def __init__(self, required: int = DEBOUNCE_POLLS) -> None:
        self.required = max(2, required)
        self._seen: dict[int, tuple[str, int]] = {}

    def offer(self, key: int, state: str) -> bool:
        """Report a reading; return True only once the verdict has settled.

        The first sighting starts a count and returns False. A different
        reading resets it: a verdict interrupted by a contradiction has not
        been observed DEBOUNCE_POLLS times, it has been observed once and
        then contradicted.
        """
        prev_state, count = self._seen.get(key, ("", 0))
        count = count + 1 if prev_state == state else 1
        self._seen[key] = (state, count)
        return count >= self.required

    def forget(self, key: int) -> None:
        self._seen.pop(key, None)

    def clear(self) -> None:
        self._seen.clear()


_observer_debouncer = _Debouncer()


async def known_provenance_files() -> list[dict]:
    """The files this observer may look at, each with a digest to compare.

    An acquisition row is preferred over a content-index row: the first is
    what a finished download produced, the second is only what a scan once
    hashed. `library_content` stores its digest under `digest`, which is why
    the two sources are coalesced rather than joined under one name.
    """
    rows = await fetch_all(
        """
        SELECT t.id AS track_id,
               t.file_path AS file_path,
               COALESCE(NULLIF(a.sha256, ''), NULLIF(c.digest, '')) AS sha256,
               COALESCE(NULLIF(a.file_size, 0), NULLIF(c.file_size, 0),
                        COALESCE(t.file_size, 0)) AS file_size
          FROM tracks t
          LEFT JOIN acquisition a ON a.id = (
                SELECT id FROM acquisition WHERE track_id = t.id
                 ORDER BY id DESC LIMIT 1)
          LEFT JOIN library_content c ON c.file_path = t.file_path
         WHERE t.file_path != ''
           AND t.library_status != 'archived'
           AND COALESCE(NULLIF(a.sha256, ''), NULLIF(c.digest, '')) != ''
         ORDER BY t.id
        """)

    seen: dict[int, dict] = {r["track_id"]: dict(r) for r in rows}

    # A canary counts even when it sits outside the library root: it was
    # acquired through the pipeline deliberately, so its disappearance is
    # the finding rather than a curiosity.
    for c in await fetch_all(
            "SELECT track_id, expected_path, sha256, file_size "
            "  FROM canary WHERE sha256 != '' AND track_id IS NOT NULL"):
        seen.setdefault(c["track_id"], {
            "track_id": c["track_id"],
            "file_path": c["expected_path"],
            "sha256": c["sha256"],
            "file_size": c["file_size"],
        })
    return sorted(seen.values(), key=lambda r: r["track_id"])


def _poll_blocking(targets: list[dict]) -> list[dict]:
    """One read of the filesystem for the watch list. Reads only.

    Hashing is conditional, because re-hashing every known file on every
    poll would make a cheap observer expensive. A file whose size matches
    what provenance says it should be is left alone; a size disagreement is
    the interesting case and does get hashed, which is how a file replaced
    in place is told apart from one that merely changed timestamp.
    """
    out = []
    budget = MAX_HASHES_PER_POLL
    for t in targets:
        path = Path(t["file_path"])
        want = t.get("sha256") or ""
        expect_size = int(t.get("file_size") or 0)

        if not path.is_file():
            out.append({**t, "state": MISSING, "detail": "not on disk"})
            continue

        try:
            size = path.stat().st_size
        except OSError as e:
            out.append({**t, "state": MISSING,
                        "detail": f"not readable: {e}"})
            continue

        if want and expect_size and size == expect_size:
            out.append({**t, "state": PRESENT,
                        "detail": "present, size unchanged"})
            continue

        if budget <= 0:
            out.append({**t, "state": PRESENT,
                        "detail": "present, hash deferred: budget spent"})
            continue
        budget -= 1

        try:
            digest, real_size = _sha256_blocking(path)
        except OSError as e:
            out.append({**t, "state": MISSING,
                        "detail": f"not readable: {e}"})
            continue

        if not want:
            out.append({**t, "state": PRESENT, "file_size": real_size,
                        "detail": "present"})
        elif digest == want:
            out.append({**t, "state": PRESENT, "file_size": real_size,
                        "detail": "present, digest matches"})
        else:
            out.append({**t, "state": RENAMED, "file_size": real_size,
                        "detail": "path holds different content"})
    return out


async def _resolve_moved(readings: list[dict]) -> list[dict]:
    """Upgrade a settled `missing` to `moved` when the bytes turn up.

    Recorded digests only, never the filesystem. The digest sweep in
    `_locate_digest_blocking` is a deliberate step further and it is the
    right answer for a one-off canary check; running it on every poll over
    every vanished file would turn a cheap observer into a full library
    rehash, and a poll that never finishes observes nothing at all.
    """
    for r in readings:
        if r["state"] != MISSING or not r.get("sha256"):
            continue
        found = await fetch_one(
            "SELECT file_path FROM library_content WHERE digest = ?",
            (r["sha256"],))
        where = found["file_path"] if found else ""
        if where and where != r["file_path"]:
            r["state"] = MOVED
            r["detail"] = f"same content now at {where}"
    return readings


async def poll(debouncer: "_Debouncer | None" = None) -> dict:
    """One debounced observation pass over known-provenance files.

    A caller that polls once and sees nothing has *not* been told the files
    are fine; it has been told they have not settled yet. That distinction
    is the whole reason this is not `observe()` in a loop.
    """
    deb = debouncer if debouncer is not None else _observer_debouncer
    targets = await known_provenance_files()
    if not targets:
        return {"checked": 0, "transitions": 0, "pending": 0}

    readings = await _resolve_moved(
        await asyncio.to_thread(_poll_blocking, targets))

    written = 0
    for r in readings:
        key = r["track_id"]
        if not deb.offer(key, r["state"]):
            continue
        was = await _last_state(key)
        if was == r["state"]:
            continue
        state = r["state"]
        # A file that comes back reads as REAPPEARED rather than PRESENT,
        # because "it returned" and "it never left" are different findings.
        if was and state == PRESENT and was != MISSING:
            state = REAPPEARED
        detail = f"{state} (debounced x{deb.required}): {r['detail']}"
        await _record(key, r["file_path"], state, detail,
                      r.get("sha256") or "", int(r.get("file_size") or 0))
        written += 1

    # `pending` counts verdicts that have not repeated enough yet, not the
    # size of the debounce table. Those are different numbers: a settled
    # verdict whose state matches the last row stays in the table forever
    # (it is what makes the next disagreement detectable) but is no longer
    # pending, and reporting it as such would tell an operator that nothing
    # has settled after it plainly has.
    unsettled = sum(1 for _, count in deb._seen.values()
                    if count < deb.required)
    return {"checked": len(readings), "transitions": written,
            "pending": unsettled}


async def _observer_loop(interval: float) -> None:
    logger.info("Continuous observation started (every %.0fs, debounce x%d)",
                interval, _observer_debouncer.required)
    while True:
        try:
            await asyncio.sleep(interval)
            result = await poll()
            if result["transitions"]:
                logger.info(
                    "Observation: %d transition(s) across %d known file(s)",
                    result["transitions"], result["checked"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A failed pass is no reason to stop watching. Losing the
            # observer to one unreadable path would be precisely the
            # failure this instrument exists to catch.
            logger.warning("Observation pass failed, continuing: %s", e)


async def start_observer(interval: float = OBSERVER_INTERVAL) -> bool:
    """Begin continuous observation. False if it is already running."""
    global _observer_task
    if _observer_task and not _observer_task.done():
        return False
    _observer_task = asyncio.create_task(_observer_loop(interval))
    return True


async def stop_observer() -> None:
    global _observer_task
    if _observer_task and not _observer_task.done():
        _observer_task.cancel()
        try:
            await _observer_task
        except asyncio.CancelledError:
            pass
    _observer_task = None


def reset_observer_state() -> None:
    """Forget pending verdicts and cached debounce counts.

    For tests, and for a deliberate resync. A caller that has just
    reorganised everything should be able to drop what it believes rather
    than have those beliefs silently debounced into existence.
    """
    _observer_debouncer.clear()


# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------

async def register_canary(track_id: int, expected_path: str,
                          label: str = "", job_id: str = "") -> dict:
    """Record a known file so its later absence is attributable.

    The digest is taken at registration, not borrowed from provenance: a
    canary is a specimen, and what makes it evidence is that its bytes
    were measured before anything happened to it.
    """
    path = Path(expected_path)
    if not path.is_file():
        raise FileNotFoundError(expected_path)
    digest, size = await asyncio.to_thread(_sha256_blocking, path)
    cur = await execute(
        """
        INSERT INTO canary
            (track_id, label, job_id, expected_path, sha256, file_size,
             registered_at, state, last_verified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'present', ?)
        """,
        (track_id, label, job_id, str(path), digest, size,
         _utcnow_iso(), _utcnow_iso()),
    )
    await _record(track_id, str(path), CREATED,
                  f"canary registered ({label})" if label
                  else "canary registered", digest, size)
    return await canary(cur.lastrowid)


async def verify_canary(canary_id: int) -> dict:
    """Re-check one canary and append what happened to it.

    Reports `moved` rather than `missing` when the same bytes turn up at
    another path inside the library, because "it moved" and "it is gone"
    call for completely different responses.
    """
    row = await fetch_one(
        "SELECT * FROM canary WHERE id = ?", (canary_id,))
    if not row:
        raise LookupError(canary_id)

    path = Path(row["expected_path"])
    state, detail, digest, size = MISSING, "", "", 0
    where = ""  # only ever a real path once MOVED is decided below
    if path.is_file():
        digest, size = await asyncio.to_thread(_sha256_blocking, path)
        if digest == row["sha256"]:
            state = PRESENT
            detail = "still present, digest matches"
        else:
            state = RENAMED
            detail = "path holds different content"
    else:
        # Recorded digests first, then the filesystem itself.
        found = await fetch_one(
            "SELECT file_path FROM library_content WHERE digest = ?",
            (row["sha256"],))
        where = found["file_path"] if found else ""
        if not where:
            where = await fetch_one(
                "SELECT t.file_path AS file_path FROM acquisition a "
                "  JOIN tracks t ON t.id = a.track_id "
                " WHERE a.sha256 = ? AND t.file_path != ? LIMIT 1",
                (row["sha256"], row["expected_path"]))
            where = where["file_path"] if where else ""
        if not where:
            from .. import config
            where = await asyncio.to_thread(
                _locate_digest_blocking,
                Path(config.get("library_path")), row["sha256"])
        if where:
            state = MOVED
            detail = f"same content now at {where}"
        else:
            state = MISSING
            detail = "not on disk, and no other path holds these bytes"

    # `last_seen_path` is where the specimen was last observed to exist,
    # so a move has to write the discovered path. Clearing it to ""
    # on `moved` discarded the only pointer to where the bytes went -
    # and repointing the row is the whole response to a move, so
    # keeping it is what makes the state actionable.
    if state in (PRESENT, RENAMED):
        last_seen = str(path)
    elif state == MOVED:
        last_seen = where
    else:
        last_seen = ""
    await execute(
        "UPDATE canary SET state=?, last_verified_at=?, "
        "last_seen_path=? WHERE id=?",
        (state, _utcnow_iso(), last_seen, canary_id),
    )
    await _record(row["track_id"], row["expected_path"], state, detail,
                  row["sha256"], row["file_size"])
    return {"id": canary_id, "state": state, "detail": detail,
            "sha256": row["sha256"],
            "expected_path": row["expected_path"],
            "found_path": where if state == MOVED else ""}


async def canary(canary_id: int) -> dict | None:
    row = await fetch_one("SELECT * FROM canary WHERE id = ?", (canary_id,))
    return dict(row) if row else None


async def list_canaries() -> list[dict]:
    rows = await fetch_all("SELECT * FROM canary ORDER BY id DESC")
    return [dict(r) for r in rows]


async def timeline(track_id: int, limit: int = 200) -> list[dict]:
    rows = await fetch_all(
        "SELECT * FROM file_observation WHERE track_id = ? "
        "  ORDER BY id DESC LIMIT ?", (track_id, limit))
    return [dict(r) for r in rows]


async def summary() -> dict:
    row = await fetch_one(
        """
        SELECT COUNT(*) AS observations,
               COUNT(DISTINCT track_id) AS tracks
          FROM file_observation
        """)
    by_state = await fetch_all(
        "SELECT state, COUNT(*) AS n FROM file_observation "
        " GROUP BY state ORDER BY n DESC")
    canaries = await fetch_one(
        "SELECT COUNT(*) AS n FROM canary WHERE state = 'present'")
    return {
        "observations": (row["observations"] if row else 0),
        "tracks_observed": (row["tracks"] if row else 0),
        "canaries_present": (canaries["n"] if canaries else 0),
        "by_state": {r["state"]: r["n"] for r in by_state},
    }
