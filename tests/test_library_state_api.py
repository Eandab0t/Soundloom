"""The library-state API surface: archive, unarchive, locate, state.

The service layer (provenance, classification, hashing) is covered in
test_library_state.py. What is pinned here is the HTTP-shaped contract the
frontend actually calls, plus the guards on `locate` - which repoints a
row at a path the user supplies, and so is the one endpoint in this
feature that takes untrusted input and writes it into `tracks.file_path`.

The four guards are the point of this module. Without them, `locate` is
an arbitrary-file-write primitive: a crafted path could point a track at
anything on the machine, after which the audio route would serve it. It
must refuse a path outside the library, a path that is not a file, an
empty path, and a file another track already claims.
"""
import pytest
from fastapi import HTTPException

from backend.database import close_db, execute, fetch_all, fetch_one, init_db


@pytest.fixture
def env(monkeypatch, tmp_path):
    import backend.config as cfg
    library = tmp_path / "Music"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
    return library


def _write(path, data=b"audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


async def _mk_track(file_path, status="present", **extra):
    cols = ["file_path", "title", "artist", "file_status"]
    vals = [str(file_path), "A Track", "An Artist", status]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    cur = await execute(
        f"INSERT INTO tracks ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})", tuple(vals))
    return cur.lastrowid


def _handlers():
    from backend.api import library as api
    return {
        "archive": api.archive_track,
        "unarchive": api.unarchive_track,
        "locate": api.locate_track,
        "state": api.library_state,
        "list_tracks": api.list_tracks,
        "provenance": api.track_provenance,
        "find": api.find_moved_track,
    }


# list_tracks declares its parameters with Query(...) defaults, so calling
# the handler directly hands it the sentinel objects rather than values and
# `order.upper()` raises. Supplying the HTTP defaults here means these tests
# exercise the same path a real request takes.
_LIST_DEFAULTS = {
    "sort": "title", "order": "ASC", "search": "", "artist": "",
    "album": "", "format": "", "status": "", "library_status": "active",
    "limit": 100, "offset": 0,
}


async def _list_tracks(**overrides):
    params = {**_LIST_DEFAULTS, **overrides}
    return await _handlers()["list_tracks"](**params)


# ---------------------------------------------------------------------------
# Archive / unarchive over the API
# ---------------------------------------------------------------------------

class TestArchiveEndpoints:
    async def test_archive_then_unarchive_round_trips(self, env):
        await init_db()
        try:
            api = _handlers()
            path = _write(env / "a.mp3", b"still here")
            tid = await _mk_track(path)

            out = await api["archive"](tid, {"reason": "not for me"})
            assert out["status"] == "archived"
            assert out["track"]["library_status"] == "archived"
            assert out["track"]["archived_reason"] == "not for me"
            assert out["track"]["archived_at"]

            back = await api["unarchive"](tid)
            assert back["status"] == "active"
            assert back["track"]["library_status"] == "active"
            assert back["track"]["archived_at"] is None
            assert back["track"]["archived_reason"] == ""
            assert (env / "a.mp3").exists(), "archiving must not touch the file"
            assert (env / "a.mp3").read_bytes() == b"still here"
        finally:
            await close_db()

    async def test_archive_works_without_a_body(self, env):
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "b.mp3")
            out = await api["archive"](tid, None)
            assert out["track"]["library_status"] == "archived"
        finally:
            await close_db()

    async def test_archiving_an_unknown_track_is_404(self, env):
        await init_db()
        try:
            api = _handlers()
            with pytest.raises(HTTPException) as exc:
                await api["archive"](999999, {})
            assert exc.value.status_code == 404
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The counts line
# ---------------------------------------------------------------------------

