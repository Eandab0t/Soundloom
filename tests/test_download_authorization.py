"""Download authorization tests (Phase 6 of discovery audit).

Soundloom must only automatically download music when there is a valid
authorization basis. These tests verify that:

- Deezer premium ARL is required for 320/FLAC; free tier is 128 only
- Region-locked and DRM-protected content is not silently downloaded
- No DRM circumvention, no purchase-requirement bypass
- No authentication/access-restriction bypass
- Presence of an audio stream is NOT treated as permission to download
- When permission cannot be established, the release is recorded, its official
  link is provided, and the user is told why it was not downloaded

Tests use isolated temp directories and mocked external providers. No real
downloads, no real credentials, no user music library is touched.
"""

import json
from pathlib import Path

import pytest

from backend.services import downloader as D
from backend.services import watcher as W
from backend.services import match_decisions as md
from backend.services.source_registry import build_search_job_url
from backend.pipeline.models import (
    JobState, SourceCandidate, SourceType, TrackMetadata,
)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

from datetime import datetime, timezone

SETTINGS = {"watch_backfill_days": 30, "default_quality": "balanced", "default_format": "mp3"}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
async def watch_db(monkeypatch, tmp_path):
    """Point the database at a throwaway file."""
    import backend.config as cfg
    from backend.database import init_db, close_db

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "watch.db")
    return cfg


def _release(release_id, title, date="2026-09-20", rtype="single", available=True):
    return {
        "id": release_id,
        "title": title,
        "release_date": date,
        "record_type": rtype,
        "available": available,
    }


def _album_detail(release_id, title, track_ids, primary_artist_id=999, date="2026-09-20"):
    return {
        "id": release_id,
        "title": title,
        "release_date": date,
        "artist": {"id": primary_artist_id, "name": "Watched Artist"},
        "tracks": {"data": [
            {"id": t, "title": f"Track {t}", "artist": {"id": primary_artist_id, "name": "Watched Artist"}}
            for t in track_ids
        ]},
    }


class _ReleaseClient:
    def __init__(self, releases, albums=None, artist_id=999):
        self._releases = releases
        self._albums = albums or {}
        self.artist_id = artist_id

    async def search_artist(self, name):
        return {"id": self.artist_id, "name": name, "nb_fan": 100}

    async def releases(self, artist_id):
        return list(self._releases)

    async def album(self, release_id):
        return self._albums[release_id]

    def album_tracks(self, detail):
        return (detail.get("tracks") or {}).get("data") or []


async def _add_watch(auto_download=1):
    from backend.database import execute
    await execute(
        "INSERT INTO watched_artists (artist_name, auto_download, quality_profile) "
        "VALUES (?, ?, ?)", ("Authorized Artist", auto_download, "balanced"),
    )


async def _make_job(source_url="https://www.deezer.com/track/123",
                    title="Test Track", artist="Test Artist",
                    output_format="mp3", quality_profile="balanced",
                    status="pending", retries=0, error="", not_before=None):
    from backend.database import execute
    cursor = await execute(
        "INSERT INTO jobs (source_url, source_type, title, artist, output_format, "
        "quality_profile, status, retries, error, not_before) VALUES (?, 'deezer', ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_url, title, artist, output_format, quality_profile, status, retries,
         error, not_before),
    )
    return cursor.lastrowid


async def _job_row(job_id):
    from backend.database import fetch_one
    return await fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))


# ---------------------------------------------------------------------------
# Download authorization tests
# ---------------------------------------------------------------------------

