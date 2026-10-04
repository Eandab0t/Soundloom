"""Tests for the Needs Attention layer.

What has to hold for this list to be trustworthy rather than a second log page:

- re-raising the same problem folds into one row instead of flooding the list;
- a problem that comes back after the user cleared it reopens instead of hiding;
- a problem a producer stops reporting disappears on its own;
- a producer that *fails* does not silently retire its own items;
- one producer failing never hides what another one found.
"""
from types import SimpleNamespace

import pytest

from backend.services import attention
from backend.services.attention import (
    AttentionError,
    AttentionItem,
    AttentionStatus,
    Severity,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """The producer registry is module-global state; keep it out of other tests."""
    attention.clear_producers()
    yield
    attention.clear_producers()


@pytest.fixture
def att_env(monkeypatch, tmp_path):
    """Throwaway DB + settings, same shape as test_operations.op_env."""
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "attention.db")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "_cache", {**cfg.DEFAULTS, "fetch_confidence": 0.7})
    return cfg


def _item(key="test:1", **kw):
    """A minimal valid item; tests override only what they care about."""
    fields = dict(
        dedupe_key=key,
        type="test_problem",
        title="Something needs a decision",
    )
    fields.update(kw)
    return AttentionItem(**fields)


class _FakeProducer:
    """A producer whose scan output - or failure - the test controls."""

    def __init__(self, name, items=(), error=None):
        self.name = name
        self.items = list(items)
        self.error = error
        self.scans = 0

    async def scan(self):
        self.scans += 1
        if self.error is not None:
            raise self.error
        return list(self.items)


async def _open_items():
    return await attention.list_items(status=AttentionStatus.OPEN.value)


async def _keys(status=None):
    rows = await attention.list_items(
        status=status or AttentionStatus.OPEN.value)
    return [r["dedupe_key"] for r in rows]


async def _ordered_keys(limit=100, offset=0):
    rows = await attention.list_items(limit=limit, offset=offset)
    return [r["dedupe_key"] for r in rows]


async def _mk_track(path="/library/a.mp3", title="Sunset", artist="Kay Tran"):
    from backend.database import execute
    cur = await execute(
        "INSERT INTO tracks (file_path, title, artist) VALUES (?, ?, ?)",
        (path, title, artist))
    return cur.lastrowid


async def _mk_candidate(track_id, confidence, status="pending",
                        source="songkick", path="/library/a.mp3"):
    from backend.database import execute
    cur = await execute(
        "INSERT INTO fetch_candidates "
        "(track_id, file_path, source, confidence, status) VALUES (?, ?, ?, ?, ?)",
        (track_id, path, source, confidence, status))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class TestAttentionItem:
    async def test_requires_a_dedupe_key(self):
        """Without an identity the layer cannot dedupe, so refuse it loudly."""
        with pytest.raises(AttentionError):
            AttentionItem(dedupe_key="", type="t", title="x")

    async def test_accepts_severity_as_a_string(self):
        """Producers read config values; they should not have to import the enum."""
        item = AttentionItem(dedupe_key="a:1", type="t", title="x",
                             severity="critical")
        assert item.severity is Severity.CRITICAL

    async def test_rejects_an_unknown_severity(self):
        with pytest.raises(AttentionError):
            _item(severity="catastrophic")

    async def test_severity_ranks_critical_first(self):
        ranks = [s.rank for s in (Severity.CRITICAL, Severity.HIGH,
                                  Severity.MEDIUM, Severity.LOW)]
        assert ranks == sorted(ranks) == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Raising and dedupe
# ---------------------------------------------------------------------------