class TestStateEndpoint:
    async def test_buckets_partition_the_library(self, env):
        """available + missing + archived must equal the total. A missing
        file that is also archived is counted once, as archived - otherwise
        the counts line lies by a track and the user cannot trust it."""
        await init_db()
        try:
            api = _handlers()
            await _mk_track(_write(env / "p1.mp3"))
            await _mk_track(_write(env / "p2.mp3"))
            await _mk_track(env / "gone.mp3", status="missing")
            await _mk_track(env / "old.mp3", status="missing",
                            library_status="archived")

            state = await api["state"]()
            assert state == {"all_tracks": 4, "available": 2,
                             "missing": 1, "archived": 1}
            assert (state["available"] + state["missing"]
                    + state["archived"]) == state["all_tracks"]
        finally:
            await close_db()

    async def test_empty_library_reports_zeros_not_nulls(self, env):
        await init_db()
        try:
            state = await _handlers()["state"]()
            assert state == {"all_tracks": 0, "available": 0,
                             "missing": 0, "archived": 0}
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestListTracksFiltering:
    async def test_default_hides_archived_and_missing(self, env):
        await init_db()
        try:
            api = _handlers()
            await _mk_track(_write(env / "play.mp3"))
            await _mk_track(env / "gone.mp3", status="missing")
            await _mk_track(env / "old.mp3", library_status="archived")

            out = await _list_tracks()
            assert [t["file_path"] for t in out["tracks"]] == [str(env / "play.mp3")]
        finally:
            await close_db()

    async def test_missing_filter_returns_missing_active_rows(self, env):
        await init_db()
        try:
            api = _handlers()
            await _mk_track(_write(env / "play.mp3"))
            await _mk_track(env / "gone.mp3", status="missing")

            out = await _list_tracks(status="missing")
            assert out["total"] == 1
            assert out["tracks"][0]["file_status"] == "missing"
        finally:
            await close_db()

    async def test_archived_filter_keeps_archived_rows_even_when_missing(self, env):
        """Reviewing what you archived means seeing everything you archived.
        Dropping the missing ones here would hide the exact rows that need
        deciding on."""
        await init_db()
        try:
            api = _handlers()
            await _mk_track(env / "a-missing.mp3", status="missing",
                            library_status="archived")
            await _mk_track(env / "b-present.mp3", library_status="archived")
            await _mk_track(_write(env / "play.mp3"))

            out = await _list_tracks(library_status="archived")
            assert out["total"] == 2
            assert {t["file_status"] for t in out["tracks"]} == {"missing", "present"}
        finally:
            await close_db()

    async def test_all_includes_everything(self, env):
        await init_db()
        try:
            api = _handlers()
            await _mk_track(_write(env / "play.mp3"))
            await _mk_track(env / "gone.mp3", status="missing")
            await _mk_track(env / "old.mp3", library_status="archived")

            out = await _list_tracks(library_status="all")
            assert out["total"] == 3
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Locate: the four guards
# ---------------------------------------------------------------------------

