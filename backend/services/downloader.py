"""Download orchestrator - processes queue jobs through the pipeline.

Pipeline flow:
    pending → resolving → matching → downloading → converting →
    tagging → organizing → indexing → complete

FIXED: Uses source-quality acquisition, proper quality profiles,
weighted progress, and the pipeline interface system.
"""
import asyncio
import datetime
import logging
import tempfile
import uuid
from pathlib import Path

from ..database import fetch_one, fetch_all, execute
from .. import config
from ..errors import (
    DownloadError, ResolveError, ConversionError, TagError,
    OrganizeError, log_error, ErrorContext,
)
from ..events import emit_job_update, emit_library_change
from ..pipeline.models import (
    JobState, TrackMetadata, SourceCandidate, SourceType,
    QualityProfile, QUALITY_PRESETS,
)
from ..pipeline.matcher import score_candidate, confidence_level
from ..pipeline.normalize import parse_artists
from ..pipeline.editor import apply_metadata_to_file
from ..pipeline.cleanup import run_cleanup
from ..sources.ytdlp_source import YtdlpResolver, YtdlpDownloader, detect_source
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


async def start_worker():
    global _worker_tasks, _semaphore
    _stop_event.clear()
    _semaphore = asyncio.Semaphore(1)
    _worker_tasks = [asyncio.create_task(_worker_loop())]
    logger.info("Download worker started (1 slot)")


async def stop_worker():
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
    max_concurrent = max(1, min(max_concurrent, 8))
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
    job = await fetch_one(
        "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
    )
    if job:
        await _set_state(job["id"], JobState.RESOLVING)
        job["status"] = "resolving"
    return job


async def _set_state(job_id: int, state: JobState, progress: float = None):
    updates = {"status": state.value, "updated_at": datetime.datetime.utcnow().isoformat()}
    if progress is not None:
        updates["progress"] = progress
    set_parts = [f"{k}=?" for k in updates]
    values = list(updates.values()) + [job_id]
    await execute(f"UPDATE jobs SET {', '.join(set_parts)} WHERE id=?", tuple(values))


async def _update_job(job_id: int, **kwargs):
    if not kwargs:
        return
    kwargs["updated_at"] = datetime.datetime.utcnow().isoformat()
    set_parts = []
    values = []
    for k, v in kwargs.items():
        set_parts.append(f"{k}=?")
        values.append(v)
    sql = f"UPDATE jobs SET {', '.join(set_parts)} WHERE id=?"
    values.append(job_id)
    await execute(sql, tuple(values))


async def _process_job(job: dict):
    job_id = job["id"]
    source_url = job["source_url"]
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

    try:
        await _set_state(job_id, JobState.RESOLVING, 5)
        await emit_job_update(job_id, "resolving")
        resolver = YtdlpResolver()
        meta = await resolver.resolve(source_url)
        await _update_job(job_id, title=meta.title, artist=meta.artist, progress=10)

        await _set_state(job_id, JobState.MATCHING, 10)
        await emit_job_update(job_id, "matching", 10)
        candidate = SourceCandidate(
            url=source_url,
            source_type=SourceType(detect_source(source_url)),
            title=meta.title,
            artist=meta.artist,
            duration=meta.duration,
        )
        match = score_candidate(meta, candidate)
        conf_level = confidence_level(match.confidence)
        logger.info(f"Job {job_id} match: {match.confidence}% ({conf_level})")

        thumb_path = ""
        downloader = YtdlpDownloader()
        if candidate.source_type != SourceType.LOCAL:
            try:
                thumb_dir = Path(tempfile.gettempdir()) / "bigpickle"
                thumb_dir.mkdir(exist_ok=True)
                thumb_base = str(thumb_dir / f"thumb_{job_id}")
                thumb_path = await downloader.download_thumbnail(candidate, thumb_base)
            except Exception:
                thumb_path = ""

        await _set_state(job_id, JobState.DOWNLOADING, 15)
        await emit_job_update(job_id, "downloading", 15)
        dl_dir = Path(tempfile.gettempdir()) / "bigpickle" / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        dl_base = str(dl_dir / f"dl_{uuid.uuid4().hex[:12]}")

        def dl_progress(pct):
            mapped = 15 + (pct / 100) * 50
            asyncio.ensure_future(_update_job(job_id, progress=mapped))

        actual_path, info = await downloader.download(candidate, dl_base, dl_progress)
        await _update_job(job_id, progress=65)

        await _set_state(job_id, JobState.CONVERTING, 65)
        await emit_job_update(job_id, "converting", 65)
        conv_dir = Path(tempfile.gettempdir()) / "bigpickle" / "converted"
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
        await write_tags(conv_path, tag_data, cover_path=thumb_path)
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
        await emit_library_change("added")
        await _update_job(job_id, output_path=final_path)
        logger.info(f"Job {job_id} complete: {final_path}")

        _cleanup_temp(dl_base + ".*")
        _cleanup_temp(conv_path)

    except ResolveError as e:
        log_error(e, ErrorContext(operation="resolve", job_id=job_id, source_url=source_url))
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
        retries = job.get("retries", 0) + 1
        if retries < max_retries:
            await _update_job(job_id, status="pending", error=str(e), retries=retries)
            await emit_job_update(job_id, "retrying", error=str(e))
        else:
            await _set_state(job_id, JobState.FAILED)
            await _update_job(job_id, error=str(e), retries=retries)
            await emit_job_update(job_id, "failed", error=str(e))


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


def _cleanup_temp(pattern: str):
    import glob
    for f in glob.glob(pattern):
        try:
            Path(f).unlink(missing_ok=True)
        except Exception:
            pass
