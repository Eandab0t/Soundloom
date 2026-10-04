"""Soundloom — main application entry point.

Weaves new music into your library from the sources you follow: watched
artists, synced playlists and one-off links.
"""
import sys
import os
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

ROOT = Path(__file__).resolve().parent.parent
if getattr(sys, "frozen", False):
    # PyInstaller build: bundled modules live in _MEIPASS.
    ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
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
from backend.api.identify import router as identify_router
from backend.api.sync import router as sync_router
from backend.api.playlists import router as playlists_router, auth_router as connections_router
from backend.api.operations import router as operations_router
from backend.api.attention import router as attention_router
from backend.api.matches import router as matches_router

setup_logging(config.LOGS_DIR)
logger = logging.getLogger("soundloom")

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
    from backend.services.downloader import start_worker, stop_worker, set_concurrency, reclaim_orphans
    from backend.services.operations import reconcile
    from backend.services.watcher import start_watcher, stop_watcher
    from backend.services.sync import start_sync_loop, stop_sync_loop
    # Order matters. reconcile() asks the disk what actually finished and
    # indexes it, reclaim_orphans() then sweeps only what no operation still
    # owns. Sweeping first would delete resumable artifacts.
    try:
        recovery = await reconcile()
        if recovery.get("examined"):
            logger.info("Startup recovery: %s", recovery)
    except Exception as e:
        # A recovery bug must never stop the app from starting.
        logger.exception("Startup reconciliation failed, continuing: %s", e)
    await reclaim_orphans()

    # A provenance run left 'running' means the process died mid-sweep.
    # Relabel it so the panel offers an honest Resume instead of claiming
    # a task is in flight when nothing is. Independent of the filesystem
    # check below, and deliberately not an auto-resume: that would
    # silently restart gigabytes of hashing nobody asked for a second time.
    try:
        from backend.services import backfill as _backfill
        await _backfill.mark_interrupted()
    except Exception as be:
        logger.warning("Could not reconcile backfill run state: %s", be)

    # Bring the library rows back in line with the disk before anything reads
    # them. This has to happen *before* the Needs Attention snapshot below, or
    # that snapshot would be taken against a library that still claims files
    # exist when they do not - and the counts on screen would be wrong until
    # the next refresh.
    if config.get("verify_library_on_startup", True):
        try:
            from backend.services.scanner import verify_filesystem
            verified = await verify_filesystem()
            if verified.get("status") == "complete" and (
                verified.get("marked_missing") or verified.get("restored_present")
            ):
                logger.info(
                    "Startup library verify: %s",
                    {k: v for k, v in verified.items() if k != "reason"},
                )
            elif verified.get("status") == "skipped":
                logger.info("Startup library verify skipped: %s", verified.get("reason"))
        except Exception as e:
            # A verification bug must never stop the app from starting, and
            # must never be the reason a library looks empty.
            logger.exception("Startup library verify failed, continuing: %s", e)

    # Needs Attention: register the producers, then take a first snapshot so
    # the list is populated before the UI asks for it. A scan failure must not
    # block startup - the list will simply be empty until the next refresh.
    try:
        from backend.services.attention_producers import register_default_producers
        from backend.services.attention import run_producers
        register_default_producers()
        snapshot = await run_producers()
        if snapshot["raised"] or snapshot["auto_resolved"]:
            logger.info("Needs Attention: %s", snapshot)
    except Exception as e:
        logger.warning("Needs Attention scan skipped at startup: %s", e)

    await start_worker()
    max_dl = config.get("max_concurrent_downloads", 1)
    set_concurrency(max_dl)
    await start_watcher()
    if config.get("sync_enabled", True):
        await start_sync_loop()
    _shutdown_task = asyncio.create_task(_auto_shutdown_watchdog())
    logger.info("Soundloom started on port %s", config.get("server_port", 5555))
    yield
    if _shutdown_task:
        _shutdown_task.cancel()
    await stop_sync_loop()
    await stop_watcher()
    await stop_worker()
    # Stop any hashing sweep before the connection closes, or it
    # writes into a dead database on the way out.
    try:
        from backend.services import backfill as _bf
        await _bf.shutdown()
    except Exception as se:
        logger.warning("Could not stop backfill cleanly: %s", se)
    try:
        from backend.services import content_index as _ci
        await _ci.shutdown()
    except Exception as ce:
        logger.warning("Could not stop content index cleanly: %s", ce)

    await close_db()
    logger.info("Soundloom shut down")


app = FastAPI(title="Soundloom", version=config.APP_VERSION, lifespan=lifespan)


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
app.include_router(identify_router)
app.include_router(sync_router)
app.include_router(playlists_router)
app.include_router(connections_router)
app.include_router(system_router)
app.include_router(operations_router)
app.include_router(attention_router)
app.include_router(matches_router)

FRONTEND_DIR = config.FRONTEND_DIR


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers must revalidate before using a cached copy.

    Plain StaticFiles sends no Cache-Control, so browsers heuristically cache
    JS/CSS and serve stale UI after an update. `no-cache` forces a cheap
    conditional request (304 on localhost) so edits show up on reload.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory=str(FRONTEND_DIR)), name="static")


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
