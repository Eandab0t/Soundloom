"""Input validation for paths, templates, and user data.

Prevents:
- Path traversal attacks (../)
- Unsafe filenames
- Template injection via arbitrary variables
"""
import os
import re
from pathlib import Path

from .errors import ValidationError, PathTraversalError

ALLOWED_TEMPLATE_VARS = {
    "title", "artist", "primary_artist", "display_artist", "album_artist",
    "album", "track_number", "disc_number", "year", "genre", "format",
}

TEMPLATE_VAR_PATTERN = re.compile(r"\{(\w+)\}")


def validate_path(path: str, base_dir: str | Path) -> Path:
    """Validate that a path is within the allowed base directory.

    Raises PathTraversalError if the path escapes the base directory.
    Returns the resolved Path.
    """
    base = Path(base_dir).resolve()
    target = (base / path).resolve()

    if not str(target).startswith(str(base)):
        raise PathTraversalError(f"Path escapes base directory: {path}")

    return target


def validate_folder_template(template: str) -> str:
    """Validate a folder template string.

    Only allows whitelisted template variables.
    Raises ValidationError for unknown variables.
    """
    variables = TEMPLATE_VAR_PATTERN.findall(template)
    unknown = set(variables) - ALLOWED_TEMPLATE_VARS
    if unknown:
        raise ValidationError(
            f"Unknown template variables: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ALLOWED_TEMPLATE_VARS))}"
        )

    if ".." in template:
        raise ValidationError("Template must not contain ..")

    return template


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Sanitize a string for use as a filename component.

    Removes path separators and other dangerous characters.
    """
    if not name:
        return ""

    invalid = '<>:"/\\|?*\x00'
    for ch in invalid:
        name = name.replace(ch, "")

    name = name.strip(". ")

    name = re.sub(r"\s+", " ", name)

    return name[:max_length] if name else ""


def validate_url(url: str) -> str:
    """Validate and normalize a URL input.

    Raises ValidationError for empty or obviously invalid URLs.
    """
    url = url.strip()
    if not url:
        raise ValidationError("URL cannot be empty")

    if len(url) > 2048:
        raise ValidationError("URL is too long (max 2048 characters)")

    if any(ch in url for ch in ["\x00", "\n", "\r"]):
        raise ValidationError("URL contains invalid characters")

    return url
