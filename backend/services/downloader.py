"""Download orchestrator - processes queue jobs through the pipeline.

Pipeline flow:
    pending → resolving → matching → downloading → converting →
    tagging → organizing → indexing → complete

Reliability rules enforced here:
- Rate limits (yt-dlp 429 / "Sign in to confirm you're not a bot") back off
  exponentially instead of burning retries; the job is parked and retried.
- Downloads land in a per-job temp directory and are moved into the library
  atomically (os.replace), so a crash can never leave a half-written file in
  the library. `reclaim_orphans()` sweeps any temp leftovers at startup.
- Timestamps are timezone-aware (datetime.utcnow() is deprecated on 3.12).
"""
import asyncio
import datetime
import glob
import logging
import shutil
import uuid
from pathlib import Path

from ..database import fetch_one, fetch_all, execute
from .. import config
from ..errors import (
    DownloadError, ResolveError, ConversionError, TagError,
    OrganizeError, log_error, ErrorContext,
)
from ..events import emit_job_update, emit, emit_log
from ..pipeline.models import (
    JobState, TrackMetadata, SourceCandidate, SourceType,
    QualityProfile, QUALITY_PRESETS,
)
from ..pipeline.matcher import score_candidate, confidence_level
from ..pipeline.normalize import parse_artists
from ..pipeline.editor import apply_metadata_to_file
from ..pipeline.cleanup import run_cleanup
from ..sources.ytdlp_source import YtdlpResolver, YtdlpDownloader, detect_source
from ..sources.deezer_source import DeezerResolver, DeezerDownloader, is_deezer_url, detect_deezer
from . import source_registry
from ..services.converter import convert
from ..services.organizer import organize_file
from ..services.tagger import write_tags, read_tags

logger = logging.getLogger(__name__)

_worker_tasks = []
_stop_event = asyncio.Event()
_semaphore: asyncio.Semaphore | None = None

PHASE_RANGES = {
    "resolving": (0, 10),
    "matching": (10, 15),
    "downloading": (15, 65),
    "converting": (65, 80),
    "tagging": (80, 90),
    "organizing": (90, 97),
    "indexing": (97, 100),
}

# --- Rate-limit backoff -----------------------------------------------------
# yt-dlp answers HTTP 429 (or the "Sign in to confirm you're not a bot"
# interstitial) when YouTube throttles us. Retrying immediately burns all
# max_retries within seconds. Instead the job is parked in 'backoff' status
# with a not-before timestamp, and exponential delay is applied per job.
RATE_LIMIT_DELAY_BASE = 120.0      # seconds before first rate-limit retry
RATE_LIMIT_DELAY_MAX = 3600.0      # never wait more than an hour
RATE_LIMIT_MAX_PARKS = 4           # after this many parks, fail the job

# Markers that mean "YouTube is throttling us" rather than "this video broke".
_RATE_LIMIT_MARKERS = (
    "429",
    "too many requests",
    "sign in to confirm",
    "not a bot",
    "rate limit",
)


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def is_rate_limit_error(message: str) -> bool:
    """Heuristic: does this error string mean we are being throttled?"""
    msg = (message or "").lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


async def park_for_backoff(job_id: int, retries: int, error: str) -> float:
    """Park a throttled job and return how long it will wait.

    Delay doubles with each park: 2min → 4min → 8min → 16min. The job keeps
    status 'backoff' so the UI can show it as paused-for-rate-limit rather
    than failed; _pick_next_job() skips it until not_before passes.
    """
    delay = min(RATE_LIMIT_DELAY_BASE * (2 ** retries), RATE_LIMIT_DELAY_MAX)
    not_before = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=delay)
    ).isoformat()
    await execute(
        "UPDATE jobs SET status='backoff', error=?, retries=?, not_before=?, "
        "progress=0, updated_at=? WHERE id=?",
        (error, retries, not_before, _utcnow_iso(), job_id),
    )
    await emit_job_update(job_id, "backoff", error=f"rate limited; retrying in {int(delay)}s")
    logger.warning(
        f"Job {job_id} rate limited; parked for {int(delay)}s (park {retries + 1})"
    )
    return delay


async def _wake_backoff_jobs():
    """Move parked jobs whose backoff expired back to pending."""
    now = _utcnow_iso()
    rows = await fetch_all(
        "SELECT id FROM jobs WHERE status='backoff' AND not_before <= ?", (now,)
    )
    for row in rows:
        await execute(
            "UPDATE jobs SET status='pending', updated_at=? WHERE id=?",
            (_utcnow_iso(), row["id"]),
        )
        logger.info(f"Job {row['id']} backoff expired, requeued")


