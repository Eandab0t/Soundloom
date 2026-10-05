"""The content index and automatic moved-file suggestions.

The content index caches digests for library files that no track row
claims, so that "is this missing track's file sitting somewhere else?"
becomes a join instead of re-hashing 800-odd files on every page load.

The rule that matters: **only an identical SHA-256 produces a
suggestion.** There is no fuzzy tier, no title comparison, no duration
heuristic. Every row `find_moves()` returns is proof, which is what
lets the UI offer `[Restore this file]` as the primary action without
asking.

Two ways a suggestion must *not* appear:

- a track whose file is already accounted for somewhere. The index only
  holds unclaimed files, so a file that belongs to another track can
  never be offered as "the one that moved";
- a track with no recorded hash. There is nothing to compare against, so
  it is absent from the result rather than guessed at.
"""
import asyncio
import hashlib

import pytest

from backend.database import close_db, execute, fetch_all, fetch_one, init_db
from backend.services import content_index


@pytest.fixture(autouse=True)
async def _no_leaked_task():
    await content_index.shutdown()
    try:
        yield
    finally:
        await content_index.shutdown()


@pytest.fixture
def env(monkeypatch, tmp_path):
    import backend.config as cfg
    library = tmp_path / "Music"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "content.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
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


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

class TestIndexing:
    async def test_indexes_only_unclaimed_files(self, env):
        await init_db()
        try:
            claimed = _write(env / "tracked.mp3", b"tracked")
            unclaimed = _write(env / "stray.mp3", b"stray")
            await _track(claimed)

            await content_index.rebuild()
            await content_index.wait_idle()

            rows = await fetch_all("SELECT * FROM library_content")
            paths = {r["file_path"] for r in rows}
            assert unclaimed in paths
            assert claimed not in paths, "a file a track already owns is not a candidate"
        finally:
            await close_db()

    async def test_reports_state(self, env):
        await init_db()
        try:
            _write(env / "a.mp3", b"a")
            _write(env / "b.mp3", b"b")
            await content_index.rebuild()
            await content_index.wait_idle()

            s = await content_index.status()
            assert s["indexed"] == 2
            assert s["unclaimed"] == 2
            assert s["running"] is False
            assert s["indexed_bytes"] == 2
        finally:
            await close_db()

    async def test_rebuild_is_idempotent(self, env):
        await init_db()
        try:
            _write(env / "a.mp3", b"a")
            await content_index.rebuild()
            await content_index.wait_idle()
            first = (await fetch_all("SELECT * FROM library_content"))[0]

            await content_index.rebuild()
            await content_index.wait_idle()
            rows = await fetch_all("SELECT * FROM library_content")

            assert len(rows) == 1, "a second pass must not duplicate"
            assert rows[0]["digest"] == first["digest"]
        finally:
            await close_db()

    async def test_a_second_rebuild_is_refused_while_running(self, env):
        await init_db()
        try:
            for i in range(40):
                _write(env / f"t{i}.mp3", f"x{i}".encode())
            monkey = content_index.CHUNK_SIZE
            content_index.CHUNK_SIZE = 1
            try:
                first = await content_index.rebuild()
                second = await content_index.rebuild()
                assert first["action"] == "started"
                assert second["action"] == "already_running"
                await content_index.wait_idle()
            finally:
                content_index.CHUNK_SIZE = monkey
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Suggestions - the rule that only proof qualifies
# ---------------------------------------------------------------------------

