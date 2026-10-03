"""Metadata matcher with weighted scoring and confidence system.

Score breakdown:
    Title similarity          30%
    Primary artist            25%
    Album                     15%
    Album artist              10%
    Duration                  10%
    Track number               5%
    Release year               5%

Penalties for variant keywords (live, remix, etc.) unless both sides match.
"""
import re
from difflib import SequenceMatcher
from ..pipeline.models import TrackMetadata, SourceCandidate, MatchResult
from ..pipeline.normalize import (
    normalize_title, normalize_artist, normalize_album,
    is_variant_match, parse_artists,
)


WEIGHTS = {
    "title": 0.30,
    "artist": 0.25,
    "album": 0.15,
    "album_artist": 0.10,
    "duration": 0.10,
    "track_number": 0.05,
    "year": 0.05,
}

VARIANT_KEYWORDS = [
    "live", "remix", "acoustic", "radio edit", "extended",
    "instrumental", "karaoke", "cover", "demo", "alternate",
]


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _duration_score(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 0.5
    diff = abs(a - b)
    if diff <= 2:
        return 1.0
    if diff <= 10:
        return 0.8
    if diff <= 30:
        return 0.5
    return 0.2


def _check_variant_penalty(desired_title: str, candidate_title: str) -> list[str]:
    """Check if there's a variant mismatch penalty."""
    warnings = []
    d_lower = desired_title.lower()
    c_lower = candidate_title.lower()
    d_has_variant = any(kw in d_lower for kw in VARIANT_KEYWORDS)
    c_has_variant = any(kw in c_lower for kw in VARIANT_KEYWORDS)
    if d_has_variant != c_has_variant:
        warnings.append("Variant mismatch (live/remix/acoustic)")
    return warnings


def _strip_artist_from_title(title: str, artist: str) -> str:
    """Strip leading 'Artist - ' from title if present."""
    n_title = normalize_title(title)
    n_artist = normalize_artist(parse_artists(artist)["primary"])
    if n_artist and n_title.startswith(n_artist):
        remainder = n_title[len(n_artist):].lstrip(" -")
        if remainder:
            return remainder
    return n_title


def score_candidate(desired: TrackMetadata, candidate: SourceCandidate) -> MatchResult:
    """Score a single candidate against desired metadata."""
    breakdown = {}
    warnings = []

    n_desired_title = normalize_title(desired.title)
    n_cand_title = _strip_artist_from_title(candidate.title, candidate.artist)
    breakdown["title"] = _similarity(n_desired_title, n_cand_title)

    parsed_desired = parse_artists(desired.artist)
    parsed_candidate = parse_artists(candidate.artist)
    n_desired_artist = normalize_artist(parsed_desired["primary"])
    n_cand_artist = normalize_artist(parsed_candidate["primary"])
    breakdown["artist"] = _similarity(n_desired_artist, n_cand_artist)

    n_desired_album = normalize_album(desired.album)
    n_cand_album = normalize_album(candidate.album)
    breakdown["album"] = _similarity(n_desired_album, n_cand_album)

    n_desired_aa = normalize_artist(desired.album_artist or parsed_desired["album_artist"])
    n_cand_aa = normalize_artist(parsed_candidate["album_artist"])
    breakdown["album_artist"] = _similarity(n_desired_aa, n_cand_aa)

    breakdown["duration"] = _duration_score(desired.duration, candidate.duration)

    if desired.track_number > 0 and candidate.metadata.get("track_number", 0) > 0:
        breakdown["track_number"] = 1.0 if desired.track_number == candidate.metadata.get("track_number") else 0.0
    else:
        breakdown["track_number"] = 0.5

    if desired.year > 0 and candidate.metadata.get("year", 0) > 0:
        diff = abs(desired.year - candidate.metadata.get("year", 0))
        breakdown["year"] = 1.0 if diff == 0 else (0.7 if diff <= 2 else 0.3)
    else:
        breakdown["year"] = 0.5

    weighted_sum = sum(breakdown[k] * WEIGHTS[k] for k in WEIGHTS)
    score = round(weighted_sum * 100, 1)

    warnings.extend(_check_variant_penalty(desired.title, candidate.title))

    if parsed_desired["featured"] and parsed_candidate["featured"]:
        desired_feat = set(normalize_artist(a) for a in parsed_desired["featured"])
        candidate_feat = set(normalize_artist(a) for a in parsed_candidate["featured"])
        if desired_feat == candidate_feat:
            score = min(100, score + 5)

    confidence = score
    if warnings:
        confidence = max(0, confidence - 15)

    explanation_parts = []
    if breakdown["title"] >= 0.95:
        explanation_parts.append("Title exact")
    elif breakdown["title"] >= 0.7:
        explanation_parts.append(f"Title similar ({breakdown['title']:.0%})")
    else:
        explanation_parts.append(f"Title differs ({breakdown['title']:.0%})")

    if breakdown["artist"] >= 0.95:
        explanation_parts.append("Artist exact")
    elif breakdown["artist"] >= 0.7:
        explanation_parts.append(f"Artist similar ({breakdown['artist']:.0%})")
    else:
        explanation_parts.append(f"Artist differs ({breakdown['artist']:.0%})")

    if breakdown["album"] >= 0.95:
        explanation_parts.append("Album exact")
    elif breakdown["album"] >= 0.5:
        explanation_parts.append(f"Album similar ({breakdown['album']:.0%})")

    dur_diff = abs(desired.duration - candidate.duration)
    if dur_diff <= 2:
        explanation_parts.append(f"Duration match ({dur_diff:.1f}s diff)")
    elif dur_diff <= 10:
        explanation_parts.append(f"Duration close ({dur_diff:.1f}s diff)")
    else:
        explanation_parts.append(f"Duration differs ({dur_diff:.1f}s diff)")

    if warnings:
        explanation_parts.extend(warnings)

    return MatchResult(
        candidate=candidate,
        score=score,
        confidence=confidence,
        breakdown=breakdown,
        explanation=" | ".join(explanation_parts),
        warnings=warnings,
    )


def rank_candidates(desired: TrackMetadata,
                    candidates: list[SourceCandidate]) -> list[MatchResult]:
    """Score and rank all candidates. Returns sorted by confidence descending."""
    results = [score_candidate(desired, c) for c in candidates]
    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


def confidence_level(confidence: float) -> str:
    """Return human-readable confidence level."""
    if confidence >= 95:
        return "auto_accept"
    elif confidence >= 85:
        return "accept_review"
    elif confidence >= 70:
        return "ask_user"
    else:
        return "reject"
