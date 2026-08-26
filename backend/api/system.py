"""Log and heartbeat endpoints."""
import time
import logging
from pathlib import Path
from fastapi import APIRouter
from .. import config

router = APIRouter(tags=["system"])

_last_heartbeat: float = 0.0


def get_last_heartbeat() -> float:
    return _last_heartbeat


@router.post("/api/heartbeat")
async def heartbeat():
    global _last_heartbeat
    _last_heartbeat = time.time()
    return {"ok": True}


@router.get("/api/logs")
async def get_logs():
    log_file = config.LOGS_DIR / "app.log"
    if not log_file.exists():
        return {"logs": []}
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"logs": lines[-500:]}


@router.get("/api/status")
async def system_status():
    return {
        "version": "0.1.0",
        "name": "VividlyMusicaly (Big Pickle)",
        "library_path": config.get("library_path"),
        "download_path": config.get("download_path"),
        "db_path": str(config.DB_PATH),
    }