async def reclaim_orphans() -> int:
    """Clean temp dirs left behind by a crash mid-pipeline.

    Downloads are staged under data/tmp/<job-uuid>/ and converted under
    data/tmp/converted/. Anything left there from a previous run that no live
    job owns is deleted. Returns number of files removed. Safe to call at
    every startup.
    """
    tmp_root = Path(config.ROOT) / "data" / "tmp"
    if not tmp_root.exists():
        return 0
    removed = 0
    for entry in tmp_root.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            else:
                entry.unlink(missing_ok=True)
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info(f"Reclaimed {removed} orphaned temp entr(y/ies) from previous run")
    return removed


def _deemix_bitrate(profile: QualityProfile) -> str:
    """Pick the best deemix tier this quality profile can actually deliver.

    Only 128 streams without a premium ARL, but asking for it up front and
    letting DeezerDownloader fall back is better than hardcoding 128 and
    throwing away quality for people who have logged in.
    """
    if profile.lossless or profile.output_format == "flac":
        return "flac"
    # QualityProfile.bitrate is a string like "320k" / "128k".
    digits = "".join(ch for ch in str(profile.bitrate or "") if ch.isdigit())
    kbps = int(digits) if digits else 0
    return "320" if kbps >= 320 else "128"


async def start_worker():
    global _worker_tasks, _semaphore
    _stop_event.clear()
    _semaphore = asyncio.Semaphore(1)
    _worker_tasks = [asyncio.create_task(_worker_loop())]
    logger.info("Download worker started (1 slot)")


async def stop_worker():
    global _worker_tasks
    _stop_event.set()
    for t in _worker_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    _worker_tasks = []
    logger.info("Download workers stopped")


def set_concurrency(max_concurrent: int):
    """Update worker concurrency at runtime."""
    global _semaphore, _worker_tasks
    requested = max_concurrent
    max_concurrent = max(1, min(max_concurrent, 8))
    if max_concurrent != requested:
        logger.warning(
            f"max_concurrent_downloads={requested} clamped to {max_concurrent} (hard cap is 8)"
        )
    _semaphore = asyncio.Semaphore(max_concurrent)
    current = len(_worker_tasks)
    if max_concurrent > current:
        for _ in range(max_concurrent - current):
            _worker_tasks.append(asyncio.create_task(_worker_loop()))
    logger.info(f"Download concurrency set to {max_concurrent}")


async def _worker_loop():
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    while not _stop_event.is_set():
        try:
            async with _semaphore:
                await _wake_backoff_jobs()
                job = await _pick_next_job()
                if job:
                    await _process_job(job)
                else:
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            await asyncio.sleep(5)


async def _pick_next_job() -> dict | None:
    """Atomically claim the next pending job.

    One UPDATE ... RETURNING statement, so the claim cannot race a competing
    worker. It must talk to the connection directly rather than going through
    database.execute(): that helper takes _db_lock itself, and asyncio.Lock is
    not reentrant, so calling it while already holding the lock deadlocks the
    whole app on the first job.
    """
    from ..database import _db_lock, get_db

    async with _db_lock:
        async with get_db() as db:
            cursor = await db.execute(
                """
                UPDATE jobs
                   SET status = 'resolving', updated_at = ?
                 WHERE id = (
                       SELECT id FROM jobs
                        WHERE status = 'pending'
                        ORDER BY created_at ASC
                        LIMIT 1
                 )
                   AND status = 'pending'
                RETURNING *
                """,
                (_utcnow_iso(),),
            )
            row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None


async def _set_state(job_id: int, state: JobState, progress: float = None):
    updates = {"status": state.value, "updated_at": _utcnow_iso()}
    if progress is not None:
        updates["progress"] = progress
    set_parts = [f"{k}=?" for k in updates]
    values = list(updates.values()) + [job_id]
    await execute(f"UPDATE jobs SET {', '.join(set_parts)} WHERE id=?", tuple(values))


async def _update_job(job_id: int, **kwargs):
    if not kwargs:
        return
    kwargs["updated_at"] = _utcnow_iso()
    set_parts = []
    values = []
    for k, v in kwargs.items():
        set_parts.append(f"{k}=?")
        values.append(v)
    sql = f"UPDATE jobs SET {', '.join(set_parts)} WHERE id=?"
    values.append(job_id)
    await execute(sql, tuple(values))


