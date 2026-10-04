"""match_threshold has to be able to stop a download.

The bug this file exists to prevent: `_process_job` built its candidate out of
the metadata the resolver returned and then scored that same metadata against
it. Every field matched itself, so the confidence was a constant 80.0 no
matter what the source actually was - always above the default threshold of
70, which made the setting decorative and put the wrong track in the library.

These tests drive the real `_process_job` with a stubbed resolver, so the
gate is exercised the way a user meets it: a job that fails the threshold
must never reach `download`.
"""
import pytest

from backend.services import downloader as dl
from backend.services.downloader import _job_intent, _search_payload
from backend.pipeline.models import TrackMetadata

WANTED_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class _FakeResolver:
    """Stands in for yt-dlp: reports whatever the 'source' claims about itself."""

    def __init__(self, meta):
        self._meta = meta

    async def resolve(self, url):
        return self._meta


class _FakeDownloader:
    """Records that it was reached, then fails loudly."""

    reached = False

    def __init__(self, *args, **kwargs):
        pass

    async def download_thumbnail(self, candidate, output_base):
        return ""

    async def download(self, candidate, output_base, progress_callback=None):
        _FakeDownloader.reached = True
        raise RuntimeError("REACHED_DOWNLOAD")


def _stub_source(monkeypatch, tmp_path, meta):
    """Point the pipeline at a fake source and a temp staging dir."""
    monkeypatch.setattr(dl, "YtdlpResolver", lambda: _FakeResolver(meta))
    monkeypatch.setattr(dl, "YtdlpDownloader", _FakeDownloader)

    def _tmp(job_id):
        d = tmp_path / f"job_{job_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(dl, "_job_tmp_dir", _tmp)
    _FakeDownloader.reached = False


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """Throwaway DB + settings + library, as in test_operations.op_env."""
    import backend.config as cfg
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "gate.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})
    return cfg


async def _mk_job(**fields):
    from backend.database import execute, fetch_one
    row = dict(source_url=WANTED_URL, status="pending", output_format="mp3",
               quality_profile="balanced")
    row.update(fields)
    cur = await execute(
        "INSERT INTO jobs (source_url, title, artist, album, album_artist, year,"
        " query, status, output_format, quality_profile)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["source_url"], row.get("title", ""), row.get("artist", ""),
         row.get("album", ""), row.get("album_artist", ""), row.get("year", 0),
         row.get("query", ""), row["status"], row["output_format"],
         row["quality_profile"]),
    )
    return await fetch_one("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,))


async def _job(job_id):
    from backend.database import fetch_one
    return await fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))


# ---------------------------------------------------------------------------
# Reading the request
# ---------------------------------------------------------------------------

