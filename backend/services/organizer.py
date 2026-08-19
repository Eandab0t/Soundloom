"""Folder organizer - renders templates, handles duplicates, moves files.

File flow:
    Download staging (temp) → Processing (temp) → Library (final)

download_path = staging area for new downloads before processing
library_path  = final organized library root
"""
import logging
import re
import shutil
from pathlib import Path

from ..validation import validate_folder_template, sanitize_filename

logger = logging.getLogger(__name__)


def render_template(template: str, metadata: dict) -> str:
    """Render a folder template string with metadata variables."""
    safe = {k: _sanitize(v) for k, v in metadata.items()}
    result = template
    for key, val in safe.items():
        result = result.replace(f"{{{key}}}", val or "Unknown")
    result = re.sub(r"\{[^}]+\}", "Unknown", result)
    return result


def _sanitize(name: str) -> str:
    """Remove or replace characters that are invalid in file paths."""
    if not name:
        return ""
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "")
    name = name.strip(". ")
    return name[:200] if name else ""


async def organize_file(source_path: str, metadata: dict, library_path: str,
                        folder_template: str,
                        duplicate_policy: str = "keep_separate",
                        download_path: str = "") -> str:
    """Move processed file to its organized location in the library.

    Args:
        source_path: Path to the processed file (tagged, converted).
        metadata: Track metadata dict for template rendering.
        library_path: Library root (final destination).
        folder_template: Template like '{album_artist}/{album}/{title}.{format}'.
        duplicate_policy: skip / overwrite / keep_separate.
        download_path: Staging area (unused for final placement, kept for API compat).

    Returns:
        Final path in the library.
    """
    if not source_path or not Path(source_path).exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    ext = Path(source_path).suffix
    rel_path = render_template(folder_template, metadata)
    final_dir = Path(library_path) / Path(rel_path).parent
    final_dir.mkdir(parents=True, exist_ok=True)

    final_name = Path(rel_path).stem + ext
    final_path = final_dir / final_name

    if final_path.exists():
        if duplicate_policy == "skip":
            logger.info(f"Skipping duplicate: {final_path}")
            return str(final_path)
        elif duplicate_policy == "overwrite":
            final_path.unlink()
        else:
            counter = 1
            while final_path.exists():
                stem = Path(rel_path).stem
                final_path = final_dir / f"{stem} ({counter}){ext}"
                counter += 1

    shutil.move(str(source_path), str(final_path))
    logger.info(f"Organized: {final_path}")
    return str(final_path)


def preview_path(metadata: dict, library_path: str, folder_template: str) -> str:
    """Preview where a file would be organized without moving anything."""
    rel_path = render_template(folder_template, metadata)
    return str(Path(library_path) / rel_path)