def _job_tmp_dir(job_id: int) -> Path:
    """Per-job staging directory inside the project (not the OS temp dir).

    Keeping it under data/tmp means reclaim_orphans() knows exactly what is
    ours to delete, and Windows %TEMP% does not accumulate junk.
    """
    d = Path(config.ROOT) / "data" / "tmp" / f"job_{job_id}_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _fallback_url_for(job: dict, failed_url: str, settings: dict) -> str | None:
    """Next search-source URL for a failed search job, per source_priority.

    Only search jobs (ytsearch1:/scsearch1:/dzsearch:) reroute - direct URLs
    are pinned to their source. Returns None when fallback is disabled, the
    job is not a search job, or no unused source remains.
    """
    if not settings.get("source_fallback", True):
        return None
    if not source_registry.is_search_job_url(failed_url):
        return None
    order = source_registry.normalize_priority(settings.get("source_priority"))
    current = source_registry.search_source_of(failed_url)
    remaining = order[order.index(current) + 1:] if current in order else order
    if not remaining:
        return None
    from urllib.parse import unquote
    query = unquote(failed_url.split(":", 1)[1])
    artist = (job.get("artist") or "").strip() or query
    title = (job.get("title") or "").strip() or query
    return source_registry.build_search_job_url(remaining[0], artist, title)


async def _try_fallback(job_id: int, job: dict, failed_url: str, settings: dict, error: str) -> bool:
    """Requeue a failed search job on the next source in source_priority.

    Returns True when the job was rerouted (the caller must not retry or
    fail it). Direct-URL jobs never reroute; retries are not consumed by a
    source switch so each source gets a fair shot.
    """
    next_url = await _fallback_url_for(job, failed_url, settings)
    if not next_url:
        return False
    await execute(
        "UPDATE jobs SET source_url=?, status='pending', error=? WHERE id=?",
        (next_url, f"{error[:200]} — trying next source", job_id),
    )
    await emit_job_update(job_id, "retrying", error=f"trying next source ({error[:120]})")
    logger.info(f"Job {job_id}: {failed_url} failed ({error[:80]}); falling back to {next_url}")
    return True


