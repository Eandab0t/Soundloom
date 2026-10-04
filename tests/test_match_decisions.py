"""Deciding a download the match gate blocked.

The gate refusing a bad match is only half the feature. If the only outcome is
a dead-end `failed` job, the user is pushed back into the queue tab to read a
prose error and guess. These tests pin the other half:

- **accept** is the *only* thing that lets a below-threshold candidate reach the
  downloader, and it buys exactly one attempt;
- **reject** cannot reach the downloader, with or without an override;
- **re-point** changes the request and re-arms the gate, so a wrong correction
  blocks again instead of being waved through;
- every decision leaves an audit record naming the score it overrode.

They drive the real `_process_job` with a stubbed resolver, the same way
`test_match_gate.py` does, so the gate is exercised the way a user meets it
rather than by calling the matcher directly.
"""
import json
from urllib.parse import unquote

import pytest
from fastapi import HTTPException

from backend.database import close_db, execute, fetch_one, init_db
from backend.pipeline.models import TrackMetadata
from backend.services import attention
from backend.services import downloader as dl
from backend.services import match_decisions as md
from backend.services import source_registry
from backend.services.attention import AttentionItem
from backend.services.attention_producers import (
    TYPE_BLOCKED_DOWNLOAD,
    BlockedMatchProducer,
)

WANTED_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# What the user asked for, and what the source hands back instead: the wrong
# artist for the right title, which the gate is supposed to refuse.
REQUEST = TrackMetadata(title="Lovefool", artist="The Cardigans")
WRONG_CANDIDATE = TrackMetadata(title="Lovefool", artist="Someone Else Entirely")


class _FakeResolver:
    """Stands in for yt-dlp: reports whatever the 'source' claims about itself."""

    def __init__(self, meta):
        self._meta = meta

    async def resolve(self, url):
        return self._meta


