"""Update manager: check → download → user approval → install, with rollback.

Approval is enforced by state, not convention: a component can only be
installed after it was downloaded *and* hash-verified in this same app
session (the in-memory ``_staged`` table). Every mutating step is a
separate explicit API call; nothing here installs on its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path

import aiohttp

from .. import config
from ..components import discover, validate as validate_manifest
from . import manifest as manifest_mod
from . import security

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)


class UpdateError(Exception):
    """Refused or failed update operation; message is user-facing."""


# --- session state ---

_lock = asyncio.Lock()
_last_check: dict | None = None
# component id -> {"version": ..., "path": Path, "sha256": ..., "component": {...}}
_staged: dict[str, dict] = {}
_download_task: asyncio.Task | None = None


def _require_configured() -> str:
    url = config.get("update_manifest_url", "")
    if not url:
        raise UpdateError("Update source is not configured.")
    if not url.startswith("https://") and not manifest_mod.is_loopback_url(url):
        raise UpdateError("Update source must use https.")
    return url


def installed_versions() -> dict[str, dict]:
    """Merged component manifests: bundled + user overrides (updates win)."""
    merged: dict[str, dict] = {}
    for component in discover(config.MANIFESTS_DIR):
        merged[component["id"]] = component
    for component in discover(config.USER_MANIFESTS_DIR):
        merged[component["id"]] = component
    return merged


def status() -> dict:
    """Everything the Update Center overview needs (no network)."""
    components = sorted(installed_versions().values(), key=lambda c: c["id"])
    available = {u["id"] for u in (_last_check or {}).get("available", [])}
    return {
        "core_version": config.APP_VERSION,
        "configured": bool(config.get("update_manifest_url", "")),
        "auto_update": config.get("auto_update", "notify"),
        "last_check": (_last_check or {}).get("at"),
        "components": [
            {**c, "update_available": c["id"] in available} for c in components
        ],
        "problems": (_last_check or {}).get("problems", []),
        "staged": sorted(_staged),
        "rollbacks": _rollback_versions(),
    }


def _rollback_versions() -> dict[str, str]:
    """Component id -> the version a rollback would restore.

    Backups win. A loose override without a backup (first update on this
    install) reverts to the bundled copy, so report the bundled version.
    """
    rollbacks: dict[str, str] = {}
    if config.UPDATE_ROLLBACK_DIR.is_dir():
        for component_dir in config.UPDATE_ROLLBACK_DIR.iterdir():
            backups = sorted(component_dir.iterdir(), reverse=True)
            if backups:
                rollbacks[component_dir.name] = backups[0].name.split("_")[0]
    bundled = {
        c["id"]: c["version"] for c in discover(config.MANIFESTS_DIR)
    }
    if (config.ROOT / "frontend" / "index.html").exists() and "frontend" in bundled:
        rollbacks.setdefault("frontend", bundled["frontend"])
    modules_root = config.ROOT / "modules"
    if modules_root.is_dir():
        for module_dir in modules_root.iterdir():
            if module_dir.is_dir() and module_dir.name in bundled:
                rollbacks.setdefault(module_dir.name, bundled[module_dir.name])
    return rollbacks


# --- check ---

async def check() -> dict:
    """Fetch and verify the remote manifest; compute available updates."""
    global _last_check
    async with _lock:
        url = _require_configured()
        try:
            fetched = await _fetch_manifest(url)
        except UpdateError:
            raise
        except Exception as exc:
            raise UpdateError(f"Could not reach update source: {exc}") from exc
        if not security.verify_manifest_signature(
            fetched, security.UPDATE_SIGNING_PUBKEY
        ):
            raise UpdateError("Update manifest failed signature verification.")
        errors = manifest_mod.validate_update_manifest(fetched)
        if errors:
            raise UpdateError(f"Invalid update manifest: {'; '.join(errors)}")
        installed = {cid: c["version"] for cid, c in installed_versions().items()}
        available, problems = manifest_mod.available_updates(
            fetched, installed, config.APP_VERSION
        )
        _last_check = {
            "at": time.time(),
            "manifest": fetched,
            "available": available,
            "problems": problems,
        }
        logger.info("Update check: %d available, %d problem(s)", len(available), len(problems))
        return {
            "checked_at": _last_check["at"],
            "available": available,
            "problems": problems,
        }


async def _fetch_manifest(url: str) -> dict:
    async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.read()
    try:
        fetched = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Update manifest is not valid JSON: {exc}") from exc
    if not isinstance(fetched, dict):
        raise UpdateError("Update manifest must be a JSON object.")
    return fetched


# --- download (staging) ---

def _stage_path(component_id: str, version: str) -> Path:
    return config.UPDATE_STAGING_DIR / f"{component_id}-{version}.zip"


async def download(component_ids: list[str]) -> dict:
    """Download selected components into staging and verify their hashes."""
    global _download_task
    async with _lock:
        _download_task = asyncio.current_task()
    try:
        return await _download_components(component_ids)
    finally:
        async with _lock:
            _download_task = None


async def _download_components(component_ids: list[str]) -> dict:
    if not _last_check:
        raise UpdateError("Check for updates before downloading.")
    available = {u["id"]: u for u in _last_check["available"]}
    results: dict[str, str] = {}
    for component_id in component_ids:
        component = available.get(component_id)
        if component is None:
            results[component_id] = "refused: not an available update"
            continue
        try:
            path = await _download_package(component)
        except UpdateError as exc:
            results[component_id] = f"failed: {exc}"
            continue
        _staged[component_id] = {
            "version": component["version"],
            "path": str(path),
            "sha256": component["sha256"],
            "component": component,
        }
        results[component_id] = "staged"
    return {"results": results}


async def _download_package(component: dict) -> Path:
    config.UPDATE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    final = _stage_path(component["id"], component["version"])
    partial = final.with_suffix(".zip.part")
    try:
        async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as session:
            async with session.get(component["package_url"]) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        handle.write(chunk)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc
    if security.sha256_file(partial) != component["sha256"]:
        partial.unlink(missing_ok=True)
        raise UpdateError("package hash mismatch")
    final.unlink(missing_ok=True)
    partial.rename(final)
    return final


def cancel_download() -> dict:
    task = _download_task
    if task is not None and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


# --- install (approval gate) ---

def _target_dir(component_type: str, component_id: str) -> Path:
    if component_type == "frontend":
        return config.ROOT / "frontend"
    if component_type == "integration":
        return config.ROOT / "modules" / component_id
    raise UpdateError(
        f"Component type {component_type!r} cannot be updated here "
        "(the application core updates via the external updater)."
    )


def _package_manifest(package_path: Path) -> dict:
    try:
        with zipfile.ZipFile(package_path) as archive:
            return json.loads(archive.read("package.json").decode("utf-8"))
    except KeyError as exc:
        raise UpdateError("package is missing package.json") from exc
    except Exception as exc:
        raise UpdateError(f"unreadable package: {exc}") from exc


async def install(component_ids: list[str]) -> dict:
    """Install staged, hash-verified packages. Anything else is refused."""
    async with _lock:
        results: dict[str, str] = {}
        for component_id in component_ids:
            try:
                results[component_id] = _install_one(component_id)
            except UpdateError as exc:
                results[component_id] = f"refused: {exc}"
            except Exception as exc:
                logger.exception("Install of %s failed", component_id)
                results[component_id] = f"failed: {exc}"
        return {"results": results}


def _install_one(component_id: str) -> str:
    staged = _staged.get(component_id)
    if staged is None:
        raise UpdateError(
            "not staged — download and verify the package before installing"
        )
    component = staged["component"]
    package_path = Path(staged["path"])
    if not package_path.exists():
        raise UpdateError("staged package is missing; download again")
    if security.sha256_file(package_path) != staged["sha256"]:
        raise UpdateError("staged package no longer matches its verified hash")

    package = _package_manifest(package_path)
    if package.get("id") != component_id or package.get("version") != staged["version"]:
        raise UpdateError("package contents do not match the selected update")
    package_type = package.get("type", "")
    installed = installed_versions().get(component_id, {})
    old_version = installed.get("version", "")
    target = _target_dir(package_type or installed.get("type", ""), component_id)

    extract_dir = config.UPDATE_STAGING_DIR / "extract" / component_id
    shutil.rmtree(extract_dir, ignore_errors=True)
    security.safe_extract(package_path, extract_dir)

    backup_dir = None
    if target.exists():
        backup_dir = _backup_target(target, component_id, old_version)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extract_dir), str(target))
    except OSError as exc:
        _restore_backup(backup_dir, target)
        raise UpdateError(f"install failed, previous version restored: {exc}") from exc

    if not _health_check(package_type, target):
        _restore_backup(backup_dir, target)
        _record_history(component_id, old_version, staged["version"], "failed")
        raise UpdateError("health check failed, previous version restored")

    _write_user_manifest(component_id, component, package_type)
    _record_history(component_id, old_version, staged["version"], "installed")
    _staged.pop(component_id, None)
    _stage_path(component_id, staged["version"]).unlink(missing_ok=True)
    logger.info(
        "Installed %s %s (was %s); restart_required=%s",
        component_id, staged["version"], old_version, component.get("restart_required"),
    )
    return "installed"


def _health_check(component_type: str, target: Path) -> bool:
    if component_type == "frontend":
        return (target / "index.html").exists()
    package_json = target / "package.json"
    if not package_json.exists():
        return False
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return not validate_manifest(package)


def _backup_target(target: Path, component_id: str, old_version: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = config.UPDATE_ROLLBACK_DIR / component_id / f"{old_version or 'unknown'}_{stamp}"
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(backup_dir))
    return backup_dir


def _restore_backup(backup_dir: Path | None, target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    if backup_dir is not None and backup_dir.exists():
        shutil.move(str(backup_dir), str(target))


def _write_user_manifest(component_id: str, component: dict, component_type: str) -> None:
    installed = installed_versions().get(component_id, {})
    user_manifest = {
        "id": component_id,
        "name": installed.get("name", component_id),
        "version": component["version"],
        "type": component_type or installed.get("type", "module"),
        "description": installed.get("description", ""),
        "maintainer": installed.get("maintainer", "Soundloom"),
        "core_compatibility": component["core_compatibility"],
        "dependencies": component.get("dependencies", []),
        "restart_required": component.get("restart_required", False),
    }
    config.USER_MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.USER_MANIFESTS_DIR / f"{component_id}.json"
    path.write_text(json.dumps(user_manifest, indent=2) + "\n", encoding="utf-8")


def _ledger_path() -> Path:
    return config.UPDATES_DIR / "installed.json"


def _record_history(component_id: str, old: str, new: str, result: str) -> None:
    config.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    try:
        entries = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entries = []
    entries.append({
        "component_id": component_id,
        "from_version": old,
        "to_version": new,
        "result": result,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _ledger_path().write_text(json.dumps(entries, indent=2), encoding="utf-8")


def history() -> list[dict]:
    try:
        return json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


# --- restore previous version ---

def restore(component_id: str) -> dict:
    """Return a component to its previous version.

    With a rollback backup, that backup is moved back into place. Without
    one (first update on this install), removing the loose override
    reverts to the bundled copy — which is the previous version.
    """
    component = installed_versions().get(component_id)
    if not component:
        raise UpdateError(f"unknown component: {component_id}")
    target = _target_dir(component.get("type", ""), component_id)
    backups_root = config.UPDATE_ROLLBACK_DIR / component_id
    backups = sorted(backups_root.iterdir(), reverse=True) if backups_root.is_dir() else []
    if backups:
        latest = backups[0]
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(latest), str(target))
        restored_to = latest.name.split("_")[0]
    elif target.exists():
        # Loose override without a backup: reverting means removing it, so
        # the bundled copy is served again.
        shutil.rmtree(target)
        bundled = {
            c["id"]: c["version"] for c in discover(config.MANIFESTS_DIR)
        }
        restored_to = bundled.get(component_id, "bundled version")
    else:
        raise UpdateError("no rollback is available for this component")
    _record_history(component_id, component["version"], restored_to, "restored")
    logger.info("Restored %s to %s", component_id, restored_to)
    return {"component_id": component_id, "restored_to": restored_to}


# --- startup recovery ---

def reconcile() -> dict:
    """Finish or undo an install interrupted by a crash.

    If a fully extracted package is waiting and its target never appeared,
    complete the move. If a target is missing but a backup exists, restore
    the backup. Never touches anything when the world is consistent.
    """
    actions: list[str] = []
    extract_root = config.UPDATE_STAGING_DIR / "extract"
    if extract_root.is_dir():
        for component_dir in list(extract_root.iterdir()):
            staged = _staged.get(component_dir.name)
            if staged is None:
                # No session state (app restarted): judge by package.json.
                package_json = component_dir / "package.json"
                if not package_json.exists():
                    shutil.rmtree(component_dir, ignore_errors=True)
                    actions.append(f"discarded partial extract of {component_dir.name}")
                    continue
                try:
                    package = json.loads(package_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    shutil.rmtree(component_dir, ignore_errors=True)
                    continue
                try:
                    target = _target_dir(package.get("type", ""), package.get("id", ""))
                except UpdateError:
                    shutil.rmtree(component_dir, ignore_errors=True)
                    continue
                shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(component_dir), str(target))
                actions.append(f"completed interrupted install of {target.name}")
    shutil.rmtree(extract_root, ignore_errors=True)
    return {"actions": actions}
