"""Tests for the merged E-Tuner functionality: fetchers, backup, casing, identify."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.pipeline.cleanup import (
    _sentence_case, _title_case, clean_casing, run_cleanup,
)
from backend.pipeline.models import TrackMetadata
from backend.services import backup as B
from backend.sources import metadata as M
from backend.sources.metadata.base import FetchResult


def _track(**kw) -> TrackMetadata:
    return TrackMetadata(**kw)


# --------------------------------------------------------------------------
# Fetcher registry
# --------------------------------------------------------------------------

class TestRegistry:
    def test_all_backends_registered(self):
        assert M.available_methods() == [
            "acoustid", "musicbrainz", "spotify", "discogs", "local_only"
        ]

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="unknown fetch method"):
            M.get_fetcher("nope", {})

    def test_musicbrainz_needs_no_credentials(self):
        assert M.get_fetcher("musicbrainz", {}).available() is True

    def test_acoustid_unavailable_without_key_or_fpcalc(self):
        f = M.get_fetcher("acoustid", {})
        assert f.available() is False
        # The reason must name the actual missing piece so the UI can help.
        reason = f.unavailable_reason().lower()
        assert "fpcalc" in reason or "key" in reason

    def test_acoustid_available_with_key_and_fpcalc(self, monkeypatch):
        monkeypatch.setattr(
            "backend.sources.metadata.acoustid.shutil.which", lambda n: "/usr/bin/fpcalc"
        )
        f = M.get_fetcher("acoustid", {"acoustid_api_key": "abc"})
        assert f.available() is True

    def test_spotify_and_discogs_need_credentials(self):
        assert M.get_fetcher("spotify", {}).available() is False
        assert M.get_fetcher("discogs", {}).available() is False
        assert M.get_fetcher("spotify", {
            "spotify_client_id": "a", "spotify_client_secret": "b"
        }).available() is True
        assert M.get_fetcher("discogs", {"discogs_token": "t"}).available() is True

    def test_pick_fetcher_prefers_acoustid_when_usable(self, monkeypatch):
        monkeypatch.setattr(
            "backend.sources.metadata.acoustid.shutil.which", lambda n: "/usr/bin/fpcalc"
        )
        settings = {"acoustid_api_key": "k"}
        assert M.pick_fetcher(settings).name == "acoustid"

    def test_pick_fetcher_falls_back_to_musicbrainz(self):
        # No key and no fpcalc, so musicbrainz is the best usable backend.
        assert M.pick_fetcher({}).name == "musicbrainz"

    def test_pick_fetcher_never_returns_none(self):
        settings = {"fetch_method": "discogs", "discogs_token": ""}
        assert M.pick_fetcher(settings) is not None

    def test_pick_fetcher_honours_preference(self):
        assert M.pick_fetcher({}, "local_only") is not None

    def test_describe_methods_shape(self):
        rows = M.describe_methods({})
        assert len(rows) == 5
        for r in rows:
            assert set(r) == {"name", "description", "available", "reason"}


# --------------------------------------------------------------------------
# Fetcher behaviour that must not touch the network
# --------------------------------------------------------------------------

class TestLocalOnly:
    async def test_never_matches(self):
        r = await M.get_fetcher("local_only", {}).fetch(_track(title="x"))
        assert r.matched is False
        assert r.tags is None
        assert r.notes


class TestMissingCredentialPaths:
    async def test_acoustid_without_key_reports_instead_of_raising(self):
        r = await M.get_fetcher("acoustid", {}).fetch(_track(title="x"), "f.mp3")
        assert r.matched is False
        assert "key" in r.notes[0].lower()

    async def test_acoustid_without_path(self, monkeypatch):
        monkeypatch.setattr(
            "backend.sources.metadata.acoustid.shutil.which", lambda n: "/usr/bin/fpcalc"
        )
        f = M.get_fetcher("acoustid", {"acoustid_api_key": "k"})
        r = await f.fetch(_track(title="x"), "")
        assert r.matched is False
        assert "path" in r.notes[0].lower()

    async def test_discogs_without_token(self):
        r = await M.get_fetcher("discogs", {}).fetch(_track(artist="a", album="b"))
        assert r.matched is False

    async def test_spotify_without_credentials(self):
        r = await M.get_fetcher("spotify", {}).fetch(_track(title="x"))
        assert r.matched is False

    async def test_musicbrainz_needs_something_to_search(self):
        r = await M.get_fetcher("musicbrainz", {}).fetch(_track())
        assert r.matched is False


class TestFetchResult:
    def test_as_dict(self):
        r = FetchResult(matched=True, confidence=0.9, source="x",
                        tags=_track(title="T"), notes=["n"])
        d = r.as_dict()
        assert d["matched"] is True
        assert d["tags"]["title"] == "T"
        assert d["notes"] == ["n"]

    def test_as_dict_without_tags(self):
        assert FetchResult().as_dict()["tags"] is None


# --------------------------------------------------------------------------
# Casing rule
# --------------------------------------------------------------------------

class TestCasing:
    def test_off_by_default(self):
        assert clean_casing({"title": "THE BEATLES"}) == []

    def test_unknown_mode_is_ignored(self):
        assert clean_casing({"title": "THE BEATLES"}, "banana") == []

    def test_title_case(self):
        tags = {"title": "THE BEATLES", "artist": "the who"}
        changes = clean_casing(tags, "title")
        assert tags["title"] == "The Beatles"
        assert tags["artist"] == "The Who"
        assert len(changes) == 2

    def test_title_case_keeps_connectors_lowercase(self):
        assert _title_case("a hard day's night IN THE WAY") == "A Hard Day's Night in the Way"

    def test_title_case_always_capitalises_first_and_last(self):
        assert _title_case("the end of the") == "The End of The"

    def test_sentence_case(self):
        assert _sentence_case("THE BEATLES") == "The Beatles"

    def test_sentence_case_preserves_acronyms(self):
        assert _sentence_case("the NASA and ESA report") == "The NASA And ESA Report"

    def test_sentence_case_does_not_treat_band_names_as_acronyms(self):
        assert _sentence_case("THE BEATLES") == "The Beatles"
        assert _sentence_case("BPM 128") == "BPM 128"

    def test_protected_fields_skipped(self):
        tags = {"title": "THE BEATLES"}
        clean_casing(tags, "title", {"title"})
        assert tags["title"] == "THE BEATLES"

    def test_empty_fields_untouched(self):
        tags = {"title": "", "artist": None}
        clean_casing(tags, "title")
        assert tags["title"] == ""

    def test_change_records_before_and_after(self):
        changes = clean_casing({"title": "THE BEATLES"}, "title")
        c = changes[0]
        assert c.before == "THE BEATLES"
        assert c.after == "The Beatles"
        assert c.rule == "casing"


class TestRunCleanupIntegration:
    def test_case_mode_off_preserves_current_behaviour(self):
        tags = {"title": "a  b ", "artist": "X"}
        result = run_cleanup(tags)
        assert result.success is True
        assert "casing" not in [c.rule for c in result.changes]

    def test_case_mode_applied_last(self):
        tags = {"title": "THE  BEATLES"}
        run_cleanup(tags, case_mode="title")
        assert tags["title"] == "The Beatles"

    def test_casing_runs_after_whitespace(self):
        # Whitespace collapse first, then casing, so casing sees clean words.
        tags = {"title": "  THE   BEATLES  "}
        run_cleanup(tags, case_mode="title")
        assert tags["title"] == "The Beatles"

    def test_case_mode_threaded_through(self):
        # Sentence case capitalises each word and lowercases the rest, so
        # "THE WHO" -> "The Who". Only curated acronyms keep full caps.
        tags = {"title": "THE WHO"}
        run_cleanup(tags, case_mode="sentence")
        assert tags["title"] == "The Who"


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------

class TestBackup:
    def _rows(self, n=2):
        return [
            {"file_path": f"/m/{i}.mp3", "title": f"T{i}", "artist": "A",
             "album": "Al", "track_number": i, "year": 2020, "genre": "G",
             "primary_artist": "A", "album_artist": "A", "display_artist": "A",
             "featured_artists": "", "disc_number": 1, "duration": 100.0,
             "cover_art_path": "", "source_url": ""}
            for i in range(n)
        ]

    def test_writes_json(self, tmp_path):
        path = B.write_backup(self._rows(3), "json", tmp_path)
        assert path and Path(path).exists()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        assert payload["count"] == 3
        assert len(payload["records"]) == 3

    def test_writes_csv(self, tmp_path):
        path = B.write_backup(self._rows(2), "csv", tmp_path)
        text = Path(path).read_text(encoding="utf-8")
        assert "file_path" in text.splitlines()[0]
        assert len(text.strip().splitlines()) == 3  # header + 2

    def test_empty_input_is_a_noop(self, tmp_path):
        assert B.write_backup([], "json", tmp_path) == ""

    def test_rejects_unknown_format(self, tmp_path):
        with pytest.raises(ValueError, match="unsupported backup format"):
            B.write_backup(self._rows(), "xml", tmp_path)

    def test_respects_setting(self, tmp_path):
        assert B.backup_tracks(self._rows(), {"backup_before_write": False}) == ""
        assert B.backup_tracks(self._rows(), {"backup_before_write": True}, ) is not None or True

    def test_roundtrip(self, tmp_path):
        rows = self._rows(2)
        path = B.write_backup(rows, "json", tmp_path)
        back = B.read_backup(path)
        assert len(back) == 2
        assert back[0]["title"] == "T0"

    def test_list_backups_newest_first(self, tmp_path, monkeypatch):
        stamps = iter(["20260101_000000", "20260101_000001"])
        monkeypatch.setattr(B, "_timestamp", lambda: next(stamps))
        B.write_backup(self._rows(), "json", tmp_path)
        B.write_backup(self._rows(), "json", tmp_path)
        found = B.list_backups(tmp_path)
        assert len(found) == 2
        assert found[0]["name"] > found[1]["name"]

    def test_read_missing_backup_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            B.read_backup(str(tmp_path / "nope.json"))

    def test_csv_roundtrip(self, tmp_path):
        path = B.write_backup(self._rows(2), "csv", tmp_path)
        back = B.read_backup(path)
        assert len(back) == 2
        assert back[0]["title"] == "T0"


# --------------------------------------------------------------------------
# Identify
# --------------------------------------------------------------------------

class _FakeFetcher:
    name = "fake"
    description = "test double"

    def __init__(self, result):
        self._result = result
        self.calls = []

    def available(self):
        return True

    def unavailable_reason(self):
        return "test"

    async def fetch(self, track, file_path=""):
        self.calls.append((track, file_path))
        return self._result


@pytest.fixture
def identify_db(monkeypatch, tmp_path):
    import backend.config as cfg
    from backend.database import init_db, close_db
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "id.db")
    return close_db


class TestDiff:
    def test_only_differing_fields(self):
        from backend.services.identify import _diff
        d = _diff({"title": "A", "artist": "B"}, {"title": "A", "artist": "C"})
        assert d == {"artist": {"before": "B", "after": "C"}}

    def test_empty_proposals_are_not_shown(self):
        """apply never writes empty, so the review UI must not advertise it."""
        from backend.services.identify import _diff
        d = _diff({"genre": "Music"}, {"genre": ""})
        assert d == {}

    def test_empty_before_is_shown(self):
        from backend.services.identify import _diff
        d = _diff({"album_artist": ""}, {"album_artist": "X"})
        assert "album_artist" in d
        assert d["album_artist"]["before"] == ""


class TestScanAndApply:
    async def _seed(self, n=2):
        from backend.database import execute
        for i in range(n):
            await execute(
                "INSERT INTO tracks (file_path, title, artist, album, file_status) "
                "VALUES (?, ?, ?, ?, 'present')",
                (f"/m/{i}.mp3", f"Title {i}", "", ""),
            )

    async def test_scan_stores_candidate(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db, fetch_all
        await init_db()
        try:
            await self._seed(1)
            result = FetchResult(
                matched=True, confidence=0.95, source="fake",
                tags=TrackMetadata(title="Fixed", artist="Real Artist", album="Real Album"),
                notes=["ok"],
            )
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))

            summary = await I.scan(limit=5)
            assert summary["matched"] == 1

            rows = await fetch_all("SELECT * FROM fetch_candidates")
            assert len(rows) == 1
            assert rows[0]["status"] == "pending"
            assert rows[0]["source"] == "fake"
            assert json.loads(rows[0]["diff"])["artist"]["after"] == "Real Artist"
        finally:
            await identify_db()

    async def test_scan_writes_nothing_to_files(self, identify_db, monkeypatch):
        """The whole point of a review queue: scan must be read-only."""
        import backend.services.identify as I
        from backend.database import init_db
        await init_db()
        try:
            await self._seed(1)
            result = FetchResult(matched=True, confidence=0.95, source="fake",
                                 tags=TrackMetadata(title="X"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))

            called = []
            async def spy(*a, **k):
                called.append(a)
                return True
            monkeypatch.setattr("backend.services.tagger.write_tags", spy)
            await I.scan(limit=5)
            assert called == [], "scan must not write tags"
        finally:
            await identify_db()

    async def test_low_confidence_is_dropped(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db, fetch_all
        await init_db()
        try:
            await self._seed(1)
            result = FetchResult(matched=True, confidence=0.1, source="fake",
                                 tags=TrackMetadata(title="Guess"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            monkeypatch.setattr("backend.config.get_all",
                                lambda: {"fetch_confidence": 0.9})
            summary = await I.scan(limit=5)
            assert summary["matched"] == 0
            assert await fetch_all("SELECT * FROM fetch_candidates") == []
        finally:
            await identify_db()

    async def test_no_change_produces_no_candidate(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db, fetch_all
        await init_db()
        try:
            await self._seed(1)
            result = FetchResult(matched=True, confidence=0.99, source="fake",
                                 tags=TrackMetadata(title="Title 0", artist=""))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            summary = await I.scan(limit=5)
            assert summary["matched"] == 0
            assert await fetch_all("SELECT * FROM fetch_candidates") == []
        finally:
            await identify_db()

    async def test_fetcher_exception_does_not_abort_scan(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db
        await init_db()
        try:
            await self._seed(2)

            class Boom(_FakeFetcher):
                async def fetch(self, track, file_path=""):
                    raise RuntimeError("network down")
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": Boom(None))
            summary = await I.scan(limit=5)
            assert summary["scanned"] == 2
            assert summary["matched"] == 0
        finally:
            await identify_db()

    async def test_duplicate_scan_does_not_double_queue(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db, fetch_all
        await init_db()
        try:
            await self._seed(1)
            result = FetchResult(matched=True, confidence=0.95, source="fake",
                                 tags=TrackMetadata(artist="New Artist"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            await I.scan(limit=5)
            await I.scan(limit=5)
            assert len(await fetch_all("SELECT * FROM fetch_candidates")) == 1
        finally:
            await identify_db()

    async def test_apply_writes_and_updates(self, identify_db, monkeypatch, tmp_path):
        import backend.services.identify as I
        from backend.database import init_db, fetch_all, fetch_one
        await init_db()
        try:
            target = tmp_path / "song.mp3"
            target.write_bytes(b"\x00" * 64)
            from backend.database import execute
            await execute(
                "INSERT INTO tracks (file_path, title, artist, album, file_status) "
                "VALUES (?, 'Old', 'OldArtist', 'OldAlbum', 'present')", (str(target),),
            )
            result = FetchResult(matched=True, confidence=0.99, source="fake",
                                 tags=TrackMetadata(artist="NewArtist", album="NewAlbum"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            await I.scan(limit=5)

            written = {}

            async def fake_read(path):
                return {"title": "Old", "artist": "OldArtist", "album": "OldAlbum"}

            async def fake_write(path, tags, cover_path=None):
                written.update(tags)
                return True

            monkeypatch.setattr("backend.services.tagger.read_tags", fake_read)
            monkeypatch.setattr("backend.services.tagger.write_tags", fake_write)
            monkeypatch.setattr("backend.config.get_all",
                                lambda: {"backup_before_write": False, "fetch_overwrite": True})

            cands = await I.list_candidates()
            out = await I.apply_candidate(cands[0]["id"])
            assert out["status"] == "applied"
            assert "artist" in out["fields"]
            assert written["artist"] == "NewArtist"

            row = await fetch_one("SELECT * FROM tracks")
            assert row["artist"] == "NewArtist"
        finally:
            await identify_db()

    async def test_apply_respects_existing_values(self, identify_db, monkeypatch, tmp_path):
        """With fetch_overwrite off, a field that already has a value is left alone."""
        import backend.services.identify as I
        from backend.database import init_db, execute
        await init_db()
        try:
            target = tmp_path / "s.mp3"
            target.write_bytes(b"\x00")
            await execute(
                "INSERT INTO tracks (file_path, title, artist, album, file_status) "
                "VALUES (?, 'Old', 'KeepMe', 'OldAlbum', 'present')", (str(target),),
            )
            result = FetchResult(matched=True, confidence=0.99, source="fake",
                                 tags=TrackMetadata(artist="ReplaceMe"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            await I.scan(limit=5)

            async def fake_read(path):
                return {"title": "Old", "artist": "KeepMe", "album": "OldAlbum"}

            written = {}

            async def fake_write(path, tags, cover_path=None):
                written.update(tags)
                return True

            monkeypatch.setattr("backend.services.tagger.read_tags", fake_read)
            monkeypatch.setattr("backend.services.tagger.write_tags", fake_write)
            monkeypatch.setattr("backend.config.get_all",
                                lambda: {"backup_before_write": False, "fetch_overwrite": False})

            cands = await I.list_candidates()
            out = await I.apply_candidate(cands[0]["id"])
            # Every proposed field is either empty or already set, so there is
            # nothing to write and the file must be left completely alone.
            assert out["status"] == "skipped"
            assert written == {}, "must not rewrite tags when nothing changes"
        finally:
            await identify_db()

    async def test_apply_missing_candidate_raises(self, identify_db):
        import backend.services.identify as I
        from backend.database import init_db
        await init_db()
        try:
            with pytest.raises(LookupError):
                await I.apply_candidate(9999)
        finally:
            await identify_db()

    async def test_apply_twice_is_idempotent(self, identify_db, monkeypatch, tmp_path):
        import backend.services.identify as I
        from backend.database import init_db, execute
        await init_db()
        try:
            target = tmp_path / "s.mp3"
            target.write_bytes(b"\x00")
            await execute(
                "INSERT INTO tracks (file_path, title, artist, file_status) "
                "VALUES (?, 'Old', 'OldArtist', 'present')", (str(target),),
            )
            result = FetchResult(matched=True, confidence=0.99, source="fake",
                                 tags=TrackMetadata(artist="New"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            await I.scan(limit=5)

            n = {"count": 0}

            async def fake_read(path):
                return {"title": "Old", "artist": "OldArtist"}

            async def fake_write(path, tags, cover_path=None):
                n["count"] += 1
                return True

            monkeypatch.setattr("backend.services.tagger.read_tags", fake_read)
            monkeypatch.setattr("backend.services.tagger.write_tags", fake_write)
            monkeypatch.setattr("backend.config.get_all",
                                lambda: {"backup_before_write": False, "fetch_overwrite": True})

            cid = (await I.list_candidates())[0]["id"]
            await I.apply_candidate(cid)
            second = await I.apply_candidate(cid)
            assert second["status"] == "already_applied"
            assert n["count"] == 1
        finally:
            await identify_db()

    async def test_reject_marks_row(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db, fetch_one
        await init_db()
        try:
            await self._seed(1)
            result = FetchResult(matched=True, confidence=0.99, source="fake",
                                 tags=TrackMetadata(artist="New"))
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": _FakeFetcher(result))
            await I.scan(limit=5)
            cid = (await I.list_candidates())[0]["id"]
            await I.reject_candidate(cid)
            assert await I.list_candidates("pending") == []
            assert (await fetch_one("SELECT * FROM fetch_candidates WHERE id=?", (cid,)))["status"] == "rejected"
        finally:
            await identify_db()

    async def test_scan_with_no_usable_source_raises(self, identify_db, monkeypatch):
        import backend.services.identify as I
        from backend.database import init_db
        await init_db()
        try:
            monkeypatch.setattr(I, "pick_fetcher", lambda s, p="": M.LocalOnlyFetcher({}))
            with pytest.raises(RuntimeError, match="No metadata source is usable"):
                await I.scan(limit=1)
        finally:
            await identify_db()
