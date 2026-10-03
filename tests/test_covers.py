"""Tests for backend.services.covers - extraction, cache, fallbacks."""
from pathlib import Path

import pytest

from backend.services import covers as C


@pytest.fixture
def covers_db(monkeypatch, tmp_path):
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "covers.db")
    monkeypatch.setattr(cfg, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(C, "_HAVE_ART", set())
    monkeypatch.setattr(C, "_NO_ART", set())
    return cfg


def _mp3_with_art(path, blob=b"\xff\xd8\xff\xe0FAKEJPEGDATA"):
    """Create a file with a real embedded ID3v2 APIC picture."""
    from mutagen.id3 import ID3, APIC
    path.write_bytes(b"\x00" * 16)          # dummy "audio" payload
    tags = ID3()
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=blob))
    tags.save(str(path))
    return path


async def _seed_track(file_path, cover=""):
    from backend.database import execute, fetch_one
    await execute(
        "INSERT INTO tracks (file_path, title, artist, cover_art_path) VALUES (?, ?, ?, ?)",
        (str(file_path), "T", "A", cover),
    )
    row = await fetch_one("SELECT id FROM tracks ORDER BY id DESC LIMIT 1")
    return row["id"]


class TestExtractEmbedded:
    def test_mp3_apic(self, tmp_path):
        p = _mp3_with_art(tmp_path / "song.mp3")
        assert C.extract_embedded(str(p)) == b"\xff\xd8\xff\xe0FAKEJPEGDATA"

    def test_no_art_returns_none(self, tmp_path):
        p = tmp_path / "plain.mp3"
        p.write_bytes(b"\x00" * 64)
        assert C.extract_embedded(str(p)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert C.extract_embedded(str(tmp_path / "nope.mp3")) is None

    def test_prefers_largest_picture(self, tmp_path):
        from mutagen.id3 import ID3, APIC
        p = tmp_path / "multi.mp3"
        p.write_bytes(b"\x00" * 16)
        tags = ID3()
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="small", data=b"ab"))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="big", data=b"abcdef"))
        tags.save(str(p))
        assert C.extract_embedded(str(p)) == b"abcdef"


class TestSiblingCover:
    def test_finds_cover_jpg(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(b"jpg")
        assert C.find_sibling_cover(str(tmp_path / "song.mp3")) == str(tmp_path / "cover.jpg")

    def test_none_when_absent(self, tmp_path):
        assert C.find_sibling_cover(str(tmp_path / "song.mp3")) is None


class TestExtractCover:
    async def test_embedded_art_extracted_and_recorded(self, covers_db, tmp_path):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            audio = _mp3_with_art(tmp_path / "song.mp3")
            tid = await _seed_track(audio)
            path = await C.extract_cover(tid)
            assert path, "should find embedded art"
            p = Path(path)
            assert p.is_file()
            assert p.read_bytes() == b"\xff\xd8\xff\xe0FAKEJPEGDATA"
            row = await fetch_one("SELECT cover_art_path FROM tracks WHERE id=?", (tid,))
            assert row["cover_art_path"] == path
        finally:
            await close_db()

    async def test_sibling_cover_preferred_over_extraction(self, covers_db, tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            folder = tmp_path / "album"
            folder.mkdir()
            (folder / "cover.jpg").write_bytes(b"siblingjpg")
            audio = _mp3_with_art(folder / "song.mp3", blob=b"embedded")
            tid = await _seed_track(audio)
            path = await C.extract_cover(tid)
            assert path == str(folder / "cover.jpg"), "sibling beats extraction"
        finally:
            await close_db()

    async def test_artless_track_negative_cached(self, covers_db, tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            audio = tmp_path / "plain.mp3"
            audio.write_bytes(b"\x00" * 64)
            tid = await _seed_track(audio)
            assert await C.extract_cover(tid) == ""
            assert tid in C._NO_ART, "artless track must be negative-cached"
        finally:
            await close_db()

    async def test_second_call_uses_cache_no_reread(self, covers_db, tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            audio = _mp3_with_art(tmp_path / "song.mp3")
            tid = await _seed_track(audio)
            first = await C.extract_cover(tid)
            # Simulate the audio file disappearing: cache must still answer.
            audio.unlink()
            second = await C.extract_cover(tid)
            assert first == second
        finally:
            await close_db()

    async def test_unknown_track_returns_empty(self, covers_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            assert await C.extract_cover(9999) == ""
        finally:
            await close_db()


class TestCoverResponse:
    async def test_placeholder_fallback_for_artless(self, covers_db, tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            audio = tmp_path / "plain.mp3"
            audio.write_bytes(b"\x00" * 64)
            tid = await _seed_track(audio)
            resp = await C.cover_response(tid)
            assert resp.media_type == "image/svg+xml"
        finally:
            await close_db()

    async def test_real_art_served_when_present(self, covers_db, tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            audio = _mp3_with_art(tmp_path / "song.mp3")
            tid = await _seed_track(audio)
            await C.extract_cover(tid)
            resp = await C.cover_response(tid)
            assert resp.media_type == "image/jpeg"
        finally:
            await close_db()
