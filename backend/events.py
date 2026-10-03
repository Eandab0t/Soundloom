"""Event bus for real-time WebSocket broadcasting.

All backend events flow through here. Frontend subscribes via /ws/events.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_subscribers: list[asyncio.Queue] = []

# A subscriber that stops draining (backgrounded tab, suspended laptop) would
# otherwise grow its queue without bound. Cap it: on overflow we drop the
# subscriber so its backlog cannot exhaust memory. Queues are per-connection,
# so this only ever costs a reconnecting client, never correctness.
MAX_QUEUE_SIZE = 200


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue):
    if q in _subscribers:
        _subscribers.remove(q)


async def emit(event_type: str, data: dict = None):
    """Broadcast an event to all connected WebSocket subscribers."""
    message = {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
    }
    text = json.dumps(message)
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(text)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)
        logger.warning(
            "Dropped event subscriber: queue exceeded %d events", MAX_QUEUE_SIZE
        )


async def emit_job_update(job_id: int, status: str, progress: float = 0,
                          error: str = "", output_path: str = ""):
    """Convenience: emit a job status update."""
    await emit("job_update", {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "error": error,
        "output_path": output_path,
    })


async def emit_log(level: str, message: str, source: str = ""):
    """Convenience: emit a log event for the frontend log viewer."""
    await emit("log", {
        "level": level,
        "message": message,
        "source": source,
    })
