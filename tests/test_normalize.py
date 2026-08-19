"""Tests for pipeline.normalize — text normalization, artist parsing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.pipeline.normalize import (
    normalize_text, normalize_artist, normalize_title,
    normalize_album, parse_artists, is_variant_match,
)


class TestNormalizeText:
    def test_lowercases(self):
        assert normalize_text("HELLO WORLD") == "hello world"

    def test_strips_whitespace(self):
        assert normalize_text("  hello  ") == "hello"

    def test_collapses_whitespace(self):
        assert normalize_text("a   b") == "a   b"

    def test_removes_articles(self):
        assert normalize_text("the beatles") == "the beatles"

    def test_strips_accents(self):
        assert normalize_text("café") == "cafe"

    def test_empty(self):
        assert normalize_text("") == ""
        assert normalize_text(None) == ""


class TestNormalizeArtist:
    def test_strips_feat_dot(self):
        assert normalize_artist("Rick Astley feat. someone") == "rick astley"

    def test_strips_feat_word(self):
        assert normalize_artist("Artist featuring X") == "artist"

    def test_strips_ft(self):
        assert normalize_artist("A ft B") == "a"

    def test_strips_parens(self):
        assert normalize_artist("Artist (feat. X)") == "artist"

    def test_strips_remix_suffix(self):
        assert normalize_artist("Artist - Remix") == "artist"

    def test_removes_articles(self):
        assert normalize_artist("The Beatles") == "beatles"

    def test_empty(self):
        assert normalize_artist("") == ""


class TestNormalizeTitle:
    def test_strips_official_video_parens(self):
        n = normalize_title("Never Gonna Give You Up (Official Video)")
        assert n == "never gonna give you up"

    def test_strips_official_video_brackets(self):
        n = normalize_title("Song [Official Video]")
        assert n == "song"

    def test_strips_lyric_video(self):
        n = normalize_title("Song Title (Lyric Video)")
        assert n == "song title"

    def test_strips_live_suffix(self):
        n = normalize_title("Song - Live")
        assert n == "song"

    def test_strips_remix(self):
        n = normalize_title("Song (Remix)")
        assert n == "song"

    def test_strips_parens_without_keyword(self):
        n = normalize_title("Song (Something)")
        assert n == "song something"

    def test_strips_brackets_without_keyword(self):
        n = normalize_title("Song [Official Video]")
        assert n == "song"


class TestNormalizeAlbum:
    def test_strips_edition(self):
        n = normalize_album("Album (Deluxe Edition)")
        assert n == "album"

    def test_strips_remaster(self):
        n = normalize_album("Album (Remastered)")
        assert n == "album"

    def test_strips_remix(self):
        n = normalize_album("Album (Remix)")
        assert n == "album"


class TestParseArtists:
    def test_feat_dot(self):
        r = parse_artists("Kanye West feat. Rihanna")
        assert r["primary"] == "Kanye West"
        assert r["featured"] == ["Rihanna"]
        assert r["album_artist"] == "Kanye West"

    def test_feat_x(self):
        r = parse_artists("Artist feat. X, Y")
        assert r["featured"] == ["X", "Y"]

    def test_ft(self):
        r = parse_artists("A ft B")
        assert r["primary"] == "A"
        assert r["featured"] == ["B"]

    def test_no_feat(self):
        r = parse_artists("Solo Artist")
        assert r["primary"] == "Solo Artist"
        assert r["featured"] == []

    def test_display(self):
        r = parse_artists("A feat. B, C")
        assert "feat." in r["display"]

    def test_empty(self):
        r = parse_artists("")
        assert r["primary"] == ""


class TestIsVariantMatch:
    def test_same(self):
        assert is_variant_match("live", "live") is True
        assert is_variant_match("acoustic", "acoustic") is True

    def test_different(self):
        assert is_variant_match("live", "studio") is False
        assert is_variant_match(None, "live") is False

    def test_none_both(self):
        assert is_variant_match(None, None) is True

    def test_case_insensitive(self):
        assert is_variant_match("Live", "live") is True