class TestJobIntent:
    def test_prefers_what_the_user_named(self):
        intent = _job_intent({"title": "Lovefool", "artist": "The Cardigans",
                              "album": "Gran Turismo", "year": 1996})
        assert (intent.title, intent.artist) == ("Lovefool", "The Cardigans")
        assert intent.album == "Gran Turismo"
        assert intent.year == 1996

    def test_falls_back_to_what_was_typed_into_the_queue(self):
        intent = _job_intent({"query": "The Cardigans Lovefool"})
        assert intent.title == "The Cardigans Lovefool"

    def test_falls_back_to_the_search_url_payload(self):
        intent = _job_intent({}, "ytsearch1:The%20Cardigans%20Lovefool")
        assert intent.title == "The Cardigans Lovefool"

    def test_a_direct_url_states_nothing(self):
        """A pasted link is not a request, and pretending otherwise would make
        every score meaningless."""
        intent = _job_intent({}, WANTED_URL)
        assert intent.title == "" and intent.artist == ""

    def test_a_non_numeric_year_does_not_blow_up(self):
        assert _job_intent({"title": "x", "year": "n/a"}).year == 0

    def test_search_payload_is_empty_for_a_direct_url(self):
        assert _search_payload(WANTED_URL) == ""

    def test_the_search_payload_is_url_decoded(self):
        """Every scheme the registry can actually emit."""
        for url in ("ytsearch1:The%20Cardigans%20Lovefool",
                    "scsearch1:The%20Cardigans%20Lovefool",
                    "dzsearch:The%20Cardigans%20Lovefool"):
            assert _search_payload(url) == "The Cardigans Lovefool", url


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class TestMatchThresholdGates:
    async def test_a_wrong_track_is_never_downloaded(self, gate_env, monkeypatch,
                                                     tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            # The source answers with a completely different recording.
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Creep", artist="Radiohead", album="Pablo Honey",
                duration=238))
            job = await _mk_job(title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            row = await _job(job["id"])
            assert row["status"] == "failed"
            assert "below threshold" in row["error"]
            # The message has to say what was wanted and what arrived, or the
            # user cannot tell a bad match from a broken download.
            assert "Lovefool" in row["error"] and "Creep" in row["error"]
            assert not _FakeDownloader.reached, \
                "the wrong track was downloaded anyway"
        finally:
            await close_db()

    async def test_the_wrong_artist_for_the_right_title_is_rejected(self, gate_env,
                                                                    monkeypatch,
                                                                    tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Lovefool", artist="Carly Simon", duration=200))
            job = await _mk_job(title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            row = await _job(job["id"])
            assert row["status"] == "failed"
            assert "below threshold" in row["error"]
            assert not _FakeDownloader.reached
        finally:
            await close_db()

    async def test_the_right_track_is_allowed_through(self, gate_env, monkeypatch,
                                                      tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Lovefool", artist="The Cardigans",
                album="Gran Turismo", duration=200))
            job = await _mk_job(title="Lovefool", artist="The Cardigans",
                                album="Gran Turismo")
            await dl._process_job(job)

            assert _FakeDownloader.reached, "a correct match was blocked"
            row = await _job(job["id"])
            assert "below threshold" not in row["error"]
        finally:
            await close_db()

    async def test_a_publisher_prefixed_title_is_still_the_right_track(self,
                                                                      gate_env,
                                                                      monkeypatch,
                                                                      tmp_path):
        """`The Cardigans - Lovefool (Official Video)` is the track that was
        asked for. Rejecting it would be the gate doing harm, not good."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="The Cardigans - Lovefool (Official Video)",
                artist="The Cardigans", album="Gran Turismo", duration=232))
            job = await _mk_job(title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            assert _FakeDownloader.reached
        finally:
            await close_db()

    async def test_a_variant_the_user_did_not_ask_for_is_rejected(self, gate_env,
                                                                  monkeypatch,
                                                                  tmp_path):
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Lovefool (Live)", artist="The Cardigans", duration=260))
            job = await _mk_job(title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            assert (await _job(job["id"]))["status"] == "failed"
            assert not _FakeDownloader.reached
        finally:
            await close_db()

    async def test_the_setting_is_what_gates_not_a_hardcoded_number(self, gate_env,
                                                                    monkeypatch,
                                                                    tmp_path):
        """Raising the threshold past a good match must block it; lowering it
        below a bad match must let that one through."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            gate_env._cache["match_threshold"] = 95
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Lovefool", artist="The Cardigans",
                album="Gran Turismo", duration=200))
            job = await _mk_job(title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            assert (await _job(job["id"]))["status"] == "failed"
            assert not _FakeDownloader.reached

            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Creep", artist="Radiohead", duration=238))
            lenient = await _mk_job(title="Lovefool", artist="The Cardigans")
            gate_env._cache["match_threshold"] = 20
            await dl._process_job(lenient)

            assert _FakeDownloader.reached, "threshold=20 still blocked a match"
        finally:
            await close_db()

    async def test_a_bare_url_download_is_not_blocked(self, gate_env, monkeypatch,
                                                      tmp_path):
        """Nothing was requested, so there is nothing to hold the source to.
        Skipping the gate is right - but it must not be silent."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Whatever It Is", artist="Someone", duration=300))
            job = await _mk_job()          # no title, no artist, no query
            await dl._process_job(job)

            assert _FakeDownloader.reached
            row = await _job(job["id"])
            assert "below threshold" not in row["error"]
            # The row asked for nothing to keep, so it is filled in from the
            # source and the queue shows what was fetched.
            assert row["title"] == "Whatever It Is"
        finally:
            await close_db()

    async def test_the_request_survives_the_resolver(self, gate_env, monkeypatch,
                                                     tmp_path):
        """The row's title/artist are the request. Overwriting them with the
        source's guess would make the next attempt gate on the wrong thing."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Lovefool", artist="The Cardigans", duration=200))
            job = await _mk_job(title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            row = await _job(job["id"])
            assert (row["title"], row["artist"]) == ("Lovefool", "The Cardigans")
        finally:
            await close_db()

    async def test_a_rejected_search_job_reroutes_on_the_original_request(self,
                                                                        gate_env,
                                                                        monkeypatch,
                                                                        tmp_path):
        """Otherwise the fallback searches for what the rejected source claimed
        the user wanted, and walks further from the request with every hop."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            _stub_source(monkeypatch, tmp_path, TrackMetadata(
                title="Creep", artist="Radiohead", duration=238))
            job = await _mk_job(
                source_url="ytsearch1:The%20Cardigans%20Lovefool",
                title="Lovefool", artist="The Cardigans")
            await dl._process_job(job)

            row = await _job(job["id"])
            assert row["status"] == "pending"          # rerouted, not failed
            assert row["source_url"].startswith("dzsearch:")
            assert "Lovefool" in row["source_url"]
            assert "Creep" not in row["source_url"]
        finally:
            await close_db()
