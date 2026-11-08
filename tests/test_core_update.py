"""Core update flow: staging, receipts, startup recovery, external helper.

The helper logic (tools/updater_helper.py) is tested directly with fake
exe trees; the startup recovery (backend/updater/core_update.py) is tested
against the same temp layout. No process spawning happens in tests.
"""
import json

import pytest

from backend import config
from backend.updater import core_update
from tools import updater_helper


@pytest.fixture()
def core_paths(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    install_root.mkdir()
    monkeypatch.setattr(config, "ROOT", install_root)
    monkeypatch.setattr(config, "UPDATE_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(config, "UPDATES_DIR", tmp_path / "updates")
    monkeypatch.setattr(config, "UPDATE_ROLLBACK_DIR", tmp_path / "rollbacks")
    monkeypatch.setattr(config, "USER_MANIFESTS_DIR", tmp_path / "user_manifests")
    return install_root


def _core_package_bytes() -> bytes:
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("package.json", json.dumps({
            "id": "core", "name": "Soundloom Core", "version": "0.3.0",
            "type": "core",
        }))
        archive.writestr("Soundloom.exe", b"MZ fake new exe")
        archive.writestr("_internal/code.pyd", b"new dll")
    return buffer.getvalue()


def _fake_install_tree(install_root, exe_bytes=b"MZ fake old exe"):
    (install_root / "_internal").mkdir()
    (install_root / "Soundloom.exe").write_bytes(exe_bytes)
    (install_root / "_internal" / "code.pyd").write_bytes(b"old dll")


class TestStageAndPrepare:
    def test_stage_extracts_and_validates(self, core_paths, tmp_path):
        package = tmp_path / "core-0.3.0.zip"
        package.write_bytes(_core_package_bytes())
        staged = core_update.stage_core_package(package)
        assert (staged / "Soundloom.exe").exists()
        assert (staged / "_internal" / "code.pyd").exists()

    def test_stage_rejects_package_without_exe(self, core_paths, tmp_path):
        import io
        import zipfile
        package = tmp_path / "core-bad.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("readme.txt", "not a core package")
        with pytest.raises(core_update.CoreUpdateError, match="Soundloom.exe"):
            core_update.stage_core_package(package)
        # Nothing half-extracted left behind.
        assert not (config.UPDATE_STAGING_DIR / "core" / "new").exists()

    def test_prepare_writes_receipt_with_install_dir(self, core_paths):
        new_dir = config.UPDATE_STAGING_DIR / "core" / "new"
        new_dir.mkdir(parents=True)
        receipt = core_update.prepare_apply(new_dir, "0.3.0")
        assert receipt["install_dir"] == str(core_paths)
        assert receipt["version"] == "0.3.0"
        assert core_update.pending_receipt()["version"] == "0.3.0"

    def test_pending_receipt_survives_garbage(self, core_paths):
        core_dir = config.UPDATE_STAGING_DIR / "core"
        core_dir.mkdir(parents=True)
        (core_dir / "pending.json").write_text("{broken", encoding="utf-8")
        assert core_update.pending_receipt() is None

    def test_pending_receipt_treats_applied_as_consumed(self, core_paths):
        """Once the helper applied the update, the receipt must read as
        consumed — otherwise shutdown re-spawns the helper and overwrites
        the rollback backup."""
        core_dir = config.UPDATE_STAGING_DIR / "core"
        core_dir.mkdir(parents=True)
        (core_dir / "pending.json").write_text(json.dumps({
            "staged_dir": str(core_dir / "new"), "version": "0.3.0",
        }), encoding="utf-8")
        assert core_update.pending_receipt() is not None
        (core_dir / "applied.json").write_text("{}", encoding="utf-8")
        assert core_update.pending_receipt() is None


class TestStartupRecovery:
    def test_commit_when_verified(self, core_paths):
        core_dir = config.UPDATE_STAGING_DIR / "core"
        core_dir.mkdir(parents=True)
        (core_dir / "applied.json").write_text("{}", encoding="utf-8")
        (core_dir / "core_ok.json").write_text("{}", encoding="utf-8")
        actions = core_update.recover_on_startup()
        assert actions == ["core update committed"]
        assert not (core_dir / "applied.json").exists()
        assert not (core_dir / "core_ok.json").exists()

    def test_rollback_when_not_verified(self, core_paths):
        # Simulate: helper swapped the new exe in, kept the old in backup,
        # but the app never verified (crashed).
        _fake_install_tree(core_paths, exe_bytes=b"MZ new exe")
        backup = config.UPDATE_STAGING_DIR / "core" / "backup"
        backup.mkdir(parents=True)
        (backup / "Soundloom.exe").write_bytes(b"MZ old exe")
        (backup / "_internal").mkdir()
        (backup / "_internal" / "code.pyd").write_bytes(b"old dll")
        core_dir = config.UPDATE_STAGING_DIR / "core"
        (core_dir / "applied.json").write_text(json.dumps({
            "backup_dir": str(backup),
        }), encoding="utf-8")

        actions = core_update.recover_on_startup()
        assert any("rolled back" in action for action in actions)
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ old exe"
        assert (core_paths / "_internal" / "code.pyd").read_bytes() == b"old dll"
        assert not (core_dir / "applied.json").exists()

    def test_noop_when_no_markers(self, core_paths):
        assert core_update.recover_on_startup() == []

    def test_mark_verified_only_after_apply(self, core_paths):
        core_update.mark_verified()
        assert not (config.UPDATE_STAGING_DIR / "core" / "core_ok.json").exists()
        core_dir = config.UPDATE_STAGING_DIR / "core"
        core_dir.mkdir(parents=True)
        (core_dir / "applied.json").write_text("{}", encoding="utf-8")
        core_update.mark_verified()
        assert (core_dir / "core_ok.json").exists()


class TestHelperApply:
    def test_happy_path_swaps_and_marks_applied(self, core_paths, tmp_path):
        _fake_install_tree(core_paths)
        core_dir = config.UPDATE_STAGING_DIR / "core"
        staged = core_dir / "new"
        staged.mkdir(parents=True)
        (staged / "Soundloom.exe").write_bytes(b"MZ new exe")
        (staged / "_internal").mkdir()
        (staged / "_internal" / "code.pyd").write_bytes(b"new dll")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths
        )
        assert result["applied"] is True
        # New files in place, old preserved in backup.
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ new exe"
        assert (core_paths / "_internal" / "code.pyd").read_bytes() == b"new dll"
        assert (core_dir / "backup" / "Soundloom.exe").read_bytes() == b"MZ fake old exe"
        assert (core_dir / "applied.json").exists()

    def test_missing_staged_exe_rolls_back(self, core_paths, tmp_path):
        _fake_install_tree(core_paths)
        core_dir = config.UPDATE_STAGING_DIR / "core"
        staged = core_dir / "new"
        staged.mkdir(parents=True)
        (staged / "readme.txt").write_text("oops", encoding="utf-8")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths
        )
        assert result["applied"] is False
        # Install dir exactly as before.
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ fake old exe"
        assert (core_paths / "_internal" / "code.pyd").read_bytes() == b"old dll"
        assert (core_dir / "failed.json").exists()
        assert not (core_dir / "applied.json").exists()

    def test_interrupted_swap_restores_from_backup(self, core_paths, tmp_path):
        # The exe move succeeds, the _internal move fails mid-way: the undo
        # must put both back.
        _fake_install_tree(core_paths)
        core_dir = config.UPDATE_STAGING_DIR / "core"
        staged = core_dir / "new"
        staged.mkdir(parents=True)
        (staged / "Soundloom.exe").write_bytes(b"MZ new exe")
        (staged / "_internal").mkdir()  # empty internal: move succeeds, then...

        import shutil as _shutil
        original_move = _shutil.move
        calls = {"n": 0}

        def flaky_move(src, dst):
            # Fail when moving the staged _internal into place.
            if str(src).endswith("_internal") and "new" in str(src):
                calls["n"] += 1
                raise OSError("disk error")
            return original_move(src, dst)

        _shutil.move = flaky_move
        try:
            result = updater_helper.apply_staged_core(
                {"staged_dir": str(staged), "version": "0.3.0"}, core_paths
            )
        finally:
            _shutil.move = original_move

        assert result["applied"] is False
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ fake old exe"
        assert (core_paths / "_internal" / "code.pyd").read_bytes() == b"old dll"
        assert (core_dir / "failed.json").exists()


