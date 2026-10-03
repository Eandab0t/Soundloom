"""Tests for source priority: sync enqueues, downloader fallback, settings API."""
import pytest

from backend.services import sync as sync_mod
from backend.services import source_registry as R
from backend.services import downloader as D


class _StubConfig:
    def __init__(self, settings):
        self._s = settings

    def get(self, key, default=None):
        return self._s.get(key, default)

    def get_all(self):
        return dict(self._s)


@pytest.fixture(autouse=True)
def stub_sync_config(monkeypatch):
    base = {
        "sync_max_tracks": 200, "sync_interval": 21600,
        "source_priority": ["youtube", "deezer", "soundcloud"],
        "source_fallback": True,
    }

    class _Stub:
        def get(self, key, default=None):
            return base.get(key, default)

        def get_all(self):
            return dict(base)

    monkeypatch.setattr(sync_mod, "config", _Stub())
    return base


def make_client_cls(detail, tracks):
    """Stand-in for SpotifyClient: sync instantiates it with its own args."""
    class C:
        def __init__(self, *a, **k):
            pass

        async def playlist(self, pid):
            return detail

        async def playlist_tracks(self, pid):
            return list(tracks)

    return C


@pytest.fixture
def prio_db(monkeypatch, tmp_path):
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "prio.db")
    return cfg


async def _add_sync():
    from backend.database import execute, fetch_one
    await execute(
        "INSERT INTO synced_playlists (name, source_type, source_ref, auto_sync) "
        "VALUES ('Mix', 'spotify', 'abc', 1)"
    )
    row = await fetch_one("SELECT * FROM synced_playlists ORDER BY id DESC LIMIT 1")
    return row["id"]


