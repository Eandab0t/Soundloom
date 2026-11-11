"""Scanner must populate the artist-split columns like every other source.

A track credited "bunii, ayowitty" that arrives via a file scan has to land
under bunii's card, not in its own bucket — and a rescan must never clobber
a primary_artist that the identify/match pipeline already set.

read_tags is patched so these tests exercise the scanner's column logic, not
mutagen (the tagger has its own tests).
"""
import pytest

from backend import database as db
from backend.services import scanner
from backend.services.tagger import write_tags


@pytest.fixture()
async def lib_db(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.config.DB_PATH", tmp_path / "lib.db")
    if db._db is not None:
        await db._db.close()
    db._db = None
    await db.init_db()
    yield db
    await db.close_db()


def _file(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 64)
    return str(path)


def _patch_tags(monkeypatch, tags):
    full = {
        "title": "", "artist": "", "album": "", "album_artist": "",
        "track_number": 0, "disc_number": 0, "year": 0, "genre": "",
        "duration": 0, "format": "mp3", "bitrate": 0, "sample_rate": 0,
    }
    full.update(tags)

    async def fake_read_tags(path):
        return dict(full)
    monkeypatch.setattr(scanner, "read_tags", fake_read_tags)


async def test_multi_artist_credit_lands_under_primary(lib_db, tmp_path, monkeypatch):
    path = _file(tmp_path, "a.mp3")
    _patch_tags(monkeypatch, {
        "title": "T", "artist": "bunii, ayowitty", "album": "A",
        "album_artist": "bunii, ayowitty", "year": 2026, "genre": "Test",
        "track_number": 1, "format": "mp3",
    })

    await scanner.scan_single_file(path)

    row = await db.fetch_one("SELECT * FROM tracks WHERE file_path=?", (path,))
    assert row["artist"] == "bunii, ayowitty"          # raw tag untouched
    assert row["primary_artist"] == "bunii"
    assert row["featured_artists"] == "ayowitty"
    assert "bunii" in row["display_artist"]


async def test_single_artist_credit_sets_primary(lib_db, tmp_path, monkeypatch):
    path = _file(tmp_path, "b.mp3")
    _patch_tags(monkeypatch, {
        "title": "T", "artist": "bunii", "album": "A", "album_artist": "bunii",
        "track_number": 1, "format": "mp3",
    })

    await scanner.scan_single_file(path)

    row = await db.fetch_one("SELECT * FROM tracks WHERE file_path=?", (path,))
    assert row["primary_artist"] == "bunii"
    assert row["featured_artists"] == ""


async def test_rescan_never_clobbers_set_primary(lib_db, tmp_path, monkeypatch):
    """identify.py / the match pipeline may have set a better primary;
    rescanning the file must not undo that correction."""
    path = _file(tmp_path, "c.mp3")
    _patch_tags(monkeypatch, {
        "title": "T", "artist": "bunii, ayowitty", "album": "A",
        "album_artist": "bunii", "track_number": 1, "format": "mp3",
    })
    await scanner.scan_single_file(path)
    await db.execute(
        "UPDATE tracks SET primary_artist='The Real Bunii', "
        "featured_artists='someone', display_artist='The Real Bunii' "
        "WHERE file_path=?", (path,)
    )

    await scanner.scan_single_file(path)  # rescan the same file

    row = await db.fetch_one("SELECT * FROM tracks WHERE file_path=?", (path,))
    assert row["primary_artist"] == "The Real Bunii"
    assert row["featured_artists"] == "someone"


async def test_scan_folder_fills_empty_primary_on_rescan(lib_db, tmp_path, monkeypatch):
    """A pre-fix row with primary_artist='' is healed by the next scan,
    while a row with a set primary stays as-is."""
    _patch_tags(monkeypatch, {
        "title": "T", "artist": "bunii, ayowitty", "album": "A",
        "album_artist": "bunii", "track_number": 1, "format": "mp3",
    })
    # A file already in the DB with empty split columns.
    old = _file(tmp_path, "old.mp3")
    await db.execute(
        "INSERT INTO tracks (file_path, title, artist, album_artist, album,"
        " format, file_status) VALUES (?, 'T', 'bunii, ayowitty', 'bunii',"
        " 'A', 'mp3', 'present')", (old,)
    )
    # A sibling file whose primary was corrected.
    corrected = _file(tmp_path, "corr.mp3")
    await db.execute(
        "INSERT INTO tracks (file_path, title, artist, primary_artist,"
        " album_artist, album, format, file_status) VALUES (?, 'T',"
        " 'bunii, ayowitty', 'Corrected', 'bunii', 'A', 'mp3', 'present')",
        (corrected,)
    )

    await scanner.scan_folder(str(tmp_path))

    old_row = await db.fetch_one(
        "SELECT primary_artist FROM tracks WHERE file_path=?", (old,))
    assert old_row["primary_artist"] == "bunii"
    corr_row = await db.fetch_one(
        "SELECT primary_artist FROM tracks WHERE file_path=?", (corrected,))
    assert corr_row["primary_artist"] == "Corrected"


class TestBackfillMissingArtists:
    async def test_fills_empty_primary_only(self, lib_db):
        # Empty-primary comma credit: filled.
        await db.execute(
            "INSERT INTO tracks (file_path, title, artist, album_artist, album,"
            " format, file_status) VALUES ('a.mp3', 'T', 'bunii, ayowitty',"
            " 'bunii', 'A', 'mp3', 'present')"
        )
        # Set primary: untouched. Empty artist: skipped.
        await db.execute(
            "INSERT INTO tracks (file_path, title, artist, primary_artist,"
            " album_artist, album, format, file_status) VALUES ('b.mp3', 'T',"
            " 'bunii', 'Corrected', 'bunii', 'A', 'mp3', 'present')"
        )
        await db.execute(
            "INSERT INTO tracks (file_path, title, artist, album_artist, album,"
            " format, file_status) VALUES ('c.mp3', 'T', '', '', 'A', 'mp3',"
            " 'present')"
        )

        result = await scanner.backfill_missing_artists()

        assert result == {"examined": 1, "fixed": 1}
        row = await db.fetch_one(
            "SELECT primary_artist, featured_artists FROM tracks"
            " WHERE file_path='a.mp3'")
        assert row["primary_artist"] == "bunii"
        assert row["featured_artists"] == "ayowitty"
        kept = await db.fetch_one(
            "SELECT primary_artist FROM tracks WHERE file_path='b.mp3'")
        assert kept["primary_artist"] == "Corrected"

    async def test_repeated_runs_are_harmless(self, lib_db):
        await db.execute(
            "INSERT INTO tracks (file_path, title, artist, album_artist, album,"
            " format, file_status) VALUES ('a.mp3', 'T', 'bunii, ayowitty',"
            " 'bunii', 'A', 'mp3', 'present')"
        )
        first = await scanner.backfill_missing_artists()
        second = await scanner.backfill_missing_artists()
        assert first == {"examined": 1, "fixed": 1}
        assert second == {"examined": 0, "fixed": 0}
