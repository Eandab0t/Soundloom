from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import os
import re

try:
    from backend.pipeline.cleanup import auto_fix_file, run_cleanup
    CLEANUP_AVAILABLE = True
except ImportError:
    CLEANUP_AVAILABLE = False

from backend import config
from backend.database import fetch_all, fetch_one, execute

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autofix", tags=["autofix"])


class ScanRequest(BaseModel):
    folder: Optional[str] = None


class FixAllRequest(BaseModel):
    folder: Optional[str] = None


class FixFileRequest(BaseModel):
    file_path: str


class FixDuplicatesRequest(BaseModel):
    # (field, current_value) pairs from the preview; empty/absent = fix all
    groups: Optional[List[dict]] = None


# --- duplicate-artist detection and fix -------------------------------------

_RE_COMMA_SEP = re.compile(r"\s*,\s*")


def _dedupe_artist_string(val: str) -> Optional[str]:
    """Deduplicate a comma-separated artist list; None when already clean.

    Mirrors cleanup.clean_artists' dedupe steps exactly: repeated words
    within one part collapse ("Nova Nova" -> "Nova"), then
    duplicate parts collapse case-insensitively ("Alpha, alpha" -> "Alpha").
    Order and first-seen casing are preserved. Only strings containing a
    comma are considered, so single corrupted tokens are left to the
    interactive editor rather than guessed at here.
    """
    if not val or "," not in val:
        return None
    cleaned_parts: List[str] = []
    for part in _RE_COMMA_SEP.split(val.strip()):
        if not part:
            continue
        seen_tokens: dict[str, str] = {}
        unique_tokens: List[str] = []
        for token in part.split():
            lower = token.lower()
            if lower not in seen_tokens:
                seen_tokens[lower] = token
                unique_tokens.append(token)
        cleaned_parts.append(" ".join(unique_tokens))
    seen: dict[str, str] = {}
    final_parts: List[str] = []
    for part in cleaned_parts:
        lower = part.lower()
        if lower not in seen:
            seen[lower] = part
            final_parts.append(part)
    deduped = ", ".join(final_parts)
    return deduped if deduped != val else None


@router.get("/duplicate-artists")
async def scan_duplicate_artists():
    """Preview of duplicate-artist corruption across the library.

    Read-only. One entry per corrupted distinct value, with the proposed fix
    and the affected track ids, worst first — the exact contract the UI's
    preview grid renders before the user consents to a write.
    """
    rows = await fetch_all(
        "SELECT id, artist, album_artist FROM tracks "
        "WHERE file_status != 'missing' AND (artist LIKE '%,%' OR album_artist LIKE '%,%')"
    )
    groups: dict[tuple, dict] = {}
    for row in rows:
        for field in ("artist", "album_artist"):
            val = (row[field] or "").strip()
            if "," not in val:
                continue
            fixed = _dedupe_artist_string(val)
            if not fixed:
                continue
            key = (field, val.casefold())
            group = groups.get(key)
            if not group:
                group = groups[key] = {
                    "field": field,
                    "current_value": val,
                    "fixed_value": fixed,
                    "track_count": 0,
                    "track_ids": [],
                    "example_track_id": row["id"],
                }
            group["track_count"] += 1
            group["track_ids"].append(row["id"])

    out = sorted(groups.values(), key=lambda g: -g["track_count"])
    return {
        "groups": out,
        "tracks_affected": sum(g["track_count"] for g in out),
        "variants": len(out),
    }


