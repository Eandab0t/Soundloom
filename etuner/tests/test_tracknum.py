"""Tests for track-number repair."""
from etuner.models import TagSet
from etuner.config import Config
from etuner.cleanup.tracknum import clean_track_numbers


def _fix(track=None, total_tracks=None):
    tags = TagSet(path="x", track=track, total_tracks=total_tracks)
    clean_track_numbers(tags, Config.default())
    return tags


def test_zero_padded():
    assert TagSet(path="x", track="01").track == 1


def test_slash_total():
    t = TagSet(path="x", track="3/14")
    assert t.track == 3
    assert t.total_tracks == 14


def test_vinyl_style():
    t = TagSet(path="x", track="A1")
    assert t.track == 1


def test_empty():
    t = TagSet(path="x", track=None)
    assert t.track is None
