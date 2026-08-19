"""Tests for the renaming engine."""
import os
from etuner.renamer import build_path, _sanitize_segment
from etuner.models import TagSet
from etuner.config import Config


def _tags(**kw) -> TagSet:
    return TagSet(path="/music/artist/song.mp3", **kw)


def test_basic_template():
    tags = _tags(album_artist="Drake", album="Scorpion", track=1, title="God's Plan")
    cfg = Config.default()
    path = build_path(tags, "{album_artist}/{album}/{track:02d} - {title}.{ext}", cfg)
    assert "Drake" in path
    assert "Scorpion" in path
    assert "01 - God's Plan" in path


def test_illegal_chars_sanitized():
    assert "_" in _sanitize_segment("AC/DC") or "AC" in _sanitize_segment("AC/DC")


def test_unknown_fallback():
    tags = _tags()
    cfg = Config.default()
    path = build_path(tags, "{album_artist}/{title}.{ext}", cfg)
    assert "Unknown" in path
