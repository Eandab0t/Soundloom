"""Tests for the duplicate-artist scan/fix endpoints."""
import pytest

from backend.api import autofix as A


@pytest.fixture
def dup_db(monkeypatch, tmp_path):
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "dup.db")
    return cfg


def _dedupe(val):
    return A._dedupe_artist_string(val)


class TestDedupeArtistString:
    def test_repeated_part(self):
        assert _dedupe("Repeatedword, Repeatedword, Repeatedword") == "Repeatedword"

    def test_repeated_words_within_part(self):
        assert _dedupe("plaxz, Repeatedword, plaxz, Repeatedword") == "plaxz, Repeatedword"

    def test_case_insensitive_parts(self):
        assert _dedupe("Alpha, alpha") == "Alpha"

    def test_clean_value_unchanged(self):
        assert _dedupe("Alpha, Beta") is None
        assert _dedupe("Solo Artist") is None
        assert _dedupe("") is None

    def test_preserves_order_and_casing(self):
        assert _dedupe("Zeta, alpha, Zeta") == "Zeta, alpha"

    def test_inner_word_dedup(self):
        assert _dedupe("beidant beidant, other") == "beidant, other"


async def _seed(file_path, artist, album_artist=""):
    from backend.database import execute, fetch_one
    await execute(
        "INSERT INTO tracks (file_path, title, artist, album_artist, "
        "primary_artist, display_artist, file_status) VALUES (?, ?, ?, ?, ?, ?, 'present')",
        (file_path, "T", artist, album_artist, artist.split(",")[0], artist),
    )
    row = await fetch_one("SELECT id FROM tracks ORDER BY id DESC LIMIT 1")
    return row["id"]


class TestScanDuplicateArtists:
    async def test_finds_corruption_grouped_by_value(self, dup_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            await _seed("/t1.mp3", "Repeatedword, Repeatedword, Repeatedword", "Repeatedword, Repeatedword, Repeatedword")
            await _seed("/t2.mp3", "Repeatedword, Repeatedword, Repeatedword")
            await _seed("/t3.mp3", "Alpha, Beta")  # clean
            result = await A.scan_duplicate_artists()
            assert result["variants"] == 2  # artist + album_artist values
            assert result["tracks_affected"] == 3  # 2 artist fields + 1 album_artist
            top = result["groups"][0]
            assert top["current_value"] == "Repeatedword, Repeatedword, Repeatedword"
            assert top["fixed_value"] == "Repeatedword"
            assert set(top["track_ids"]) == {1, 2}
        finally:
            await close_db()

    async def test_empty_library_is_clean(self, dup_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            result = await A.scan_duplicate_artists()
            assert result["groups"] == [] and result["tracks_affected"] == 0
        finally:
            await close_db()


class TestFixDuplicateArtists:
    async def test_fixes_db_rows_and_records_changes(self, dup_db, monkeypatch, tmp_path):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            tid = await _seed("/gone/t1.mp3", "Repeatedword, Repeatedword, Repeatedword",
                              "Repeatedword, Repeatedword, Repeatedword")
            result = await A.fix_duplicate_artists()
            assert result["tracks_fixed"] == 1
            assert result["backup_path"], "backup must be written"
            from pathlib import Path
            assert Path(result["backup_path"]).is_file()

            row = await fetch_one("SELECT * FROM tracks WHERE id=?", (tid,))
            assert row["artist"] == "Repeatedword"
            assert row["album_artist"] == "Repeatedword"
            assert row["primary_artist"] == "Repeatedword"
            assert row["display_artist"] == "Repeatedword"

            change = result["changes"][0]
            assert change["artist"] == ["Repeatedword, Repeatedword, Repeatedword", "Repeatedword"]
            assert change["album_artist"] == ["Repeatedword, Repeatedword, Repeatedword", "Repeatedword"]
        finally:
            await close_db()

    async def test_backup_captures_pre_fix_values(self, dup_db):
        import json
        from pathlib import Path
        from backend.database import init_db, close_db
        await init_db()
        try:
            await _seed("/gone/t1.mp3", "A, A")
            result = await A.fix_duplicate_artists()
            backup = json.loads(Path(result["backup_path"]).read_text(encoding="utf-8"))
            rec = backup["records"][0]
            assert rec["artist"] == "A, A", "backup must hold the PRE-fix values"
        finally:
            await close_db()

    async def test_nothing_to_fix_is_a_noop(self, dup_db):
        from backend.database import init_db, close_db
        await init_db()
        try:
            await _seed("/x.mp3", "Alpha, Beta")
            result = await A.fix_duplicate_artists()
            assert result["tracks_fixed"] == 0 and result["backup_path"] == ""
        finally:
            await close_db()

    async def test_group_filter_limits_the_write(self, dup_db):
        from backend.database import init_db, close_db, fetch_all
        await init_db()
        try:
            await _seed("/t1.mp3", "A, A")
            await _seed("/t2.mp3", "B, B")
            req = A.FixDuplicatesRequest(groups=[{"field": "artist", "current_value": "A, A"}])
            result = await A.fix_duplicate_artists(req)
            assert result["tracks_fixed"] == 1
            rows = await fetch_all("SELECT artist FROM tracks ORDER BY id")
            assert rows[0]["artist"] == "A"
            assert rows[1]["artist"] == "B, B", "unselected group must stay untouched"
        finally:
            await close_db()

    async def test_writes_real_file_tags_when_present(self, dup_db, tmp_path):
        from mutagen.id3 import ID3, TPE1
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            # Valid MPEG-1 Layer III frames (417 B each) so mutagen parses the
            # file; TPE1 is injected at the front by tags.save().
            frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
            audio = tmp_path / "song.mp3"
            audio.write_bytes(frame * 30)
            tags = ID3()
            tags.add(TPE1(encoding=3, text=["Repeatedword, Repeatedword"]))
            tags.save(str(audio))
            tid = await _seed(str(audio), "Repeatedword, Repeatedword")

            result = await A.fix_duplicate_artists()
            change = next(c for c in result["changes"] if c["track_id"] == tid)
            assert change["file"] == "fixed"

            reread = ID3(str(audio))
            assert str(reread["TPE1"].text[0]) == "Repeatedword"
            row = await fetch_one("SELECT artist FROM tracks WHERE id=?", (tid,))
            assert row["artist"] == "Repeatedword"
        finally:
            await close_db()
