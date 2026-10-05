"""Library scanner with reconciliation.

Scan flow:
    1. Build fingerprint set for all audio files on disk
    2. Diff against DB: detect added, missing, moved files
    3. Insert new files, mark missing files, update moved files

Files that cannot be parsed by mutagen (zero-byte, still being written, or
not audio at all despite the extension) are counted as errors and skipped,
never indexed.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..database import fetch_one, fetch_all, execute
from .. import config
from .tagger import read_tags, AUDIO_EXTENSIONS
from . import acquisition

logger = logging.getLogger(__name__)

_scan_state = {"scanning": False, "total": 0, "processed": 0,
               "added": 0, "updated": 0, "missing": 0, "errors": 0}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# SQLite's default bound-parameter ceiling is 999; stay well under it so a
# large library cannot fail the whole statement on one huge chunk.
_VERIFY_CHUNK = 400


async def verify_filesystem() -> dict:
    """Reconcile track rows against what is actually on disk, cheaply.

    Existence only: no mutagen, no tag reads. A full `_scan_and_reconcile`
    opens and parses every file, which is the right thing when the user asked
    for a rescan and the wrong thing at startup - all this needs to know is
    whether each path is still there, and on a library of any size the tag
    parsing is where the seconds go.

    Two rules make this safe to run unattended:

    **An unavailable library root marks nothing.** If `library_path` is unset,
    or the folder is not there (an unmounted drive, a network share that did
    not come up), every path is absent and a naive check would empty the entire
    library on a technicality. The check refuses to conclude anything it cannot
    distinguish from "the disk is not mounted".

    **Both directions are reconciled.** A file that came back is put back to
    `present`. Without this, marking missing is a one-way ratchet: a library on
    a removable or network drive would go permanently blank after one bad
    morning, with no way back except a full rescan.
    """
    folder = config.get("library_path")
    if not folder:
        return {"status": "skipped", "reason": "no library_path configured",
                "marked_missing": 0, "restored_present": 0, "checked": 0}

    root = Path(folder)
    if not root.exists() or not root.is_dir():
        # Deliberately does not touch a single row. See the docstring.
        logger.warning(
            "Startup verify skipped: library root %s is not available. "
            "Marking every track missing here would hide a library that is "
            "probably still there.", folder
        )
        return {"status": "skipped", "reason": f"library root unavailable: {folder}",
                "marked_missing": 0, "restored_present": 0, "checked": 0}

    rows = await fetch_all(
        "SELECT id, file_path, file_status FROM tracks WHERE file_path != ''"
    )
    if not rows:
        return {"status": "complete", "marked_missing": 0, "restored_present": 0,
                "checked": 0, "reason": "library is empty"}

    # Stat calls are blocking syscalls. On a slow or network-backed volume
    # 800+ of them would stall the event loop, and startup is exactly when the
    # server is trying to become responsive.
    def _probe() -> tuple[list[int], list[int]]:
        gone: list[int] = []
        back: list[int] = []
        for row in rows:
            present = Path(row["file_path"]).is_file()
            if present:
                if row["file_status"] != "present":
                    back.append(row["id"])
            elif row["file_status"] != "missing":
                gone.append(row["id"])
        return gone, back

    gone, back = await asyncio.to_thread(_probe)

    now = _utcnow_iso()
    for ids, status in ((gone, "missing"), (back, "present")):
        for i in range(0, len(ids), _VERIFY_CHUNK):
            chunk = ids[i:i + _VERIFY_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            await execute(
                f"UPDATE tracks SET file_status=?, updated_at=? WHERE id IN ({placeholders})",
                tuple([status, now] + chunk),
            )

    result = {"status": "complete", "checked": len(rows),
              "marked_missing": len(gone), "restored_present": len(back)}
    if gone or back:
        logger.info(
            "Startup verify: %d file(s) missing, %d restored, of %d checked",
            len(gone), len(back), len(rows),
        )
    # A whole library disappearing at once is almost always the volume, not the
    # user. Say so loudly rather than letting it read as 800 separate deletions.
    if rows and len(gone) > len(rows) / 2:
        logger.warning(
            "%d of %d track files were not found under %s. That is most of "
            "the library - check that the drive is mounted and that "
            "library_path still points at it before treating these as deleted.",
            len(gone), len(rows), folder,
        )
    return result


async def reconcile_library(deep: bool = False,
                            include_archived: bool = True) -> dict:
    """Say what actually happened to every track, instead of inferring it.

    verify_filesystem() answers one bit per track - is the file there - and
    deliberately refuses to do more, because startup has to be cheap. This
    is the opposite trade: it is called when a person is looking at the
    Missing screen and needs an answer, and it spends real time to get one.

    Each track is classified into one of four states:

      present         the file is at its recorded path
      moved           not there, but a file with the identical SHA-256 was
                      found elsewhere in the library
      missing         not there, and we have provenance describing what
                      should have been there
      never_acquired  there was a job, but no acquisition ever succeeded

    The distinction between `moved` and `missing` is the whole point.
    They are identical on disk, but they need opposite responses: a moved
    file needs its row repointed, a missing one needs archiving or
    re-fetching. Collapsing them into "missing" is what turns a diagnosis
    into a guess.

    **`deep` is off by default.** Detecting a move requires hashing every
    unclaimed file in the library, which measured at ~10 seconds for 2.7 GB
    on this machine. That is a reasonable price for a button a person
    pressed and an unreasonable price for a page load, so the cheap pass
    reports what it can and says plainly that moves were not checked.
    """
    folder = config.get("library_path")
    if not folder:
        return {"status": "skipped", "reason": "no library_path configured",
                "counts": {}, "tracks": []}

    root = Path(folder)
    if not root.exists() or not root.is_dir():
        logger.warning(
            "Reconcile skipped: library root %s is not available. Reporting "
            "every track as missing here would describe a volume that is not "
            "there, not a library that lost its files.", folder,
        )
        return {"status": "skipped", "reason": f"library root unavailable: {folder}",
                "counts": {}, "tracks": []}

    where = "" if include_archived else " WHERE library_status != 'archived'"
    rows = await fetch_all(
        "SELECT id, file_path, title, artist, album, file_status, file_size,"
        f"       library_status FROM tracks{where}"
    )
    if not rows:
        return {"status": "complete", "counts": {}, "tracks": [],
                "deep": deep}

    acq_by_track = await acquisition.latest_by_track([r["id"] for r in rows])

    # Which tracks came from a job at all? A download that never wrote an
    # acquisition row is the "never acquired" case, and the only trace of
    # the job is the operations journal.
    op_rows = await fetch_all(
        "SELECT DISTINCT track_id, job_id FROM operations "
        "WHERE track_id IS NOT NULL"
    )
    job_by_track = {r["track_id"]: r["job_id"] for r in op_rows}

    def _probe() -> dict[int, bool]:
        return {r["id"]: Path(r["file_path"]).is_file() for r in rows}

    on_disk = await asyncio.to_thread(_probe)

    present: list[dict] = []
    missing: list[dict] = []
    moved_pairs: list[dict] = []
    never_acquired: list[dict] = []

    for row in rows:
        acq = acq_by_track.get(row["id"]) or {}
        job_id = job_by_track.get(row["id"])
        base = {
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
            "file_path": row["file_path"],
            "file_status": row["file_status"],
            "library_status": row["library_status"],
            "sha256": acq.get("sha256", "") or "",
            "acquired_at": acq.get("completed_at") or "",
            "acquired_size": acq.get("file_size", 0) or 0,
            "source_url": acq.get("source_url", "") or "",
            "job_id": job_id,
        }
        if on_disk[row["id"]]:
            base["state"] = acquisition.STATE_PRESENT
            base["detail"] = (
                "path unchanged" if acq.get("file_path") == row["file_path"]
                else "path changed since acquisition"
            )
            present.append(base)
            continue

        base["detail"] = "file is not at its recorded path"
        if not acq and job_id:
            base["state"] = acquisition.STATE_NEVER_ACQUIRED
            base["detail"] = (
                f"job {job_id} exists but no successful acquisition was recorded"
            )
            never_acquired.append(base)
            continue
        base["state"] = acquisition.STATE_MISSING
        missing.append(base)

    if deep and missing:
        moved_pairs, missing, present = await _find_moved(
            missing, present, rows, on_disk, Path(folder))

    # Each track lands in exactly one bucket. A file that turned up at a new
    # path is `moved`, not `missing`, so it has to leave the missing list
    # before the counts are taken - otherwise it is counted twice and the
    # four numbers stop describing a partition of the library.
    counts = {
        acquisition.STATE_PRESENT: len(present),
        acquisition.STATE_MISSING: len(missing),
        acquisition.STATE_MOVED: len(moved_pairs),
        acquisition.STATE_NEVER_ACQUIRED: len(never_acquired),
    }
    tracks = present + missing + never_acquired + [
        {
            "id": pair["track_id"],
            "expected_path": pair["expected_path"],
            "discovered_path": pair["discovered_path"],
            "sha256": pair["sha256"],
            "state": acquisition.STATE_MOVED,
            "detail": "same content found at a different path",
        }
        for pair in moved_pairs
    ]
    return {
        "status": "complete",
        "counts": counts,
        "tracks": tracks,
        "deep": deep,
        "moved_checked": deep,
        "library_path": folder,
    }


async def _find_moved(missing, present, rows, on_disk, root: Path):
    """Hash every unclaimed file and pair it with a missing track by digest.

    Only files that no track row claims are worth hashing - a file already
    accounted for cannot be the moved one, it belongs where it is.

    Returns (pairs, still_missing, present). `still_missing` matters: a
    track whose file was located elsewhere is no longer missing, and the
    caller has to drop it from that list or it is counted in two buckets.
    """
    from .tagger import AUDIO_EXTENSIONS

    claimed = {
        str(Path(r["file_path"]).resolve()).casefold()
        for r in rows if on_disk[r["id"]]
    }

    def _unclaimed() -> list[str]:
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

    candidates = await asyncio.to_thread(_unclaimed)
    if not candidates:
        # Nothing unclaimed means nothing to compare a digest against, so
        # nothing can have moved. `missing` comes back unchanged - the
        # caller unpacks three values and uses the second to keep the
        # buckets disjoint, so dropping it here raised ValueError instead
        # of returning counts.
        return [], missing, present

    logger.info(
        "Deep reconcile: hashing %d unclaimed file(s) to look for moved tracks",
        len(candidates),
    )
    index = await acquisition.hash_paths(candidates)

    wanted = [
        {"track_id": m["id"], "expected_path": m["file_path"], "sha256": m["sha256"]}
        for m in missing if m["sha256"]
    ]
    pairs = acquisition.match_moved(wanted, index)

    moved_ids = {p["track_id"] for p in pairs}
    still_missing = [m for m in missing if m["id"] not in moved_ids]
    return pairs, still_missing, present


async def _scan_and_reconcile(folder: str) -> int:
    """Scan folder and reconcile with database. Returns total files found."""
    audio_files = []
    for fp in Path(folder).rglob("*"):
        if fp.is_file() and fp.suffix.lower() in AUDIO_EXTENSIONS:
            audio_files.append(fp)

    total = len(audio_files)
    _scan_state["total"] = total

    def _readable(file: Path) -> bool:
        """Guard against indexing a partially written or empty file."""
        try:
            return file.stat().st_size > 0
        except OSError:
            return False

    audio_files = [f for f in audio_files if _readable(f)]

    disk_paths = {str(fp.resolve()).casefold(): fp for fp in audio_files}

    db_rows = await fetch_all("SELECT id, file_path FROM tracks")
    db_paths = {}
    for row in db_rows:
        db_paths[str(Path(row["file_path"]).resolve()).casefold()] = (row["id"], row["file_path"])

    missing = set(db_paths.keys()) - set(disk_paths)
    for m in missing:
        await execute(
            "UPDATE tracks SET file_status='missing', updated_at=? WHERE id=?",
            (_utcnow_iso(), db_paths[m][0]),
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
                tags["sample_rate"], _utcnow_iso(), db_paths[normalized_path][1],
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
                _utcnow_iso(), _utcnow_iso(),
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
            tags["sample_rate"], _utcnow_iso(), str(p),
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
                _utcnow_iso(), _utcnow_iso(),
            ))
    return tags
