"""The forensic instrument: what the filesystem did, in order.

`file_status` is a snapshot. It says a file is missing *now* and forgets
what happened on the way there, which is enough to run the app and useless
for an investigation - the question is always which transition preceded the
disappearance. So every transition is appended instead.

Three properties matter, and all three are pinned here:

**Idempotence.** Observation runs at startup and on demand. If it wrote a
row per track per call it would grow without bound and bury the handful of
rows that mean something, so an unchanged track writes nothing.

**Sequence, not snapshot.** The value of the table is the order. A file
that vanishes and comes back has to read as two events, not as a file
that is currently present.

**A canary must find its own file.** Consulting an index is not enough:
an index only knows what has been indexed, and the question is whether
something moved since. So the fallback is to sweep the tree and hash what
is there - which is the difference between "moved" and the one answer
this instrument exists to avoid.
"""
import hashlib
import os
import shutil

import pytest

from backend.database import close_db, execute, fetch_all, init_db
from backend.services import forensics


@pytest.fixture
def env(monkeypatch, tmp_path):
    import backend.config as cfg
    library = tmp_path / "Music"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "forensics.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS,
                                        "library_path": str(library)})
    return library


def _write(path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _track(file_path, status="present"):
    cur = await execute(
        "INSERT INTO tracks (file_path, title, artist, file_status) "
        "VALUES (?, ?, ?, ?)", (str(file_path), "T", "A", status))
    return cur.lastrowid


async def _states(track_id):
    rows = await fetch_all(
        "SELECT state FROM file_observation WHERE track_id = ? ORDER BY id",
        (track_id,))
    return [r["state"] for r in rows]


# ---------------------------------------------------------------------------
# The transition log
# ---------------------------------------------------------------------------

class TestObservationLog:
    async def test_first_observation_is_recorded_once_per_track(self, env):
        await init_db()
        try:
            a = await _track(_write(env / "a.mp3", b"alpha"))
            b = await _track(_write(env / "b.mp3", b"beta"))

            result = await forensics.observe()
            assert result["tracks"] == 2
            assert result["transitions"] == 2
            assert await _states(a) == ["present"]
            assert await _states(b) == ["present"]
        finally:
            await close_db()

    async def test_unchanged_tracks_write_nothing(self, env):
        """The property that keeps the log readable.

        Without it, every startup adds a row per track and the transitions
        that matter are indistinguishable from the noise.
        """
        await init_db()
        try:
            await _track(_write(env / "a.mp3", b"alpha"))
            await forensics.observe()

            second = await forensics.observe()
            assert second["transitions"] == 0

            forced = await forensics.observe(force=True)
            assert forced["transitions"] == 1, \
                "force is how a canary records a timestamp with no change"
        finally:
            await close_db()

    async def test_a_vanished_file_is_recorded_as_missing(self, env):
        await init_db()
        try:
            path = _write(env / "a.mp3", b"alpha")
            tid = await _track(path)
            await forensics.observe()

            (env / "a.mp3").unlink()
            await forensics.observe()
            assert await _states(tid) == ["present", "missing"]
        finally:
            await close_db()

    async def test_a_returned_file_is_recorded_as_reappeared(self, env):
        """Sequence, not snapshot.

        A file that disappears and comes back has two events in its
        history. Collapsing them into "currently present" destroys the
        only record that it was ever gone.
        """
        await init_db()
        try:
            path = _write(env / "a.mp3", b"alpha")
            tid = await _track(path)
            await forensics.observe()
            (env / "a.mp3").unlink()
            await forensics.observe()
            _write(env / "a.mp3", b"alpha")
            await forensics.observe()

            assert await _states(tid) == ["present", "missing", "reappeared"]
        finally:
            await close_db()

    async def test_the_digest_is_recorded_not_just_the_state(self, env):
        await init_db()
        try:
            tid = await _track(_write(env / "a.mp3", b"alpha"))
            await forensics.observe()
            row = (await fetch_all(
                "SELECT sha256, file_size FROM file_observation "
                " WHERE track_id = ?", (tid,)))[0]
            assert row["sha256"] == _sha(b"alpha")
            assert row["file_size"] == 5
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------

class TestCanary:
    async def test_registration_measures_the_bytes_themselves(self, env):
        """Not borrowed from provenance.

        A canary is a specimen, and what makes it evidence is that its
        content was measured before anything happened to it.
        """
        await init_db()
        try:
            path = _write(env / "a.mp3", b"alpha")
            tid = await _track(path)
            c = await forensics.register_canary(tid, path, label="specimen")

            assert c["sha256"] == _sha(b"alpha")
            assert c["file_size"] == 5
            assert c["state"] == "present"
            assert (await _states(tid))[-1] == "created"
        finally:
            await close_db()

    async def test_registration_refuses_a_file_that_is_not_there(self, env):
        await init_db()
        try:
            tid = await _track(env / "never.mp3")
            with pytest.raises(FileNotFoundError):
                await forensics.register_canary(tid, str(env / "never.mp3"))
        finally:
            await close_db()

    async def test_verify_reports_present_when_the_digest_still_matches(self, env):
        await init_db()
        try:
            path = _write(env / "a.mp3", b"alpha")
            tid = await _track(path)
            c = await forensics.register_canary(tid, path)

            v = await forensics.verify_canary(c["id"])
            assert v["state"] == "present"
            assert "digest matches" in v["detail"]
        finally:
            await close_db()

    async def test_verify_reports_moved_and_names_the_new_path(self, env):
        """The case the instrument exists for.

        A relocated file is not a lost file, and the two call for opposite
        responses - so "moved" has to be distinguished from "missing",
        which means actually looking rather than consulting an index that
        may predate the move.
        """
        await init_db()
        try:
            path = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(path)
            c = await forensics.register_canary(tid, path)

            moved = _write(env / "elsewhere" / "a.mp3", b"alpha")
            (env / "album" / "a.mp3").unlink()

            v = await forensics.verify_canary(c["id"])
            assert v["state"] == "moved", v["detail"]
            assert v["found_path"].casefold() == moved.casefold()
        finally:
            await close_db()

    async def test_verify_reports_missing_when_the_bytes_are_gone(self, env):
        await init_db()
        try:
            path = _write(env / "a.mp3", b"alpha")
            tid = await _track(path)
            c = await forensics.register_canary(tid, path)
            (env / "a.mp3").unlink()

            v = await forensics.verify_canary(c["id"])
            assert v["state"] == "missing"
            assert "no other path" in v["detail"]
        finally:
            await close_db()

    async def test_verify_reports_renamed_when_the_path_holds_something_else(
            self, env):
        """Different bytes at the same path is not the specimen any more.

        Reporting it as present would be the worst outcome available: the
        digest was the whole basis for believing the file survived.
        """
        await init_db()
        try:
            path = _write(env / "a.mp3", b"alpha")
            tid = await _track(path)
            c = await forensics.register_canary(tid, path)
            _write(env / "a.mp3", b"completely different")

            v = await forensics.verify_canary(c["id"])
            assert v["state"] == "renamed"
            assert "different content" in v["detail"]
        finally:
            await close_db()

    async def test_the_timeline_records_every_verification(self, env):
        await init_db()
        try:
            path = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(path)
            c = await forensics.register_canary(tid, path)
            await forensics.verify_canary(c["id"])
            (env / "album" / "a.mp3").unlink()
            _write(env / "elsewhere" / "a.mp3", b"alpha")
            await forensics.verify_canary(c["id"])

            timeline = await forensics.timeline(tid)
            states = [o["state"] for o in timeline]
            assert states == ["moved", "present", "created"], \
                "newest first, and nothing dropped"
        finally:
            await close_db()

    async def test_verify_of_an_unknown_canary_is_not_silently_ok(self, env):
        await init_db()
        try:
            with pytest.raises(LookupError):
                await forensics.verify_canary(9999)
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The instrument itself
# ---------------------------------------------------------------------------

class TestInstrumentDoesNotDisturb:
    async def test_it_never_writes_to_the_library(self, env):
        """An investigation tool that changes what it investigates is not
        one - and the behaviour under suspicion here is something
        reorganising files. This is the property that must not regress."""
        await init_db()
        try:
            path = env / "album"
            p = _write(path / "a.mp3", b"alpha")
            tid = await _track(p)
            await forensics.observe()
            c = await forensics.register_canary(tid, p)
            await forensics.verify_canary(c["id"])

            on_disk = sorted(str(q.relative_to(env))
                             for q in env.rglob("*") if q.is_file())
            assert on_disk == [os.path.join("album", "a.mp3")], \
                f"the instrument left files behind or moved one: {on_disk}"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Continuous observation
# ---------------------------------------------------------------------------

class TestDebouncer:
    """The rule that makes continuous observation safe to run unattended."""

    def test_a_single_reading_is_never_enough(self):
        d = forensics._Debouncer()
        assert d.offer(1, forensics.MISSING) is False, \
            "one bad read must not be written down as a disappearance"

    def test_two_agreeing_readings_settle(self):
        d = forensics._Debouncer()
        assert d.offer(1, forensics.MISSING) is False
        assert d.offer(1, forensics.MISSING) is True

    def test_a_contradiction_resets_the_count(self):
        """A file that flickers back has not been missing twice.

        This is the whole point: a copy in progress reads as absent, and
        believing it would fill the log with absences that never happened.
        """
        d = forensics._Debouncer()
        assert d.offer(1, forensics.MISSING) is False
        assert d.offer(1, forensics.PRESENT) is False
        assert d.offer(1, forensics.MISSING) is False, \
            "the missing run restarted, it did not accumulate"
        assert d.offer(1, forensics.MISSING) is True

    def test_required_cannot_be_lowered_below_two(self):
        assert forensics._Debouncer(1).required == 2

    def test_keys_are_independent(self):
        d = forensics._Debouncer()
        d.offer(1, forensics.MISSING)
        assert d.offer(2, forensics.MISSING) is False
        assert d.offer(1, forensics.MISSING) is True


class TestObserverScope:
    """Only files whose bytes are already known may be watched."""

    async def test_a_track_with_no_digest_is_not_watched(self, env):
        """Most rows in a real library have no digest. Watching them would
        mean a filesystem monitor over files whose identity cannot be
        established at all."""
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            await _track(p)
            assert await forensics.known_provenance_files() == []
        finally:
            await close_db()

    async def test_an_acquisition_digest_makes_a_track_watchable(self, env):
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(p)
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, "
                "file_size, source) VALUES (?, ?, ?, ?, 'download')",
                (tid, str(p), _sha(b"alpha"), 5))
            found = await forensics.known_provenance_files()
            assert [f["track_id"] for f in found] == [tid]
            assert found[0]["sha256"] == _sha(b"alpha")
        finally:
            await close_db()

    async def test_a_canary_counts_even_with_no_track_row(self, env):
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            await execute(
                "INSERT INTO canary (track_id, expected_path, sha256, "
                "file_size) VALUES (?, ?, ?, ?)",
                (4242, str(p), _sha(b"alpha"), 5))
            found = await forensics.known_provenance_files()
            assert [f["track_id"] for f in found] == [4242]
        finally:
            await close_db()

    async def test_archived_tracks_are_left_alone(self, env):
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(p)
            await execute(
                "UPDATE tracks SET library_status='archived' WHERE id=?",
                (tid,))
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, "
                "file_size) VALUES (?, ?, ?, ?)",
                (tid, str(p), _sha(b"alpha"), 5))
            assert await forensics.known_provenance_files() == []
        finally:
            await close_db()


