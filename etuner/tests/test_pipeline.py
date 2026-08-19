"""End-to-end pipeline tests on synthetic TagSets."""
from etuner.models import TagSet, Album, TrackFile
from etuner.config import Config
from etuner.cleanup import clean_album, clean_track


def _make_track(path="x.mp3", **kwargs) -> TagSet:
    return TagSet(path=path, **kwargs)


def test_full_pipeline_basic():
    tags = _make_track(
        artist=" Drake feat. SZA , Future ",
        title="Good Days",
        album="  Good Days (2021)  ",
        track="03/14",
    )
    clean_track(tags, Config.default())
    assert tags.artist == "Drake"
    assert tags.album_artist == "Drake"
    assert "feat. SZA" in tags.title
    assert tags.album == "Good Days (2021)"
    assert tags.track == 3
    assert tags.total_tracks == 14


def test_pipeline_infers_album_from_folder():
    tags = _make_track(path="/music/Drake - Dark Lane Demo Tapes (2020)/01.mp3")
    tf = TrackFile(path=tags.path, fmt="mp3", tags=tags)
    album = Album(key="test", folder="/music/Drake - Dark Lane Demo Tapes (2020)")
    album.add(tf)
    clean_album(album, Config.default())
    assert tags.album == "Dark Lane Demo Tapes"
    assert tags.year == 2020


def test_legitimate_repeats_preserved():
    tags = _make_track(artist="Yeah Yeah Yeahs", title="Na Na Na")
    clean_track(tags, Config.default())
    assert tags.artist == "Yeah Yeah Yeahs"
    assert tags.title == "Na Na Na"
