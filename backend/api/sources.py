"""Source search and URL resolve endpoints."""
from fastapi import APIRouter, HTTPException, Query
from .. import config
from ..validation import validate_url, validate_folder_template
from ..services.matcher import search_metadata
from ..sources.ytdlp_source import resolve_url, is_supported_url, detect_source
from ..pipeline.models import SourceCandidate, SourceType, QUALITY_PRESETS
from ..pipeline.matcher import score_candidate, confidence_level
from ..pipeline.normalize import parse_artists
from ..services.organizer import preview_path

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("/search")
async def search(q: str = Query("")):
    if not q:
        return {"results": []}
    results = await search_metadata(q)
    return {"results": results}


@router.post("/resolve")
async def resolve(data: dict):
    url = validate_url(data.get("url", ""))
    if not is_supported_url(url):
        raise HTTPException(400, f"Unsupported URL. Detected source: {detect_source(url)}")
    try:
        info = await resolve_url(url)
        return info
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to resolve URL: {e}")


@router.post("/preview")
async def preview_download(data: dict):
    """Resolve a URL and build a DownloadPlan for user review before download.

    Returns the plan with metadata, quality options, destination path, and
    confidence score so the user can confirm or edit before downloading.
    """
    url = validate_url(data.get("url", ""))
    if not is_supported_url(url):
        raise HTTPException(400, f"Unsupported URL. Detected source: {detect_source(url)}")

    try:
        info = await resolve_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        msg = str(e)
        if "DRM" in msg.upper() or "drm" in msg.lower():
            raise HTTPException(
                422, "This content is DRM-protected and cannot be downloaded. "
                "Try a different source or video."
            )
        raise HTTPException(500, f"Failed to resolve URL: {e}")

    meta = info.get("metadata", info)
    duration = meta.get("duration", 0) or 0
    title = meta.get("title", "")
    artist = meta.get("artist", "")
    parsed = parse_artists(artist)

    quality_name = data.get("quality", data.get("quality_profile", "balanced"))
    if quality_name not in QUALITY_PRESETS:
        raise HTTPException(400, f"Unknown quality profile: {quality_name}")
    output_format = data.get("format", data.get("output_format", "mp3"))
    if output_format not in {"mp3", "m4a", "flac", "ogg", "wav"}:
        raise HTTPException(400, f"Unsupported output format: {output_format}")
    settings = config.get_all()
    folder_template = settings.get("folder_template", "{album_artist}\\{album}\\{track_number} - {title}.{format}")
    validate_folder_template(folder_template)

    source_name = detect_source(url)
    candidate = SourceCandidate(
        url=url,
        source_type=SourceType(source_name) if source_name in {item.value for item in SourceType} else SourceType.UNKNOWN,
        title=title,
        artist=artist,
        duration=duration,
        metadata=meta,
    )

    from backend.pipeline.models import TrackMetadata
    desired = TrackMetadata(
        title=title,
        artist=artist,
        primary_artist=parsed["primary"],
        album_artist=parsed["album_artist"],
        duration=duration,
    )
    match = score_candidate(desired, candidate)

    lib_path = settings.get("library_path", "")
    dest_path = preview_path({
        "title": title,
        "artist": artist,
        "primary_artist": parsed["primary"],
        "album_artist": parsed["album_artist"],
        "album": meta.get("album", ""),
        "track_number": meta.get("track_number", 0),
        "year": meta.get("year", 0),
        "genre": meta.get("genre", ""),
        "format": output_format,
    }, lib_path, folder_template)

    profile = QUALITY_PRESETS[quality_name]

    plan = {
        "url": url,
        "source_type": source_name,
        "title": title,
        "artist": artist,
        "primary_artist": parsed["primary"],
        "featured_artists": parsed["featured"],
        "display_artist": parsed["display"],
        "album": meta.get("album", ""),
        "album_artist": parsed["album_artist"],
        "duration": duration,
        "thumbnail": meta.get("thumbnail", ""),
        "match_confidence": match.confidence,
        "match_level": confidence_level(match.confidence),
        "match_explanation": match.explanation,
        "match_breakdown": match.breakdown,
        "match_warnings": match.warnings,
        "quality_profile": quality_name,
        "output_format": output_format,
        "bitrate": profile.bitrate,
        "destination_path": dest_path,
        "library_path": lib_path,
        "folder_template": folder_template,
    }

    return plan


@router.post("/confirm")
async def confirm_download(data: dict):
    """Create a download job from a previewed plan."""
    url = data.get("url", "").strip()
    if not url:
        raise HTTPException(400, "URL is required")

    output_format = data.get("output_format", "mp3")
    quality = data.get("quality_profile", "balanced")

    from ..database import execute
    cursor = await execute("""
        INSERT INTO jobs (source_url, source_type, query, output_format, quality_profile, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (
        url,
        data.get("source_type", "auto"),
        data.get("title", ""),
        output_format,
        quality,
    ))

    return {"id": cursor.lastrowid, "status": "queued"}
