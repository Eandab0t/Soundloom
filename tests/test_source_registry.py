"""Tests for the audio source registry and Deezer search."""
import pytest

from backend.services import source_registry as R


class TestNormalizePriority:
    def test_default_passthrough(self):
        assert R.normalize_priority(["youtube", "deezer", "soundcloud"]) == ["youtube", "deezer", "soundcloud"]

    def test_partial_list_appends_missing(self):
        assert R.normalize_priority(["soundcloud"]) == ["soundcloud", "youtube", "deezer"]

    def test_drops_unknown_and_duplicates(self):
        assert R.normalize_priority(["spotify", "youtube", "youtube", "deezer"]) == ["youtube", "deezer", "soundcloud"]

    def test_garbage_resets_to_default(self):
        assert R.normalize_priority(None) == R.DEFAULT_PRIORITY
        assert R.normalize_priority("youtube") == R.DEFAULT_PRIORITY
        assert R.normalize_priority([1, 2]) == R.DEFAULT_PRIORITY


class TestSearchUrls:
    def test_build_youtube(self):
        assert R.build_search_job_url("youtube", "Alpha", "Song One") == "ytsearch1:Alpha%20Song%20One"

    def test_build_soundcloud(self):
        assert R.build_search_job_url("soundcloud", "Alpha", "Song") == "scsearch1:Alpha%20Song"

    def test_build_deezer_uses_dzsearch_scheme(self):
        assert R.build_search_job_url("deezer", "Alpha", "Song") == "dzsearch:Alpha%20Song"

    def test_primary_artist_only(self):
        assert R.build_search_job_url("youtube", "Alpha, Beta", "Song") == "ytsearch1:Alpha%20Song"

    def test_parse_sources(self):
        assert R.search_source_of("dzsearch:x") == "deezer"
        assert R.search_source_of("ytsearch1:x") == "youtube"
        assert R.search_source_of("scsearch1:x") == "soundcloud"
        assert R.search_source_of("https://example.com") is None
        assert R.is_search_job_url("https://www.youtube.com/watch?v=1") is False


class TestDeezerSearch:
    async def test_picks_best_match(self, monkeypatch):
        payload = {"data": [
            {"id": 1, "title": "Boston (Live)", "rank": 900_000,
             "artist": {"name": "Someone Else"}},
            {"id": 2, "title": "Boston", "rank": 800_000,
             "artist": {"name": "Stella Lefty"}},
        ]}

        class FakeResp:
            def raise_for_status(self): pass
            async def json(self, content_type=None): return payload

        class FakeSession:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def get(self, url, **k):
                class Ctx:
                    async def __aenter__(self): return FakeResp()
                    async def __aexit__(self, *a): pass
                return Ctx()

        async def fake_session():
            return FakeSession()

        monkeypatch.setattr(R.aiohttp, "ClientSession", lambda *a, **k: FakeSession())
        url = await R.deezer_search_track("Stella Lefty", "Boston")
        assert url == "https://www.deezer.com/track/2", "artist+title match must beat rank"

    async def test_no_results_returns_none(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self): pass
            async def json(self, content_type=None): return {"data": []}

        class FakeSession:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def get(self, url, **k):
                class Ctx:
                    async def __aenter__(self): return FakeResp()
                    async def __aexit__(self, *a): pass
                return Ctx()

        monkeypatch.setattr(R.aiohttp, "ClientSession", lambda *a, **k: FakeSession())
        assert await R.deezer_search_track("X", "Y") is None

    async def test_network_error_returns_none(self, monkeypatch):
        class Boom:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def get(self, url, **k):
                raise ConnectionError("network down")

        monkeypatch.setattr(R.aiohttp, "ClientSession", lambda *a, **k: Boom())
        assert await R.deezer_search_track("X", "Y") is None
