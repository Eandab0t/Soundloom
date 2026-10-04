"""Needs Attention: one normalized list of things the user must decide.

The premise is that background systems *produce* attention items and the UI
*resolves* them, instead of the user visiting five screens to find out what
went wrong.

    Background system -> Producer.scan() -> AttentionItem -> /api/attention -> UI

Two rules make this trustworthy rather than a second log page:

- **Dedupe by problem identity, not by event.** `dedupe_key` is a stable name
  for the underlying problem (`metadata_review:412`, `interrupted_operation:9`).
  Re-raising the same problem bumps `seen_count` and `updated_at` instead of
  piling up duplicates, so a nightly scan cannot flood the list.
- **A problem that stops being detected disappears.** After each producer
  scans, its open items that it no longer reports are auto-resolved. Without
  this the list would only ever grow and would quietly start lying.

`type` is deliberately a free string, not an enum: new producers add types
without a schema change, which is the whole point of having this layer.
`severity` *is* constrained, because ordering and triage depend on it.
"""
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from ..database import execute, fetch_all, fetch_one
from ..errors import VividlyError

logger = logging.getLogger(__name__)


class Severity(Enum):
    """How loudly an item asks for attention. Rank drives list ordering."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}

# Ordering lives in SQL so paging through the list stays stable and cheap.
_SEVERITY_ORDER_SQL = (
    "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 ELSE 3 END"
)


class AttentionStatus(Enum):
    OPEN = "open"
    RESOLVED = "resolved"   # the user did the thing; keep for history
    DISMISSED = "dismissed" # the user chose to ignore it


# Statuses that still need the user's attention.
OPEN_STATUSES = (AttentionStatus.OPEN.value,)


class AttentionError(VividlyError):
    code = "attention_error"
    message = "Attention item error"


@dataclass
class AttentionItem:
    """One problem awaiting a decision. `dedupe_key` identifies the problem."""
    dedupe_key: str
    type: str
    title: str
    severity: Severity = Severity.MEDIUM
    description: str = ""
    entity_type: str = ""
    entity_id: int | None = None
    action: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.dedupe_key:
            raise AttentionError("dedupe_key is required - it is the item's identity")
        if isinstance(self.severity, str):
            self.severity = _parse_severity(self.severity)


def _parse_severity(value) -> Severity:
    if isinstance(value, Severity):
        return value
    try:
        return Severity(value)
    except ValueError:
        raise AttentionError(
            f"Unknown severity {value!r}; expected one of "
            f"{[s.value for s in Severity]}"
        )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Raising, listing, resolving
# ---------------------------------------------------------------------------

async def raise_item(producer_name: str, item: AttentionItem) -> dict:
    """Record a problem, or fold it into the row that already tracks it.

    Called on every scan, so it must be idempotent per `dedupe_key`:

    - new key      -> insert, `seen_count` 1
    - open key     -> refresh the mutable fields, bump `seen_count`/`updated_at`
    - resolved key -> reopen it (the problem came back) and bump `seen_count`

    Reopening rather than re-inserting is what keeps one row per problem for
    the life of the library.
    """
    now = _utcnow_iso()
    action_json = json.dumps(item.action or {})

    await execute(
        """
        INSERT INTO attention_items
            (dedupe_key, type, severity, title, description, entity_type,
             entity_id, source, action, status, seen_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
        ON CONFLICT(dedupe_key) DO UPDATE SET
            type=excluded.type,
            severity=excluded.severity,
            title=excluded.title,
            description=excluded.description,
            entity_type=excluded.entity_type,
            entity_id=excluded.entity_id,
            action=excluded.action,
            source=excluded.source,
            -- A problem that was resolved but is still being reported has
            -- come back: reopen it instead of silently leaving it resolved.
            status='open',
            seen_count=attention_items.seen_count + 1,
            updated_at=excluded.updated_at,
            resolved_at=NULL,
            resolved_note=''
        """,
        (item.dedupe_key, item.type, item.severity.value, item.title,
         item.description, item.entity_type, item.entity_id,
         producer_name, action_json, now, now),
    )

    row = await fetch_one(
        "SELECT * FROM attention_items WHERE dedupe_key=?", (item.dedupe_key,)
    )
    return _decode(row)


async def resolve_item(item_id: int, note: str = "") -> dict | None:
    """Mark the problem handled. The row is kept as history."""
    return await _close(item_id, AttentionStatus.RESOLVED, note)


async def dismiss_item(item_id: int, note: str = "") -> dict | None:
    """Mark the problem as not worth acting on. Also kept as history."""
    return await _close(item_id, AttentionStatus.DISMISSED, note)


async def _close(item_id: int, status: AttentionStatus, note: str) -> dict | None:
    row = await fetch_one("SELECT id FROM attention_items WHERE id=?", (item_id,))
    if not row:
        return None
    now = _utcnow_iso()
    await execute(
        "UPDATE attention_items SET status=?, resolved_note=?, resolved_at=?, "
        "updated_at=? WHERE id=?",
        (status.value, note or "", now, now, item_id),
    )
    return _decode(await fetch_one(
        "SELECT * FROM attention_items WHERE id=?", (item_id,)))


async def reopen_item(item_id: int) -> dict | None:
    """Undo a resolve/dismiss from the UI."""
    row = await fetch_one("SELECT id FROM attention_items WHERE id=?", (item_id,))
    if not row:
        return None
    now = _utcnow_iso()
    await execute(
        "UPDATE attention_items SET status='open', resolved_at=NULL, "
        "resolved_note='', updated_at=? WHERE id=?",
        (now, item_id),
    )
    return _decode(await fetch_one(
        "SELECT * FROM attention_items WHERE id=?", (item_id,)))


async def get_item(item_id: int) -> dict | None:
    return _decode(await fetch_one(
        "SELECT * FROM attention_items WHERE id=?", (item_id,)))



async def attach_provenance(items: list) -> list:
    """Add a compact provenance summary to items that reference a track.

    An item naming a track can send the user straight there, but until now
    that screen said nothing about where the file came from - which is
    usually the question behind the item. One query for all of them rather
    than one per item, and only the latest acquisition: this list is
    items worst-first, and a full audit trail on every row would make it
    unreadable. The full record stays where it belongs, on the track.

    An item whose track has no provenance gets {"known": false} rather than
    a missing key, so the frontend can tell "no record" from "not asked".
    """
    track_ids = [i.get("entity_id") for i in items
                 if isinstance(i, dict)
                 and i.get("entity_type") == "track"
                 and i.get("entity_id")]
    if not track_ids:
        return items

    placeholders = ",".join("?" * len(track_ids))
    rows = await fetch_all(
        f"""
        SELECT a.track_id, a.sha256, a.file_size, a.completed_at,
               a.source, a.source_url, a.job_id, a.organizer_result
          FROM acquisition a
         WHERE a.track_id IN ({placeholders})
           AND a.id = (SELECT MAX(b.id) FROM acquisition b
                        WHERE b.track_id = a.track_id)
        """,
        tuple(track_ids),
    )
    by_track = {r["track_id"]: dict(r) for r in rows}
    for item in items:
        if not isinstance(item, dict) or item.get("entity_type") != "track":
            continue
        rec = by_track.get(item.get("entity_id"))
        if rec is None:
            item["provenance"] = {"known": False}
            continue
        item["provenance"] = {
            "known": bool(rec["sha256"]),
            "sha256": rec["sha256"] or "",
            "file_size": rec["file_size"] or 0,
            "completed_at": rec["completed_at"] or "",
            "source": rec["source"] or "",
            "source_url": rec["source_url"] or "",
            "job_id": rec["job_id"],
            "from_backfill": rec["organizer_result"] == "backfill",
        }
    return items


async def list_items(status: str = AttentionStatus.OPEN.value,
                     type: str | None = None,
                     severity: str | None = None,
                     limit: int = 100,
                     offset: int = 0) -> list[dict]:
    """List items worst-first. Status defaults to the ones needing action."""
    sql = "SELECT * FROM attention_items WHERE 1=1"
    params: list = []

    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if type:
        sql += " AND type=?"
        params.append(type)
    if severity:
        # Reject unknown values here rather than silently returning nothing.
        sql += " AND severity=?"
        params.append(_parse_severity(severity).value)

    sql += (f" ORDER BY {_SEVERITY_ORDER_SQL}, updated_at DESC, id DESC"
            " LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    items = [_decode(r) for r in await fetch_all(sql, tuple(params))]
    return await attach_provenance(items)


async def counts_by_type(status: str = AttentionStatus.OPEN.value) -> dict:
    """Grouped totals for the 'Needs Attention - 7' header."""
    sql = "SELECT type, COUNT(*) AS n FROM attention_items WHERE 1=1"
    params: list = []
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    sql += " GROUP BY type ORDER BY n DESC, type ASC"
    rows = await fetch_all(sql, tuple(params))
    grouped = {r["type"]: r["n"] for r in rows}
    return {"total": sum(grouped.values()), "by_type": grouped}


async def counts_by_severity(status: str = AttentionStatus.OPEN.value) -> dict:
    sql = f"SELECT severity, COUNT(*) AS n FROM attention_items WHERE 1=1"
    params: list = []
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    sql += " GROUP BY severity"
    rows = await fetch_all(sql, tuple(params))
    found = {r["severity"]: r["n"] for r in rows}
    return {s.value: found.get(s.value, 0) for s in Severity}


def _decode(row: dict | None) -> dict | None:
    """Turn a DB row into the API shape, parsing the action JSON."""
    if not row:
        return None
    out = dict(row)
    try:
        out["action"] = json.loads(out.get("action") or "{}")
    except (ValueError, TypeError):
        out["action"] = {}
    return out


# ---------------------------------------------------------------------------
# Producers
# ---------------------------------------------------------------------------

@runtime_checkable
class AttentionProducer(Protocol):
    """A background system that can say what needs the user's attention.

    Implement `name` and `scan()` and register it; `run_producers()` handles
    dedupe, reopening and auto-resolution. `scan` must return only problems
    that exist *right now* - anything it stops returning is auto-resolved.
    """

    name: str

    async def scan(self) -> list[AttentionItem]: ...


_REGISTRY: list[AttentionProducer] = []


def register_producer(producer: AttentionProducer) -> AttentionProducer:
    """Register a producer. Re-registering the same name replaces it."""
    if not getattr(producer, "name", ""):
        raise AttentionError("Producer must have a non-empty name")
    _REGISTRY[:] = [p for p in _REGISTRY if p.name != producer.name]
    _REGISTRY.append(producer)
    return producer


def registered_producers() -> list[AttentionProducer]:
    return list(_REGISTRY)


def clear_producers() -> None:
    """Reset the registry. For tests and for reloading producers."""
    _REGISTRY.clear()


async def run_producers() -> dict:
    """Scan every producer, then retire what they no longer report.

    One producer failing must not hide the others' items, so failures are
    counted and logged rather than raised.
    """
    summary = {"producers": 0, "raised": 0, "auto_resolved": 0, "failed": 0}
    reported_by: dict[str, set] = {}

    for producer in list(_REGISTRY):
        summary["producers"] += 1
        try:
            items = await producer.scan()
        except Exception as e:
            summary["failed"] += 1
            logger.exception("Attention producer %s failed: %s", producer.name, e)
            # Deliberately not recorded in reported_by: a failed producer must
            # not auto-resolve everything it used to report.
            continue

        keys = set()
        for item in items:
            keys.add(item.dedupe_key)
            try:
                await raise_item(producer.name, item)
                summary["raised"] += 1
            except Exception as e:
                summary["failed"] += 1
                logger.exception(
                    "Producer %s raised a bad item %r: %s",
                    producer.name, getattr(item, "dedupe_key", "?"), e)
        reported_by[producer.name] = keys

    for producer in list(_REGISTRY):
        if producer.name not in reported_by:
            continue  # it failed; leave its items alone
        stale = await _auto_resolve_missing(producer.name, reported_by[producer.name])
        summary["auto_resolved"] += stale

    logger.info("Attention scan: %s", summary)
    return summary


async def _auto_resolve_missing(producer_name: str, current_keys: set) -> int:
    """Close this producer's open items that it no longer reports."""
    rows = await fetch_all(
        "SELECT id, dedupe_key FROM attention_items "
        "WHERE source=? AND status='open'",
        (producer_name,),
    )
    stale = [r for r in rows if r["dedupe_key"] not in current_keys]
    now = _utcnow_iso()
    for row in stale:
        await execute(
            "UPDATE attention_items SET status=?, resolved_note=?, resolved_at=?, "
            "updated_at=? WHERE id=?",
            (AttentionStatus.RESOLVED.value,
             "No longer detected", now, now, row["id"]),
        )
    return len(stale)