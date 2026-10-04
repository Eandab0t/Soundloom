"""HTTP surface for Needs Attention.

The UI contract this is built for: one list, grouped counts for the header
badge, and per-item actions that take the user to the actual decision screen
rather than to another log page. `action` on each item carries those routes,
so the frontend does not have to hardcode a mapping from item type to screen.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import attention

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attention", tags=["attention"])


@router.get("")
async def list_attention(
    status: str = attention.AttentionStatus.OPEN.value,
    type: str = None,
    severity: str = None,
    limit: int = 100,
    offset: int = 0,
):
    """Items worst-first, with grouped counts for the header."""
    try:
        items = await attention.list_items(
            status=status, type=type, severity=severity,
            limit=limit, offset=offset,
        )
    except attention.AttentionError as e:
        raise HTTPException(422, e.message)
    except Exception as e:
        logger.exception("Failed to list attention items: %s", e)
        raise HTTPException(500, f"Failed to list attention items: {e}")

    return {
        "items": items,
        "counts": await attention.counts_by_type(status),
        "severity_counts": await attention.counts_by_severity(status),
    }


@router.get("/counts")
async def attention_counts(status: str = attention.AttentionStatus.OPEN.value):
    """Badge number for the sidebar. Cheap, so the UI can poll it."""
    try:
        return {
            "counts": await attention.counts_by_type(status),
            "severity_counts": await attention.counts_by_severity(status),
        }
    except Exception as e:
        logger.exception("Failed to count attention items: %s", e)
        raise HTTPException(500, f"Failed to count attention items: {e}")


# Declared before /{item_id} so these literals are not parsed as an id.
@router.post("/refresh")
async def refresh_attention():
    """Run every producer now instead of waiting for the scheduled scan."""
    try:
        return await attention.run_producers()
    except Exception as e:
        logger.exception("Attention refresh failed: %s", e)
        raise HTTPException(500, f"Attention refresh failed: {e}")


@router.get("/{item_id}")
async def get_attention(item_id: int):
    row = await attention.get_item(item_id)
    if not row:
        raise HTTPException(404, f"Attention item {item_id} not found")
    return row


class _Resolution(BaseModel):
    """Optional note recorded when an item is cleared."""
    note: str = ""


@router.post("/{item_id}/resolve")
async def resolve_attention(item_id: int, body: _Resolution | None = None):
    """Mark handled. Kept as history so the user can see what they cleared."""
    row = await attention.resolve_item(item_id, (body.note if body else "") or "")
    if not row:
        raise HTTPException(404, f"Attention item {item_id} not found")
    return row


@router.post("/{item_id}/dismiss")
async def dismiss_attention(item_id: int, body: _Resolution | None = None):
    """Mark as not worth acting on."""
    row = await attention.dismiss_item(item_id, (body.note if body else "") or "")
    if not row:
        raise HTTPException(404, f"Attention item {item_id} not found")
    return row


@router.post("/{item_id}/reopen")
async def reopen_attention(item_id: int):
    row = await attention.reopen_item(item_id)
    if not row:
        raise HTTPException(404, f"Attention item {item_id} not found")
    return row