class TestPoll:
    async def test_one_poll_writes_nothing(self, env):
        await init_db()
        try:
            tid = await _acquired(env, b"alpha")
            result = await forensics.poll(forensics._Debouncer())
            assert result["transitions"] == 0, \
                "an unsettled reading must not reach the log"
            assert await forensics.timeline(tid) == []
        finally:
            await close_db()

    async def test_a_steady_disappearance_is_recorded(self, env):
        await init_db()
        try:
            tid = await _acquired(env, b"alpha")
            os.remove(str(env / "album" / "a.mp3"))
            deb = forensics._Debouncer()
            await forensics.poll(deb)
            second = await forensics.poll(deb)
            assert second["transitions"] == 1
            states = [o["state"] for o in await forensics.timeline(tid)]
            assert states == [forensics.MISSING]
        finally:
            await close_db()

    async def test_a_transient_blip_is_never_recorded(self, env):
        """The regression this observer exists to prevent.

        One bad read must not become a permanent-looking disappearance.
        """
        await init_db()
        try:
            tid = await _acquired(env, b"alpha")
            p = env / "album" / "a.mp3"
            good = p.read_bytes()
            deb = forensics._Debouncer()
            await forensics.poll(deb)          # present
            os.remove(str(p))                 # blip
            await forensics.poll(deb)          # first missing, unrecorded
            p.write_bytes(good)               # back again
            await forensics.poll(deb)
            await forensics.poll(deb)
            states = [o["state"] for o in await forensics.timeline(tid)]
            assert forensics.MISSING not in states,                 'the blip was recorded as a disappearance'
            # The file really is there, so the one row that settles is
            # 'present'. What must never appear is the absence.
            assert states == [forensics.PRESENT]
        finally:
            await close_db()

    async def test_content_replaced_in_place_reads_as_renamed(self, env):
        """Same path, different bytes - invisible to an existence check."""
        await init_db()
        try:
            tid = await _acquired(env, b"alpha")
            (env / "album" / "a.mp3").write_bytes(b"beta-beta-beta")
            deb = forensics._Debouncer()
            await forensics.poll(deb)
            await forensics.poll(deb)
            states = [o["state"] for o in await forensics.timeline(tid)]
            assert states == [forensics.RENAMED]
        finally:
            await close_db()

    async def test_an_unchanged_file_is_not_re_recorded(self, env):
        await init_db()
        try:
            tid = await _acquired(env, b"alpha")
            deb = forensics._Debouncer()
            for _ in range(4):
                await forensics.poll(deb)
            rows = await forensics.timeline(tid)
            assert len(rows) == 1, \
                f"a settled state must not be re-appended each poll: {rows}"
        finally:
            await close_db()

    async def test_poll_never_touches_the_library(self, env):
        """Same contract as the rest of the instrument, on the hot path.

        The observer runs unattended, so a poll is the one piece of code
        that could quietly start moving files without anyone noticing.
        """
        await init_db()
        try:
            await _acquired(env, b"alpha")
            deb = forensics._Debouncer()
            for _ in range(3):
                await forensics.poll(deb)
            on_disk = sorted(str(q.relative_to(env))
                             for q in env.rglob("*") if q.is_file())
            assert on_disk == [os.path.join("album", "a.mp3")]
        finally:
            await close_db()

    async def test_pending_counts_unsettled_not_tracked(self, env):
        """A settled verdict stays in the debounce table - it is what makes
        the next disagreement detectable - so reporting the table size as
        'pending' would say nothing had settled long after it had."""
        await init_db()
        try:
            await _acquired(env, b"alpha")
            deb = forensics._Debouncer()
            first = await forensics.poll(deb)
            assert first["pending"] == 1, "one reading is not yet a verdict"
            second = await forensics.poll(deb)
            assert second["pending"] == 0, "two agreeing readings have settled"
            third = await forensics.poll(deb)
            assert third["pending"] == 0
            assert third["transitions"] == 0
        finally:
            await close_db()


