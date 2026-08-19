"""Event bus for real-time WebSocket broadcasting.

All backend events flow through here. Frontend subscribes via /ws/events.
"""
import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_subscribers: list[asyncio.Queue] = []


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue):
    if q in _subscribers:
        _subscribers.remove(q)


async def emit(event_type: str, data: dict = None):
    """Broadcast an event to all connected WebSocket subscribers."""
    message = {
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
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


async def emit_library_change(action: str, track_id: int = 0,
                              track_count: int = 0):
    """Convenience: emit a library change event."""
    await emit("library_change", {
        "action": action,
        "track_id": track_id,
        "track_count": track_count,
    })


async def emit_log(level: str, message: str, source: str = ""):
    """Convenience: emit a log event for the frontend log viewer."""
    await emit("log", {
        "level": level,
        "message": message,
        "source": source,
    })
