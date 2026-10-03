"""Tests for backend.api.library — artist list + autocomplete search."""
import pytest


@pytest.fixture
def lib_db(monkeypatch, tmp_path):
    """Point the database at a throwaway file."""
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "lib.db")
    return cfg


async def _seed_artists():
    """Seed tracks for several artists, including casing variants."""
    from backend.database import execute
    rows = [
        ("/a1.mp3", "Song One", "Inductive", "Inductive"),
        ("/a2.mp3", "Song Two", "Inductive", "Inductive"),
        ("/a3.mp3", "Song Three", "Repeatedword", "Repeatedword"),
        ("/a4.mp3", "Song Four", "", "Soloist"),            # falls back to artist
        ("/a5.mp3", "Song Five", "The Beatles", "The Beatles"),
        ("/missing.mp3", "Gone", "vanished", "vanished"),
    ]
    for i, (path, title, artist, primary) in enumerate(rows, 1):
        status = "missing" if path == "/missing.mp3" else "present"
        await execute(
            "INSERT INTO tracks (file_path, title, artist, primary_artist, "
            "display_artist, file_status) VALUES (?, ?, ?, ?, ?, ?)",
            (path, title, artist, primary, primary or artist, status),
        )


class TestListArtists:
    async def test_no_search_returns_all_sorted(self, lib_db):
        from backend.database import init_db, close_db
        from backend.api.library import list_artists
        await init_db()
        try:
            await _seed_artists()
            artists = await list_artists(search="")
            names = [a["name"] for a in artists]
            assert "Inductive" in names and "Repeatedword" in names
            assert "vanished" not in names, "missing files must not list"
            assert names == sorted(names, key=str.lower), "plain list stays alphabetical"
        finally:
            await close_db()

    async def test_search_is_case_insensitive_substring(self, lib_db):
        from backend.database import init_db, close_db
        from backend.api.library import list_artists
        await init_db()
        try:
            await _seed_artists()
            artists = await list_artists(search="inoh")
            assert [a["name"] for a in artists] == ["Inductive"]
        finally:
            await close_db()

    async def test_search_orders_by_track_count(self, lib_db):
        from backend.database import init_db, close_db
        from backend.api.library import list_artists
        await init_db()
        try:
            # "i" matches Inductive (2 tracks), Soloist (1) and Repeatedword (1).
            await _seed_artists()
            artists = await list_artists(search="i")
            names = [a["name"] for a in artists]
            assert "Inductive" in names and "Repeatedword" in names
            assert names[0] == "Inductive", "most tracks first"
        finally:
            await close_db()

    async def test_search_empty_query_returns_all(self, lib_db):
        from backend.database import init_db, close_db
        from backend.api.library import list_artists
        await init_db()
        try:
            await _seed_artists()
            assert len(await list_artists(search="   ")) == 4
        finally:
            await close_db()

    async def test_search_no_match_returns_empty(self, lib_db):
        from backend.database import init_db, close_db
        from backend.api.library import list_artists
        await init_db()
        try:
            await _seed_artists()
            assert await list_artists(search="zzzz") == []
        finally:
            await close_db()