async def _acquired(env, data: bytes) -> int:
    """A track with a recorded digest - i.e. a file the observer may watch."""
    p = _write(env / "album" / "a.mp3", data)
    tid = await _track(p)
    await execute(
        "INSERT INTO acquisition (track_id, file_path, sha256, file_size, "
        "source) VALUES (?, ?, ?, ?, 'download')",
        (tid, str(p), _sha(data), len(data)))
    return tid


class TestCanaryLastSeenPath:
    async def test_a_move_keeps_the_discovered_path(self, env):
        """Repointing the row is the whole response to a move, so the path
        the bytes turned up at cannot be thrown away."""
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(p)
            c = await forensics.register_canary(tid, str(p))
            dest = env / "other" / "a.mp3"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            await forensics.verify_canary(c["id"])
            row = await forensics.canary(c["id"])
            assert row["state"] == forensics.MOVED
            assert row["last_seen_path"] == str(dest), \
                "the only pointer to the relocated file was discarded"
        finally:
            await close_db()

    async def test_a_present_file_keeps_its_own_path(self, env):
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(p)
            c = await forensics.register_canary(tid, str(p))
            await forensics.verify_canary(c["id"])
            row = await forensics.canary(c["id"])
            assert row["last_seen_path"] == str(p)
        finally:
            await close_db()

    async def test_a_vanished_file_clears_it(self, env):
        await init_db()
        try:
            p = _write(env / "album" / "a.mp3", b"alpha")
            tid = await _track(p)
            c = await forensics.register_canary(tid, str(p))
            os.remove(str(p))
            await forensics.verify_canary(c["id"])
            row = await forensics.canary(c["id"])
            assert row["state"] == forensics.MISSING
            assert row["last_seen_path"] == ""
        finally:
            await close_db()