class TestSyncUsesPriority:
    async def test_default_priority_enqueues_youtube(self, prio_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            pid = await _add_sync()
            monkeypatch.setattr(sync_mod, "SpotifyClient",
                                make_client_cls({"name": "Mix"}, [{"id": "s1", "title": "One", "artist": "Alpha"}]))
            await sync_mod.sync_playlist(pid)
            jobs = await fetch_all("SELECT source_url FROM jobs")
            assert jobs[0]["source_url"].startswith("ytsearch1:")
        finally:
            await close_db()

    async def test_deezer_first_enqueues_dzsearch(self, prio_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            prio_db_setting = {"sync_max_tracks": 200, "sync_interval": 21600,
                               "source_priority": ["deezer", "youtube"]}
            stub = _StubConfig(prio_db_setting)
            monkeypatch.setattr(sync_mod, "config", stub)
            pid = await _add_sync()
            monkeypatch.setattr(sync_mod, "SpotifyClient",
                                make_client_cls({"name": "Mix"}, [{"id": "s1", "title": "One", "artist": "Alpha"}]))
            await sync_mod.sync_playlist(pid)
            jobs = await fetch_all("SELECT source_url FROM jobs")
            assert jobs[0]["source_url"].startswith("dzsearch:")
        finally:
            await close_db()

    async def test_soundcloud_first_enqueues_scsearch(self, prio_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            monkeypatch.setattr(sync_mod, "config", _StubConfig({
                "sync_max_tracks": 200, "source_priority": ["soundcloud"]}))
            pid = await _add_sync()
            monkeypatch.setattr(sync_mod, "SpotifyClient",
                                make_client_cls({"name": "Mix"}, [{"id": "s1", "title": "One", "artist": "Alpha"}]))
            await sync_mod.sync_playlist(pid)
            jobs = await fetch_all("SELECT source_url FROM jobs")
            assert jobs[0]["source_url"].startswith("scsearch1:")
        finally:
            await close_db()


class TestFallbackChain:
    def _settings(self, **over):
        s = {"source_priority": ["youtube", "deezer", "soundcloud"], "source_fallback": True}
        s.update(over)
        return s

    async def test_failed_yt_job_reroutes_to_next(self, prio_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            job = {"id": 1, "source_url": "ytsearch1:Alpha%20Song",
                   "artist": "Alpha", "title": "Song", "retries": 0}
            url = await D._fallback_url_for(job, job["source_url"], self._settings())
            assert url == "dzsearch:Alpha%20Song"
        finally:
            await close_db()

    async def test_last_source_returns_none(self, prio_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            job = {"id": 1, "source_url": "scsearch1:Alpha%20Song",
                   "artist": "Alpha", "title": "Song", "retries": 0}
            assert await D._fallback_url_for(job, job["source_url"], self._settings()) is None
        finally:
            await close_db()

    async def test_direct_url_never_reroutes(self, prio_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            job = {"id": 1, "source_url": "https://www.youtube.com/watch?v=x",
                   "artist": "Alpha", "title": "Song", "retries": 0}
            assert await D._fallback_url_for(job, job["source_url"], self._settings()) is None
        finally:
            await close_db()

    async def test_disabled_fallback_returns_none(self, prio_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            job = {"id": 1, "source_url": "ytsearch1:Alpha",
                   "artist": "Alpha", "title": "Song", "retries": 0}
            s = self._settings(source_fallback=False)
            assert await D._fallback_url_for(job, job["source_url"], s) is None
        finally:
            await close_db()

    async def test_try_fallback_requeues_and_records(self, prio_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            await D.execute(
                "INSERT INTO jobs (id, source_url, title, artist, status) "
                "VALUES (7, 'ytsearch1:Alpha%20Song', 'Song', 'Alpha', 'downloading')"
            )
            row = await D.fetch_one("SELECT * FROM jobs WHERE id=7")
            ok = await D._try_fallback(7, row, row["source_url"], self._settings(), "429 too many")
            assert ok is True
            after = await D.fetch_one("SELECT * FROM jobs WHERE id=7")
            assert after["status"] == "pending"
            assert after["source_url"] == "dzsearch:Alpha%20Song"
            assert "next source" in after["error"]
        finally:
            await close_db()

    async def test_rate_limit_search_reroutes_before_backoff(self, prio_db):
        """A 429 on a search job should switch sources, not park the job."""
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            await D.execute(
                "INSERT INTO jobs (id, source_url, title, artist, status, retries) "
                "VALUES (8, 'ytsearch1:Alpha%20Song', 'Song', 'Alpha', 'downloading', 0)"
            )
            job = await D.fetch_one("SELECT * FROM jobs WHERE id=8")
            settings = self._settings()
            calls = {"park": 0}
            async def fake_park(job_id, retries, error):
                calls["park"] += 1
                return 120.0
            import backend.services.downloader as d
            orig_park = d.park_for_backoff
            d.park_for_backoff = fake_park
            try:
                # simulate the ResolveError branch's decision order
                from backend.errors import ResolveError
                e = ResolveError("HTTP Error 429: Too Many Requests")
                rerouted = await D._try_fallback(8, job, job["source_url"], settings, str(e))
                if not rerouted and D.is_rate_limit_error(str(e)):
                    await D.park_for_backoff(8, 1, str(e))
            finally:
                d.park_for_backoff = orig_park
            assert calls["park"] == 0, "rate-limited search job must reroute, not park"
            after = await D.fetch_one("SELECT * FROM jobs WHERE id=8")
            assert after["source_url"] == "dzsearch:Alpha%20Song"
        finally:
            await close_db()


class TestSettingsEndpoints:
    async def test_roundtrip_priority_and_fallback(self, monkeypatch, tmp_path):
        import backend.api.sources as S
        from backend import config as cfg
        monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(cfg, "_cache", None)
        data = await S.update_search_sources({
            "sources": [{"key": "soundcloud"}, {"key": "deezer"}], "fallback": False,
        })
        assert [s["key"] for s in data["sources"]] == ["soundcloud", "deezer", "youtube"]
        assert data["fallback"] is False
        got = await S.search_sources()
        assert [s["key"] for s in got["sources"]] == ["soundcloud", "deezer", "youtube"]
        assert got["fallback"] is False
