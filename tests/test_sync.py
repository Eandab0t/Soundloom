"""Tests for backend.services.sync — the Soundiiz-style playlist sync.

Pure-unit tests cover URL parsing, search URL construction and id-cache
parsing. DB-backed tests run sync_playlist against a throwaway database with
fake platform clients, so no network is touched.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.services import sync as sync_mod


# --------------------------------------------------------------------------
# Config stub — keeps tests hermetic, away from data/settings.json
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_sync_config(monkeypatch):
    """Hermetic config: a fresh dict per test so mutations cannot leak."""
    base = {"sync_max_tracks": 200, "sync_interval": 21600}

    class _StubConfig:
        def get(self, key, default=None):
            return base.get(key, default)

        def get_all(self):
            return dict(base)

    monkeypatch.setattr(sync_mod, "config", _StubConfig())
    return base


# --------------------------------------------------------------------------
# Fake platform clients
# --------------------------------------------------------------------------

def make_client_cls(detail, tracks, fail=False):
    """Build a stand-in for SpotifyClient / DeezerClient."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def playlist(self, playlist_id):
            if fail:
                raise RuntimeError("platform exploded")
            return detail

        async def playlist_tracks(self, playlist_id):
            return list(tracks)

    return FakeClient


def _track(tid, title="Track One", artist="Alpha"):
    return {"id": tid, "title": title, "artist": artist, "duration": 200.0, "album": "Album"}


async def _add_sync(name="My Mix", source="spotify", ref="abc123", auto=1):
    from backend.database import execute, fetch_one
    await execute(
        "INSERT INTO synced_playlists (name, source_type, source_ref, auto_sync) "
        "VALUES (?, ?, ?, ?)",
        (name, source, ref, auto),
    )
    row = await fetch_one("SELECT * FROM synced_playlists ORDER BY id DESC LIMIT 1")
    return row["id"]


@pytest.fixture
def sync_db(monkeypatch, tmp_path):
    """Point the database at a throwaway file, like the watcher tests do."""
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "sync.db")
    return cfg


# --------------------------------------------------------------------------
# parse_playlist_url
# --------------------------------------------------------------------------

class TestParsePlaylistUrl:
    def test_spotify_playlist_url(self):
        assert sync_mod.parse_playlist_url(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIG?si=abc"
        ) == ("spotify", "37i9dQZF1DXcBWIG")

    def test_spotify_album_url_is_a_track_list(self):
        assert sync_mod.parse_playlist_url(
            "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"
        ) == ("spotify", "1DFixLWuPkv3KT3TnV35m3")

    def test_spotify_uri(self):
        assert sync_mod.parse_playlist_url("spotify:playlist:37i9dQZF1DXcBWIG") == (
            "spotify", "37i9dQZF1DXcBWIG",
        )

    def test_deezer_playlist_url_with_query(self):
        assert sync_mod.parse_playlist_url(
            "https://www.deezer.com/playlist/908622995?mut=1"
        ) == ("deezer", "908622995")

    def test_bare_numeric_id_is_deezer(self):
        assert sync_mod.parse_playlist_url("908622995") == ("deezer", "908622995")

    def test_bare_base62_id_is_spotify(self):
        assert sync_mod.parse_playlist_url("37i9dQZF1DXcBWIGoYD5MP") == (
            "spotify", "37i9dQZF1DXcBWIGoYD5MP",
        )

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            sync_mod.parse_playlist_url("")

    def test_spotify_url_without_playlist_or_album_raises(self):
        with pytest.raises(ValueError):
            sync_mod.parse_playlist_url("https://open.spotify.com/artist/abc")

    def test_deezer_url_without_playlist_raises(self):
        with pytest.raises(ValueError):
            sync_mod.parse_playlist_url("https://www.deezer.com/album/123")

    def test_unsupported_host_raises(self):
        with pytest.raises(ValueError):
            sync_mod.parse_playlist_url("https://example.com/playlist/123")


# --------------------------------------------------------------------------
# Search URL building and id-cache parsing
# --------------------------------------------------------------------------

class TestSearchUrl:
    def test_builds_youtube_search(self):
        url = sync_mod._search_url(_track("1", "Boston", "Stella Lefty"), "spotify")
        assert url == "ytsearch1:Stella%20Lefty%20Boston"

    def test_primary_artist_only(self):
        url = sync_mod._search_url(_track("1", "Song", "Alpha, Beta"), "spotify")
        assert url == "ytsearch1:Alpha%20Song"

    def test_missing_title_still_searches_artist(self):
        url = sync_mod._search_url(_track("1", "", "Alpha"), "spotify")
        assert url == "ytsearch1:Alpha"

    def test_fallback_is_second_priority_source(self):
        # source_priority default is [youtube, deezer, soundcloud], so the
        # manual fallback URL now targets the second source (deezer).
        url = sync_mod._fallback_search_url(_track("1", "Boston", "Stella Lefty"))
        assert url.startswith("dzsearch:")


