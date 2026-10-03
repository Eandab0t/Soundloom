"""Tests for the artist watch poller and the download-provider fixes."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import watcher as W
from backend.services.downloader import _deemix_bitrate
from backend.services.organizer import _sanitize
from backend.sources.deezer_source import DeezerDownloader
from backend.pipeline.models import QUALITY_PRESETS


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class TestReleaseHelpers:
    def test_parse_release_date_formats(self):
        assert W._parse_release_date("2026-09-24").year == 2026
        assert W._parse_release_date("2026-09").month == 9
        assert W._parse_release_date("2026").year == 2026
        assert W._parse_release_date("") is None
        assert W._parse_release_date(None) is None
        assert W._parse_release_date("not a date") is None

    def test_is_wanted_accepts_singles(self):
        # The user asked for anything new, singles included.
        for rtype in ("album", "single", "ep", "compilation"):
            assert W._is_wanted({"record_type": rtype}) is True

    def test_is_wanted_rejects_unavailable(self):
        assert W._is_wanted({"record_type": "album", "available": False}) is False

    def test_release_key_handles_missing_id(self):
        assert W._release_key({"id": 42}) == "42"
        assert W._release_key({}) == ""

    @pytest.mark.parametrize("raw,expected", [
        ('["1","2"]', ["1", "2"]),
        ("[]", []),
        ("", []),
        (None, []),
        ("not json", []),
        ('{"a":1}', []),  # wrong shape must not explode
    ])
    def test_load_known_is_defensive(self, raw, expected):
        assert W._load_known(raw) == expected


# --------------------------------------------------------------------------
# In-memory fake Deezer client
# --------------------------------------------------------------------------

class FakeClient:
    """Stands in for DeezerWatchClient with scripted release data."""

    def __init__(self, releases, albums=None, artist_id=999):
        self._releases = releases
        self._albums = albums or {}
        self.artist_id = artist_id
        self.searched_for = None
        self.album_calls = []

    async def search_artist(self, name):
        self.searched_for = name
        return {"id": self.artist_id, "name": name, "nb_fan": 10}

    async def releases(self, artist_id):
        return list(self._releases)

    async def album(self, release_id):
        self.album_calls.append(release_id)
        return self._albums[release_id]

    def album_tracks(self, detail):
        return (detail.get("tracks") or {}).get("data") or []


def _release(rid, title, date="2026-09-20", rtype="single"):
    return {"id": rid, "title": title, "release_date": date, "record_type": rtype}


def _album(rid, title, track_ids, primary_artist_id=999, date="2026-09-20"):
    return {
        "id": rid,
        "title": title,
        "release_date": date,
        "artist": {"id": primary_artist_id, "name": "Someone"},
        "tracks": {"data": [
            {"id": t, "title": f"Track {t}", "artist": {"id": primary_artist_id, "name": "Someone"}}
            for t in track_ids
        ]},
    }


@pytest.fixture
def watch_db(monkeypatch, tmp_path):
    """Point the database at a throwaway file and return a row factory."""
    import backend.config as cfg
    from backend.database import init_db, close_db

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "watch.db")
    return cfg


SETTINGS = {"watch_backfill_days": 30, "default_quality": "balanced", "default_format": "mp3"}


async def _add_watch(name, quality="balanced"):
    from backend.database import execute
    await execute(
        "INSERT INTO watched_artists (artist_name, auto_download, quality_profile) "
        "VALUES (?, 1, ?)", (name, quality),
    )


# --------------------------------------------------------------------------
# check_artist behaviour
# --------------------------------------------------------------------------

class TestCheckArtist:
    async def test_first_run_respects_backfill_window(self, watch_db, monkeypatch):
        from backend.database import init_db, close_db, fetch_one, execute
        await init_db()
        try:
            old = _release(1, "Ancient", date="2020-01-01", rtype="album")
            recent = _release(2, "Fresh", date="2026-09-20", rtype="single")
            client = FakeClient(
                [old, recent],
                {
                    1: _album(1, "Ancient", [101, 102], date="2020-01-01"),
                    2: _album(2, "Fresh", [201, 202]),
                },
            )
            await _add_watch("Someone")
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 2, "only the in-window release should be enqueued"

            # The whole catalogue must be remembered, not just what we acted
            # on, otherwise the next pass re-queues everything.
            after = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            assert set(json.loads(after["known_releases"])) == {"1", "2"}
        finally:
            await close_db()

    async def test_second_run_is_idempotent(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, execute
        await init_db()
        try:
            client = FakeClient(
                [_release(2, "Fresh")],
                {2: _album(2, "Fresh", [201, 202])},
            )
            await _add_watch("Someone")
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            first = await W.check_artist(client, row, SETTINGS)
            after = await fetch_one("SELECT * FROM watched_artists WHERE id=1")

            second = await W.check_artist(client, after, SETTINGS)
            assert first == 2
            assert second == 0, "a known release must never be re-queued"

            from backend.database import fetch_all
            jobs = await fetch_all("SELECT source_url FROM jobs")
            assert len(jobs) == 2
        finally:
            await close_db()

    async def test_featured_appearance_is_not_a_release(self, watch_db):
        """Deezer lists releases under every credited contributor."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            release = _release(5, "Someone's Single")
            # The album is actually by a different artist; we are only credited.
            client = FakeClient(
                [release],
                {5: _album(5, "Someone's Single", [501, 502], primary_artist_id=290)},
            )
            await _add_watch("Featured Guy")
            from backend.database import fetch_one
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 0, "must not download releases we are only featured on"
        finally:
            await close_db()

    async def test_skips_tracks_already_in_library(self, watch_db):
        from backend.database import init_db, close_db, execute, fetch_one
        await init_db()
        try:
            await execute(
                "INSERT INTO tracks (file_path, title, artist, primary_artist, "
                "display_artist, file_status) VALUES ('/x.mp3', 'Track 201', 'Someone', "
                "'Someone', 'Someone', 'present')"
            )
            client = FakeClient(
                [_release(2, "Fresh")],
                {2: _album(2, "Fresh", [201, 202])},
            )
            await _add_watch("Someone")
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1, "the track already in the library must be skipped"
        finally:
            await close_db()

    async def test_resolves_and_caches_artist_id(self, watch_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            client = FakeClient([], {}, artist_id=4242)
            await _add_watch("Fresh Face")
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            await W.check_artist(client, row, SETTINGS)
            after = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            assert after["deezer_artist_id"] == "4242"
            assert client.searched_for == "Fresh Face"
            assert after["last_checked"]
        finally:
            await close_db()

    async def test_per_cycle_cap(self, watch_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            many = list(range(100, 160))  # 60 tracks
            client = FakeClient(
                [_release(2, "Huge")],
                {2: _album(2, "Huge", many)},
            )
            await _add_watch("Prolific")
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == W._MAX_TRACKS_PER_ARTIST_PER_CYCLE
        finally:
            await close_db()


class TestPollOnce:
    async def test_respects_interval(self, watch_db, monkeypatch):
        from backend.database import init_db, close_db, execute, fetch_one
        from datetime import datetime, timezone, timedelta
        await init_db()
        try:
            settings = {**SETTINGS, "watch_enabled": True, "watched_check_interval": 3600}
            monkeypatch.setattr(W.config, "get_all", lambda: settings)

            fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            await execute(
                "INSERT INTO watched_artists (artist_name, auto_download, known_releases, "
                "deezer_artist_id, last_checked) VALUES ('A', 1, '[\"1\"]', '999', ?)",
                (fresh,),
            )
            calls = []

            async def boom(*a, **k):
                calls.append(a)
                raise AssertionError("should not have been polled, it is not due")

            monkeypatch.setattr(W, "check_artist", boom)
            assert await W.poll_once() == 0
            assert calls == []
        finally:
            await close_db()

    async def test_disabled_setting_short_circuits(self, watch_db, monkeypatch):
        from backend.database import init_db, close_db
        await init_db()
        try:
            monkeypatch.setattr(W.config, "get_all", lambda: {"watch_enabled": False})
            assert await W.poll_once() == 0
        finally:
            await close_db()


# --------------------------------------------------------------------------
# Regressions for the bugs found while building this
# --------------------------------------------------------------------------

class TestDeezerBitrateSelection:
    def test_lossless_profiles_ask_for_flac(self):
        assert _deemix_bitrate(QUALITY_PRESETS["best"]) == "flac"

    def test_320k_profiles_ask_for_320(self):
        assert _deemix_bitrate(QUALITY_PRESETS["balanced"]) == "320"

    def test_lower_bitrates_ask_for_128(self):
        assert _deemix_bitrate(QUALITY_PRESETS["ipod_saver"]) == "128"

    def test_string_bitrate_is_parsed(self):
        """QualityProfile.bitrate is a string like '320k', not an int."""
        from backend.pipeline.models import QualityProfile
        assert _deemix_bitrate(QualityProfile(bitrate="128k")) == "128"
        assert _deemix_bitrate(QualityProfile(bitrate="320k")) == "320"
        assert _deemix_bitrate(QualityProfile(bitrate="")) == "128"


class TestFindDownloadedFile:
    def test_returns_none_when_deemix_wrote_nothing(self, tmp_path):
        """deemix exits 0 and prints 'Download complete' even on failure."""
        assert DeezerDownloader()._find_downloaded_file(tmp_path) is None

    def test_finds_newest_audio_file(self, tmp_path):
        (tmp_path / "old.mp3").write_bytes(b"a")
        (tmp_path / "cover.jpg").write_bytes(b"b")
        found = DeezerDownloader()._find_downloaded_file(tmp_path)
        assert found is not None and found.endswith(".mp3")

    def test_ignores_non_audio(self, tmp_path):
        (tmp_path / "notes.txt").write_bytes(b"x")
        assert DeezerDownloader()._find_downloaded_file(tmp_path) is None


class TestExecutableResolution:
    def test_reports_missing_deemix_clearly(self, monkeypatch):
        monkeypatch.setattr("backend.sources.deezer_source.shutil.which", lambda n: None)
        monkeypatch.delenv("DEEMIX_BIN", raising=False)
        with pytest.raises(RuntimeError, match="deemix not found"):
            DeezerDownloader()._resolve_executable()

    def test_honours_env_override(self, monkeypatch):
        monkeypatch.setenv("DEEMIX_BIN", r"C:\tools\deemix.cmd")
        monkeypatch.setattr("backend.sources.deezer_source.shutil.which",
                            lambda n: r"C:\tools\deemix.cmd" if n == r"C:\tools\deemix.cmd" else None)
        assert DeezerDownloader()._resolve_executable() == r"C:\tools\deemix.cmd"


class TestJobClaiming:
    """The queue worker used to deadlock on its very first claim.

    _pick_next_job held database._db_lock and then called execute(), which
    takes the same non-reentrant asyncio.Lock again. The worker hung forever
    holding the lock, so every write in the app - the watch poller included -
    blocked behind it.
    """

    async def _db(self, monkeypatch, tmp_path):
        import backend.config as cfg
        from backend.database import init_db, close_db
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "jobs.db")
        await init_db()
        return close_db

    async def test_claim_does_not_deadlock(self, monkeypatch, tmp_path):
        from backend.services.downloader import _pick_next_job
        from backend.database import close_db, execute, fetch_one
        close = await self._db(monkeypatch, tmp_path)
        try:
            await execute(
                "INSERT INTO jobs (source_url, source_type, title, status) "
                "VALUES ('u1', 'deezer', 'One', 'pending')"
            )
            # A regression here hangs instead of failing, so bound it hard.
            job = await asyncio.wait_for(_pick_next_job(), timeout=5)
            assert job is not None, "should have claimed the pending job"
            assert job["title"] == "One"
            assert job["status"] == "resolving"

            # The lock must have been released - any other writer can proceed.
            await asyncio.wait_for(
                execute("INSERT INTO jobs (source_url, source_type, status) "
                        "VALUES ('u2', 'deezer', 'pending')"),
                timeout=5,
            )
            row = await fetch_one("SELECT id FROM jobs WHERE source_url='u2'")
            assert row is not None, "a later write deadlocked behind the claim"
        finally:
            await close()

    async def test_claim_is_atomic(self, monkeypatch, tmp_path):
        """Two workers must never be handed the same job."""
        from backend.services.downloader import _pick_next_job
        from backend.database import close_db, execute, fetch_all
        close = await self._db(monkeypatch, tmp_path)
        try:
            for i in range(5):
                await execute(
                    "INSERT INTO jobs (source_url, source_type, title, status) "
                    "VALUES (?, 'deezer', ?, 'pending')", (f"u{i}", f"T{i}"),
                )
            claims = await asyncio.wait_for(
                asyncio.gather(*[_pick_next_job() for _ in range(5)]), timeout=10
            )
            ids = [c["id"] for c in claims if c]
            assert len(ids) == len(set(ids)), "the same job was claimed twice"
            assert len(ids) == 5
            remaining = await fetch_all("SELECT id FROM jobs WHERE status='pending'")
            assert remaining == []
        finally:
            await close()

    async def test_claim_returns_none_when_empty(self, monkeypatch, tmp_path):
        from backend.services.downloader import _pick_next_job
        from backend.database import close_db
        close = await self._db(monkeypatch, tmp_path)
        try:
            assert await asyncio.wait_for(_pick_next_job(), timeout=5) is None
        finally:
            await close()


class TestSanitizeHandlesNonStrings:
    def test_integers(self):
        assert _sanitize(1) == "1"
        # 0 means "unknown track number", which should render as blank
        # rather than as a literal "0" in the filename.
        assert _sanitize(0) == ""

    def test_floats(self):
        assert _sanitize(115.0) == "115.0"

    def test_none_and_empty(self):
        assert _sanitize(None) == ""
        assert _sanitize("") == ""

    def test_strings_still_sanitized(self):
        assert _sanitize('a<b>c"d') == "abcd"


class TestDownloadContract:
    def test_interface_declares_tuple(self):
        from backend.pipeline.interfaces import DownloadProvider
        ann = DownloadProvider.download.__annotations__.get("return")
        assert ann == "tuple[str, dict | None]" or ann == tuple[str, dict | None]

    def test_both_providers_declare_the_tuple_contract(self):
        import inspect
        from backend.sources.ytdlp_source import YtdlpDownloader
        from backend.sources.deezer_source import DeezerDownloader
        for cls in (YtdlpDownloader, DeezerDownloader):
            ann = cls.download.__annotations__.get("return")
            assert ann == "tuple[str, dict | None]" or ann == tuple[str, dict | None], cls.__name__