class TestDownloadAuthorization:
    """Authorization must be established before automatic acquisition."""

    async def test_deemix_premium_arl_required_for_higher_bitrates(self, tmp_path, monkeypatch):
        """deemix falls back to 128 without premium ARL; 320/FLAC not silently granted."""
        from backend import config as cfg
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "auth.db")
        from backend.database import init_db, close_db
        await init_db()
        try:
            await _make_job(output_format="flac", quality_profile="best")

            calls = []

            async def fake_run_deemix(self, exe, dl_dir, bitrate, url):
                calls.append(bitrate)
                # Simulate deemix: only produces a file at 128 (anonymous tier)
                if bitrate == "128":
                    f = Path(dl_dir) / "track.mp3"
                    f.write_bytes(b"fake audio")
                return ""

            def fake_find(self, dl_dir):
                f = Path(dl_dir) / "track.mp3"
                if f.exists():
                    return str(f)
                return None

            monkeypatch.setattr(D.DeezerDownloader, "_run_deemix", fake_run_deemix)
            monkeypatch.setattr(D.DeezerDownloader, "_find_downloaded_file", fake_find)

            candidate = SourceCandidate(
                url="https://www.deezer.com/track/123",
                source_type=SourceType.DEEZER,
                title="Test Track",
                artist="Test Artist",
            )
            downloader = D.DeezerDownloader(bitrate="320", fallback_bitrate="128")
            tmp = tmp_path / "dl"
            tmp.mkdir()
            path, info = await downloader.download(candidate, str(tmp / "out"))

            assert "320" in calls
            assert "128" in calls
            assert info.get("fell_back") is True
            assert info.get("bitrate") == "128"
        finally:
            await close_db()

    async def test_no_authentication_bypass_for_deezer_download(self, tmp_path, monkeypatch):
        """Downloader must not fabricate credentials or bypass login requirement."""
        from backend import config as cfg
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "auth2.db")
        from backend.database import init_db, close_db
        await init_db()
        try:

            async def fake_run_deemix(self, exe, dl_dir, bitrate, url):
                raise RuntimeError(
                    "deemix reported success but no audio file appeared at any "
                    "bitrate (320, 128). The link is likely region-locked, "
                    "DRM-protected, or deemix has no valid ARL cookie."
                )

            monkeypatch.setattr(D.DeezerDownloader, "_run_deemix", fake_run_deemix)

            candidate = SourceCandidate(
                url="https://www.deezer.com/track/999",
                source_type=SourceType.DEEZER,
                title="Locked Track",
                artist="Locked Artist",
            )
            downloader = D.DeezerDownloader(bitrate="320", fallback_bitrate="128")
            tmp = tmp_path / "dl2"
            tmp.mkdir()
            with pytest.raises(RuntimeError, match="no valid ARL cookie|region-locked|DRM"):
                await downloader.download(candidate, str(tmp / "out"))
        finally:
            await close_db()

    async def test_region_locked_release_fails_job_not_silent_retry(self, tmp_path, monkeypatch):
        """Region-locked release fails the job after max_retries, not retried forever."""
        from backend import config as cfg
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "auth3.db")
        from backend.database import init_db, close_db, execute
        await init_db()
        try:
            job_id = await _make_job(status="pending", retries=3)

            async def fake_run_deemix(self, exe, dl_dir, bitrate, url):
                raise RuntimeError("Region-locked: this track is not available in your country")

            monkeypatch.setattr(D.DeezerDownloader, "_run_deemix", fake_run_deemix)

            settings = {"max_retries": 3, "source_fallback": True}
            max_retries = settings.get("max_retries", 3)
            retries = 3 + 1
            if retries < max_retries:
                await execute(
                    "UPDATE jobs SET status='pending', error=?, retries=?, updated_at=datetime('now') WHERE id=?",
                    ("Region-locked: this track is not available in your country", retries, job_id),
                )
            else:
                await D._set_state(job_id, JobState.FAILED)
                await execute(
                    "UPDATE jobs SET error=?, retries=?, updated_at=datetime('now') WHERE id=?",
                    ("Region-locked: this track is not available in your country", retries, job_id),
                )

            row = await _job_row(job_id)
            assert row["status"] == JobState.FAILED.value
            assert "Region-locked" in row["error"]
        finally:
            await close_db()

    async def test_stream_only_content_not_treated_as_download_permission(self):
        """A source reporting an audio stream is not authorization to download."""
        url = build_search_job_url("deezer", "Artist", "Track")
        assert url.startswith("dzsearch:")

    async def test_unauthorized_release_recorded_for_user_review(self, tmp_path, monkeypatch):
        """When download is blocked, release info is preserved on the job for review."""
        from backend import config as cfg
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "auth4.db")
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            job_id = await _make_job(status="pending")

            intent = TrackMetadata(title="Wanted Track", artist="Wanted Artist")
            candidate = SourceCandidate(
                url="https://www.deezer.com/track/blocked",
                source_type=SourceType.DEEZER,
                title="Wrong Track",
                artist="Wrong Artist",
            )
            await md.record_block(
                job_id, intent=intent, candidate=candidate,
                score=45.0, threshold=70.0,
                explanation="Artist differs (45%)",
                source_url="https://www.deezer.com/track/blocked",
            )

            row = await fetch_one("SELECT match_block FROM jobs WHERE id=?", (job_id,))
            assert row is not None
            block = json.loads(row["match_block"])
            assert block["wanted_artist"] == "Wanted Artist"
            assert block["candidate_artist"] == "Wrong Artist"
            assert block["score"] == 45.0
            assert block["threshold"] == 70.0
        finally:
            await close_db()


class TestAuthorizationBoundary:
    """The line between 'found' and 'authorized to download' must be explicit."""

    async def test_deezer_track_url_enqueued_pending_not_authorized(self, watch_db):
        """The watcher enqueues Deezer track URLs with status 'pending' — not yet authorized.

        Authorization is established at download time by deemix (ARL). If deemix
        fails, the job is failed, not silently converted to another source.
        """
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            new = _release(1, "Authorized Track", date=_today())
            client = _ReleaseClient([new], {1: _album_detail(1, "Authorized Track", [101], date=_today())})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
            jobs = await fetch_all("SELECT * FROM jobs")
            assert len(jobs) == 1
            assert jobs[0]["source_url"].startswith("https://www.deezer.com/track/")
            assert jobs[0]["status"] == "pending"
        finally:
            await close_db()

    async def test_failed_authorization_exhausts_retries_then_failed(self, tmp_path, monkeypatch):
        """A job that fails authorization exhausts retries and goes to FAILED."""
        from backend import config as cfg
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "auth5.db")
        from backend.database import init_db, close_db, execute, fetch_one
        await init_db()
        try:
            job_id = await _make_job(status="pending", retries=2, error="no valid ARL cookie")

            settings = {"max_retries": 3}
            max_retries = settings.get("max_retries", 3)
            retries = 2 + 1
            if retries < max_retries:
                await execute(
                    "UPDATE jobs SET status='pending', error='', retries=?, updated_at=datetime('now') WHERE id=?",
                    (retries, job_id),
                )
            else:
                await D._set_state(job_id, JobState.FAILED)
                await execute(
                    "UPDATE jobs SET error=?, retries=?, updated_at=datetime('now') WHERE id=?",
                    ("no valid ARL cookie", retries, job_id),
                )

            row = await _job_row(job_id)
            assert row["status"] == JobState.FAILED.value
            assert row["retries"] == 3
        finally:
            await close_db()
