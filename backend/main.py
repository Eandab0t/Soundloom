"""VividlyMusicaly (Big Pickle) - Main application entry point."""
import sys
import os
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from backend import config
from backend.database import init_db, close_db
from backend.errors import VividlyError, log_error
from backend.events import subscribe, unsubscribe
from backend.logging_config import setup_logging
from backend.api.library import router as library_router
from backend.api.queue import router as queue_router
from backend.api.watch import router as watch_router
from backend.api.settings import router as settings_router
from backend.api.sources import router as sources_router
from backend.api.system import router as system_router, get_last_heartbeat
from backend.api.autofix import router as autofix_router

setup_logging(config.LOGS_DIR)
logger = logging.getLogger("bigpickle")

_shutdown_task = None


async def _auto_shutdown_watchdog():
    """Background task: if auto_shutdown enabled and no heartbeat for timeout, kill server."""
    import time as _time
    while True:
        await asyncio.sleep(5)
        try:
            if not config.get("auto_shutdown", True):
                continue
            timeout = config.get("shutdown_timeout", 30)
            last = get_last_heartbeat()
            if last == 0.0:
                continue
            idle = _time.time() - last
            if idle > timeout:
                logger.info(f"No heartbeat for {int(idle)}s (timeout={timeout}s). Shutting down.")
                os._exit(0)
        except Exception as e:
            logger.debug(f"Shutdown watchdog error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shutdown_task
    config._ensure_dirs()
    await init_db()
    from backend.services.downloader import start_worker, stop_worker, set_concurrency
    await start_worker()
    max_dl = config.get("max_concurrent_downloads", 1)
    set_concurrency(max_dl)
    _shutdown_task = asyncio.create_task(_auto_shutdown_watchdog())
    logger.info("Big Pickle started on port 5555")
    yield
    if _shutdown_task:
        _shutdown_task.cancel()
    await stop_worker()
    await close_db()
    logger.info("Big Pickle shut down")


app = FastAPI(title="VividlyMusicaly", version="0.1.0", lifespan=lifespan)


@app.exception_handler(VividlyError)
async def vividly_error_handler(request: Request, exc: VividlyError):
    log_error(exc)
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "internal", "message": "Internal server error", "recoverable": False},
    )

app.include_router(library_router)
app.include_router(queue_router)
app.include_router(watch_router)
app.include_router(settings_router)
app.include_router(sources_router)
app.include_router(autofix_router)
app.include_router(system_router)

FRONTEND_DIR = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """Single WebSocket channel for all real-time events (jobs, library, logs)."""
    await websocket.accept()
    queue = await subscribe()
    try:
        while True:
            msg = await queue.get()
            await websocket.send_text(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        unsubscribe(queue)


def run():
    import uvicorn
    port = config.get("server_port", 5555)
    host = config.get("server_host", "127.0.0.1")
    logger.info(f"Starting server at http://{host}:{port}")
    uvicorn.run("backend.main:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    run()
