"""Tests for backend.api.playlists routes and the OAuth connection endpoints."""
import json

import pytest

from backend.services import streaming_auth


@pytest.fixture
def pl_db(monkeypatch, tmp_path):
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "playlists.db")
    monkeypatch.setattr(cfg, "EXPORTS_DIR", tmp_path / "exports")
    return cfg


@pytest.fixture(autouse=True)
def hermetic_auth(monkeypatch):
    """Keep the auth module away from the real settings.json."""
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


async def _create(name="Mix"):
    from backend.api.playlists import create_playlist, CreateBody
    return await create_playlist(CreateBody(name=name))


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

class TestCrud:
    async def test_create_and_list(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import list_playlists
        await init_db()
        try:
            await _create("Road")
            await _create("Trip")
            data = await list_playlists()
            names = [p["name"] for p in data["playlists"]]
            assert names == ["Trip", "Road"], "most recently updated first"
            assert data["playlists"][0]["track_count"] == 0
        finally:
            await close_db()

    async def test_create_requires_name(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import create_playlist, CreateBody
        from fastapi import HTTPException
        await init_db()
        try:
            with pytest.raises(HTTPException) as err:
                await create_playlist(CreateBody(name="   "))
            assert err.value.status_code == 400
        finally:
            await close_db()

    async def test_get_includes_tracks_ordered(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import get_playlist
        await init_db()
        try:
            info = await _create("Mix")
            from backend.database import execute
            for i, (t, a) in enumerate([("Second", "B"), ("First", "A")], 1):
                await execute(
                    "INSERT INTO playlist_tracks (playlist_id, position, title, artist) "
                    "VALUES (?, ?, ?, ?)", (info["id"], i, t, a))
            data = await get_playlist(info["id"])
            assert [t["title"] for t in data["tracks"]] == ["Second", "First"]
        finally:
            await close_db()

    async def test_get_missing_404(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import get_playlist
        from fastapi import HTTPException
        await init_db()
        try:
            with pytest.raises(HTTPException) as err:
                await get_playlist(4242)
            assert err.value.status_code == 404
        finally:
            await close_db()

    async def test_delete_playlist_and_rows(self, pl_db):
        from backend.database import init_db, close_db, fetch_all
        from backend.api.playlists import delete_playlist, add_track, AddTrackBody
        await init_db()
        try:
            info = await _create("Gone")
            await add_track(info["id"], AddTrackBody(title="T", artist="A"))
            await delete_playlist(info["id"])
            assert await fetch_all("SELECT * FROM playlists") == []
            assert await fetch_all("SELECT * FROM playlist_tracks") == []
        finally:
            await close_db()

    async def test_add_track_positions_sequentially(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import add_track, AddTrackBody
        await init_db()
        try:
            info = await _create("Mix")
            r1 = await add_track(info["id"], AddTrackBody(title="One"))
            r2 = await add_track(info["id"], AddTrackBody(title="Two"))
            assert (r1["position"], r2["position"]) == (1, 2)
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Import file (iTunes XML / M3U / CSV via raw-body upload)
# --------------------------------------------------------------------------

def _fake_request(raw: bytes, filename: str = "playlist.csv"):
    class _Req:
        headers = {"X-Playlist-Filename": filename}
        async def body(self):
            return raw
    return _Req()


class TestImportFile:
    async def test_csv_import(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import import_file
        await init_db()
        try:
            info = await _create("From CSV")
            raw = b"title,artist,duration\nLovefool,The Cardigans,191\n"
            r = await import_file(info["id"], _fake_request(raw, "songs.csv"))
            assert r["added"] == 1
            assert r["matched"] == 0
            assert r["kind"] == "csv"
        finally:
            await close_db()

    async def test_kind_autosniff_from_content(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import import_file
        await init_db()
        try:
            info = await _create("Sniff")
            raw = b"#EXTM3U\n#EXTINF:100,A - T\nC:/m/a.mp3\n"
            r = await import_file(info["id"], _fake_request(raw, "no-extension"))
            assert r["kind"] == "m3u"
            assert r["added"] == 1
        finally:
            await close_db()

    async def test_bad_xml_422(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import import_file
        from fastapi import HTTPException
        await init_db()
        try:
            info = await _create("Bad")
            with pytest.raises(HTTPException) as err:
                await import_file(info["id"], _fake_request(b"<not-xml", "lib.xml"))
            assert err.value.status_code == 422
        finally:
            await close_db()

    async def test_unknown_playlist_404(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import import_file
        from fastapi import HTTPException
        await init_db()
        try:
            with pytest.raises(HTTPException) as err:
                await import_file(999, _fake_request(b"title\nX\n", "x.csv"))
            assert err.value.status_code == 404
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Export: file targets write real files; platform targets surface auth errors
# --------------------------------------------------------------------------

class TestExportFiles:
    async def _playlist_with_rows(self):
        from backend.database import execute
        info = await _create("Export Me")
        for i, (t, a) in enumerate([("Lovefool", "The Cardigans"), ("Boston", "Stella Lefty")], 1):
            await execute(
                "INSERT INTO playlist_tracks (playlist_id, position, title, artist, duration) "
                "VALUES (?, ?, ?, ?, ?)", (info["id"], i, t, a, 200.0))
        return info["id"]

    async def test_m3u_export(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import export_playlist, ExportBody
        await init_db()
        try:
            pid = await self._playlist_with_rows()
            r = await export_playlist(pid, ExportBody(target="m3u"))
            assert r["tracks"] == 2
            text = open(r["path"], encoding="utf-8").read()
            assert text.startswith("#EXTM3U")
            assert "The Cardigans - Lovefool" in text
        finally:
            await close_db()

    async def test_itunes_xml_export(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import export_playlist, ExportBody
        await init_db()
        try:
            pid = await self._playlist_with_rows()
            r = await export_playlist(pid, ExportBody(target="itunes_xml"))
            text = open(r["path"], encoding="utf-8").read()
            assert "<key>Name</key><string>Lovefool</string>" in text
            assert "<key>Name</key><string>Export Me</string>" in text
        finally:
            await close_db()

    async def test_csv_and_json_export(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import export_playlist, ExportBody
        await init_db()
        try:
            pid = await self._playlist_with_rows()
            r = await export_playlist(pid, ExportBody(target="csv"))
            assert "Lovefool" in open(r["path"], encoding="utf-8-sig").read()
            r = await export_playlist(pid, ExportBody(target="json"))
            data = json.loads(open(r["path"], encoding="utf-8").read())
            assert len(data["tracks"]) == 2
        finally:
            await close_db()

    async def test_unknown_target_400(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import export_playlist, ExportBody
        from fastapi import HTTPException
        await init_db()
        try:
            pid = await self._playlist_with_rows()
            with pytest.raises(HTTPException) as err:
                await export_playlist(pid, ExportBody(target="zune"))
            assert err.value.status_code == 400
        finally:
            await close_db()


class TestExportPlatforms:
    async def _playlist_with_row(self):
        from backend.database import execute
        info = await _create("Mix")
        await execute(
            "INSERT INTO playlist_tracks (playlist_id, position, title, artist) VALUES (1, 1, 'T', 'A')"
        )
        return info["id"]

    async def test_spotify_not_connected_is_401(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import export_playlist, ExportBody
        from fastapi import HTTPException
        await init_db()
        try:
            pid = await self._playlist_with_row()
            with pytest.raises(HTTPException) as err:
                await export_playlist(pid, ExportBody(target="spotify"))
            assert err.value.status_code == 401
        finally:
            await close_db()

    async def test_soundcloud_not_connected_is_401(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import export_playlist, ExportBody
        from fastapi import HTTPException
        await init_db()
        try:
            pid = await self._playlist_with_row()
            with pytest.raises(HTTPException) as err:
                await export_playlist(pid, ExportBody(target="soundcloud"))
            assert err.value.status_code == 401
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Connections endpoints
# --------------------------------------------------------------------------

class TestConnections:
    async def test_status_reports_configured_and_connected(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import connection_status
        await init_db()
        try:
            status = await connection_status()
            assert status["spotify"]["configured"] is True
            assert status["spotify"]["connected"] is False
            assert status["soundcloud"]["configured"] is False
        finally:
            await close_db()

    async def test_connect_starts_handshake(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import connect_start, ConnectBody
        await init_db()
        try:
            info = await connect_start(ConnectBody(platform="spotify"))
            assert "accounts.spotify.com/authorize" in info["auth_url"]
        finally:
            await close_db()

    async def test_connect_requires_credentials(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import connect_start, ConnectBody
        from fastapi import HTTPException
        await init_db()
        try:
            with pytest.raises(HTTPException) as err:
                await connect_start(ConnectBody(platform="soundcloud"))
            assert err.value.status_code == 400
        finally:
            await close_db()

    async def test_callback_exchange(self, pl_db, monkeypatch):
        from backend.database import init_db, close_db
        from backend.api.playlists import connect_start, ConnectBody, callback_exchange, CallbackBody
        await init_db()
        try:
            info = await connect_start(ConnectBody(platform="spotify"))

            async def fake_request(platform, form):
                return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
            monkeypatch.setattr(streaming_auth, "_token_request", fake_request)

            out = await callback_exchange(CallbackBody(code="c", state=info["state"]))
            assert out["platform"] == "spotify"
            assert streaming_auth.connected("spotify")
        finally:
            await close_db()

    async def test_callback_bad_state_400(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import callback_exchange, CallbackBody
        from fastapi import HTTPException
        await init_db()
        try:
            with pytest.raises(HTTPException) as err:
                await callback_exchange(CallbackBody(code="c", state="junk"))
            assert err.value.status_code == 400
        finally:
            await close_db()

    async def test_disconnect(self, pl_db):
        from backend.database import init_db, close_db
        from backend.api.playlists import disconnect
        await init_db()
        try:
            streaming_auth.config.update({
                "spotify_user_token": {"access": "a", "refresh": "r", "expiry": 9e12}
            })
            out = await disconnect("spotify")
            assert out["status"] == "disconnected"
            assert not streaming_auth.connected("spotify")
        finally:
            await close_db()