class _FakeDownloader:
    """Records that it was reached, then fails loudly so the test can see it."""

    reached = False

    def __init__(self, *args, **kwargs):
        pass

    async def download_thumbnail(self, candidate, output_base):
        return ""

    async def download(self, candidate, output_base, progress_callback=None):
        _FakeDownloader.reached = True
        raise RuntimeError("REACHED_DOWNLOAD")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Throwaway DB + settings + library, same shape as test_match_gate."""
    import backend.config as cfg
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "decide.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "library_path": str(library)})

    monkeypatch.setattr(dl, "YtdlpResolver", lambda: _FakeResolver(WRONG_CANDIDATE))
    monkeypatch.setattr(dl, "YtdlpDownloader", _FakeDownloader)

    def _tmp(job_id):
        d = tmp_path / f"job_{job_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(dl, "_job_tmp_dir", _tmp)

    attention.clear_producers()
    yield cfg
    attention.clear_producers()


async def _mk_job(**fields):
    """A job that names a track, so the gate has a request to judge against."""
    row = dict(source_url=WANTED_URL, status="pending", output_format="mp3",
               quality_profile="balanced", title=REQUEST.title,
               artist=REQUEST.artist)
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
    return await fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))


async def _blocked_job(**fields):
    """Run the gate for real and return the job it blocked."""
    _FakeDownloader.reached = False
    job = await _mk_job(**fields)
    await dl._process_job(job)
    return await _job(job["id"])


# ---------------------------------------------------------------------------
# Recording the block
# ---------------------------------------------------------------------------

class TestRecordingABlock:
    async def test_a_blocked_job_remembers_both_sides_of_the_comparison(self, env):
        """The UI has to be able to say 'you wanted X, you got Y' later."""
        await init_db()
        try:
            job = await _blocked_job()
            assert job["status"] == "failed"
            block = json.loads(job["match_block"])
            assert block["wanted_artist"] == "The Cardigans"
            assert block["wanted_title"] == "Lovefool"
            assert block["candidate_artist"] == "Someone Else Entirely"
            assert block["candidate_title"] == "Lovefool"
            assert block["threshold"] == 70
            assert 0 < block["score"] < 70
            assert block["explanation"]
        finally:
            await close_db()

    async def test_no_override_is_pending_on_a_job_nobody_accepted(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            assert job["match_override"] == ""
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------

class TestAccept:
    async def test_accept_queues_the_job_and_arms_a_one_shot_override(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            result = await md.accept(job["id"], note="it is the right song")
            after = await _job(job["id"])
            assert result["status"] == "accepted"
            assert after["status"] == "pending"
            assert after["match_override"] == md.OVERRIDE_ACCEPT
            assert after["error"] == ""
        finally:
            await close_db()

    async def test_an_accepted_candidate_reaches_the_downloader(self, env):
        """The whole point: accept is the one path past the gate."""
        await init_db()
        try:
            job = await _blocked_job()
            await md.accept(job["id"])
            assert not _FakeDownloader.reached, "gate should have blocked it first"

            # The retry, exactly as a worker would pick it up.
            claimed = await dl._pick_next_job()
            await dl._process_job(claimed)
            assert _FakeDownloader.reached, "accept did not reach the downloader"
        finally:
            await close_db()

    async def test_the_override_is_spent_by_that_one_attempt(self, env):
        """One decision buys one download, not every future retry."""
        await init_db()
        try:
            job = await _blocked_job()
            await md.accept(job["id"])
            assert await md.consume_override(job["id"]) is True
            assert await md.consume_override(job["id"]) is False
            assert (await _job(job["id"]))["match_override"] == ""
        finally:
            await close_db()

    async def test_a_later_retry_is_blocked_again(self, env):
        """After the accept is spent the gate is back to normal - including for
        a retry the user never saw, like a source fallback."""
        await init_db()
        try:
            job = await _blocked_job()
            await md.accept(job["id"])
            await dl._process_job(await _job(job["id"]))   # spends the override
            _FakeDownloader.reached = False
            await dl._process_job(await _job(job["id"]))   # next attempt
            after = await _job(job["id"])
            assert not _FakeDownloader.reached
            assert after["status"] == "failed"
            assert json.loads(after["match_block"])["score"] < 70
        finally:
            await close_db()

    async def test_an_accept_that_never_reached_the_gate_does_not_outlive_the_job(self, env):
        """An attempt can die before the match check - a resolve error, say.

        The waiver would still be sitting on a dead job, and a later manual
        Retry would then be waved through by a decision the user gave for an
        attempt that never happened.
        """
        class _BrokenResolver:
            async def resolve(self, url):
                raise dl.ResolveError("source is down")

        await init_db()
        try:
            # Block first with the working resolver, and only break it for the
            # retry - the point is an attempt that dies *after* the accept.
            job = await _blocked_job()
            await md.accept(job["id"])

            orig = dl.YtdlpResolver
            dl.YtdlpResolver = _BrokenResolver
            try:
                # A resolve error is retryable, so the job needs all of its
                # attempts before it is actually dead.
                for _ in range(6):
                    if (await _job(job["id"]))["status"] != "pending":
                        break
                    await dl._process_job(await _job(job["id"]))
            finally:
                dl.YtdlpResolver = orig
            after = await _job(job["id"])
            assert after["status"] == "failed"
            assert after["match_override"] == "", "a spent-by-death accept still waives the gate"
            assert not _FakeDownloader.reached
        finally:
            await close_db()

    async def test_accepting_records_the_score_it_overrode(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            await md.accept(job["id"], note="same song, live version")
            rows = await md.history(job["id"])
            assert len(rows) == 1
            assert rows[0]["decision"] == "accept"
            assert rows[0]["note"] == "same song, live version"
            assert rows[0]["wanted_artist"] == "The Cardigans"
            assert rows[0]["candidate_artist"] == "Someone Else Entirely"
            assert 0 < rows[0]["score"] < 70
            assert rows[0]["threshold"] == 70
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------

class TestReject:
    async def test_reject_cancels_the_job_and_arms_nothing(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            await md.reject(job["id"])
            after = await _job(job["id"])
            assert after["status"] == "cancelled"
            # No override: this is the state that cannot accidentally download.
            assert after["match_override"] == md.OVERRIDE_REJECTED
            assert await md.consume_override(job["id"]) is False
        finally:
            await close_db()

    async def test_a_rejected_job_is_never_claimed_by_a_worker(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            await md.reject(job["id"])
            assert await dl._pick_next_job() is None
        finally:
            await close_db()

    async def test_a_rejected_job_cannot_reach_the_downloader(self, env):
        """Negative check, the important one: reject must not become a back door."""
        await init_db()
        try:
            job = await _blocked_job()
            await md.reject(job["id"])
            _FakeDownloader.reached = False
            # Even if something re-queues it by hand, the gate still says no.
            await dl._process_job(await _job(job["id"]))
            assert not _FakeDownloader.reached, "a rejected job was downloaded"
        finally:
            await close_db()

    async def test_reject_records_its_own_audit_row(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            await md.reject(job["id"], note="different artist entirely")
            rows = await md.history(job["id"])
            assert [r["decision"] for r in rows] == ["reject"]
            assert rows[0]["note"] == "different artist entirely"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Re-point
# ---------------------------------------------------------------------------

class TestRePoint:
    async def test_repoint_rewrites_the_request_and_requeues(self, env):
        await init_db()
        try:
            job = await _blocked_job(source_url="ytsearch1:The%20Cardigans%20Lovefool")
            result = await md.repoint(
                job["id"], artist="The Cardigans", title="Lovefool (Remastered)"
            )
            after = await _job(job["id"])
            assert result["status"] == "repointed"
            assert after["title"] == "Lovefool (Remastered)"
            assert after["artist"] == "The Cardigans"
            assert after["status"] == "pending"
            # Whatever source the job ended up on (a blocked attempt reroutes
            # before it fails), the new question must be in the URL.
            assert source_registry.is_search_job_url(after["source_url"])
            assert unquote(after["source_url"]).endswith("Lovefool (Remastered)")
        finally:
            await close_db()

    async def test_repoint_does_not_buy_a_gate_bypass(self, env):
        """The whole difference from accept: the gate is re-armed, not waived."""
        await init_db()
        try:
            job = await _blocked_job(source_url="ytsearch1:The%20Cardigans%20Lovefool")
            await md.repoint(job["id"], artist="Still Wrong", title="Still Wrong")
            assert await md.consume_override(job["id"]) is False

            claimed = await dl._pick_next_job()
            await dl._process_job(claimed)
            after = await _job(job["id"])
            assert not _FakeDownloader.reached, "a re-point bypassed the gate"
            # The gate fired again, against the *corrected* intent - which is
            # what makes a re-point honest rather than a slow accept. A search
            # job whose gate trips is rerouted to the next source rather than
            # failed outright, so the evidence is the fresh block.
            block = json.loads(after["match_block"])
            assert block["wanted_artist"] == "Still Wrong"
            assert block["score"] < 70
        finally:
            await close_db()

    async def test_repointing_only_the_title_keeps_the_artist(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            await md.repoint(job["id"], title="Lovefool (Official Video)")
            after = await _job(job["id"])
            assert after["artist"] == "The Cardigans"
            assert after["title"] == "Lovefool (Official Video)"
        finally:
            await close_db()

    async def test_a_repoint_with_nothing_to_search_for_is_refused(self, env):
        """Re-pointing to an empty request would be a slower version of the
        same failure, so it is refused before it can waste a resolve."""
        await init_db()
        try:
            job = await _blocked_job()
            with pytest.raises(md.MatchDecisionError):
                await md.repoint(job["id"])
            assert (await _job(job["id"]))["status"] == "failed"
        finally:
            await close_db()

    async def test_a_direct_url_keeps_its_url_but_gains_the_new_intent(self, env):
        """A pasted link has no query to correct, so only the intent moves."""
        await init_db()
        try:
            job = await _blocked_job(source_url=WANTED_URL)
            await md.repoint(job["id"], title="Lovefool (Remastered)")
            after = await _job(job["id"])
            assert after["source_url"] == WANTED_URL
            assert after["title"] == "Lovefool (Remastered)"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Decisions on a job with no block
# ---------------------------------------------------------------------------

class TestDecisionsNeedABlock:
    async def test_deciding_an_unblocked_job_is_an_error(self, env):
        await init_db()
        try:
            job = await _mk_job()
            with pytest.raises(md.MatchDecisionError):
                await md.accept(job["id"])
            with pytest.raises(md.MatchDecisionError):
                await md.reject(job["id"])
            with pytest.raises(md.MatchDecisionError):
                await md.repoint(job["id"], title="Anything")
        finally:
            await close_db()

    async def test_a_decision_cannot_be_applied_twice(self, env):
        """The block is cleared by the decision, so a double-click cannot
        accept once and then re-arm the same job."""
        await init_db()
        try:
            job = await _blocked_job()
            await md.accept(job["id"])
            with pytest.raises(md.MatchDecisionError):
                await md.accept(job["id"])
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Surfacing the block in Needs Attention
# ---------------------------------------------------------------------------

class TestBlockedMatchProducer:
    async def test_a_failed_blocked_job_becomes_an_attention_item(self, env):
        await init_db()
        try:
            job = await _blocked_job()
            items = await BlockedMatchProducer().scan()
            assert len(items) == 1
            item = items[0]
            assert item.type == TYPE_BLOCKED_DOWNLOAD
            assert item.entity_type == "job"
            assert item.entity_id == job["id"]
            assert "The Cardigans - Lovefool" in item.title
            assert "Someone Else Entirely" in item.description
        finally:
            await close_db()

    async def test_the_item_carries_the_three_decision_routes(self, env):
        await init_db()
        try:
            await _blocked_job()
            action = (await BlockedMatchProducer().scan())[0].action
            assert action["kind"] == "blocked_download"
            assert action["score"] < action["threshold"] == 70
            for key in ("accept_route", "reject_route", "repoint_route"):
                assert key in action, key
            assert action["accept_route"].endswith("/accept")
            assert action["reject_route"].endswith("/reject")
            assert action["repoint_route"].endswith("/repoint")
        finally:
            await close_db()

    async def test_a_job_that_is_merely_failed_is_not_a_match_problem(self, env):
        """A network failure is not something to ask the user to accept,
        reject or re-point."""
        await init_db()
        try:
            await _mk_job(status="failed")
            assert await BlockedMatchProducer().scan() == []
        finally:
            await close_db()

    async def test_a_blocked_job_still_retrying_is_not_reported(self, env):
        """The gate fires before a job gives up; only a job that actually ended
        blocked is worth a human decision."""
        await init_db()
        try:
            job = await _blocked_job()
            await execute("UPDATE jobs SET status='pending' WHERE id=?", (job["id"],))
            assert await BlockedMatchProducer().scan() == []
        finally:
            await close_db()

    async def test_a_decided_job_stops_being_reported(self, env):
        """Accept/reject/re-point all take the job out of 'failed', so the item
        retires itself on the next scan instead of coming back."""
        await init_db()
        try:
            job = await _blocked_job()
            await md.reject(job["id"])
            assert await BlockedMatchProducer().scan() == []
        finally:
            await close_db()

    async def test_an_unreadable_block_is_skipped_not_fatal(self, env):
        await init_db()
        try:
            bad = await _mk_job(status="failed")
            await execute(
                "UPDATE jobs SET match_block='{not json' WHERE id=?", (bad["id"],)
            )
            assert await BlockedMatchProducer().scan() == []
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

async def _raise_blocked_item():
    """Put a blocked job on the Needs Attention list, as a producer scan would."""
    items = await BlockedMatchProducer().scan()
    assert len(items) == 1, "expected exactly one blocked item"
    return await attention.raise_item("matches", items[0])


class TestDecisionEndpoints:
    async def test_accept_resolves_the_item_and_requeues_the_job(self, env):
        from backend.api import matches as api
        await init_db()
        try:
            job = await _blocked_job()
            raised = await _raise_blocked_item()
            result = await api.accept_blocked_download(job["id"])
            after = await _job(job["id"])
            closed = await attention.get_item(raised["id"])
            assert result["status"] == "accepted"
            assert after["status"] == "pending"
            assert closed["status"] == "resolved"
            assert "accepted" in closed["resolved_note"]
        finally:
            await close_db()

    async def test_reject_resolves_the_item_and_cancels_the_job(self, env):
        from backend.api import matches as api
        await init_db()
        try:
            job = await _blocked_job()
            raised = await _raise_blocked_item()
            result = await api.reject_blocked_download(job["id"])
            after = await _job(job["id"])
            closed = await attention.get_item(raised["id"])
            assert result["status"] == "rejected"
            assert after["status"] == "cancelled"
            assert closed["status"] == "resolved"
            assert "rejected" in closed["resolved_note"]
        finally:
            await close_db()

    async def test_repoint_carries_the_corrected_request_through(self, env):
        from backend.api import matches as api
        await init_db()
        try:
            job = await _blocked_job()
            raised = await _raise_blocked_item()
            result = await api.repoint_blocked_download(
                job["id"],
                api._RePoint(artist="The Cardigans", title="Lovefool (Live)"),
            )
            after = await _job(job["id"])
            closed = await attention.get_item(raised["id"])
            assert result["status"] == "repointed"
            assert after["title"] == "Lovefool (Live)"
            assert after["status"] == "pending"
            assert closed["status"] == "resolved"
        finally:
            await close_db()

    async def test_the_route_a_producer_writes_is_the_route_that_works(self, env):
        """Regression: the producer builds these routes before the attention
        item exists, so it can only know the job id. When the endpoints were
        keyed by item id instead, every button in the UI 404'd - and the
        endpoint tests passed, because they were handed the item id by hand.

        This drives the exact URL the producer emitted.
        """
        from backend.api import matches as api
        await init_db()
        try:
            job = await _blocked_job()
            raised = await _raise_blocked_item()
            assert raised["action"]["accept_route"] == f"/api/matches/{job['id']}/accept"
            result = await api.accept_blocked_download(job["id"])
            assert result["status"] == "accepted"
        finally:
            await close_db()

    async def test_deciding_an_unknown_job_is_a_409(self, env):
        from backend.api import matches as api
        await init_db()
        try:
            with pytest.raises(HTTPException) as exc:
                await api.accept_blocked_download(9999)
            assert exc.value.status_code == 409
        finally:
            await close_db()

    async def test_every_decision_notes_the_score_it_was_made_against(self, env):
        """The audit line on the attention row is how a user finds out later
        *why* something was waved through. A '?%' placeholder in any of the
        three would be worse than no number."""
        from backend.api import matches as api
        await init_db()
        try:
            for verb, call in (
                ("accept", lambda j: api.accept_blocked_download(j)),
                ("reject", lambda j: api.reject_blocked_download(j)),
                ("repoint", lambda j: api.repoint_blocked_download(
                    j, api._RePoint(title="Something Else"))),
            ):
                job = await _blocked_job()
                raised = await _raise_blocked_item()
                await call(job["id"])
                closed = await attention.get_item(raised["id"])
                block = json.loads((await _job(job["id"]))["match_block"] or "{}")
                assert closed["resolved_note"].startswith(verb.split("_")[0]), closed
                assert "?" not in closed["resolved_note"], (
                    f"{verb} left an unknown score in its note: {closed['resolved_note']!r}"
                )
                assert "vs" in closed["resolved_note"]
        finally:
            await close_db()

    async def test_history_returns_the_decisions(self, env):
        from backend.api import matches as api
        await init_db()
        try:
            job = await _blocked_job()
            await md.accept(job["id"], note="yes, that one")
            out = await api.decision_history(job_id=job["id"])
            assert len(out["decisions"]) == 1
            assert out["decisions"][0]["decision"] == "accept"
            assert out["decisions"][0]["note"] == "yes, that one"
        finally:
            await close_db()