class TestHelperStateMachine:
    """The helper must be idempotent and must never overwrite the backup."""

    def _state(self, core_paths):
        core_dir = config.UPDATE_STAGING_DIR / "core"
        return core_dir, core_dir / "new", core_dir / "backup", core_dir / "pending.json"

    def test_replay_after_apply_is_idempotent(self, core_paths):
        core_dir, staged, backup, pending = self._state(core_paths)
        _fake_install_tree(core_paths, exe_bytes=b"MZ new exe")
        backup.mkdir(parents=True)
        (backup / "Soundloom.exe").write_bytes(b"MZ fake old exe")
        (core_dir / "applied.json").write_text("{}", encoding="utf-8")
        pending.write_text("{}", encoding="utf-8")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths,
            pending_path=pending,
        )
        assert result["applied"] is True and result.get("already_applied")
        # Nothing was touched and the receipt was consumed.
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ new exe"
        assert (backup / "Soundloom.exe").read_bytes() == b"MZ fake old exe"
        assert not pending.exists()

    def test_successful_apply_consumes_pending_receipt(self, core_paths):
        core_dir, staged, backup, pending = self._state(core_paths)
        _fake_install_tree(core_paths)
        staged.mkdir(parents=True)
        (staged / "Soundloom.exe").write_bytes(b"MZ new exe")
        pending.write_text("{}", encoding="utf-8")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths,
            pending_path=pending,
        )
        assert result["applied"] is True
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ new exe"
        assert (backup / "Soundloom.exe").read_bytes() == b"MZ fake old exe"
        assert not pending.exists()

    def test_reapply_after_interrupted_apply_preserves_backup(self, core_paths):
        """Crash mid-swap, then retry: the genuine old backup must survive
        and the swap must start from the restored backup state."""
        core_dir, staged, backup, pending = self._state(core_paths)
        _fake_install_tree(core_paths, exe_bytes=b"MZ old exe")
        backup.mkdir(parents=True)
        (backup / "Soundloom.exe").write_bytes(b"MZ old exe")
        (backup / "_internal").mkdir()
        (backup / "_internal" / "code.pyd").write_bytes(b"old dll")
        # Interrupted state: install dir has the new exe but old _internal.
        (core_paths / "Soundloom.exe").write_bytes(b"MZ new exe")
        staged.mkdir(parents=True)
        (staged / "Soundloom.exe").write_bytes(b"MZ new exe")
        (staged / "_internal").mkdir()
        (staged / "_internal" / "code.pyd").write_bytes(b"new dll")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths,
            pending_path=pending,
        )
        assert result["applied"] is True
        # Swap completed against the restored state.
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ new exe"
        assert (core_paths / "_internal" / "code.pyd").read_bytes() == b"new dll"
        # The backup still holds the genuine old files.
        assert (backup / "Soundloom.exe").read_bytes() == b"MZ old exe"
        assert (backup / "_internal" / "code.pyd").read_bytes() == b"old dll"

    def test_marker_write_interrupted_is_reconstructed(self, core_paths):
        """Swap finished, marker write crashed, staged emptied:
        reconstruct the marker instead of failing."""
        core_dir, staged, backup, pending = self._state(core_paths)
        _fake_install_tree(core_paths, exe_bytes=b"MZ new exe")
        (core_paths / "_internal" / "code.pyd").write_bytes(b"new dll")
        backup.mkdir(parents=True)
        (backup / "Soundloom.exe").write_bytes(b"MZ old exe")
        staged.mkdir(parents=True)  # emptied by the completed swap
        pending.write_text("{}", encoding="utf-8")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths,
            pending_path=pending,
        )
        assert result["applied"] is True and result.get("reconstructed")
        assert (core_dir / "applied.json").exists()
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ new exe"
        assert not pending.exists()

    def test_missing_staged_and_no_backup_fails_clean(self, core_paths):
        core_dir, staged, backup, pending = self._state(core_paths)
        _fake_install_tree(core_paths)
        staged.mkdir(parents=True)
        pending.write_text("{}", encoding="utf-8")

        result = updater_helper.apply_staged_core(
            {"staged_dir": str(staged), "version": "0.3.0"}, core_paths,
            pending_path=pending,
        )
        assert result["applied"] is False
        # Install dir untouched; pending kept for inspection.
        assert (core_paths / "Soundloom.exe").read_bytes() == b"MZ fake old exe"
        assert pending.exists()
        assert (core_dir / "failed.json").exists()


