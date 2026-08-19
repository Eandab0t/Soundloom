"""Tests for backend.pipeline.cleanup — tag cleanup pipeline."""
import pytest
from backend.pipeline.cleanup import (
    clean_whitespace, clean_duplicates, clean_artists,
    clean_track_numbers, run_cleanup, CleanupChange, CleanupResult,
)


class TestCleanWhitespace:
    def test_strips_whitespace(self):
        tags = {"title": "  Hello World  ", "artist": "  Test Artist  "}
        clean_whitespace(tags)
        assert tags["title"] == "Hello World"
        assert tags["artist"] == "Test Artist"

    def test_collapses_whitespace(self):
        tags = {"title": "Hello   World"}
        clean_whitespace(tags)
        assert tags["title"] == "Hello World"

    def test_unifies_feat(self):
        tags = {"title": "Song ft. Artist"}
        clean_whitespace(tags)
        assert "feat." in tags["title"]

    def test_unifies_featuring(self):
        tags = {"title": "Song featuring Someone"}
        clean_whitespace(tags)
        assert "feat." in tags["title"]

    def test_normalizes_dashes(self):
        tags = {"title": "Song \u2013 Version"}
        clean_whitespace(tags)
        assert tags["title"] == "Song - Version"

    def test_strips_trailing_junk(self):
        tags = {"title": "Song!!! "}
        clean_whitespace(tags)
        assert tags["title"] == "Song"

    def test_creates_empty_keys(self):
        tags = {}
        clean_whitespace(tags)
        assert "title" in tags
        assert tags["title"] == ""

    def test_missing_field_gets_empty_string(self):
        tags = {"artist": "  Test  "}
        clean_whitespace(tags)
        assert tags["artist"] == "Test"
        assert tags["title"] == ""


class TestCleanDuplicates:
    def test_echo_word(self):
        tags = {"title": "Song Song"}
        clean_duplicates(tags)
        assert tags["title"] == "Song"

    def test_echo_word_long(self):
        tags = {"title": "Hello Hello"}
        clean_duplicates(tags)
        assert tags["title"] == "Hello"

    def test_short_word_not_deduped(self):
        tags = {"title": "Na Na"}
        clean_duplicates(tags)
        assert tags["title"] == "Na Na"

    def test_no_false_positive(self):
        tags = {"title": "Unique Title"}
        clean_duplicates(tags)
        assert tags["title"] == "Unique Title"

    def test_artist_echo(self):
        tags = {"artist": "Band Band"}
        clean_duplicates(tags)
        assert tags["artist"] == "Band"

    def test_three_way_artist_dash(self):
        tags = {"artist": "Artist - Artist - Track"}
        clean_duplicates(tags)
        assert tags["artist"] == "Artist"


class TestCleanArtists:
    def test_feat_moved_to_title(self):
        tags = {"artist": "Drake feat. SZA", "title": "Song"}
        clean_artists(tags)
        assert tags["artist"] == "Drake"
        assert "SZA" in tags["title"]

    def test_feat_not_duplicated(self):
        tags = {"artist": "Drake feat. SZA", "title": "Song (SZA)"}
        clean_artists(tags)
        assert tags["artist"] == "Drake"
        assert tags["title"] == "Song (SZA)"

    def test_album_artist_set_from_primary(self):
        tags = {"artist": "Drake, Future, Lil Wayne", "title": "Song"}
        clean_artists(tags)
        assert tags.get("album_artist") == "Drake"

    def test_album_artist_overwrites_wrong(self):
        tags = {"artist": "Drake", "album_artist": "", "title": "Song"}
        clean_artists(tags)
        assert tags["album_artist"] == "Drake"

    def test_dedupes_comma_artists(self):
        tags = {"artist": "Drake, Drake, Drake", "title": "Song"}
        clean_artists(tags)
        assert tags["artist"] == "Drake"

    def test_ft_variant(self):
        tags = {"artist": "Drake ft. SZA", "title": "Song"}
        clean_artists(tags)
        assert tags["artist"] == "Drake"
        assert "SZA" in tags["title"]

    def test_no_artist_noop(self):
        tags = {"title": "Song"}
        changes = clean_artists(tags)
        assert len(changes) == 0


class TestCleanTrackNumbers:
    def test_leading_zero(self):
        tags = {"track_number": "01"}
        clean_track_numbers(tags)
        assert tags["track_number"] == 1

    def test_slash_format(self):
        tags = {"track_number": "3/12"}
        clean_track_numbers(tags)
        assert tags["track_number"] == 3

    def test_already_clean(self):
        tags = {"track_number": 5}
        clean_track_numbers(tags)
        assert tags["track_number"] == 5

    def test_invalid_zeros_out(self):
        tags = {"track_number": "abc"}
        clean_track_numbers(tags)
        assert tags["track_number"] == 0

    def test_negative_becomes_zero(self):
        tags = {"track_number": -1}
        clean_track_numbers(tags)
        assert tags["track_number"] == 0


class TestRunCleanup:
    def test_returns_result(self):
        tags = {"title": "  Hello  ", "artist": "Drake feat. SZA"}
        result = run_cleanup(tags)
        assert isinstance(result, CleanupResult)
        assert result.success is True
        assert len(result.changes) > 0

    def test_multiple_fixes(self):
        tags = {
            "title": "  Song Song  ",
            "artist": "Drake ft. SZA, Drake",
        }
        result = run_cleanup(tags)
        assert len(result.changes) >= 2

    def test_with_folder(self):
        tags = {"title": "Song"}
        result = run_cleanup(tags, folder_path="/some/Artist - Album (2020)/track.mp3")
        album_changes = [c for c in result.changes if c.field == "album"]
        assert len(album_changes) > 0
