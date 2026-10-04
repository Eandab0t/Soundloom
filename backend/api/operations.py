"""HTTP surface for the operation journal.

Lets the user see what a crash left behind and decide what to do about it.
Recovery that frees already-downloaded files happens automatically at startup;
these endpoints cover the decisions that need a human: retry something that
never reached the library, or discard it.
"""
import logging

from fastapi import APIRouter, HTTPException

from backend.services import operations as ops

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.get("")
async def list_ops(state: str = None, limit: int = 100):
    """List operations, newest first. `state` filters to one state."""
    try:
        return {"operations": await ops.list_operations(state=state, limit=limit)}
    except Exception as e:
        logger.exception("Failed to list operations: %s", e)
        raise HTTPException(500, f"Failed to list operations: {e}")


@router.get("/interrupted")
async def list_interrupted():
    """Only the non-terminal operations - what recovery has not judged yet."""
    try:
        return {"operations": await ops.interrupted_operations()}
    except Exception as e:
        logger.exception("Failed to list interrupted operations: %s", e)
        raise HTTPException(500, f"Failed to list interrupted operations: {e}")


@router.get("/{op_id}")
async def get_op(op_id: int):
    row = await ops.get_operation(op_id)
    if not row:
        raise HTTPException(404, f"Operation {op_id} not found")
    return row


@router.post("/{op_id}/retry")
async def retry_op(op_id: int):
    """Re-queue a failed or abandoned operation and its owning job."""
    row = await ops.retry_operation(op_id)
    if not row:
        raise HTTPException(404, f"Operation {op_id} not found")
    return row


@router.post("/{op_id}/discard")
async def discard_op(op_id: int):
    """Give up on an operation and remove its staged artifact."""
    row = await ops.discard_operation(op_id)
    if not row:
        raise HTTPException(404, f"Operation {op_id} not found")
    return row


@router.post("/reconcile")
async def run_reconcile():
    """Run recovery on demand instead of waiting for the next restart."""
    try:
        return await ops.reconcile()
    except Exception as e:
        logger.exception("Manual reconcile failed: %s", e)
        raise HTTPException(500, f"Reconciliation failed: {e}")