class TestManagerCoreInstall:
    async def test_install_core_stages_for_restart(self, core_paths, tmp_path, monkeypatch):
        """Full manager path for a core component: check → download → install."""
        import base64
        import hashlib
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from backend.updater import manager, security

        private_key = Ed25519PrivateKey.generate()
        monkeypatch.setattr(
            security, "UPDATE_SIGNING_PUBKEY",
            private_key.public_key().public_bytes_raw().hex(),
        )
        manager._last_check = None
        manager._staged.clear()

        package = _core_package_bytes()
        entry = {
            "id": "core",
            "version": "0.3.0",
            "core_compatibility": ">=0.2.2",
            "dependencies": [],
            "restart_required": True,
            "release_notes": "core update",
            "package_url": "",
            "sha256": hashlib.sha256(package).hexdigest(),
        }
        store = {"packages": {"core-0.3.0.zip": package}}

        async def package_handler(request):
            return web.Response(
                body=request.app["store"]["packages"][request.match_info["name"]],
                content_type="application/zip",
            )

        async def manifest_handler(request):
            return web.Response(
                body=request.app["store"]["manifest"], content_type="application/json"
            )

        app = web.Application()
        app["store"] = store
        app.router.add_get("/packages/{name}", package_handler)
        app.router.add_get("/updates.json", manifest_handler)
        test_server = TestServer(app)
        await test_server.start_server()
        base = str(test_server.make_url("/"))
        entry["package_url"] = base + "packages/core-0.3.0.zip"

        manifest = {
            "manifest_version": 1,
            "app_version": "0.3.0",
            "released_at": "",
            "release_notes": "",
            "components": [entry],
        }
        manifest["signature"] = base64.b64encode(
            private_key.sign(security.canonical_payload_bytes(manifest))
        ).decode("ascii")
        store["manifest"] = json.dumps(manifest).encode("utf-8")

        try:
            monkeypatch.setattr(config, "get", lambda k, d=None: {
                "update_manifest_url": base + "updates.json",
            }.get(k, d))
            await manager.check()
            await manager.download(["core"])
            result = await manager.install(["core"])
        finally:
            await test_server.close()

        assert result["results"]["core"] == "staged — restart Soundloom to apply"
        assert core_update.pending_receipt()["version"] == "0.3.0"
        assert manager.history()[-1]["result"] == "staged_for_restart"
        assert manager.status()["pending_core_restart"] is True
