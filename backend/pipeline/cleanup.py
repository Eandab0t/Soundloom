"""Music tag cleanup pipeline.

Normalizes, deduplicates, and corrects common tag issues in audio files.
Adapted from E-Tuner cleanup rules for VividlyMusicaly's pipeline.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.services.tagger import read_tags, write_tags

logger = logging.getLogger(__name__)

# ── Regex patterns ──────────────────────────────────────────────────────────

_RE_MULTI_SPACE = re.compile(r"[ \t]+")
_RE_TRAIL_JUNK = re.compile(r"[.!?\-–—\s]+$")
_RE_LEAD_JUNK = re.compile(r"^[\-–—\s/|]+")
_RE_FEAT_VARIANTS = re.compile(
    r"\s*[\(\[]?\s*(?:ft\.?|featuring|feat\.?)\s*",
    re.IGNORECASE,
)
_RE_COMMA_SEP = re.compile(r"\s*,\s*")
_RE_ECHO_WORD = re.compile(
    r"\b(\w{3,})\s+\1\b",
    re.IGNORECASE,
)
_RE_TRACK_NUM = re.compile(r"^(\d{1,3})\s*/\s*\d+$")
_RE_TRACK_INT = re.compile(r"^(\d{1,3})$")
_RE_FOLDER_ARTIST_ALBUM = re.compile(
    r"^(?P<artist>.+?)\s*[-–—]\s*(?P<album>.+?)$",
)
_RE_FOLDER_YEAR_SUFFIX = re.compile(
    r"^(?P<album>.+?)\s*[\(\[](?P<year>\d{4})[\)\]]$",
)


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class CleanupChange:
    """A single tag mutation made during cleanup."""

    field: str
    before: str
    after: str
    rule: str


@dataclass
class CleanupResult:
    """Outcome of a cleanup pass on one file."""

    path: str = ""
    changes: list[CleanupChange] = field(default_factory=list)
    success: bool = True


# ── Helpers ─────────────────────────────────────────────────────────────────

def _s(value: object) -> str:
    """Coerce a tag value to a clean string (handles mutagen int values)."""
    if isinstance(value, int):
        return str(value)
    if value is None:
        return ""
    return str(value).strip()


def _nfc(text: str) -> str:
    """NFC-normalise unicode."""
    return unicodedata.normalize("NFC", text)


def _record(
    changes: list[CleanupChange],
    field: str,
    before: str,
    after: str,
    rule: str,
) -> None:
    """Append a change only if the value actually changed."""
    if before != after:
        changes.append(CleanupChange(field=field, before=before, after=after, rule=rule))


# ── Individual cleanup rules ───────────────────────────────────────────────

def clean_whitespace(tags: dict) -> list[CleanupChange]:
    """Strip whitespace, collapse runs, normalise dashes/unicode, unify feat."""
    changes: list[CleanupChange] = []
    text_keys = ("title", "artist", "album_artist", "album", "genre")

    for key in text_keys:
        original = _s(tags.get(key, ""))
        value = _nfc(original)

        # Translate unicode dashes to ASCII hyphen
        value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")

        # Collapse whitespace
        value = _RE_MULTI_SPACE.sub(" ", value)

        # Unify feat. spellings (ft./featuring/feat. → feat.)
        value = _RE_FEAT_VARIANTS.sub(" feat. ", value)

        # Strip trailing junk punctuation and whitespace
        value = _RE_TRAIL_JUNK.sub("", value)

        # Strip leading junk
        value = _RE_LEAD_JUNK.sub("", value)

        # Final trim
        value = value.strip()
        tags[key] = value
        _record(changes, key, original, value, "whitespace")

    return changes


def clean_duplicates(tags: dict) -> list[CleanupChange]:
    """Remove echo patterns: 'Song Song', 'Artist - Artist - Track'."""
    changes: list[CleanupChange] = []

    # ── Title duplicates ────────────────────────────────────────────────
    title = _s(tags.get("title", ""))
    if title:
        # "Foo Bar - Foo Bar" → "Foo Bar" (split on dash, check if both sides match)
        parts = _RE_MULTI_SPACE.split(title.replace(" - ", "-").replace(" – ", "-").replace(" — ", "-").strip("-"))
        if len(parts) == 2 and parts[0].strip().lower() == parts[1].strip().lower():
            short = parts[0].strip()
            if len(short) >= 3:
                tags["title"] = short
                _record(changes, "title", title, short, "duplicate_pair")
                title = short

        # "Word Word" (each word ≥3 chars) → "Word"
        m2 = _RE_ECHO_WORD.match(title)
        if m2:
            short = m2.group(1)
            tags["title"] = short
            _record(changes, "title", title, short, "echo_word")
            title = short

    # ── Artist duplicates ───────────────────────────────────────────────
    artist = _s(tags.get("artist", ""))
    if artist:
        # "Artist - Artist" → "Artist"
        parts = _RE_MULTI_SPACE.split(artist.replace(" - ", "-").replace(" – ", "-").replace(" — ", "-").strip("-"))
        if len(parts) == 2 and parts[0].strip().lower() == parts[1].strip().lower():
            short = parts[0].strip()
            if len(short) >= 3:
                tags["artist"] = short
                _record(changes, "artist", artist, short, "duplicate_pair")
                artist = short

        # "Artist - Artist - Track" on artist field (malformed split)
        raw = artist.replace(" - ", "-").replace(" – ", "-").replace(" — ", "-")
        dash_parts = [p.strip() for p in raw.split("-") if p.strip()]
        if len(dash_parts) >= 3 and dash_parts[0].lower() == dash_parts[1].lower():
            clean = dash_parts[0]
            tags["artist"] = clean
            _record(changes, "artist", artist, clean, "artist_dash_duplicate")
            artist = clean

    return changes


def clean_artists(tags: dict) -> list[CleanupChange]:
    """Extract featured artists from artist into title, set album_artist, dedupe."""
    changes: list[CleanupChange] = []

    artist = _s(tags.get("artist", ""))
    title = _s(tags.get("title", ""))

    if not artist:
        return changes

    # ── Split featured artists out of artist field ──────────────────────
    feat_match = _RE_FEAT_VARIANTS.search(artist)
    if feat_match:
        # Re-split cleanly: the part before feat. is primary
        parts = _RE_FEAT_VARIANTS.split(artist, maxsplit=1)
        primary = parts[0].strip().strip(",&")
        feat_list = parts[1].strip().strip(", ") if len(parts) > 1 else ""

        if primary:
            old_artist = artist
            tags["artist"] = primary
            _record(changes, "artist", old_artist, primary, "extract_feat_from_artist")

        # Append feat. credit to title if not already present
        if feat_list and feat_list.lower() not in title.lower():
            feat_tag = f"({feat_list})"
            new_title = f"{title} {feat_tag}".strip()
            old_title = title
            tags["title"] = new_title
            _record(changes, "title", old_title, new_title, "append_feat_to_title")

    # ── Album artist = primary artist (first in comma list) ─────────────
    current_aa = _s(tags.get("album_artist", ""))
    artist_val = _s(tags.get("artist", ""))
    if artist_val:
        primary = _RE_COMMA_SEP.split(artist_val)[0].strip()
        if not current_aa or current_aa != primary:
            old_aa = current_aa
            tags["album_artist"] = primary
            _record(changes, "album_artist", old_aa, primary, "set_album_artist")

    # ── Deduplicate comma-separated artist lists ────────────────────────
    for key in ("artist", "album_artist"):
        val = _s(tags.get(key, ""))
        if "," in val:
            parts = _RE_COMMA_SEP.split(val)
            seen: list[str] = []
            for part in parts:
                norm = part.strip()
                if norm and norm.lower() not in {s.lower() for s in seen}:
                    seen.append(norm)
            deduped = ", ".join(seen)
            if deduped != val:
                old = val
                tags[key] = deduped
                _record(changes, key, old, deduped, "dedupe_artists")

    return changes


def clean_track_numbers(tags: dict) -> list[CleanupChange]:
    """Normalise track_number and disc_number to plain integers."""
    changes: list[CleanupChange] = []

    for key in ("track_number", "disc_number"):
        raw = tags.get(key)
        original_str = _s(raw)

        if isinstance(raw, int):
            if raw < 0:
                tags[key] = 0
                _record(changes, key, original_str, "0", "invalid_track_number")
            continue

        if not original_str:
            continue

        m = _RE_TRACK_NUM.match(original_str)
        if m:
            num = int(m.group(1))
            tags[key] = num
            _record(changes, key, original_str, str(num), "track_number_split")
            continue

        m2 = _RE_TRACK_INT.match(original_str)
        if m2:
            num = int(m2.group(1))
            tags[key] = num
            _record(changes, key, original_str, str(num), "track_number_clean")
            continue

        # Last resort: extract any leading digits
        digits = re.match(r"^(\d+)", original_str)
        if digits:
            num = int(digits.group(1))
            tags[key] = num
            _record(changes, key, original_str, str(num), "track_number_extract")
        else:
            tags[key] = 0
            _record(changes, key, original_str, "0", "track_number_zeroed")

    return changes


def clean_album_from_folder(tags: dict, folder_path: str) -> list[CleanupChange]:
    """Infer album (and possibly artist / year) from parent folder name."""
    changes: list[CleanupChange] = []
    current_album = _s(tags.get("album", ""))

    if current_album:
        return changes

    folder_name = Path(folder_path).name.strip()
    if not folder_name:
        return changes

    # "Artist - Album (Year)" → extract all three
    m = _RE_FOLDER_ARTIST_ALBUM.match(folder_name)
    if m:
        folder_artist = m.group("artist").strip()
        remainder = m.group("album").strip()

        # Check for trailing "(Year)"
        ym = _RE_FOLDER_YEAR_SUFFIX.match(remainder)
        if ym:
            folder_album = ym.group("album").strip()
            folder_year = int(ym.group("year"))
        else:
            folder_album = remainder
            folder_year = 0

        if not current_album and folder_album:
            tags["album"] = folder_album
            _record(changes, "album", "", folder_album, "album_from_folder")

        if folder_artist and not _s(tags.get("artist", "")):
            tags["artist"] = folder_artist
            _record(changes, "artist", "", folder_artist, "artist_from_folder")

        if folder_year and not tags.get("year"):
            tags["year"] = folder_year
            _record(changes, "year", "0", str(folder_year), "year_from_folder")
    else:
        # Just a plain folder name → use as album
        tags["album"] = folder_name
        _record(changes, "album", "", folder_name, "album_from_folder")

    return changes


# ── Orchestrator ────────────────────────────────────────────────────────────

def run_cleanup(
    tags: dict,
    folder_path: Optional[str] = None,
) -> CleanupResult:
    """Run all cleanup rules in order on a tags dict (mutates in place).

    Returns a :class:`CleanupResult` summarising every change made.

    Rules are applied in dependency order:
        1. whitespace – normalise text before pattern matching
        2. duplicates – remove echo / copy patterns
        3. artists – extract feat., set album_artist, dedupe
        4. track_numbers – normalise to int
        5. album_from_folder – fill missing album from folder name
    """
    result = CleanupResult(path=tags.get("file_path", ""), success=True)

    try:
        result.changes.extend(clean_whitespace(tags))
        result.changes.extend(clean_duplicates(tags))
        result.changes.extend(clean_artists(tags))
        result.changes.extend(clean_track_numbers(tags))
        if folder_path:
            result.changes.extend(clean_album_from_folder(tags, folder_path))
    except Exception as exc:
        logger.exception("Cleanup failed for %s", tags.get("file_path", "<unknown>"))
        result.success = False
        result.changes.append(
            CleanupChange(field="_error", before="", after=str(exc), rule="exception")
        )

    return result


async def auto_fix_file(file_path: str) -> CleanupResult:
    """Read tags from *file_path*, run cleanup, write back, and return the result.

    This is the main entry point for single-file cleanup.  It uses
    :func:`backend.services.tagger.read_tags` and
    :func:`backend.services.tagger.write_tags` for I/O.
    """
    p = Path(file_path)
    folder = str(p.parent)
    result = CleanupResult(path=file_path, success=False)

    try:
        tags = await read_tags(file_path)
    except Exception as exc:
        logger.exception("Failed to read tags from %s", file_path)
        result.changes.append(
            CleanupChange(field="_error", before="", after=str(exc), rule="read_tags")
        )
        return result

    cleanup = run_cleanup(tags, folder_path=folder)
    result.changes = cleanup.changes

    if not cleanup.success:
        result.success = False
        return result

    try:
        ok = await write_tags(file_path, tags)
        result.success = ok
    except Exception as exc:
        logger.exception("Failed to write tags to %s", file_path)
        result.success = False
        result.changes.append(
            CleanupChange(field="_error", before="", after=str(exc), rule="write_tags")
        )

    return result
