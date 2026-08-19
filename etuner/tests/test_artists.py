"""Tests for artist normalization."""
from etuner.models import TagSet
from etuner.config import Config
from etuner.cleanup.artists import clean_artists


def _fix(artist=None, album_artist=None, title=None):
    tags = TagSet(path="x", artist=artist, album_artist=album_artist, title=title)
    clean_artists(tags, Config.default())
    return tags


def test_album_artist_from_primary():
    tags = _fix(artist="Drake")
    assert tags.album_artist == "Drake"


def test_multi_artist_takes_first():
    tags = _fix(artist="Drake, Future, Lil Wayne")
    assert tags.album_artist == "Drake"


def test_feat_moved_to_title():
    tags = _fix(artist="Drake feat. SZA", title="Good Days")
    assert tags.artist == "Drake"
    assert "feat. SZA" in tags.title


def test_feat_not_duplicated_in_title():
    tags = _fix(artist="Drake feat. SZA", title="Good Days (feat. SZA)")
    assert tags.title.count("feat.") == 1


def test_existing_album_artist_normalized():
    tags = _fix(artist="Drake", album_artist="Drake, Future")
    assert tags.album_artist == "Drake"


def test_handle_the_creates_sort_field():
    tags = TagSet(path="x", artist="The Beatles")
    clean_artists(tags, Config.default().override(handle_the=True))
    assert tags.album_artist == "The Beatles"
    assert tags.album_artist_sort == "Beatles, The"


def test_handle_the_only_with_space_after_article():
    tags = TagSet(path="x", artist="A-Ha")
    clean_artists(tags, Config.default().override(handle_the=True))
    # "A" not followed by space — _to_sort_form returns None, so we fall
    # back to the primary artist as the sort form.
    assert tags.album_artist_sort == "A-Ha"


def test_sort_artists_sets_sort_field_from_primary():
    """Song sorter: always set album_artist_sort from primary artist."""
    tags = TagSet(path="x", artist="Repeatedword")
    clean_artists(tags, Config.default())
    assert tags.album_artist_sort == "Repeatedword"


def test_sort_artists_disabled():
    tags = TagSet(path="x", artist="Repeatedword")
    clean_artists(tags, Config.default().override(sort_artists=False))
    assert tags.album_artist_sort is None


def test_sort_artists_takes_first_from_multi():
    tags = TagSet(path="x", artist="Repeatedword, duskydemise, Repeatedword")
    clean_artists(tags, Config.default())
    assert tags.album_artist == "Repeatedword"
    assert tags.album_artist_sort == "Repeatedword"


def test_dedupe_artist_names_removes_duplicates():
    tags = _fix(artist="Repeatedword, Repeatedword, Repeatedword")
    assert tags.artist == "Repeatedword"


def test_dedupe_artist_names_preserves_order_and_case():
    tags = _fix(artist="Repeatedword, Repeatedword, duskydemise, Repeatedword")
    assert tags.artist == "Repeatedword, duskydemise"


def test_dedupe_artist_names_case_insensitive():
    tags = _fix(artist="plaxz, Plaxz, Repeatedword, PLAXZ")
    assert tags.artist == "plaxz, Repeatedword"


def test_dedupe_preserves_legitimate_repeats():
    """yeah yeah yeahs should not be collapsed (not comma-separated)."""
    tags = _fix(artist="Yeah Yeah Yeahs")
    assert tags.artist == "Yeah Yeah Yeahs"


def test_dedupe_skipped_when_fix_artists_off():
    tags = TagSet(path="x", artist="Repeatedword, Repeatedword")
    clean_artists(tags, Config.default().override(fix_artists=False))
    assert tags.artist == "Repeatedword, Repeatedword"
    assert tags.album_artist is None