@router.post("/fix-duplicate-artists")
async def fix_duplicate_artists(req: FixDuplicatesRequest = None):
    """Fix duplicate-artist corruption in bulk. Always backs up first.

    The file is the source of truth: tags are read from disk, deduped,
    written back, and the DB row is then synced (including primary/display
    artist, which the pipeline derives from artist). Files that are missing
    or unreadable still get their DB row repaired — the DB is what feeds
    matching and the UI.
    """
    wanted = (req.groups if req else None) or []
    wanted_keys = {
        (g.get("field", ""), str(g.get("current_value", "")).casefold())
        for g in wanted
    }

    preview = await scan_duplicate_artists()
    targets = [
        g for g in preview["groups"]
        if not wanted_keys or (g["field"], g["current_value"].casefold()) in wanted_keys
    ]
    if not targets:
        return {"backup_path": "", "tracks_fixed": 0, "tracks_failed": 0,
                "changes": [], "errors": [], "message": "Nothing to fix"}

    track_ids = sorted({tid for g in targets for tid in g["track_ids"]})

    # --- backup BEFORE any write (always, regardless of the toggle) --------
    from backend.services.backup import write_backup
    backup_rows = await fetch_all(
        f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(track_ids))})",
        tuple(track_ids),
    )
    try:
        backup_path = write_backup(backup_rows)
    except OSError as e:
        raise HTTPException(500, f"Backup failed — aborting before any write: {e}")

    from backend.services.tagger import read_tags, write_tags
    from backend.pipeline.normalize import normalize_text

    changes: List[dict] = []
    errors: List[str] = []
    fixed = 0

    for tid in track_ids:
        row = await fetch_one(
            "SELECT id, file_path, artist, album_artist FROM tracks WHERE id=?", (tid,)
        )
        if not row:
            continue
        change = {"track_id": tid, "file": "unreadable",
                  "artist": None, "album_artist": None}

        # 1) the file, when we can reach it
        file_artist = file_aa = None
        fp = row["file_path"]
        if fp and os.path.isfile(fp):
            try:
                tags = await read_tags(fp)
                change["file"] = "clean"
                for field in ("artist", "album_artist"):
                    file_fixed = _dedupe_artist_string(str(tags.get(field) or ""))
                    if file_fixed:
                        change["file"] = "fixed"
                        change[field] = [str(tags.get(field) or ""), file_fixed]
                        tags[field] = file_fixed
                    if field == "artist":
                        file_artist = str(tags.get("artist") or "")
                    else:
                        file_aa = str(tags.get("album_artist") or "")
                if change["file"] == "fixed":
                    try:
                        await write_tags(fp, tags)
                    except Exception:
                        change["file"] = "unreadable"
                        errors.append(f"Track {tid}: tag write failed")
            except Exception as e:
                errors.append(f"Track {tid}: {e}")

        # 2) the DB row — from the file's now-clean values, or deduped DB
        #    values when the file is gone/unreadable
        db_artist = file_artist if file_artist is not None else (row["artist"] or "")
        db_aa = file_aa if file_aa is not None else (row["album_artist"] or "")
        if file_artist is None:
            db_artist = _dedupe_artist_string(db_artist) or db_artist
        if file_aa is None:
            db_aa = _dedupe_artist_string(db_aa) or db_aa

        # record the effective before/after for the result grid
        if change["artist"] is None and db_artist != (row["artist"] or ""):
            change["artist"] = [row["artist"] or "", db_artist]
        if change["album_artist"] is None and db_aa != (row["album_artist"] or ""):
            change["album_artist"] = [row["album_artist"] or "", db_aa]

        primary = db_artist.split(",")[0].strip() if db_artist else ""
        await execute(
            "UPDATE tracks SET artist=?, album_artist=?, primary_artist=?, "
            "display_artist=? WHERE id=?",
            (db_artist, db_aa, primary or row["album_artist"] or "", db_artist, tid),
        )
        changes.append(change)
        fixed += 1

    from backend.events import emit
    await emit("library_change", {})
    logger.info(f"Duplicate-artist fix: {fixed} track(s), backup {backup_path}")
    return {
        "backup_path": backup_path,
        "tracks_fixed": fixed,
        "tracks_failed": len(errors),
        "changes": changes,
        "errors": errors,
    }


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
            "SELECT * FROM tracks WHERE file_status = 'present'"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    # This rewrites tags on every file in the library, so snapshot the
    # current tag state first. Honour the backup_before_write setting.
    from backend.services.backup import backup_tracks
    backup = backup_tracks(rows, config.get_all())
    if config.get("backup_before_write", True) and not backup:
        logger.warning("fix-all: continuing without a backup")

    case_mode = config.get("tag_case_mode", "off")
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
            result = await auto_fix_file(fp, case_mode=case_mode)
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
        "backup": backup,
        "results": results,
    }


@router.post("/fix-file")
async def fix_file(req: FixFileRequest):
    if not CLEANUP_AVAILABLE:
        raise HTTPException(status_code=503, detail="Cleanup module not available")

    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

    try:
        result = await auto_fix_file(
            req.file_path, case_mode=config.get("tag_case_mode", "off")
        )
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
        result = run_cleanup(
            tags,
            folder_path=os.path.dirname(fp),
            case_mode=config.get("tag_case_mode", "off"),
        )
        if hasattr(result, "model_dump"):
            return result.model_dump()
        elif hasattr(result, "__dict__"):
            return vars(result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview failed: {e}")