async def _process_job(job: dict):
    job_id = job["id"]
    original_url = job["source_url"]
    source_url = original_url
    output_format = job.get("output_format", "mp3")
    quality_name = job.get("quality_profile", "balanced")
    settings = config.get_all()
    max_retries = settings.get("max_retries", 3)

    profile = QUALITY_PRESETS.get(quality_name, QUALITY_PRESETS["balanced"])
    if output_format != profile.output_format:
        profile = QualityProfile(
            name=quality_name, output_format=output_format,
            bitrate=profile.bitrate, sample_rate=profile.sample_rate,
        )

    logger.info(f"Processing job {job_id}: {source_url}")
    tmp_dir = _job_tmp_dir(job_id)

    try:
        # Deezer search jobs resolve to a concrete track URL first; "nothing
        # found" raises ResolveError and the fallback chain moves on.
        if source_url.startswith("dzsearch:"):
            from urllib.parse import unquote
            query = unquote(source_url.split(":", 1)[1])
            resolved = await source_registry.deezer_search_track(
                (job.get("artist") or "").strip() or query,
                (job.get("title") or "").strip() or query,
            )
            if not resolved:
                raise ResolveError(f"Deezer search found nothing for '{query}'")
            logger.info(f"Job {job_id}: dzsearch resolved to {resolved}")
            source_url = resolved

        await _set_state(job_id, JobState.RESOLVING, 5)
        await emit_job_update(job_id, "resolving")

        # Route to Deezer adapter if URL is from Deezer
        is_deezer = is_deezer_url(source_url)
        if is_deezer:
            resolver = DeezerResolver()
            source_type_str = detect_deezer(source_url) or "deezer"
        else:
            resolver = YtdlpResolver()
            source_type_str = detect_source(source_url)

        meta = await resolver.resolve(source_url)
        await _update_job(job_id, title=meta.title, artist=meta.artist, progress=10)

        await _set_state(job_id, JobState.MATCHING, 10)
        await emit_job_update(job_id, "matching", 10)
        candidate = SourceCandidate(
            url=source_url,
            source_type=SourceType(source_type_str) if source_type_str in [e.value for e in SourceType] else SourceType.UNKNOWN,
            title=meta.title,
            artist=meta.artist,
            duration=meta.duration,
        )
        match = score_candidate(meta, candidate)
        conf_level = confidence_level(match.confidence)
        logger.info(f"Job {job_id} match: {match.confidence}% ({conf_level})")

        match_threshold = settings.get("match_threshold", 70)
        if match.confidence < match_threshold:
            raise DownloadError(
                f"Match confidence {match.confidence}% below threshold {match_threshold}%: "
                f"{match.explanation}",
                recoverable=False,
            )

        thumb_path = ""
        # Use Deezer adapter for Deezer URLs, yt-dlp for everything else
        if is_deezer:
            downloader = DeezerDownloader(
                bitrate=_deemix_bitrate(profile),
                fallback_bitrate="128",
            )
        else:
            downloader = YtdlpDownloader()

        if candidate.source_type != SourceType.LOCAL:
            try:
                thumb_base = str(tmp_dir / "thumb")
                thumb_path = await downloader.download_thumbnail(candidate, thumb_base)
            except Exception:
                thumb_path = ""

        await _set_state(job_id, JobState.DOWNLOADING, 15)
        await emit_job_update(job_id, "downloading", 15)
        dl_dir = tmp_dir / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        dl_base = str(dl_dir / f"dl_{uuid.uuid4().hex[:12]}")

        loop = asyncio.get_running_loop()

        def dl_progress(pct):
            mapped = 15 + (pct / 100) * 50
            loop.call_soon_threadsafe(
                asyncio.ensure_future, _update_job(job_id, progress=mapped)
            )

        try:
            actual_path, info = await downloader.download(candidate, dl_base, dl_progress)
        except Exception as e:
            if is_rate_limit_error(str(e)):
                retries = job.get("retries", 0) + 1
                if retries < RATE_LIMIT_MAX_PARKS:
                    await park_for_backoff(job_id, retries, str(e))
                    return
                raise DownloadError(
                    f"Still rate limited after {retries} backoff waits: {e}",
                    recoverable=False,
                ) from e
            raise

        if isinstance(info, dict) and info.get("fell_back"):
            # We are about to re-encode a low-bitrate source up to the target
            # profile, which inflates the file size without adding quality.
            logger.warning(
                f"Job {job_id}: source was only {info.get('bitrate')}k but the "
                f"{quality_name} profile targets {profile.bitrate}; the result will be "
                f"larger than the source with no extra fidelity. Log in to deemix "
                f"(`deemix login`) for a premium ARL to unlock 320/FLAC."
            )
        await _update_job(job_id, progress=65)

        await _set_state(job_id, JobState.CONVERTING, 65)
        await emit_job_update(job_id, "converting", 65)
        conv_dir = tmp_dir / "converted"
        conv_dir.mkdir(parents=True, exist_ok=True)
        conv_path = str(conv_dir / f"conv_{uuid.uuid4().hex[:12]}.{profile.output_format}")

        await convert(actual_path, conv_path, profile=profile)
        await _update_job(job_id, progress=80)

        await _set_state(job_id, JobState.TAGGING, 80)
        await emit_job_update(job_id, "tagging", 80)
        tag_data = {
            "title": meta.title,
            "artist": meta.artist,
            "primary_artist": meta.primary_artist,
            "album_artist": meta.album_artist,
            "album": meta.album,
            "year": meta.year,
            "genre": meta.genre,
            "track_number": meta.track_number,
        }
        cleanup_result = run_cleanup(tag_data)
        if cleanup_result.changes:
            logger.info(f"Job {job_id} cleanup: {len(cleanup_result.changes)} fixes")
            for ch in cleanup_result.changes:
                logger.debug(f"  {ch.field}: '{ch.before}' → '{ch.after}' ({ch.rule})")
        try:
            await write_tags(conv_path, tag_data, cover_path=thumb_path)
        except Exception as exc:
            # The audio is already downloaded and converted at this point, so a
            # tag failure must not discard the file. Surface it on the job and
            # continue - the organizer writes tags again on the final copy.
            logger.warning("Job %s: tag write failed on %s: %s", job_id, conv_path, exc)
            await _update_job(job_id, error=f"Tag write failed: {exc}")
            await emit_log("warning", f"Tag write failed for job {job_id}: {exc}", source="tagger")
        await _update_job(job_id, progress=90)

        await _set_state(job_id, JobState.ORGANIZING, 90)
        await emit_job_update(job_id, "organizing", 90)
        folder_template = settings.get("folder_template", "{album_artist}\\{album}\\{track_number} - {title}.{format}")
        library_path = settings.get("library_path", str(Path.home() / "Music"))
        dup_policy = settings.get("duplicate_policy", "keep_separate")

        final_path = await organize_file(
            conv_path, tag_data,
            library_path=library_path,
            folder_template=folder_template,
            duplicate_policy=dup_policy,
        )
        await _update_job(job_id, progress=97)

        await _set_state(job_id, JobState.INDEXING, 97)
        await emit_job_update(job_id, "indexing", 97)
        await _add_to_library(final_path, meta, source_url)
        await _set_state(job_id, JobState.COMPLETE, 100)
        await emit_job_update(job_id, "complete", 100, output_path=final_path)
        await emit("library_change", {"action": "added"})
        await _update_job(job_id, output_path=final_path)
        logger.info(f"Job {job_id} complete: {final_path}")

    except ResolveError as e:
        log_error(e, ErrorContext(operation="resolve", job_id=job_id, source_url=source_url))
        # Search jobs try the next source before parking: a YouTube 429 says
        # nothing about Deezer or SoundCloud.
        if await _try_fallback(job_id, job, original_url, settings, str(e)):
            return
        if is_rate_limit_error(str(e)):
            retries = job.get("retries", 0) + 1
            if retries < RATE_LIMIT_MAX_PARKS:
                await park_for_backoff(job_id, retries, str(e))
                return
        retries = job.get("retries", 0) + 1
        if retries < max_retries:
            await _update_job(job_id, status="pending", error=str(e), retries=retries)
            await emit_job_update(job_id, "retrying", error=str(e))
        else:
            await _set_state(job_id, JobState.FAILED)
            await _update_job(job_id, error=str(e), retries=retries)
            await emit_job_update(job_id, "failed", error=str(e))

    except (DownloadError, ConversionError, TagError, OrganizeError) as e:
        log_error(e, ErrorContext(operation=e.code, job_id=job_id, source_url=source_url))
        if await _try_fallback(job_id, job, original_url, settings, str(e)):
            return
        if is_rate_limit_error(str(e)) and e.recoverable:
            retries = job.get("retries", 0) + 1
            if retries < RATE_LIMIT_MAX_PARKS:
                await park_for_backoff(job_id, retries, str(e))
                return
        retries = job.get("retries", 0) + 1
        if retries < max_retries and e.recoverable:
            await _update_job(job_id, status="pending", error=str(e), retries=retries)
            await emit_job_update(job_id, "retrying", error=str(e))
        else:
            await _set_state(job_id, JobState.FAILED)
            await _update_job(job_id, error=str(e), retries=retries)
            await emit_job_update(job_id, "failed", error=str(e))

    except Exception as e:
        log_error(e, ErrorContext(operation="unknown", job_id=job_id, source_url=source_url))
        if await _try_fallback(job_id, job, original_url, settings, str(e)):
            return
        if is_rate_limit_error(str(e)):
            retries = job.get("retries", 0) + 1
            if retries < RATE_LIMIT_MAX_PARKS:
                await park_for_backoff(job_id, retries, str(e))
                return
        retries = job.get("retries", 0) + 1
        if retries < max_retries:
            await _update_job(job_id, status="pending", error=str(e), retries=retries)
            await emit_job_update(job_id, "retrying", error=str(e))
        else:
            await _set_state(job_id, JobState.FAILED)
            await _update_job(job_id, error=str(e), retries=retries)
            await emit_job_update(job_id, "failed", error=str(e))

    finally:
        # Whatever happened, this job's staging directory can go. The finished
        # file was moved out by organize_file/_atomic_move before indexing.
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _add_to_library(file_path: str, meta: TrackMetadata, source_url: str):
    """Insert a newly downloaded track into the library."""
    try:
        tags = await read_tags(file_path)
    except Exception:
        tags = {}

    parsed = parse_artists(meta.artist)
    file_size = Path(file_path).stat().st_size if Path(file_path).exists() else 0
    await execute("""
        INSERT OR REPLACE INTO tracks
        (file_path, title, artist, primary_artist, featured_artists, display_artist,
         album_artist, album, track_number, disc_number,
         year, genre, duration, file_size, format, bitrate, sample_rate,
         cover_art_path, source_url, source_type, file_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'present')
    """, (
        file_path,
        tags.get("title", meta.title),
        tags.get("artist", meta.artist),
        parsed["primary"],
        ",".join(parsed["featured"]),
        parsed["display"],
        tags.get("album_artist", parsed["album_artist"]),
        tags.get("album", meta.album),
        tags.get("track_number", meta.track_number),
        tags.get("disc_number", meta.disc_number),
        tags.get("year", meta.year),
        tags.get("genre", meta.genre),
        tags.get("duration", meta.duration),
        file_size,
        Path(file_path).suffix.lstrip("."),
        tags.get("bitrate", 0),
        tags.get("sample_rate", 0),
        tags.get("cover_art_path", ""),
        source_url,
        "download",
    ))
