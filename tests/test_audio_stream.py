"""Tests for GET /api/library/tracks/{id}/audio - the persistent player's source.

The endpoint hands the browser a path taken from the database, so it is the one
route where a bad row becomes a filesystem read. These tests pin both halves: it
serves what is inside the library, and it refuses everything else.
"""
from pathlib import Path

import pytest


@pytest.fixture
def audio_env(monkeypatch, tmp_path):
    """Throwaway DB + settings + library root, as in test_match_gate.gate_env."""
    import backend.config as cfg
    library = tmp_path / "library"
    (library / "Artist").mkdir(parents=True)
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "audio.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
    return {"cfg": cfg, "library": library, "tmp": tmp_path}


async def _seed(file_path, status="present"):
    from backend.database import execute, fetch_one
    await execute(
        "INSERT INTO tracks (file_path, title, artist, file_status) "
        "VALUES (?, ?, ?, ?)",
        (str(file_path), "Title", "Artist", status),
    )
    row = await fetch_one("SELECT id FROM tracks ORDER BY id DESC LIMIT 1")
    return row["id"]


class TestServesLibraryAudio:
    async def test_serves_a_file_inside_the_library(self, audio_env):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        track = audio_env["library"] / "Artist" / "song.mp3"
        track.write_bytes(b"\x00" * 64)
        await init_db()
        try:
            tid = await _seed(track)
            resp = await stream_track_audio(tid)
            assert Path(resp.path) == track.resolve()
            assert resp.media_type == "audio/mpeg"
        finally:
            await close_db()

    async def test_media_type_follows_the_extension(self, audio_env):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        wanted = {".flac": "audio/flac", ".m4a": "audio/mp4",
                  ".opus": "audio/ogg", ".wav": "audio/wav"}
        await init_db()
        try:
            for ext, media in wanted.items():
                f = audio_env["library"] / f"t{ext}"
                f.write_bytes(b"\x00" * 16)
                tid = await _seed(f)
                resp = await stream_track_audio(tid)
                assert resp.media_type == media, ext
        finally:
            await close_db()

    async def test_file_response_supports_seeking(self, audio_env):
        """A whole-file response cannot be scrubbed; FileResponse handles Range."""
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        f = audio_env["library"] / "song.mp3"
        f.write_bytes(b"\x00" * 4096)
        await init_db()
        try:
            tid = await _seed(f)
            resp = await stream_track_audio(tid)
            assert resp.status_code == 200
        finally:
            await close_db()


class TestRejects:
    async def test_unknown_track_is_404(self, audio_env):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        from fastapi import HTTPException
        await init_db()
        try:
            with pytest.raises(HTTPException) as exc:
                await stream_track_audio(999999)
            assert exc.value.status_code == 404
        finally:
            await close_db()

    async def test_row_marked_missing_is_404(self, audio_env):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        from fastapi import HTTPException
        f = audio_env["library"] / "gone.mp3"
        f.write_bytes(b"\x00" * 16)          # on disk, but the row says missing
        await init_db()
        try:
            tid = await _seed(f, status="missing")
            with pytest.raises(HTTPException) as exc:
                await stream_track_audio(tid)
            assert exc.value.status_code == 404
        finally:
            await close_db()

    async def test_deleted_from_disk_is_404(self, audio_env):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        from fastapi import HTTPException
        await init_db()
        try:
            tid = await _seed(audio_env["library"] / "never-existed.mp3")
            with pytest.raises(HTTPException) as exc:
                await stream_track_audio(tid)
            assert exc.value.status_code == 404
        finally:
            await close_db()

    async def test_empty_path_is_404(self, audio_env):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        from fastapi import HTTPException
        await init_db()
        try:
            tid = await _seed("")
            with pytest.raises(HTTPException) as exc:
                await stream_track_audio(tid)
            assert exc.value.status_code == 404
        finally:
            await close_db()


class TestLibraryRootGuard:
    """A row is not a capability. Only files under library_path are served."""

    async def _expect_403(self, audio_env, path):
        from backend.database import init_db, close_db
        from backend.api.library import stream_track_audio
        from fastapi import HTTPException
        await init_db()
        try:
            tid = await _seed(path)
            with pytest.raises(HTTPException) as exc:
                await stream_track_audio(tid)
            assert exc.value.status_code == 403
        finally:
            await close_db()

    async def test_file_outside_the_library_is_403(self, audio_env):
        secret = audio_env["tmp"] / "secrets.txt"
        secret.write_text("top secret", encoding="utf-8")
        await self._expect_403(audio_env, secret)

    async def test_parent_traversal_is_403(self, audio_env):
        """The attack a path column invites: walk out with ..."""
        secret = audio_env["tmp"] / "secrets.txt"
        secret.write_text("top secret", encoding="utf-8")
        sneaky = audio_env["library"] / "Artist" / ".." / ".." / "secrets.txt"
        assert sneaky.resolve() == secret.resolve()
        await self._expect_403(audio_env, sneaky)

    async def test_sibling_directory_sharing_a_prefix_is_403(self, audio_env):
        """/music-backup must not pass a startswith-style check against /music."""
        sibling = audio_env["tmp"] / "library-backup"
        sibling.mkdir()
        leak = sibling / "song.mp3"
        leak.write_bytes(b"\x00" * 16)
        await self._expect_403(audio_env, leak)

    async def test_missing_library_setting_is_403(self, audio_env, monkeypatch):
        monkeypatch.setattr(audio_env["cfg"], "_cache", {**audio_env["cfg"].DEFAULTS})
        f = audio_env["library"] / "song.mp3"
        f.write_bytes(b"\x00" * 16)
        await self._expect_403(audio_env, f)
