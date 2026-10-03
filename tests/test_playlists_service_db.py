"""DB-backed tests for the playlist hub: matching, enqueue, OAuth flow, routes."""
import json

import pytest

from backend.services import playlists as svc
from backend.services import streaming_auth


@pytest.fixture
def pl_db(monkeypatch, tmp_path):
    """Throwaway database + exports dir, like the sync tests."""
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "playlists.db")
    monkeypatch.setattr(cfg, "EXPORTS_DIR", tmp_path / "exports")
    return cfg


async def _mk_playlist(name="Mix", origin="manual"):
    from backend.database import execute, fetch_one
    cur = await execute("INSERT INTO playlists (name, origin) VALUES (?, ?)", (name, origin))
    return cur.lastrowid


async def _add_row(pid, title, artist="", album="", duration=0.0, path="", pos=0):
    from backend.database import execute, fetch_one
    if not pos:
        row = await fetch_one(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM playlist_tracks WHERE playlist_id=?",
            (pid,),
        )
        pos = row["p"]
    cur = await execute(
        "INSERT INTO playlist_tracks (playlist_id, position, title, artist, album, duration, path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, pos, title, artist, album, duration, path),
    )
    return cur.lastrowid


async def _add_lib_track(path, title, artist, primary=None):
    from backend.database import execute
    await execute(
        "INSERT INTO tracks (file_path, title, artist, primary_artist, display_artist, "
        "file_status) VALUES (?, ?, ?, ?, ?, 'present')",
        (path, title, artist, primary or artist, primary or artist),
    )


# --------------------------------------------------------------------------
# refresh_local_matches
# --------------------------------------------------------------------------

