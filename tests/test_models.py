"""Tests for pipeline.models — data classes, quality presets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.pipeline.models import (
    JobState, TrackMetadata, SourceCandidate, MatchResult,
    DownloadPlan, QualityProfile, QUALITY_PRESETS, SourceType,
)


class TestJobState:
    def test_all_states_exist(self):
        expected = [
            "pending", "resolving", "matching", "downloading",
            "converting", "tagging", "organizing", "indexing",
            "complete", "failed", "cancelled", "paused", "retrying",
        ]
        actual = [s.value for s in JobState]
        for e in expected:
            assert e in actual, f"Missing state: {e}"


class TestTrackMetadata:
    def test_defaults(self):
        m = TrackMetadata()
        assert m.title == ""
        assert m.artist == ""
        assert m.album == ""
        assert m.duration == 0.0
        assert m.featured_artists == []

    def test_fields(self):
        m = TrackMetadata(
            title="Song", artist="Artist", album="Album",
            primary_artist="Artist", featured_artists=["B"],
            display_artist="Artist feat. B", album_artist="Artist",
        )
        assert m.display_artist == "Artist feat. B"
        assert m.featured_artists == ["B"]

    def test_display_name_with_artist(self):
        m = TrackMetadata(title="T", display_artist="A", album="Al")
        assert m.display_name == "A - Al"

    def test_display_name_title_only(self):
        m = TrackMetadata(title="T")
        assert m.display_name == "T"

    def test_to_dict(self):
        m = TrackMetadata(title="T", artist="A")
        d = m.to_dict()
        assert d["title"] == "T"
        assert d["artist"] == "A"


class TestSourceCandidate:
    def test_defaults(self):
        c = SourceCandidate()
        assert c.url == ""
        assert c.source_type == SourceType.UNKNOWN

    def test_from_youtube(self):
        c = SourceCandidate(
            url="https://youtube.com/watch?v=abc",
            source_type=SourceType.YOUTUBE,
            title="Song", artist="Artist", duration=200,
        )
        assert c.source_type == SourceType.YOUTUBE


class TestMatchResult:
    def test_fields(self):
        r = MatchResult(score=90, confidence=90, breakdown={"title": 1.0})
        assert r.score == 90
        assert r.breakdown == {"title": 1.0}
        assert r.warnings == []


class TestQualityProfile:
    def test_best_preset(self):
        p = QUALITY_PRESETS["best"]
        assert p.output_format == "flac"
        assert p.lossless is True

    def test_balanced_preset(self):
        p = QUALITY_PRESETS["balanced"]
        assert p.output_format == "mp3"
        assert p.bitrate == "320k"

    def test_ipod_saver_preset(self):
        p = QUALITY_PRESETS["ipod_saver"]
        assert p.output_format == "m4a"
        assert p.bitrate == "128k"

    def test_best_method(self):
        p = QualityProfile.best()
        assert p.output_format == "flac"

    def test_balanced_method(self):
        p = QualityProfile.balanced()
        assert p.output_format == "mp3"

    def test_ipod_saver_method(self):
        p = QualityProfile.ipod_saver()
        assert p.output_format == "m4a"


class TestDownloadPlan:
    def test_fields(self):
        d = DownloadPlan(output_format="mp3")
        assert d.quality_profile == "balanced"
        assert d.output_format == "mp3"
