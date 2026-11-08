"""Core application updates: stage now, apply on restart, verify after.

The running exe and its _internal DLLs cannot be overwritten in place, so
a core update never touches them while the app runs:

1. install approval → the verified package is extracted into
   ``update_staging/core/new`` and a ``pending.json`` receipt is written.
   The user is told to restart.
2. On shutdown, the app spawns the external updater helper (a separate
   small executable holding no locks on the install dir) and exits.
3. The helper waits for the app to exit, moves the current exe and
   _internal aside into a backup, moves the staged files in, writes
   ``applied.json`` and launches the new exe.
4. The next run verifies itself: a successful startup writes
   ``core_ok.json``. A run that crashes before that point causes the
   following start to roll back to the backup automatically.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from .. import config
from . import security

logger = logging.getLogger(__name__)

CORE_STAGING = "core"
_NEW = "new"
_BACKUP = "backup"


class CoreUpdateError(Exception):
    pass


def _core_dir() -> Path:
    return config.UPDATE_STAGING_DIR / CORE_STAGING


def pending_receipt() -> dict | None:
    """The staged-but-not-yet-applied core update receipt, if any.

    An ``applied.json`` marker means the helper already consumed this
    receipt; reporting it as pending would re-run the apply on the next
    shutdown and overwrite the rollback backup.
    """
    if (_core_dir() / "applied.json").exists():
        return None
    try:
        receipt = json.loads((_core_dir() / "pending.json").read_text(encoding="utf-8"))
        return receipt if receipt.get("staged_dir") else None
    except (OSError, json.JSONDecodeError):
        return None


def spawn_helper() -> bool:
    """Launch the external updater (detached) during shutdown, if staged.

    The helper waits for this process to exit, then swaps the staged core
    in and starts the new exe. Returns False when nothing is staged or the
    helper is unavailable.
    """
    import os
    import subprocess
    import sys

    receipt = pending_receipt()
    if not receipt:
        return False
    if getattr(sys, "frozen", False):
        helper = config.ROOT / "SoundloomUpdater.exe"
        if not helper.exists():
            logger.warning("Core update staged but %s is missing", helper)
            return False
        command = [str(helper)]
    else:
        command = [sys.executable, str(config.ROOT / "tools" / "updater_helper.py")]
    try:
        subprocess.Popen(
            command + [
                "--apply", str(_core_dir() / "pending.json"),
                "--parent", str(os.getpid()),
            ],
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        logger.info("Spawned core updater helper")
        return True
    except OSError as exc:
        logger.warning("Could not spawn core updater helper: %s", exc)
        return False


def _marker(name: str) -> Path:
    return _core_dir() / name


def stage_core_package(package_path: Path) -> Path:
    """Extract a verified core package into the staging area."""
    new_dir = _core_dir() / _NEW
    shutil.rmtree(new_dir, ignore_errors=True)
    security.safe_extract(package_path, new_dir)
    if not (new_dir / "Soundloom.exe").exists():
        shutil.rmtree(new_dir, ignore_errors=True)
        raise CoreUpdateError("core package does not contain Soundloom.exe")
    return new_dir


def prepare_apply(staged_dir: Path, version: str) -> dict:
    """Write the pending receipt; the helper applies it after restart."""
    _core_dir().mkdir(parents=True, exist_ok=True)
    receipt = {
        "staged_dir": str(staged_dir),
        "install_dir": str(config.ROOT),
        "version": version,
        "from_version": config.APP_VERSION,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (_core_dir() / "pending.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    logger.info("Core update %s staged; will apply on restart", version)
    return receipt


def recover_on_startup() -> list[str]:
    """Run at every startup, before serving. Returns actions taken."""
    actions: list[str] = []
    core_dir = _core_dir()
    applied = core_dir / "applied.json"
    core_ok = core_dir / "core_ok.json"
    if core_ok.exists():
        # A previous run verified the applied core: commit and clean up.
        _cleanup_after_commit(core_ok)
        actions.append("core update committed")
        return actions
    if applied.exists():
        # The last run never verified: restore the backup.
        restored = _restore_backup(applied)
        if restored:
            actions.append("core update rolled back (previous run did not verify)")
    return actions


def mark_verified() -> None:
    """Called after a successful startup of an applied core."""
    if _marker("applied.json").exists():
        try:
            _marker("core_ok.json").write_text(
                json.dumps({"verified_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
                encoding="utf-8",
            )
            logger.info("Applied core update verified")
        except OSError:
            logger.warning("Could not write core verification marker")


def _cleanup_after_commit(core_ok: Path) -> None:
    for name in ("pending.json", "applied.json", "core_ok.json"):
        _marker(name).unlink(missing_ok=True)
    shutil.rmtree(_core_dir() / _BACKUP, ignore_errors=True)
    shutil.rmtree(_core_dir() / _NEW, ignore_errors=True)


def _restore_backup(applied: Path) -> bool:
    try:
        receipt = json.loads(applied.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        applied.unlink(missing_ok=True)
        return False
    backup_dir = Path(receipt.get("backup_dir", ""))
    target = config.ROOT
    exe = target / "Soundloom.exe"
    internal = target / "_internal"
    ok = True
    if not backup_dir or not backup_dir.is_dir():
        # The helper completed the swap but the backup is gone; there is
        # nothing to roll back to, so the applied core is accepted.
        logger.warning(
            "Core update was applied but no backup exists; keeping the "
            "applied version %s", receipt.get("version"),
        )
        applied.unlink(missing_ok=True)
        return ok
    if backup_dir.is_dir():
        # Undo the helper's swap: current files go back to staging trash,
        # backup returns to the install dir.
        trash = _core_dir() / "failed_apply"
        shutil.rmtree(trash, ignore_errors=True)
        trash.mkdir(parents=True)
        try:
            if exe.exists():
                shutil.move(str(exe), str(trash / "Soundloom.exe"))
            if internal.exists():
                shutil.move(str(internal), str(trash / "_internal"))
            if (backup_dir / "Soundloom.exe").exists():
                shutil.move(
                    str(backup_dir / "Soundloom.exe"), str(exe)
                )
            if (backup_dir / "_internal").exists():
                shutil.move(str(backup_dir / "_internal"), str(internal))
        except OSError as exc:
            logger.exception("Core rollback failed: %s", exc)
            ok = False
    applied.unlink(missing_ok=True)
    return ok
