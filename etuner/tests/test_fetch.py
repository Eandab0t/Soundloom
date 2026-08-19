"""Tests for Phase 2 metadata fetchers.

No real network calls are made — ``requests.get``/``requests.post`` are
monkeypatched with canned responses shaped like what each API actually
returns, so these tests exercise the parsing/confidence logic without
needing connectivity or live API keys.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from etuner.config import Config
from etuner.models import TagSet
from etuner.fetch import get_fetcher, available_methods
from etuner.fetch.acoustid import AcoustIDFetcher, FingerprintError
from etuner.fetch.discogs import DiscogsFetcher
from etuner.fetch.musicbrainz import MusicBrainzFetcher
from etuner.fetch.spotify import SpotifyFetcher


def _mock_response(json_data, status_ok=True):
    resp = MagicMock()
    resp.json.return_value = json_data
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        import requests
        resp.raise_for_status.side_effect = requests.HTTPError("boom")
    return resp


# ── registry ────────────────────────────────────────────────────────────

def test_registry_lists_all_methods():
    methods = available_methods()
    for name in ("local_only", "musicbrainz", "acoustid", "discogs", "spotify"):
        assert name in methods


def test_get_fetcher_unknown_raises():
    with pytest.raises(ValueError):
        get_fetcher("not_a_real_method", Config.default())


def test_get_fetcher_returns_configured_instance():
    cfg = Config.default().override(discogs_token="tok123")
    fetcher = get_fetcher("discogs", cfg)
    assert fetcher.available() is True


# ── MusicBrainz ─────────────────────────────────────────────────────────

def test_musicbrainz_available_always_true():
    fetcher = MusicBrainzFetcher(Config.default())
    assert fetcher.available() is True


def test_musicbrainz_no_query_fields_no_match():
    fetcher = MusicBrainzFetcher(Config.default())
    result = fetcher.fetch(TagSet(path="x"))
    assert result.matched is False


def test_musicbrainz_parses_best_match():
    payload = {
        "recordings": [
            {
                "id": "rec-1",
                "score": 60,
                "title": "Paranoid Android",
                "artist-credit": [{"name": "Radiohead"}],
                "releases": [
                    {"title": "OK Computer", "date": "1997-05-21",
                     "media": [{"track-count": 12, "tracks": [{"title": "Paranoid Android", "number": "2"}]}]}
                ],
            },
            {
                "id": "rec-2",
                "score": 95,
                "title": "Paranoid Android",
                "artist-credit": [{"name": "Radiohead"}],
                "releases": [
                    {"title": "OK Computer (Remaster)", "date": "1997-05-21",
                     "media": [{"track-count": 12, "tracks": [{"title": "Paranoid Android", "number": "2"}]}]}
                ],
            },
        ]
    }
    with patch("etuner.fetch.musicbrainz.requests.get", return_value=_mock_response(payload)):
        fetcher = MusicBrainzFetcher(Config.default())
        result = fetcher.fetch(TagSet(path="x", title="Paranoid Android", artist="Radiohead"))

    assert result.matched is True
    assert result.confidence == pytest.approx(0.95)
    assert result.tags.artist == "Radiohead"
    assert result.tags.album in ("OK Computer", "OK Computer (Remaster)")
    assert result.tags.year == 1997


def test_musicbrainz_no_recordings_returns_unmatched():
    with patch("etuner.fetch.musicbrainz.requests.get", return_value=_mock_response({"recordings": []})):
        fetcher = MusicBrainzFetcher(Config.default())
        result = fetcher.fetch(TagSet(path="x", title="Nonexistent Song"))
    assert result.matched is False


# ── AcoustID ────────────────────────────────────────────────────────────

def test_acoustid_unavailable_without_key_or_fpcalc():
    fetcher = AcoustIDFetcher(Config.default())
    assert fetcher.available() is False
    result = fetcher.fetch(TagSet(path="x"))
    assert result.matched is False


def test_acoustid_parses_lookup_result():
    cfg = Config.default().override(acoustid_api_key="key123")
    payload = {
        "status": "ok",
        "results": [
            {
                "id": "res-1",
                "score": 0.93,
                "recordings": [
                    {
                        "title": "Idioteque",
                        "artists": [{"name": "Radiohead"}],
                        "releasegroups": [{"title": "Kid A", "type": "Album"}],
                    }
                ],
            }
        ],
    }
    with patch("etuner.fetch.acoustid.shutil.which", return_value="/usr/bin/fpcalc"), \
         patch("etuner.fetch.acoustid._fingerprint", return_value=(240, "AQAB...fakefp")), \
         patch("etuner.fetch.acoustid.requests.post", return_value=_mock_response(payload)):
        fetcher = AcoustIDFetcher(cfg)
        assert fetcher.available() is True
        result = fetcher.fetch(TagSet(path="/music/track.mp3"))

    assert result.matched is True
    assert result.confidence == pytest.approx(0.93)
    assert result.tags.title == "Idioteque"
    assert result.tags.artist == "Radiohead"
    assert result.tags.album == "Kid A"


def test_acoustid_fingerprint_failure_is_reported_not_raised():
    cfg = Config.default().override(acoustid_api_key="key123")
    with patch("etuner.fetch.acoustid.shutil.which", return_value="/usr/bin/fpcalc"), \
         patch("etuner.fetch.acoustid._fingerprint", side_effect=FingerprintError("no such file")):
        fetcher = AcoustIDFetcher(cfg)
        result = fetcher.fetch(TagSet(path="/music/missing.mp3"))
    assert result.matched is False
    assert "no such file" in result.notes[0]


# ── Discogs ─────────────────────────────────────────────────────────────

def test_discogs_unavailable_without_token():
    fetcher = DiscogsFetcher(Config.default())
    assert fetcher.available() is False


def test_discogs_parses_artist_dash_album_title():
    cfg = Config.default().override(discogs_token="tok123")
    payload = {"results": [{"title": "Radiohead - OK Computer", "year": "1997", "genre": ["Rock"]}]}
    with patch("etuner.fetch.discogs.requests.get", return_value=_mock_response(payload)):
        fetcher = DiscogsFetcher(cfg)
        result = fetcher.fetch(TagSet(path="x", artist="Radiohead", album="OK Computer"))

    assert result.matched is True
    assert result.tags.artist == "Radiohead"
    assert result.tags.album == "OK Computer"
    assert result.tags.year == 1997
    assert result.tags.genre == "Rock"


# ── Spotify ─────────────────────────────────────────────────────────────

def test_spotify_unavailable_without_credentials():
    fetcher = SpotifyFetcher(Config.default())
    assert fetcher.available() is False


def test_spotify_parses_track_and_caches_token():
    cfg = Config.default().override(spotify_client_id="id", spotify_client_secret="secret")
    token_payload = {"access_token": "tok-abc", "expires_in": 3600}
    search_payload = {
        "tracks": {
            "items": [
                {
                    "name": "Idioteque",
                    "artists": [{"name": "Radiohead"}],
                    "album": {"name": "Kid A", "release_date": "2000-10-02", "total_tracks": 10},
                    "track_number": 8,
                    "popularity": 70,
                }
            ]
        }
    }
    with patch("etuner.fetch.spotify.requests.post", return_value=_mock_response(token_payload)) as post_mock, \
         patch("etuner.fetch.spotify.requests.get", return_value=_mock_response(search_payload)):
        fetcher = SpotifyFetcher(cfg)
        result1 = fetcher.fetch(TagSet(path="x", title="Idioteque", artist="Radiohead"))
        result2 = fetcher.fetch(TagSet(path="x", title="Idioteque", artist="Radiohead"))

    assert result1.matched is True
    assert result1.tags.album == "Kid A"
    assert result1.tags.year == 2000
    assert result1.tags.track == 8
    # Token should be cached across calls, not re-requested.
    assert post_mock.call_count == 1


def test_spotify_requires_title():
    cfg = Config.default().override(spotify_client_id="id", spotify_client_secret="secret")
    fetcher = SpotifyFetcher(cfg)
    result = fetcher.fetch(TagSet(path="x", artist="Radiohead"))
    assert result.matched is False
