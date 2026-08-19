"""Source search and URL resolve endpoints."""
from fastapi import APIRouter, HTTPException, Query
from .. import config
from ..validation import validate_url, validate_folder_template
from ..services.matcher import search_metadata
from ..sources.ytdlp_source import resolve_url, is_supported_url, detect_source
from ..pipeline.models import DownloadPlan, SourceCandidate, SourceType, QUALITY_PRESETS
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


@router.post("/search-web")
async def search_web(data: dict):
    """Search across YouTube, Spotify, MusicBrainz, and SoundCloud for metadata.

    Accepts either structured fields (title/artist/album) or a free-text query string.
    """
    title = data.get("title", "")
    artist = data.get("artist", "")
    album = data.get("album", "")
    query = data.get("query", "")

    if not title and not artist and not query:
        raise HTTPException(400, "Need at least title, artist, or query to search")

    try:
        from ..services.metadata_search import search_for_track, search_all

        kwargs = {}
        client_id = config.get("spotify_client_id", "")
        client_secret = config.get("spotify_client_secret", "")
        if client_id and client_secret:
            kwargs["client_id"] = client_id
            kwargs["client_secret"] = client_secret

        if query and not title and not artist:
            results = await search_all(query, **kwargs)
        else:
            results = await search_for_track(title, artist, album, **kwargs)
        return {"results": [vars(r) for r in results]}
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


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
    output_format = data.get("format", data.get("output_format", "mp3"))
    settings = config.get_all()
    folder_template = settings.get("folder_template", "{album_artist}\\{album}\\{track_number} - {title}.{format}")
    validate_folder_template(folder_template)

    candidate = SourceCandidate(
        url=url,
        source_type=SourceType(detect_source(url)),
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

    profile = QUALITY_PRESETS.get(quality_name, QUALITY_PRESETS["balanced"])

    plan = {
        "url": url,
        "source_type": detect_source(url),
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
        "bitrate": profile.bitrate if hasattr(profile, 'bitrate') else "",
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