class TestMatching:
    async def test_matches_by_artist_title(self, pl_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            await _add_lib_track("C:/m/love.mp3", "Lovefool", "The Cardigans")
            pid = await _mk_playlist()
            await _add_row(pid, "Lovefool", "The Cardigans")
            out = await svc.refresh_local_matches(pid)
            assert out["matched"] == 1
            row = await fetch_one(
                "SELECT track_id FROM playlist_tracks WHERE playlist_id=?", (pid,))
            assert row["track_id"] is not None
        finally:
            await close_db()

    async def test_matches_by_path_when_titles_differ(self, pl_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            await _add_lib_track("C:/m/weird name.mp3", "Completely Different", "Who")
            pid = await _mk_playlist()
            await _add_row(pid, "Local Title", "", path="C:/m/weird name.mp3")
            await svc.refresh_local_matches(pid)
            row = await fetch_one(
                "SELECT track_id FROM playlist_tracks WHERE playlist_id=?", (pid,))
            assert row["track_id"] is not None
        finally:
            await close_db()

    async def test_ambiguous_title_only_stays_unmatched(self, pl_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            # Same title by two different artists -> title-only match must not fire.
            await _add_lib_track("C:/m/a1.mp3", "Boston", "Artist One")
            await _add_lib_track("C:/m/a2.mp3", "Boston", "Artist Two")
            pid = await _mk_playlist()
            await _add_row(pid, "Boston", "")
            out = await svc.refresh_local_matches(pid)
            assert out["matched"] == 0
            row = await fetch_one(
                "SELECT track_id FROM playlist_tracks WHERE playlist_id=?", (pid,))
            assert row["track_id"] is None
        finally:
            await close_db()

    async def test_missing_file_does_not_count_as_owned(self, pl_db):
        from backend.database import init_db, close_db, fetch_one, execute
        await init_db()
        try:
            await _add_lib_track("C:/m/gone.mp3", "Gone Song", "Ghost")
            await execute("UPDATE tracks SET file_status='missing' WHERE file_path='C:/m/gone.mp3'")
            pid = await _mk_playlist()
            await _add_row(pid, "Gone Song", "Ghost")
            out = await svc.refresh_local_matches(pid)
            # A missing file must not count as "in the library", otherwise
            # enqueue would skip a track the user cannot actually play.
            assert out["matched"] == 0
        finally:
            await close_db()


# --------------------------------------------------------------------------
# enqueue_missing
# --------------------------------------------------------------------------

class TestEnqueue:
    async def test_enqueues_unmatched_rows_via_source_priority(self, pl_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_all
        # Hermetic source priority regardless of the local settings.json.
        class _StubConfig:
            def get_all(self):
                return {"source_priority": ["youtube", "deezer", "soundcloud"]}
        monkeypatch.setattr(svc, "config", _StubConfig())
        await init_db()
        try:
            pid = await _mk_playlist()
            await _add_row(pid, "Lovefool", "The Cardigans")
            await _add_row(pid, "Boston", "Stella Lefty")
            out = await svc.enqueue_missing(pid)
            assert out["enqueued"] == 2
            jobs = await fetch_all("SELECT * FROM jobs")
            assert all(j["source_url"].startswith("ytsearch1:") for j in jobs)
        finally:
            await close_db()

    async def test_matched_rows_not_enqueued(self, pl_db):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            await _add_lib_track("C:/m/love.mp3", "Lovefool", "The Cardigans")
            pid = await _mk_playlist()
            await _add_row(pid, "Lovefool", "The Cardigans")
            await svc.refresh_local_matches(pid)
            out = await svc.enqueue_missing(pid)
            assert out["enqueued"] == 0
            assert await fetch_all("SELECT * FROM jobs") == []
        finally:
            await close_db()

    async def test_already_queued_not_duplicated(self, pl_db):
        from backend.database import init_db, close_db, fetch_all, execute
        await init_db()
        try:
            from backend.services.source_registry import build_search_job_url
            url = build_search_job_url("youtube", "The Cardigans", "Lovefool")
            await execute(
                "INSERT INTO jobs (source_url, source_type, query, status) VALUES (?, 'youtube', ?, 'pending')",
                (url, url),
            )
            pid = await _mk_playlist()
            await _add_row(pid, "Lovefool", "The Cardigans")
            out = await svc.enqueue_missing(pid)
            assert out["enqueued"] == 0
            assert out["already_queued"] == 1
            assert len(await fetch_all("SELECT * FROM jobs")) == 1
        finally:
            await close_db()

    async def test_limit_respected(self, pl_db):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            pid = await _mk_playlist()
            for i in range(5):
                await _add_row(pid, f"Song {i}", f"Artist {i}")
            out = await svc.enqueue_missing(pid, limit=2)
            assert out["enqueued"] == 2
            assert out["unmatched_total"] == 5
        finally:
            await close_db()


# --------------------------------------------------------------------------
# streaming_auth — the OAuth handshake machinery (network mocked out)
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def auth_config(monkeypatch):
    import backend.config as real_config
    base = {
        "spotify_client_id": "cid", "spotify_client_secret": "secret",
        "spotify_redirect_uri": "http://127.0.0.1:5555/api/connections/callback",
        "spotify_user_token": "",
        "soundcloud_client_id": "", "soundcloud_client_secret": "",
        "soundcloud_redirect_uri": "http://127.0.0.1:5555/api/connections/callback",
        "soundcloud_user_token": "",
    }
    class _Stub:
        def get(self, key, default=None):
            return base.get(key, default)
        def update(self, partial):
            base.update(partial)
    monkeypatch.setattr(streaming_auth, "config", _Stub())
    streaming_auth._pending.clear()
    return base


class TestAuthHandshake:
    def test_pkce_pair_is_valid(self):
        v, c = streaming_auth.make_pkce_pair()
        import base64, hashlib
        assert 43 <= len(v) <= 128
        expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert c == expect

    def test_begin_auth_builds_url_and_state(self):
        info = streaming_auth.begin_auth("spotify")
        assert "accounts.spotify.com/authorize" in info["auth_url"]
        assert "code_challenge_method=S256" in info["auth_url"]
        assert "playlist-modify" in info["auth_url"]
        assert info["state"] in streaming_auth._pending

    def test_begin_auth_requires_credentials(self, auth_config):
        with pytest.raises(ValueError):
            streaming_auth.begin_auth("soundcloud")   # no client id configured

    async def test_complete_auth_exchanges_and_stores(self, monkeypatch):
        info = streaming_auth.begin_auth("spotify")
        captured = {}

        async def fake_request(platform, form):
            captured["form"] = form
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

        monkeypatch.setattr(streaming_auth, "_token_request", fake_request)
        store = await streaming_auth.complete_auth("spotify", "code123", info["state"])
        assert store["access"] == "at" and store["refresh"] == "rt"
        assert streaming_auth.connected("spotify")
        assert captured["form"]["code"] == "code123"
        assert captured["form"]["grant_type"] == "authorization_code"
        # The state is consumed: the verifier can no longer be replayed.
        assert info["state"] not in streaming_auth._pending

    async def test_complete_auth_rejects_unknown_state(self, monkeypatch):
        async def fake_request(platform, form):
            return {}
        monkeypatch.setattr(streaming_auth, "_token_request", fake_request)
        with pytest.raises(ValueError):
            await streaming_auth.complete_auth("spotify", "code", "bogus-state")

    async def test_state_single_use(self, monkeypatch):
        info = streaming_auth.begin_auth("spotify")

        async def fake_request(platform, form):
            return {"access_token": "a", "refresh_token": "r", "expires_in": 60}
        monkeypatch.setattr(streaming_auth, "_token_request", fake_request)
        await streaming_auth.complete_auth("spotify", "code", info["state"])
        with pytest.raises(ValueError):
            await streaming_auth.complete_auth("spotify", "code2", info["state"])

    def test_platform_for_state(self, monkeypatch):
        monkeypatch.setattr(streaming_auth, "_creds", lambda p: ("cid", "sec", "uri"))
        info = streaming_auth.begin_auth("soundcloud")
        assert streaming_auth.platform_for_state(info["state"]) == "soundcloud"

    def test_clear_disconnects(self, monkeypatch):
        streaming_auth.config.update({"spotify_user_token": {"access": "x", "refresh": "y", "expiry": 1}})
        assert streaming_auth.connected("spotify")
        streaming_auth.clear("spotify")
        assert not streaming_auth.connected("spotify")


class TestAccessTokenRefresh:
    async def test_returns_cached_token_when_fresh(self):
        import time
        streaming_auth.config.update({
            "spotify_user_token": {"access": "live", "refresh": "r", "expiry": time.time() + 600}
        })
        assert await streaming_auth.get_access_token("spotify") == "live"

    async def test_refreshes_when_expired(self, monkeypatch):
        import time
        streaming_auth.config.update({
            "spotify_user_token": {"access": "dead", "refresh": "rtok", "expiry": time.time() - 10}
        })
        async def fake_request(platform, form):
            assert form["grant_type"] == "refresh_token"
            assert form["refresh_token"] == "rtok"
            return {"access_token": "fresh", "refresh_token": "rtok2", "expires_in": 3600}
        monkeypatch.setattr(streaming_auth, "_token_request", fake_request)
        assert await streaming_auth.get_access_token("spotify") == "fresh"

    async def test_raises_when_never_connected(self):
        with pytest.raises(ValueError):
            await streaming_auth.get_access_token("soundcloud")
