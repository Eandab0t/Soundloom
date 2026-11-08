"""External core-update helper (stdlib only, frozen as its own exe).

The running Soundloom exe and its _internal DLLs are locked, so the swap
is done by this separate process after the app exits:

    SoundloomUpdater.exe --apply <receipt.json> --parent <pid>

Flow: wait for the app process to exit → move the current exe and
_internal into a backup → move the staged files into the install dir →
write applied.json → launch the new exe. On any failure the moves are
undone and failed.json is written instead.

This module must never import anything from backend/ — it runs outside
the application and must stay dependency-free.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APPLY_TIMEOUT_SECONDS = 120


def wait_for_exit(pid: int | None, timeout: int = APPLY_TIMEOUT_SECONDS) -> bool:
    """Block until the given process exits (Windows) or timeout expires."""
    if pid is None or not hasattr(os, "waitpid"):
        # Non-Windows fallback: the caller (launcher) spawns us after the
        # server thread exits, so a short grace period is enough.
        time.sleep(2)
        return True
    import ctypes
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True  # already gone
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000)
        return result == 0  # WAIT_OBJECT_0 = exited
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def apply_staged_core(receipt: dict, install_dir: Path) -> dict:
    """Swap the staged core into install_dir with rollback on failure.

    The receipt must contain ``staged_dir``. On success ``applied.json``
    records the swap (the app then verifies itself on startup); on
    failure ``failed.json`` records what happened and the install dir is
    left exactly as it was.
    """
    core_dir = Path(receipt["staged_dir"]).parent
    staged = Path(receipt["staged_dir"])
    exe = install_dir / "Soundloom.exe"
    internal = install_dir / "_internal"
    backup = core_dir / "backup"

    def write_marker(name: str, payload: dict) -> None:
        (core_dir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    done: list[Path] = []  # moves completed, for undo

    def move(src: Path, dst: Path) -> None:
        shutil.move(str(src), str(dst))
        done.append(src)

    try:
        backup.mkdir(parents=True, exist_ok=True)
        if exe.exists():
            move(exe, backup / "Soundloom.exe")
        if internal.exists():
            move(internal, backup / "_internal")
        if not (staged / "Soundloom.exe").exists():
            raise FileNotFoundError("staged core is missing Soundloom.exe")
        move(staged / "Soundloom.exe", exe)
        if (staged / "_internal").exists():
            move(staged / "_internal", internal)
    except OSError as exc:
        _undo(backup, install_dir, staged)
        write_marker("failed.json", {"error": str(exc), "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return {"applied": False, "error": str(exc)}

    applied = {
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": receipt.get("version"),
        "backup_dir": str(backup),
    }
    write_marker("applied.json", applied)
    return {"applied": True, "version": receipt.get("version"), "backup_dir": str(backup)}


def _undo(backup: Path, install_dir: Path, staged: Path) -> None:
    """Best-effort restoration after a failed swap.

    Whatever new files made it in before the failure are moved aside, then
    the backup is returned to the install dir — the install dir must never
    be left with a mix of old and new files.
    """
    exe = install_dir / "Soundloom.exe"
    internal = install_dir / "_internal"
    trash = staged.parent / "failed_apply"
    shutil.rmtree(trash, ignore_errors=True)
    if exe.exists() or internal.exists():
        trash.mkdir(parents=True, exist_ok=True)
    if exe.exists():
        shutil.move(str(exe), str(trash / "Soundloom.exe"))
    if internal.exists():
        shutil.move(str(internal), str(trash / "_internal"))
    if (backup / "Soundloom.exe").exists():
        shutil.move(str(backup / "Soundloom.exe"), str(exe))
    if (backup / "_internal").exists():
        shutil.move(str(backup / "_internal"), str(internal))
    shutil.rmtree(staged, ignore_errors=True)


def launch_app(install_dir: Path) -> None:
    subprocess.Popen(
        [str(install_dir / "Soundloom.exe")],
        cwd=str(install_dir),
        close_fds=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", required=True, help="path to pending.json")
    parser.add_argument("--parent", type=int, default=None, help="app PID to wait for")
    parser.add_argument("--no-launch", action="store_true", help="do not restart the app")
    args = parser.parse_args()

    receipt_path = Path(args.apply)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable receipt: {exc}", file=sys.stderr)
        return 1

    install_dir = receipt.get("install_dir")
    if not install_dir:
        # Receipts written before the install_dir field existed: staging
        # lives at <data>/update_staging/core, so fall back two levels up.
        install_dir = str(receipt_path.parent.parent.parent)
    wait_for_exit(args.parent)
    result = apply_staged_core(receipt, Path(install_dir))
    if not result["applied"]:
        return 2
    if not args.no_launch:
        launch_app(install_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