class TestLocateGuards:
    async def test_happy_path_repoints_and_marks_present(self, env):
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "old place.mp3", status="missing")
            payload = b"the bytes we actually found"
            target = _write(env / "new place.mp3", payload)

            out = await api["locate"](tid, {"path": target})
            assert out["status"] == "located"
            assert out["previous_path"] == str(env / "old place.mp3")
            assert out["track"]["file_path"] == target
            assert out["track"]["file_status"] == "present"
            assert out["track"]["file_size"] == len(payload)
        finally:
            await close_db()

    async def test_refuses_a_path_outside_the_library(self, env):
        """Without this, locate is an arbitrary-file-write: point a row at
        any file on the machine and the audio route will serve it."""
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "gone.mp3", status="missing")
            outside = env.parent / "outside.mp3"
            outside.write_bytes(b"secret")

            with pytest.raises(HTTPException) as exc:
                await api["locate"](tid, {"path": str(outside)})
            assert exc.value.status_code == 403

            row = await fetch_one("SELECT file_path FROM tracks WHERE id=?", (tid,))
            assert row["file_path"] == str(env / "gone.mp3"), "must not be written"
        finally:
            await close_db()

    async def test_refuses_a_traversal_path(self, env):
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "gone.mp3", status="missing")
            sneaky = env / ".." / "outside.mp3"
            sneaky.write_bytes(b"secret")
            try:
                with pytest.raises(HTTPException) as exc:
                    await api["locate"](tid, {"path": str(sneaky)})
                assert exc.value.status_code == 403
            finally:
                sneaky.unlink(missing_ok=True)
        finally:
            await close_db()

    async def test_refuses_a_path_that_is_not_a_file(self, env):
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "gone.mp3", status="missing")
            with pytest.raises(HTTPException) as exc:
                await api["locate"](tid, {"path": str(env / "nope.mp3")})
            assert exc.value.status_code == 400
        finally:
            await close_db()

    async def test_refuses_an_empty_path(self, env):
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "gone.mp3", status="missing")
            for body in ({}, {"path": ""}, {"path": "   "}):
                with pytest.raises(HTTPException) as exc:
                    await api["locate"](tid, body)
                assert exc.value.status_code == 400
        finally:
            await close_db()

    async def test_refuses_a_file_another_track_already_claims(self, env):
        """file_path is UNIQUE, so without this the write would raise a raw
        IntegrityError and 500 instead of saying something useful."""
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "gone.mp3", status="missing")
            taken = _write(env / "taken.mp3")
            await _mk_track(taken, status="missing")

            with pytest.raises(HTTPException) as exc:
                await api["locate"](tid, {"path": taken})
            assert exc.value.status_code == 409
        finally:
            await close_db()

    async def test_locating_an_unknown_track_is_404(self, env):
        await init_db()
        try:
            api = _handlers()
            target = _write(env / "any.mp3")
            with pytest.raises(HTTPException) as exc:
                await api["locate"](999999, {"path": target})
            assert exc.value.status_code == 404
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class TestProvenance:
    async def test_reports_never_acquired_honestly(self, env):
        """Not an error and not an empty blob: the acquisition table having
        no row for this track IS the answer."""
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "old.mp3", status="missing")
            out = await api["provenance"](tid)
            assert out["never_acquired"] is True
            assert out["acquisitions"] == []
        finally:
            await close_db()

    async def test_returns_recorded_provenance(self, env):
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "d.mp3")
            await execute(
                "INSERT INTO acquisition (job_id, track_id, source, source_url,"
                " file_path, file_size, sha256) VALUES (?,?,?,?,?,?,?)",
                (7, tid, "ytsearch1", "ytsearch1:a b", str(env / "d.mp3"),
                 5, "deadbeef"))

            out = await api["provenance"](tid)
            assert out["never_acquired"] is False
            assert len(out["acquisitions"]) == 1
            assert out["acquisitions"][0]["sha256"] == "deadbeef"
            assert out["acquisitions"][0]["job_id"] == 7
        finally:
            await close_db()

    async def test_find_says_unknown_rather_than_guessing(self, env):
        """A track with no recorded hash cannot be identified by content.
        Offering the nearest-looking file would be a guess dressed as an
        answer, so it returns the reason instead."""
        await init_db()
        try:
            api = _handlers()
            tid = await _mk_track(env / "old.mp3", status="missing")
            out = await api["find"](tid)
            assert out["status"] == "unknown"
            assert "SHA-256" in out["reason"]
            assert out["candidates"] == []
        finally:
            await close_db()

    async def test_find_locates_an_identical_file_by_content(self, env):
        await init_db()
        try:
            import hashlib

            api = _handlers()
            payload = b"recognisable content"
            tid = await _mk_track(env / "vanished" / "song.mp3", status="missing")
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, file_size)"
                " VALUES (?,?,?,?)",
                (tid, str(env / "vanished" / "song.mp3"),
                 hashlib.sha256(payload).hexdigest(), len(payload)))
            moved = _write(env / "song.mp3", payload)

            out = await api["find"](tid)
            assert out["status"] == "found"
            assert out["candidates"][0]["discovered_path"] == moved
        finally:
            await close_db()