class TestMoveSuggestions:
    async def _arrange_a_move(self, env):
        """A track whose file moved into a subfolder, with the old path gone."""
        payload = b"the bytes of a track that changed location"
        original = env / "Artist" / "Album" / "01 - Song.mp3"
        _write(original, payload)
        tid = await _track(original)
        await execute(
            "INSERT INTO acquisition (track_id, file_path, sha256, file_size, "
            "source) VALUES (?, ?, ?, ?, 'backfill')",
            (tid, str(original), _sha(payload), len(payload)))

        original.unlink()
        original.parent.rmdir()
        original.parent.parent.rmdir()
        moved = _write(env / "01 - Song.mp3", payload)

        await execute(
            "UPDATE tracks SET file_status='missing' WHERE id=?", (tid,))
        await content_index.rebuild()
        await content_index.wait_idle()
        return tid, str(original), moved

    async def test_finds_a_move_with_an_identical_digest(self, env):
        await init_db()
        try:
            tid, original, moved = await self._arrange_a_move(env)

            moves = await content_index.find_moves()
            assert len(moves) == 1
            m = moves[0]
            assert m["track_id"] == tid
            assert m["expected_path"] == original
            assert m["discovered_path"] == moved
            assert m["sha256"] == _sha(b"the bytes of a track that changed location")
        finally:
            await close_db()

    async def test_different_content_at_the_new_path_is_not_a_move(self, env):
        """Someone else's recording in our old place is a different problem,
        and reporting it as our file moving would be the exact wrong answer
        this feature exists to avoid."""
        await init_db()
        try:
            payload = b"our original recording"
            original = env / "song.mp3"
            _write(original, payload)
            tid = await _track(original)
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, source) "
                "VALUES (?, ?, ?, 'backfill')",
                (tid, str(original), _sha(payload)))
            original.unlink()
            _write(env / "song.mp3", b"a different recording entirely")
            await execute("UPDATE tracks SET file_status='missing' WHERE id=?", (tid,))

            await content_index.rebuild()
            await content_index.wait_idle()
            assert await content_index.find_moves() == []
        finally:
            await close_db()

    async def test_no_suggestion_without_a_recorded_hash(self, env):
        """A track with no hash cannot be identified by content, so it is
        absent from the result rather than matched on name or duration."""
        await init_db()
        try:
            tid = await _track(env / "gone.mp3", status="missing")
            _write(env / "gone.mp3", b"a file with the same name")
            await content_index.rebuild()
            await content_index.wait_idle()
            assert await content_index.find_moves() == []
        finally:
            await close_db()

    async def test_never_offers_a_file_another_track_claims(self, env):
        """The index only holds unclaimed files, and the query double-checks,
        so a duplicate copy belonging to a real track cannot be presented as
        the missing one's new home."""
        await init_db()
        try:
            payload = b"shared content"
            missing = env / "missing.mp3"
            _write(missing, payload)
            tid = await _track(missing, status="missing")
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, source) "
                "VALUES (?, ?, ?, 'backfill')",
                (tid, str(missing), _sha(payload)))
            missing.unlink()

            copy = _write(env / "copy.mp3", payload)
            await _track(copy)  # a real, present track owns it now

            await content_index.rebuild()
            await content_index.wait_idle()
            indexed = await fetch_all("SELECT file_path FROM library_content")
            assert copy not in {r["file_path"] for r in indexed}
            assert await content_index.find_moves() == []
        finally:
            await close_db()

    async def test_uses_the_latest_acquisition_only(self, env):
        """A track re-acquired has several rows; only the newest describes
        the file that is actually there now."""
        await init_db()
        try:
            payload = b"the current version of this file"
            path = _write(env / "v2.mp3", payload)
            tid = await _track(path, status="missing")
            (env / "v2.mp3").unlink()
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, source) "
                "VALUES (?, ?, 'stale0000', 'ytsearch1')",
                (tid, str(env / "v1.mp3")))
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, source) "
                "VALUES (?, ?, ?, 'backfill')",
                (tid, str(path), _sha(payload)))

            moved = _write(env / "elsewhere.mp3", payload)
            await content_index.rebuild()
            await content_index.wait_idle()

            moves = await content_index.find_moves()
            assert len(moves) == 1
            assert moves[0]["discovered_path"] == moved
        finally:
            await close_db()


class TestDeepReconcileWhenNothingIsUnclaimed:
    """A tidy library must still reconcile.

    The early exit that skipped hashing returned two values where three
    were expected, so a deep reconcile over a library with no unclaimed
    files raised ValueError rather than reporting counts. The real library
    has 825 unclaimed files and never saw it; a well-organised one would,
    and the absence of unclaimed files is exactly when it triggers.
    """

    async def test_missing_survive_an_empty_candidate_set(self, env):
        from backend.services import scanner

        await init_db()
        try:
            await _track(_write(env / "only.mp3", b"the one file"))
            # The row points at a path that was never written. Writing the
            # file would make it *unclaimed*, which is the opposite of the
            # condition under test and silently skips the branch entirely.
            gone = await _track(env / "gone.mp3", status="missing")
            digest = _sha(b"never on disk")
            await execute(
                "INSERT INTO acquisition (track_id, file_path, sha256, "
                "source, file_size) VALUES (?,?,?,'download',?)",
                (gone, "somewhere-else.mp3", digest, 13))

            # `sha256` lives on acquisition, not on tracks - a track only
            # has content identity if something recorded one.
            rows = await fetch_all(
                "SELECT id, file_path FROM tracks ORDER BY id")
            on_disk = {rows[0]["id"]: True, rows[1]["id"]: False}
            missing = [{"id": gone, "file_path": rows[1]["file_path"],
                        "sha256": digest}]
            present = [{"id": rows[0]["id"]}]

            pairs, still_missing, back_present = await scanner._find_moved(
                missing, present, rows, on_disk, env)

            assert pairs == [], "nothing unclaimed means nothing moved"
            assert [m["id"] for m in still_missing] == [gone], \
                "the missing list must survive unchanged"
            assert back_present == present
        finally:
            await close_db()
