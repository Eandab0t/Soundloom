from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import os

try:
    from backend.pipeline.cleanup import auto_fix_file, run_cleanup
    CLEANUP_AVAILABLE = True
except ImportError:
    CLEANUP_AVAILABLE = False

from backend import config
from backend.database import fetch_all, fetch_one

router = APIRouter(prefix="/api/autofix", tags=["autofix"])


class ScanRequest(BaseModel):
    folder: Optional[str] = None


class FixAllRequest(BaseModel):
    folder: Optional[str] = None


class FixFileRequest(BaseModel):
    file_path: str


def _check_tags_for_issues(tags: dict) -> List[dict]:
    issues = []
    title = tags.get("title", "")
    artist = tags.get("artist", "")
    album = tags.get("album", "")
    genre = tags.get("genre", "")

    if not title or not title.strip():
        issues.append({"field": "title", "type": "missing", "value": str(title)})
    elif title != title.strip():
        issues.append({"field": "title", "type": "whitespace", "value": str(title), "fixed": title.strip()})

    if not artist or not artist.strip():
        issues.append({"field": "artist", "type": "missing", "value": str(artist)})
    elif artist != artist.strip():
        issues.append({"field": "artist", "type": "whitespace", "value": str(artist), "fixed": artist.strip()})

    if not album or not album.strip():
        issues.append({"field": "album", "type": "missing", "value": str(album)})

    if not genre or not genre.strip():
        issues.append({"field": "genre", "type": "missing", "value": str(genre)})

    if title and "feat." in title.lower() or "ft." in title.lower():
        issues.append({"field": "title", "type": "feat_in_wrong_field", "value": str(title)})

    for field in ["artist", "album", "genre"]:
        val = str(tags.get(field, ""))
        if val and val != val.strip():
            issues.append({"field": field, "type": "whitespace", "value": val, "fixed": val.strip()})

    return issues


@router.post("/scan")
async def scan_library(req: ScanRequest = None):
    if not CLEANUP_AVAILABLE:
        raise HTTPException(status_code=503, detail="Cleanup module not available")

    folder = (req.folder if req else None) or config.get("library_path")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=400, detail=f"Invalid folder: {folder}")

    try:
        rows = await fetch_all(
            "SELECT id, file_path FROM tracks WHERE file_status = 'present'"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    issues_list = []
    total_checked = 0

    for row in rows:
        fp = row["file_path"]
        if not os.path.isfile(fp):
            continue
        total_checked += 1

        try:
            from backend.services.tagger import read_tags
            tags = await read_tags(fp)
        except Exception:
            issues_list.append({"file_path": fp, "track_id": row["id"], "issues": [{"type": "read_error"}]})
            continue

        file_issues = _check_tags_for_issues(tags)
        if file_issues:
            issues_list.append({"file_path": fp, "track_id": row["id"], "issues": file_issues})

    return {
        "issues": issues_list,
        "total_checked": total_checked,
        "total_with_issues": len(issues_list),
    }


@router.post("/fix-all")
async def fix_all(req: FixAllRequest = None):
    if not CLEANUP_AVAILABLE:
        raise HTTPException(status_code=503, detail="Cleanup module not available")

    folder = (req.folder if req else None) or config.get("library_path")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=400, detail=f"Invalid folder: {folder}")

    try:
        rows = await fetch_all(
            "SELECT id, file_path FROM tracks WHERE file_status = 'present'"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    files_checked = 0
    files_fixed = 0
    total_changes = 0
    results = []

    for row in rows:
        fp = row["file_path"]
        if not os.path.isfile(fp):
            continue
        files_checked += 1

        try:
            result = await auto_fix_file(fp)
            changes_count = len(result.changes) if hasattr(result, "changes") else 0
            total_changes += changes_count
            if changes_count > 0:
                files_fixed += 1
            results.append({
                "file_path": fp,
                "track_id": row["id"],
                "changes_count": changes_count,
                "success": True,
            })
        except Exception as e:
            results.append({
                "file_path": fp,
                "track_id": row["id"],
                "changes_count": 0,
                "success": False,
                "error": str(e),
            })

    return {
        "files_checked": files_checked,
        "files_fixed": files_fixed,
        "total_changes": total_changes,
        "results": results,
    }


@router.post("/fix-file")
async def fix_file(req: FixFileRequest):
    if not CLEANUP_AVAILABLE:
        raise HTTPException(status_code=503, detail="Cleanup module not available")

    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

    try:
        result = await auto_fix_file(req.file_path)
        if hasattr(result, "model_dump"):
            return result.model_dump()
        elif hasattr(result, "__dict__"):
            return vars(result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fix failed: {e}")


@router.get("/preview/{track_id}")
async def preview_fix(track_id: int):
    if not CLEANUP_AVAILABLE:
        raise HTTPException(status_code=503, detail="Cleanup module not available")

    try:
        row = await fetch_one("SELECT id, file_path FROM tracks WHERE id = ?", (track_id,))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Track {track_id} not found")

    fp = row["file_path"]
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail=f"File not found: {fp}")

    try:
        from backend.services.tagger import read_tags
        tags = await read_tags(fp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read tags: {e}")

    try:
        result = run_cleanup(tags, folder_path=os.path.dirname(fp))
        if hasattr(result, "model_dump"):
            return result.model_dump()
        elif hasattr(result, "__dict__"):
            return vars(result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview failed: {e}")
