"""Metadata matcher with weighted scoring and confidence system.

The first argument is what the **user asked for** and the second is what a
**source actually is**. Scoring one against the other is the entire job: pass
the source in as both and every field self-matches, which returns a constant
score no matter what was downloaded and leaves `match_threshold` unable to
reject anything.

Score breakdown:
    Title similarity          30%
    Primary artist            25%
    Album                     15%
    Album artist              10%
    Duration                  10%
    Track number               5%
    Release year               5%

Two rules keep the number honest:

- **Missing evidence scores neutral, not zero.** A source with no album is
  unknown, not wrong. Duration, year and track number already worked this
  way; the text fields now do too, so a correct match is not punished for
  metadata the request never mentioned.
- **A candidate with no identity is not a match.** Nothing said, nothing to
  compare, so it scores zero rather than riding the neutral floor.

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


def _comparable(a: str, b: str) -> float:
    """Similarity where an unstated field is neutral rather than a mismatch.

    `_similarity` returns 0.0 when either side is blank, which cannot tell
    "this source has no album" apart from "this source has the wrong album".
    Once a match is scored against a real request, that difference decides
    whether a correct track gets downloaded or rejected, so blank scores 0.5 -
    the same neutral floor `_duration_score` and the year/track-number
    branches already use.
    """
    if not a or not b:
        return 0.5
    return SequenceMatcher(None, a, b).ratio()


def has_comparable_intent(desired: TrackMetadata) -> bool:
    """True when the caller actually stated what it wanted.

    Scoring against a blank request always lands on the neutral floor, which
    says nothing at all about the candidate. Callers must check this and skip
    the gate out loud instead of reporting a meaningless number as a match.
    """
    return any(
        (value or "").strip()
        for value in (desired.title, desired.artist, desired.album)
    )


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
    """Strip leading 'Artist - ' from title if present.

    `normalize_artist` drops a leading article ("The Cardigans" ->
    "cardigans") but `normalize_title` does not, so a plain startswith test
    never fires for "The Cardigans - Lovefool". That went unnoticed while the
    score was a constant 80%; with a real match in play it would score the
    correct track as a poor title match and reject it, so the leading article
    is dropped from the title before the prefix test as well.
    """
    n_title = normalize_title(title)
    n_artist = normalize_artist(parse_artists(artist)["primary"])
    if not n_artist:
        return n_title
    for candidate in (n_title, re.sub(r"^the\s+", "", n_title)):
        if candidate.startswith(n_artist):
            remainder = candidate[len(n_artist):].lstrip(" -")
            if remainder:
                return remainder
    return n_title


def _text_verdict(label: str, score: float, a: str, b: str) -> str:
    """One clause about a text field, honest about what was not stated.

    A neutral 0.5 means one side was blank, so calling it "similar" would tell
    the user their album matched when nobody ever named one.
    """
    if not a or not b:
        return f"{label} not stated by one side"
    if score >= 0.95:
        return f"{label} exact"
    if score >= 0.7:
        return f"{label} similar ({score:.0%})"
    return f"{label} differs ({score:.0%})"


def score_candidate(desired: TrackMetadata, candidate: SourceCandidate) -> MatchResult:
    """Score one source against what was asked for.

    `desired` is the user's request, `candidate` is what a source resolved to.
    """
    # A source that never said what it is cannot be matched against anything.
    # This has to stay a hard zero rather than the neutral floor, which would
    # report an identity-less candidate as a coin flip.
    if not (candidate.title or "").strip() and not (candidate.artist or "").strip():
        return MatchResult(
            candidate=candidate,
            score=0.0,
            confidence=0.0,
            breakdown={},
            explanation="Candidate has no title or artist to compare",
            warnings=["Candidate has no title or artist"],
        )

    breakdown = {}
    warnings = []

    n_desired_title = normalize_title(desired.title)
    n_cand_title = _strip_artist_from_title(candidate.title, candidate.artist)
    breakdown["title"] = _comparable(n_desired_title, n_cand_title)

    parsed_desired = parse_artists(desired.artist)
    parsed_candidate = parse_artists(candidate.artist)
    n_desired_artist = normalize_artist(parsed_desired["primary"])
    n_cand_artist = normalize_artist(parsed_candidate["primary"])
    breakdown["artist"] = _comparable(n_desired_artist, n_cand_artist)

    n_desired_album = normalize_album(desired.album)
    n_cand_album = normalize_album(candidate.album)
    breakdown["album"] = _comparable(n_desired_album, n_cand_album)

    n_desired_aa = normalize_artist(desired.album_artist or parsed_desired["album_artist"])
    # A source's real album artist is better evidence than re-parsing its
    # artist string, when the source bothered to state one.
    n_cand_aa = normalize_artist(
        candidate.metadata.get("album_artist") or parsed_candidate["album_artist"])
    breakdown["album_artist"] = _comparable(n_desired_aa, n_cand_aa)

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

    # This text is the user's only account of *why* a download was rejected,
    # so a field nobody stated has to say "not stated" rather than dress its
    # neutral score up as agreement.
    explanation_parts = [
        _text_verdict("Title", breakdown["title"], n_desired_title, n_cand_title),
        _text_verdict("Artist", breakdown["artist"], n_desired_artist, n_cand_artist),
        _text_verdict("Album", breakdown["album"], n_desired_album, n_cand_album),
    ]
    if desired.duration <= 0:
        explanation_parts.append("Duration not stated")
    elif candidate.duration <= 0:
        explanation_parts.append("Duration unknown")
    else:
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