class TestRaiseItem:
    async def test_first_raise_inserts_one_open_item(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            assert row["status"] == "open"
            assert row["seen_count"] == 1
            assert row["source"] == "p"
            assert row["severity"] == "medium"
        finally:
            await close_db()

    async def test_the_same_problem_folds_into_one_row(self, att_env):
        """A scan that runs nightly must not turn one problem into 300 rows."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            await attention.raise_item("p", _item("p:1"))
            await attention.raise_item("p", _item("p:1"))
            row = await attention.raise_item("p", _item("p:1"))

            items = await attention.list_items(status="all")
            assert len(items) == 1
            assert row["seen_count"] == 3
        finally:
            await close_db()

    async def test_re_raising_refreshes_the_mutable_fields(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            await attention.raise_item("p", _item("p:1", title="Stale title"))
            row = await attention.raise_item("p", _item(
                "p:1", title="Fresh title", severity=Severity.HIGH))

            assert row["title"] == "Fresh title"
            assert row["severity"] == "high"
        finally:
            await close_db()

    async def test_action_payload_round_trips_as_json(self, att_env):
        """The action is how the frontend avoids hardcoding a type-to-screen map."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1", action={
                "kind": "operation",
                "route": "/operations/9",
                "retry_route": "/api/operations/9/retry",
            }))
            assert row["action"]["retry_route"] == "/api/operations/9/retry"

            reread = await attention.get_item(row["id"])
            assert reread["action"] == row["action"]
        finally:
            await close_db()

    async def test_entity_reference_is_preserved(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item(
                "p:1", entity_type="track", entity_id=42))
            assert (row["entity_type"], row["entity_id"]) == ("track", 42)
        finally:
            await close_db()

    async def test_a_problem_that_comes_back_reopens(self, att_env):
        """Cleared by the user, then detected again: it must not stay hidden."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            await attention.resolve_item(row["id"], "fixed by hand")

            again = await attention.raise_item("p", _item("p:1"))

            assert again["id"] == row["id"]      # same row, not a new one
            assert again["status"] == "open"
            assert again["seen_count"] == 2
            assert again["resolved_at"] is None
            assert again["resolved_note"] == ""
            assert len(await attention.list_items(status="all")) == 1
        finally:
            await close_db()

    async def test_dedupe_is_global_on_the_key(self, att_env):
        """Why producers namespace their keys (`ops:op:9`), and why they must.

        The UNIQUE constraint is on `dedupe_key` alone, so two producers using
        the same bare key collapse into one row and the second scan silently
        inherits the first one's `source` - which would break auto-resolution
        for whichever producer lost.
        """
        from backend.database import init_db, close_db
        await init_db()
        try:
            await attention.raise_item("a", _item("9"))
            row = await attention.raise_item("b", _item("9"))

            everything = await attention.list_items(status="all")
            assert len(everything) == 1
            assert row["source"] == "b"
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------

class TestResolution:
    async def test_resolve_keeps_the_row_as_history(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            closed = await attention.resolve_item(row["id"], "moved the file")

            assert closed["status"] == "resolved"
            assert closed["resolved_note"] == "moved the file"
            assert closed["resolved_at"]
            assert await _keys() == []            # gone from the open list
            assert len(await attention.list_items(status="all")) == 1
        finally:
            await close_db()

    async def test_dismiss_is_distinct_from_resolve(self, att_env):
        """'I dealt with it' and 'not my problem' are different user intents."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            closed = await attention.dismiss_item(row["id"], "not my problem")
            assert closed["status"] == "dismissed"
            assert closed["resolved_note"] == "not my problem"
        finally:
            await close_db()

    async def test_reopen_clears_the_resolution(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            await attention.resolve_item(row["id"], "fixed")
            reopened = await attention.reopen_item(row["id"])

            assert reopened["status"] == "open"
            assert reopened["resolved_at"] is None
            assert reopened["resolved_note"] == ""
            assert reopened["seen_count"] == 1    # undoing is not a re-detection
        finally:
            await close_db()

    async def test_unknown_ids_return_none(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            assert await attention.resolve_item(999) is None
            assert await attention.dismiss_item(999) is None
            assert await attention.reopen_item(999) is None
            assert await attention.get_item(999) is None
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestListing:
    async def _seed(self):
        from backend.database import init_db
        await init_db()
        await attention.raise_item("p", _item(
            "p:low", type="storage", severity=Severity.LOW))
        await attention.raise_item("p", _item(
            "p:crit", type="network", severity=Severity.CRITICAL))
        await attention.raise_item("p", _item(
            "p:med", type="network", severity=Severity.MEDIUM))

    async def test_lists_worst_first(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            assert await _keys() == ["p:crit", "p:med", "p:low"]
        finally:
            await close_db()

    async def test_filters_by_type(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            rows = await attention.list_items(type="network")
            assert [r["dedupe_key"] for r in rows] == ["p:crit", "p:med"]
            assert await attention.list_items(type="nothing") == []
        finally:
            await close_db()

    async def test_filters_by_severity(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            assert await attention.list_items(severity="high") == []
            rows = await attention.list_items(severity="critical")
            assert [r["dedupe_key"] for r in rows] == ["p:crit"]
        finally:
            await close_db()

    async def test_rejects_an_unknown_severity_filter(self, att_env):
        """Silently returning nothing would read as 'nothing is wrong'."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            with pytest.raises(AttentionError):
                await attention.list_items(severity="urgent")
        finally:
            await close_db()

    async def test_paging_walks_the_list_without_repeats(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            page1 = await _ordered_keys(limit=2, offset=0)
            page2 = await _ordered_keys(limit=2, offset=2)
            assert page1 == ["p:crit", "p:med"]
            assert page2 == ["p:low"]
            assert not set(page1) & set(page2)
        finally:
            await close_db()

    async def test_status_all_includes_closed_items(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            row = await attention.raise_item("p", _item("p:crit"))
            await attention.resolve_item(row["id"])
            assert len(await attention.list_items(status="all")) == 3
            assert await _keys() == ["p:med", "p:low"]
        finally:
            await close_db()

    async def test_counts_group_by_type_largest_first(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            counts = await attention.counts_by_type()
            assert counts == {"total": 3, "by_type": {"network": 2, "storage": 1}}
            assert list(counts["by_type"]) == ["network", "storage"]
        finally:
            await close_db()

    async def test_counts_ignore_closed_items_unless_asked(self, att_env):
        from backend.database import close_db
        await self._seed()
        try:
            rows = await attention.list_items(type="network")
            await attention.resolve_item(rows[0]["id"])
            assert await attention.counts_by_type() == {
                "total": 2, "by_type": {"network": 1, "storage": 1}}
            assert (await attention.counts_by_type(status="all"))["total"] == 3
        finally:
            await close_db()

    async def test_severity_counts_always_report_every_bucket(self, att_env):
        """The UI draws four buckets; an empty one must read as zero, not vanish."""
        from backend.database import close_db
        await self._seed()
        try:
            counts = await attention.counts_by_severity()
            assert counts == {"critical": 1, "high": 0, "medium": 1, "low": 1}
            assert sum(counts.values()) == 3
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The producer registry and scan lifecycle
# ---------------------------------------------------------------------------

class TestProducerRegistry:
    async def test_registers_and_reports_a_summary(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            attention.register_producer(_FakeProducer("p", [_item("p:1")]))
            summary = await attention.run_producers()
            assert summary == {"producers": 1, "raised": 1,
                               "auto_resolved": 0, "failed": 0}
        finally:
            await close_db()

    async def test_re_registering_a_name_replaces_it(self, att_env):
        """Otherwise a reload would scan the same system twice per cycle."""
        first = _FakeProducer("dup")
        second = _FakeProducer("dup")
        assert attention.register_producer(first) is first
        attention.register_producer(second)
        assert attention.registered_producers() == [second]

    async def test_a_nameless_producer_is_refused(self, att_env):
        with pytest.raises(AttentionError):
            attention.register_producer(SimpleNamespace(name="", scan=None))


class TestScanLifecycle:
    async def test_a_problem_that_stops_being_reported_disappears(self, att_env):
        """Otherwise the list only ever grows and starts lying about the library."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            producer = _FakeProducer("p", [_item("p:1"), _item("p:2")])
            attention.register_producer(producer)
            await attention.run_producers()

            producer.items = [_item("p:1")]        # p:2 got fixed by itself
            summary = await attention.run_producers()

            assert summary["auto_resolved"] == 1
            assert await _keys() == ["p:1"]
            everything = {r["dedupe_key"]: r
                          for r in await attention.list_items(status="all")}
            assert everything["p:2"]["status"] == "resolved"
            assert everything["p:2"]["resolved_note"] == "No longer detected"
        finally:
            await close_db()

    async def test_a_user_resolved_item_that_is_still_detected_reopens(self, att_env):
        """Otherwise the user's 'fix' would permanently mask a real problem."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            attention.register_producer(_FakeProducer("p", [_item("p:1")]))
            row = await attention.raise_item("p", _item("p:1"))
            await attention.resolve_item(row["id"], "I sorted it")

            await attention.run_producers()
            again = await attention.get_item(row["id"])
            assert again["status"] == "open"
        finally:
            await close_db()

    async def test_a_failing_producer_keeps_its_items(self, att_env):
        """A crashed scan knows nothing; retiring on it would hide real warnings."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            producer = _FakeProducer("p", [_item("p:1")])
            attention.register_producer(producer)
            await attention.run_producers()

            producer.error = RuntimeError("the thing it reads is unavailable")
            summary = await attention.run_producers()

            assert summary["failed"] == 1
            assert summary["auto_resolved"] == 0
            assert await _keys() == ["p:1"]
        finally:
            await close_db()

    async def test_one_failing_producer_does_not_hide_another(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            good = _FakeProducer("good", [_item("good:1")])
            bad = _FakeProducer("bad", [_item("bad:1")], error=RuntimeError("boom"))
            attention.register_producer(good)
            attention.register_producer(bad)

            summary = await attention.run_producers()

            assert (summary["raised"], summary["failed"]) == (1, 1)
            assert await _keys() == ["good:1"]
        finally:
            await close_db()

    async def test_only_a_successful_producer_may_auto_resolve(self, att_env):
        """The sharp version: bad reports nothing and fails; good reports nothing
        because it is fixed. Only good's item may be retired."""
        from backend.database import init_db, close_db
        await init_db()
        try:
            good = _FakeProducer("good", [_item("good:1")])
            bad = _FakeProducer("bad", [_item("bad:1")])
            attention.register_producer(good)
            attention.register_producer(bad)
            await attention.run_producers()

            good.items = []                       # really fixed
            bad.error = RuntimeError("boom")      # unknown, not fixed
            summary = await attention.run_producers()

            assert summary["auto_resolved"] == 1
            assert await _keys() == ["bad:1"]
        finally:
            await close_db()

    async def test_a_bad_item_does_not_discard_the_rest_of_the_scan(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            broken = SimpleNamespace(
                dedupe_key=None, type="t", title="broken", severity=Severity.LOW,
                description="", entity_type="", entity_id=None, action={})
            attention.register_producer(_FakeProducer("p", [broken, _item("p:ok")]))

            summary = await attention.run_producers()

            assert (summary["raised"], summary["failed"]) == (1, 1)
            assert await _keys() == ["p:ok"]
        finally:
            await close_db()

    async def test_repeated_scans_are_idempotent(self, att_env):
        from backend.database import init_db, close_db
        await init_db()
        try:
            attention.register_producer(_FakeProducer("p", [_item("p:1")]))
            for _ in range(5):
                await attention.run_producers()
            items = await attention.list_items(status="all")
            assert len(items) == 1
            assert items[0]["seen_count"] == 5
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# The producers that ship with Soundloom
# ---------------------------------------------------------------------------

class TestInterruptedOperationProducer:
    async def test_raises_failed_and_abandoned_operations(self, att_env):
        from backend.database import init_db, close_db
        from backend.services import operations as ops
        from backend.services.operations import OpState
        from backend.services.attention_producers import (
            InterruptedOperationProducer, TYPE_INTERRUPTED_OPERATION)

        await init_db()
        try:
            failed = await ops.begin_operation(
                "job:1", title="Lovefool", artist="The Cardigans")
            await ops.advance(failed, OpState.DOWNLOADING)
            await ops.advance(failed, OpState.FAILED, error="connection reset")

            abandoned = await ops.begin_operation(
                "job:2", title="Hounds of Love",
                source_url="https://example.test/track")
            await ops.advance(abandoned, OpState.DOWNLOADING)
            await ops.advance(abandoned, OpState.ABANDONED)

            items = await InterruptedOperationProducer().scan()
            by_key = {i.dedupe_key: i for i in items}

            assert set(by_key) == {f"operations:op:{failed}",
                                   f"operations:op:{abandoned}"}
            assert all(i.type == TYPE_INTERRUPTED_OPERATION for i in items)
            assert by_key[f"operations:op:{failed}"].severity is Severity.HIGH
            assert by_key[f"operations:op:{abandoned}"].severity is Severity.MEDIUM
            assert "connection reset" in by_key[
                f"operations:op:{failed}"].description
            assert by_key[f"operations:op:{failed}"].entity_id == failed

            action = by_key[f"operations:op:{failed}"].action
            assert action["route"] == f"/operations/{failed}"
            assert action["retry_route"] == f"/api/operations/{failed}/retry"
            assert action["discard_route"] == f"/api/operations/{failed}/discard"
        finally:
            await close_db()

    async def test_ignores_operations_that_are_fine(self, att_env):
        from backend.database import init_db, close_db
        from backend.services import operations as ops
        from backend.services.operations import OpState
        from backend.services.attention_producers import (
            InterruptedOperationProducer)

        await init_db()
        try:
            done = await ops.begin_operation("job:1", title="Fine")
            for state in (OpState.DOWNLOADING, OpState.DOWNLOADED,
                          OpState.VALIDATED, OpState.TAGGED,
                          OpState.ORGANIZED, OpState.COMMITTED):
                await ops.advance(done, state)

            # A mid-flight operation is not a decision the user owes yet.
            in_progress = await ops.begin_operation("job:2", title="Working")
            await ops.advance(in_progress, OpState.DOWNLOADING)

            assert await InterruptedOperationProducer().scan() == []
        finally:
            await close_db()

    async def test_falls_back_to_the_source_url_when_nothing_is_tagged(self, att_env):
        from backend.database import init_db, close_db
        from backend.services import operations as ops
        from backend.services.operations import OpState
        from backend.services.attention_producers import (
            InterruptedOperationProducer)

        await init_db()
        try:
            op_id = await ops.begin_operation(
                "job:1", source_url="https://example.test/loveless")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED)

            item = (await InterruptedOperationProducer().scan())[0]
            assert item.title == "Interrupted: https://example.test/loveless"
        finally:
            await close_db()


class TestMetadataReviewProducer:
    async def test_raises_only_candidates_below_the_threshold(self, att_env):
        from backend.database import init_db, close_db
        from backend.services.attention_producers import (
            MetadataReviewProducer, TYPE_METADATA_REVIEW)

        await init_db()
        try:
            track = await _mk_track()
            weak = await _mk_candidate(track, 0.42)
            await _mk_candidate(track, 0.91)   # confident: applied on its own

            items = await MetadataReviewProducer().scan()

            assert [i.dedupe_key for i in items] == [f"metadata:candidate:{weak}"]
            assert items[0].type == TYPE_METADATA_REVIEW
            assert items[0].entity_type == "track"
            assert items[0].entity_id == track
            assert "Sunset" in items[0].title
            assert "42%" in items[0].description
            assert items[0].action == {
                "kind": "match",
                "route": f"/identify?track_id={track}",
                "label": "Choose correct tags",
            }
        finally:
            await close_db()

    async def test_severity_follows_how_far_below_the_line_it_fell(self, att_env):
        from backend.database import init_db, close_db
        from backend.services.attention_producers import MetadataReviewProducer

        await init_db()
        try:
            track = await _mk_track()
            await _mk_candidate(track, 0.30)
            await _mk_candidate(track, 0.60)

            severities = [i.severity
                          for i in await MetadataReviewProducer().scan()]
            assert severities == [Severity.HIGH, Severity.MEDIUM]
        finally:
            await close_db()

    async def test_ignores_candidates_that_are_already_settled(self, att_env):
        from backend.database import init_db, close_db
        from backend.services.attention_producers import MetadataReviewProducer

        await init_db()
        try:
            track = await _mk_track()
            await _mk_candidate(track, 0.20, status="applied")
            assert await MetadataReviewProducer().scan() == []
        finally:
            await close_db()

    async def test_falls_back_to_the_filename_when_tags_are_missing(self, att_env):
        from backend.database import init_db, close_db
        from backend.services.attention_producers import MetadataReviewProducer

        await init_db()
        try:
            track = await _mk_track(title="", artist="")
            await _mk_candidate(track, 0.25, path="/library/untitled take.mp3")

            item = (await MetadataReviewProducer().scan())[0]
            assert "untitled take" in item.title
        finally:
            await close_db()


class TestDefaultProducers:
    async def test_registers_every_default_producer(self, att_env):
        """Every system that owns state the user must decide about has one.

        'matches' is the blocked-download producer: a download the match gate
        refused is a question for the user, not a failed job to read about in
        the queue tab.
        """
        from backend.services.attention_producers import register_default_producers

        registered = register_default_producers()
        assert {p.name for p in registered} == {"operations", "metadata", "matches"}
        assert attention.registered_producers() == registered

    async def test_a_full_scan_on_an_empty_library_raises_nothing(self, att_env):
        from backend.database import init_db, close_db
        from backend.services.attention_producers import register_default_producers

        await init_db()
        try:
            register_default_producers()
            summary = await attention.run_producers()
            assert summary == {"producers": 3, "raised": 0,
                               "auto_resolved": 0, "failed": 0}
        finally:
            await close_db()

    async def test_a_real_scan_records_its_producer_as_the_source(self, att_env):
        """Auto-resolution is scoped by `source`, so that column has to be right."""
        from backend.database import init_db, close_db
        from backend.services import operations as ops
        from backend.services.operations import OpState
        from backend.services.attention_producers import register_default_producers

        await init_db()
        try:
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED, error="killed")
            register_default_producers()
            await attention.run_producers()

            rows = await attention.list_items(status="all")
            assert [r["source"] for r in rows] == ["operations"]
            assert rows[0]["dedupe_key"] == f"operations:op:{op_id}"
        finally:
            await close_db()

    async def test_retrying_an_operation_quietens_its_attention_item(self, att_env):
        """The end-to-end promise: act on the item, the list gets quiet."""
        from backend.database import init_db, close_db
        from backend.services import operations as ops
        from backend.services.operations import OpState
        from backend.services.attention_producers import register_default_producers

        await init_db()
        try:
            op_id = await ops.begin_operation("job:1", title="Lovefool")
            await ops.advance(op_id, OpState.DOWNLOADING)
            await ops.advance(op_id, OpState.FAILED, error="killed")
            register_default_producers()
            await attention.run_producers()
            assert len(await _open_items()) == 1

            await ops.retry_operation(op_id)
            summary = await attention.run_producers()

            assert summary["auto_resolved"] == 1
            assert await _open_items() == []
        finally:
            await close_db()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestAttentionApi:
    async def test_list_returns_items_and_grouped_counts(self, att_env):
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            await attention.raise_item("p", _item(
                "p:1", type="network", severity=Severity.CRITICAL))
            await attention.raise_item("p", _item(
                "p:2", type="storage", severity=Severity.LOW))

            body = await api.list_attention()

            assert [i["dedupe_key"] for i in body["items"]] == ["p:1", "p:2"]
            assert body["counts"] == {"total": 2,
                                      "by_type": {"network": 1, "storage": 1}}
            assert body["severity_counts"] == {"critical": 1, "high": 0,
                                               "medium": 0, "low": 1}
            assert isinstance(body["items"][0]["action"], dict)
        finally:
            await close_db()

    async def test_list_passes_filters_and_paging_through(self, att_env):
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            await attention.raise_item("p", _item(
                "p:1", type="network", severity=Severity.CRITICAL))
            await attention.raise_item("p", _item("p:2", type="storage"))

            body = await api.list_attention(type="network", limit=10, offset=0)
            assert [i["dedupe_key"] for i in body["items"]] == ["p:1"]
            # Counts are scoped by status, not by the filter: the chips need
            # every type's total so the user can switch away from this one.
            assert body["counts"] == {"total": 2,
                                      "by_type": {"network": 1, "storage": 1}}
        finally:
            await close_db()

    async def test_list_rejects_an_unknown_severity_with_422(self, att_env):
        from fastapi import HTTPException
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            with pytest.raises(HTTPException) as exc:
                await api.list_attention(severity="urgent")
            assert exc.value.status_code == 422
        finally:
            await close_db()

    async def test_counts_endpoint(self, att_env):
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            await attention.raise_item("p", _item("p:1", type="network"))
            body = await api.attention_counts()
            assert body["counts"]["total"] == 1
            assert body["severity_counts"]["medium"] == 1
        finally:
            await close_db()

    async def test_refresh_runs_the_producers(self, att_env):
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            attention.register_producer(_FakeProducer("p", [_item("p:1")]))
            body = await api.refresh_attention()

            assert body == {"producers": 1, "raised": 1,
                            "auto_resolved": 0, "failed": 0}
            assert len(await _open_items()) == 1
        finally:
            await close_db()

    async def test_get_returns_the_item_and_404s_when_unknown(self, att_env):
        from fastapi import HTTPException
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            assert (await api.get_attention(row["id"]))["dedupe_key"] == "p:1"

            with pytest.raises(HTTPException) as exc:
                await api.get_attention(999)
            assert exc.value.status_code == 404
        finally:
            await close_db()

    async def test_resolve_endpoint_records_the_note(self, att_env):
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            closed = await api.resolve_attention(row["id"], api._Resolution(
                note="fixed by hand"))

            assert closed["status"] == "resolved"
            assert closed["resolved_note"] == "fixed by hand"
        finally:
            await close_db()

    async def test_resolve_endpoint_accepts_no_body(self, att_env):
        """The UI has to be able to clear an item with a single click."""
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))
            closed = await api.resolve_attention(row["id"])
            assert closed["status"] == "resolved"
            assert closed["resolved_note"] == ""
        finally:
            await close_db()

    async def test_dismiss_and_reopen_endpoints(self, att_env):
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            row = await attention.raise_item("p", _item("p:1"))

            dismissed = await api.dismiss_attention(row["id"], None)
            assert dismissed["status"] == "dismissed"

            reopened = await api.reopen_attention(row["id"])
            assert reopened["status"] == "open"
        finally:
            await close_db()

    async def test_resolution_endpoints_404_when_unknown(self, att_env):
        from fastapi import HTTPException
        from backend.database import init_db, close_db
        import backend.api.attention as api

        await init_db()
        try:
            for call in (api.resolve_attention, api.dismiss_attention,
                         api.reopen_attention):
                with pytest.raises(HTTPException) as exc:
                    await call(999)
                assert exc.value.status_code == 404
        finally:
            await close_db()
