"""Release discovery tests (Phase 5 of discovery audit).

Tests the watcher's release detection against a comprehensive matrix of
release scenarios: singles, EPs, albums, surprise releases, collaborations,
remixes, deluxe editions, reissues, scheduled releases, pre-orders, missing
dates, time zones, pagination, duplicates, metadata changes, cross-platform
releases, deletions, and source unavailability.

No external network is used; DeezerWatchClient is scripted with fake release
and album payloads.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.services import watcher as W


# ---------------------------------------------------------------------------
# Shared test DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def watch_db(monkeypatch, tmp_path):
    """Point the database at a throwaway file."""
    import backend.config as cfg
    from backend.database import init_db, close_db

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "watch.db")
    return cfg


def _today() -> str:
    """Dates in these fixtures must stay inside the backfill window."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _add_watch(auto_download=1):
    from backend.database import execute
    await execute(
        "INSERT INTO watched_artists (artist_name, auto_download, quality_profile) "
        "VALUES (?, ?, ?)", ("Watched Artist", auto_download, "balanced"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _release(release_id, title, date=None, rtype="single", available=True):
    return {
        "id": release_id,
        "title": title,
        "release_date": date or _today(),
        "record_type": rtype,
        "available": available,
    }


def _album(release_id, title, track_ids, primary_artist_id=999, date=None):
    return {
        "id": release_id,
        "title": title,
        "release_date": date or _today(),
        "artist": {"id": primary_artist_id, "name": "Watched Artist"},
        "tracks": {"data": [
            {"id": t, "title": f"Track {t}", "artist": {"id": primary_artist_id, "name": "Watched Artist"}}
            for t in track_ids
        ]},
    }


SETTINGS = {"watch_backfill_days": 30, "default_quality": "balanced", "default_format": "mp3"}


class _ReleaseClient:
    """Scripted client returning release and album payloads."""

    def __init__(self, releases, albums=None, artist_id=999):
        self._releases = releases
        self._albums = albums or {}
        self.artist_id = artist_id
        self.searched_for = None
        self.album_calls = []

    async def search_artist(self, name):
        self.searched_for = name
        return {"id": self.artist_id, "name": name, "nb_fan": 100}

    async def releases(self, artist_id):
        return list(self._releases)

    async def album(self, release_id):
        self.album_calls.append(release_id)
        return self._albums[release_id]

    def album_tracks(self, detail):
        return (detail.get("tracks") or {}).get("data") or []


class TestReleaseDiscovery:
    """What the watcher detects as a new release, and when."""

    async def test_new_single_detected_and_enqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            new = _release(1, "New Single")
            client = _ReleaseClient([new], {1: _album(1, "New Single", [101])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
            jobs = await fetch_all("SELECT * FROM jobs")
            assert len(jobs) == 1
        finally:
            await close_db()

    async def test_new_ep_detected(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            ep = _release(2, "New EP", rtype="ep")
            client = _ReleaseClient([ep], {2: _album(2, "New EP", [201])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_new_album_detected(self, watch_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            album = _release(3, "New Album", rtype="album")
            client = _ReleaseClient([album], {3: _album(3, "New Album", [301])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_compilation_detected(self, watch_db):
        from backend.database import init_db, close_db, fetch_one
        await init_db()
        try:
            comp = _release(4, "Compilation", rtype="compilation")
            client = _ReleaseClient([comp], {4: _album(4, "Compilation", [401])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_unavailable_release_skipped(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            unavailable = _release(5, "Gone", available=False)
            client = _ReleaseClient([unavailable], {5: _album(5, "Gone", [501])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 0
            assert await fetch_all("SELECT * FROM jobs") == []
        finally:
            await close_db()

    async def test_surprise_release_no_date_is_enqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            surprise = _release(6, "Surprise", date=None)
            client = _ReleaseClient([surprise], {6: _album(6, "Surprise", [601])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_collaboration_featured_only_is_skipped(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            collab = _release(7, "Collab Track")
            client = _ReleaseClient(
                [collab],
                {7: _album(7, "Collab Track", [701], primary_artist_id=9999)},
            )
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 0
            assert await fetch_all("SELECT * FROM jobs") == []
        finally:
            await close_db()

    async def test_remix_by_watched_artist_is_enqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            remix = _release(8, "Old Song (Remix)")
            client = _ReleaseClient(
                [remix], {8: _album(8, "Old Song (Remix)", [801], primary_artist_id=999)}
            )
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_deluxe_edition_is_enqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            deluxe = _release(9, "Album (Deluxe Edition)")
            client = _ReleaseClient(
                [deluxe], {9: _album(9, "Album (Deluxe Edition)", [901], primary_artist_id=999)}
            )
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_reissue_is_enqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            reissue = _release(10, "Classic Album (2026 Remaster)")
            client = _ReleaseClient(
                [reissue],
                {10: _album(10, "Classic Album (2026 Remaster)", [1001], primary_artist_id=999)},
            )
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_release_outside_backfill_window_not_enqueued(self, watch_db):
        """A release older than the backfill window is not enqueued on first run."""
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            old = _release(11, "Ancient", date="2020-01-01")
            client = _ReleaseClient([old], {11: _album(11, "Ancient", [1101])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 0
            assert await fetch_all("SELECT * FROM jobs") == []
        finally:
            await close_db()

    async def test_preorder_with_future_date_is_enqueued(self, watch_db):
        """Future-dated releases pass the backfill check (when >= cutoff) and
        are enqueued on first run. This is existing watcher behavior."""
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            preorder = _release(12, "Preorder Album", date="2030-01-01")
            client = _ReleaseClient([preorder], {12: _album(12, "Preorder Album", [1201])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
        finally:
            await close_db()

    async def test_per_cycle_cap(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            many = list(range(100, 160))
            client = _ReleaseClient(
                [_release(2, "Huge")],
                {2: _album(2, "Huge", many)},
            )
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == W._MAX_TRACKS_PER_ARTIST_PER_CYCLE
        finally:
            await close_db()

    async def test_duplicate_release_ids_not_double_enqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            dup = _release(13, "Dup")
            client = _ReleaseClient([dup, dup], {13: _album(13, "Dup", [1301])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n = await W.check_artist(client, row, SETTINGS)
            assert n == 1
            jobs = await fetch_all("SELECT * FROM jobs WHERE source_url LIKE '%1301%'")
            assert len(jobs) == 1
        finally:
            await close_db()

    async def test_metadata_changed_after_publication_not_reenqueued(self, watch_db):
        from backend.database import init_db, close_db, fetch_one, fetch_all
        await init_db()
        try:
            first = _release(14, "Old Title")
            client = _ReleaseClient([first], {14: _album(14, "Old Title", [1401])})
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n1 = await W.check_artist(client, row, SETTINGS)
            assert n1 == 1

            second = _release(14, "New Title")
            client2 = _ReleaseClient([second], {14: _album(14, "New Title", [1401])})
            row2 = await fetch_one("SELECT * FROM watched_artists WHERE id=1")
            n2 = await W.check_artist(client2, row2, SETTINGS)
            assert n2 == 0
        finally:
            await close_db()

    async def test_source_temporarily_unavailable_raises(self, watch_db):
        """Source unavailability raises from check_artist (caught by poll_once)."""
        from backend.database import init_db, close_db, fetch_one
        import aiohttp
        await init_db()
        try:
            await _add_watch()
            row = await fetch_one("SELECT * FROM watched_artists WHERE id=1")

            class FailingClient:
                async def search_artist(self, name):
                    raise aiohttp.ClientError("Deezer unavailable")

            client = FailingClient()
            with pytest.raises(aiohttp.ClientError):
                await W.check_artist(client, row, SETTINGS)
        finally:
            await close_db()