class TestLoadIds:
    def test_empty_and_none(self):
        assert sync_mod._load_ids(None) == set()
        assert sync_mod._load_ids("") == set()

    def test_valid_json_list(self):
        assert sync_mod._load_ids('["1", 2, "3"]') == {"1", "2", "3"}

    def test_invalid_json(self):
        assert sync_mod._load_ids("not json") == set()

    def test_non_list_json(self):
        assert sync_mod._load_ids('{"a": 1}') == set()


# --------------------------------------------------------------------------
# sync_playlist against a real (throwaway) database
# --------------------------------------------------------------------------

class TestSyncPlaylist:
    async def test_enqueues_new_tracks(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            import backend.services.sync as S
            pid = await _add_sync()
            tracks = [_track("s1", "One"), _track("s2", "Two"), _track("s3", "Three")]
            monkeypatch.setattr(S, "SpotifyClient", make_client_cls({"name": "My Mix"}, tracks))
            result = await S.sync_playlist(pid)

            assert result["enqueued"] == 3
            jobs = await fetch_all("SELECT * FROM jobs")
            assert len(jobs) == 3
            assert all(j["source_url"].startswith("ytsearch1:") for j in jobs)
            assert all(j["source_type"] == "youtube" for j in jobs)
        finally:
            await close_db()

    async def test_second_run_is_idempotent(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            import backend.services.sync as S
            pid = await _add_sync()
            tracks = [_track("s1", "One"), _track("s2", "Two")]
            monkeypatch.setattr(S, "SpotifyClient", make_client_cls({"name": "My Mix"}, tracks))
            first = await S.sync_playlist(pid)
            second = await S.sync_playlist(pid)

            assert first["enqueued"] == 2
            assert second["enqueued"] == 0, "known ids must never re-enqueue"
            assert len(await fetch_all("SELECT * FROM jobs")) == 2
        finally:
            await close_db()

    async def test_owned_track_skipped(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, execute
        await init_db()
        try:
            import backend.services.sync as S
            await execute(
                "INSERT INTO tracks (file_path, title, artist, primary_artist, "
                "display_artist, file_status) VALUES ('/x.mp3', 'One', 'Alpha', "
                "'Alpha', 'Alpha', 'present')"
            )
            pid = await _add_sync()
            monkeypatch.setattr(
                S, "SpotifyClient",
                make_client_cls({"name": "My Mix"}, [_track("s1", "One"), _track("s2", "Two")]),
            )
            result = await S.sync_playlist(pid)

            assert result["owned"] == 1
            assert result["enqueued"] == 1
        finally:
            await close_db()

    async def test_already_queued_track_skipped(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, execute
        await init_db()
        try:
            import backend.services.sync as S
            await execute(
                "INSERT INTO jobs (source_url, query, status) VALUES "
                "('ytsearch1:Alpha One', 'ytsearch1:Alpha One', 'pending')"
            )
            pid = await _add_sync()
            monkeypatch.setattr(
                S, "SpotifyClient",
                make_client_cls({"name": "My Mix"}, [_track("s1", "One"), _track("s2", "Two")]),
            )
            result = await S.sync_playlist(pid)

            assert result["already_queued"] == 1
            assert result["enqueued"] == 1
        finally:
            await close_db()

    async def test_duplicate_search_urls_deduped_within_pass(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            import backend.services.sync as S
            pid = await _add_sync()
            # Two platform ids resolving to the same audio search.
            monkeypatch.setattr(
                S, "SpotifyClient",
                make_client_cls({"name": "My Mix"}, [_track("s1", "One"), _track("s2", "One")]),
            )
            result = await S.sync_playlist(pid)

            assert result["enqueued"] == 1
            assert result["already_queued"] == 1
            assert len(await fetch_all("SELECT * FROM jobs")) == 1
        finally:
            await close_db()

    async def test_per_playlist_cap(self, sync_db, stub_sync_config, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            import backend.services.sync as S
            stub_sync_config["sync_max_tracks"] = 2
            pid = await _add_sync()
            tracks = [_track(f"s{i}", f"Song {i}") for i in range(5)]
            monkeypatch.setattr(S, "SpotifyClient", make_client_cls({"name": "My Mix"}, tracks))
            result = await S.sync_playlist(pid)

            assert result["enqueued"] == 2
            assert any(
                "stopped" in e.lower() for e in result["errors"]
            ), "cap must halt the pass with an explanation"
            assert len(await fetch_all("SELECT * FROM jobs")) == 2
        finally:
            await close_db()

    async def test_pass_budget_limits_enqueues(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            import backend.services.sync as S
            monkeypatch.setattr(S, "_MAX_ENQUEUES_PER_PASS", 2)
            pid = await _add_sync()
            tracks = [_track(f"s{i}", f"Song {i}") for i in range(5)]
            monkeypatch.setattr(S, "SpotifyClient", make_client_cls({"name": "My Mix"}, tracks))
            result = await S.sync_playlist(pid)

            assert result["enqueued"] == 2
            assert any("budget" in e.lower() for e in result["errors"])
        finally:
            await close_db()

    async def test_persists_track_ids_name_and_last_synced(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            import backend.services.sync as S
            pid = await _add_sync(name="Old Name")
            monkeypatch.setattr(
                S, "SpotifyClient",
                make_client_cls({"name": "Stella Mix"}, [_track("s1"), _track("s2")]),
            )
            await S.sync_playlist(pid)

            row = await fetch_one("SELECT * FROM synced_playlists WHERE id=?", (pid,))
            assert json.loads(row["last_track_ids"]) == ["s1", "s2"]
            assert row["name"] == "Stella Mix"
            assert row["last_synced"]
            assert row["last_error"] == ""
        finally:
            await close_db()

    async def test_deezer_uses_title_field(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            import backend.services.sync as S
            pid = await _add_sync(source="deezer", ref="908622995", name="Old")
            monkeypatch.setattr(
                S, "DeezerClient",
                make_client_cls({"title": "Deezer Mix"}, [_track("d1", "Boston", "Stella Lefty")]),
            )
            await S.sync_playlist(pid)

            row = await fetch_one("SELECT * FROM synced_playlists WHERE id=?", (pid,))
            assert row["name"] == "Deezer Mix"
        finally:
            await close_db()

    async def test_failure_records_last_error_and_reraises(self, sync_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            import backend.services.sync as S
            pid = await _add_sync()
            monkeypatch.setattr(S, "SpotifyClient", make_client_cls({}, [], fail=True))
            with pytest.raises(RuntimeError):
                await S.sync_playlist(pid)

            row = await fetch_one("SELECT * FROM synced_playlists WHERE id=?", (pid,))
            assert "platform exploded" in row["last_error"]
            assert row["last_synced"]
        finally:
            await close_db()

    async def test_unknown_playlist_raises(self, sync_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            with pytest.raises(ValueError):
                await sync_mod.sync_playlist(9999)
        finally:
            await close_db()


# --------------------------------------------------------------------------
# sync_due — the scheduler's "who needs a refresh" query
# --------------------------------------------------------------------------

class TestSyncDue:
    async def test_fresh_auto_playlist_is_due(self, sync_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            await _add_sync(auto=1)
            assert await sync_mod.sync_due() == [1]
        finally:
            await close_db()

    async def test_manual_playlist_never_due(self, sync_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            await _add_sync(auto=0)
            assert await sync_mod.sync_due() == []
        finally:
            await close_db()

    async def test_recently_synced_not_due(self, sync_db):
        from backend.database import init_db, close_db, execute
        await init_db()
        try:
            recent = sync_mod._iso(sync_mod._utcnow() - timedelta(seconds=60))
            await _add_sync(auto=1)
            await execute(
                "UPDATE synced_playlists SET last_synced=?", (recent,)
            )
            assert await sync_mod.sync_due() == []
        finally:
            await close_db()

    async def test_stale_playlist_is_due(self, sync_db):
        from backend.database import init_db, close_db, execute
        await init_db()
        try:
            stale = "2020-01-01T00:00:00+00:00"
            await _add_sync(auto=1)
            await execute("UPDATE synced_playlists SET last_synced=?", (stale,))
            assert await sync_mod.sync_due() == [1]
        finally:
            await close_db()

    async def test_per_playlist_interval_override(self, sync_db):
        from backend.database import init_db, close_db, execute
        await init_db()
        try:
            # Synced 2 minutes ago; row wants a 60-second interval.
            soon = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
            await _add_sync(auto=1)
            await execute(
                "UPDATE synced_playlists SET sync_interval=60, last_synced=?", (soon,)
            )
            assert await sync_mod.sync_due() == [1]
        finally:
            await close_db()
