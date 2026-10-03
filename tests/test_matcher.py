"""Tests for pipeline.matcher — weighted scoring, confidence levels."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.pipeline.models import TrackMetadata, SourceCandidate, SourceType
from backend.pipeline.matcher import (
    score_candidate, rank_candidates, confidence_level,
    _strip_artist_from_title,
)


class TestConfidenceLevel:
    def test_auto_accept(self):
        assert confidence_level(95) == "auto_accept"
        assert confidence_level(100) == "auto_accept"

    def test_accept_review(self):
        assert confidence_level(85) == "accept_review"
        assert confidence_level(94) == "accept_review"

    def test_ask_user(self):
        assert confidence_level(70) == "ask_user"
        assert confidence_level(84) == "ask_user"

    def test_reject(self):
        assert confidence_level(69) == "reject"
        assert confidence_level(0) == "reject"


class TestScoreCandidate:
    def test_perfect_match(self):
        desired = TrackMetadata(title="Song", artist="Artist", album="Album", duration=200)
        cand = SourceCandidate(url="yt", title="Song", artist="Artist", album="Album", duration=200)
        result = score_candidate(desired, cand)
        assert result.confidence >= 95

    def test_artist_stripped_from_title(self):
        desired = TrackMetadata(title="Song", artist="Artist", duration=200)
        cand = SourceCandidate(url="yt", title="Artist - Song (Official Video)", artist="Artist", duration=200)
        result = score_candidate(desired, cand)
        assert result.confidence >= 80

    def test_duration_mismatch_penalizes(self):
        desired = TrackMetadata(title="Song", artist="Artist", duration=200)
        cand = SourceCandidate(url="yt", title="Song", artist="Artist", duration=300)
        result = score_candidate(desired, cand)
        assert result.breakdown["duration"] < 1.0

    def test_wrong_artist_penalizes(self):
        desired = TrackMetadata(title="Song", artist="Artist A")
        cand = SourceCandidate(url="yt", title="Song", artist="Artist B")
        result = score_candidate(desired, cand)
        assert result.breakdown["artist"] < 0.95

    def test_variant_penalty(self):
        desired = TrackMetadata(title="Song", artist="Artist", duration=200)
        cand = SourceCandidate(url="yt", title="Song (Live)", artist="Artist", duration=200)
        result = score_candidate(desired, cand)
        assert any("variant" in w.lower() for w in result.warnings)

    def test_featured_artist_bonus(self):
        desired = TrackMetadata(title="Song", artist="A", featured_artists=["B"])
        cand = SourceCandidate(url="yt", title="Song", artist="A feat. B", duration=200)
        result = score_candidate(desired, cand)
        assert result.breakdown["artist"] >= 0.9

    def test_empty_candidate(self):
        desired = TrackMetadata(title="Song", artist="Artist")
        cand = SourceCandidate(url="")
        result = score_candidate(desired, cand)
        assert result.confidence < 50

    def test_explanation_strings(self):
        desired = TrackMetadata(title="Song", artist="A")
        cand = SourceCandidate(url="yt", title="Song", artist="A", duration=200)
        result = score_candidate(desired, cand)
        assert isinstance(result.explanation, str)
        assert len(result.explanation) > 0


class TestStripArtistFromTitle:
    def test_strips_prefix(self):
        r = _strip_artist_from_title("Artist - Song", "Artist")
        assert r == "song"

    def test_no_strip_needed(self):
        r = _strip_artist_from_title("Song", "Artist")
        assert "song" in r

    def test_case_insensitive(self):
        r = _strip_artist_from_title("ARTIST - Song", "artist")
        assert r == "song"


class TestRankCandidates:
    def test_orders_by_score(self):
        desired = TrackMetadata(title="Song", artist="A")
        c1 = SourceCandidate(url="1", title="Song", artist="A")
        c2 = SourceCandidate(url="2", title="Wrong", artist="B")
        ranked = rank_candidates(desired, [c2, c1])
        assert ranked[0].confidence >= ranked[1].confidence

    def test_empty(self):
        desired = TrackMetadata(title="Song")
        assert rank_candidates(desired, []) == []
