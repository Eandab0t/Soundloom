"""Tests for the update manager and Update Center API.

Remote update servers are mocked with a local aiohttp TestServer; nothing
here touches the network beyond 127.0.0.1. Approval semantics are tested
directly: installing without staging must always be refused.
"""
import base64
import io
import json
import zipfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend import config
from backend.updater import manager, security


@pytest.fixture()
def keypair(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        security, "UPDATE_SIGNING_PUBKEY",
        private_key.public_key().public_bytes_raw().hex(),
    )
    return private_key


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    """Redirect every update path and the install root into a scratch dir."""
    install_root = tmp_path / "install"
    install_root.mkdir()
    monkeypatch.setattr(config, "ROOT", install_root)
    monkeypatch.setattr(config, "UPDATE_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(config, "UPDATES_DIR", tmp_path / "updates")
    monkeypatch.setattr(config, "UPDATE_ROLLBACK_DIR", tmp_path / "rollbacks")
    monkeypatch.setattr(config, "USER_MANIFESTS_DIR", tmp_path / "user_manifests")
    return install_root


@pytest.fixture(autouse=True)
def reset_session_state():
    manager._last_check = None
    manager._staged.clear()
    yield
    manager._last_check = None
    manager._staged.clear()


def _zip_bytes(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _package(component_id: str, version: str, package_type: str, files: dict) -> bytes:
    entries = {
        "package.json": json.dumps({
            "id": component_id,
            "name": component_id.title(),
            "version": version,
            "type": package_type,
        }),
        **files,
    }
    return _zip_bytes(entries)


def _component_entry(component_id: str, version: str, package: bytes, base_url: str,
                     core_compatibility=">=0.2.2", **overrides):
    import hashlib
    entry = {
        "id": component_id,
        "version": version,
        "core_compatibility": core_compatibility,
        "dependencies": [],
        "restart_required": True,
        "release_notes": "test update",
        "package_url": f"{base_url}packages/{component_id}-{version}.zip",
        "sha256": hashlib.sha256(package).hexdigest(),
    }
    entry.update(overrides)
    return entry


def _signed_manifest(private_key: Ed25519PrivateKey, components: list) -> dict:
    body = {
        "manifest_version": 1,
        "app_version": "0.3.0",
        "released_at": "2026-11-08T00:00:00Z",
        "release_notes": "test channel",
        "components": components,
    }
    body["signature"] = base64.b64encode(
        private_key.sign(security.canonical_payload_bytes(body))
    ).decode("ascii")
    return body


@pytest.fixture()
async def server(keypair):
    """Mock remote update source: /updates.json + /packages/*."""
    private_key = keypair
    state = {"manifest": {}, "packages": {}}

    async def manifest_handler(request):
        return web.json_response(state["manifest"])

    async def package_handler(request):
        name = request.match_info["name"]
        package = state["packages"].get(name)
        if package is None:
            return web.Response(status=404, text="no such package")
        return web.Response(body=package, content_type="application/zip")

    app = web.Application()
    app.router.add_get("/updates.json", manifest_handler)
    app.router.add_get("/packages/{name}", package_handler)
    test_server = TestServer(app)
    await test_server.start_server()
    state["base"] = str(test_server.make_url("/"))
    state["private_key"] = private_key
    yield state
    await test_server.close()


def _publish(server, component_id: str, version: str, package_type: str,
             files: dict, **entry_overrides):
    package = _package(component_id, version, package_type, files)
    server["packages"][f"{component_id}-{version}.zip"] = package
    entry = _component_entry(
        component_id, version, package, server["base"], **entry_overrides
    )
    manifest_body = dict(server["manifest"])
    if not manifest_body:
        server["manifest"] = _signed_manifest(
            server["private_key"], [entry]
        )
    else:
        # Republish with the extra component, re-signing the body.
        body = {
            "manifest_version": 1,
            "app_version": "0.3.0",
            "released_at": "2026-11-08T00:00:00Z",
            "release_notes": "test channel",
            "components": [
                c for c in manifest_body.get("components", []) if c["id"] != component_id
            ] + [entry],
        }
        body["signature"] = base64.b64encode(
            server["private_key"].sign(security.canonical_payload_bytes(body))
        ).decode("ascii")
        server["manifest"] = body
    return entry


class TestCheck:
    async def test_not_configured(self):
        with pytest.raises(manager.UpdateError):
            await manager.check()

    async def test_http_non_loopback_refused(self, monkeypatch, keypair):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": "http://updates.example.com/updates.json",
        }.get(k, d))
        with pytest.raises(manager.UpdateError, match="https"):
            await manager.check()

    async def test_bad_signature_refused(self, server, monkeypatch):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        server["manifest"]["components"][0]["version"] = "9.9.9"  # tamper
        with pytest.raises(manager.UpdateError, match="signature"):
            await manager.check()

    async def test_valid_manifest_lists_updates(self, server, monkeypatch):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        result = await manager.check()
        assert [u["id"] for u in result["available"]] == ["youtube"]
        assert manager.status()["last_check"] == result["checked_at"]

    async def test_unconfigured_pubkey_fails_closed(self, server, monkeypatch):
        monkeypatch.setattr(security, "UPDATE_SIGNING_PUBKEY", "")
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        with pytest.raises(manager.UpdateError, match="signature"):
            await manager.check()


class TestDownload:
    async def test_requires_check_first(self):
        with pytest.raises(manager.UpdateError, match="Check for updates"):
            await manager.download(["youtube"])

    async def test_unknown_component_refused(self, server, monkeypatch, keypair):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        await manager.check()
        result = await manager.download(["ghost"])
        assert "refused" in result["results"]["ghost"]
        assert manager.status()["staged"] == []

    async def test_happy_path_stages(self, server, monkeypatch, keypair):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        await manager.check()
        result = await manager.download(["youtube"])
        assert result["results"]["youtube"] == "staged"
        assert manager.status()["staged"] == ["youtube"]

    async def test_hash_mismatch_not_staged(self, server, monkeypatch, keypair):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        entry = _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        server["packages"][f"youtube-0.2.3.zip"] += b"tampered"
        await manager.check()
        result = await manager.download(["youtube"])
        assert "hash mismatch" in result["results"]["youtube"]
        assert manager.status()["staged"] == []


class TestInstallApproval:
    async def test_install_without_staging_refused(self, paths, server, monkeypatch, keypair):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {"code.py": "x"})
        await manager.check()
        result = await manager.install(["youtube"])
        assert "not staged" in result["results"]["youtube"]

    async def test_install_without_check_refused(self, paths):
        result = await manager.install(["youtube"])
        assert "not staged" in result["results"]["youtube"]


class TestInstallFrontend:
    async def _check_and_stage(self, server, monkeypatch):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "frontend", "0.3.0", "frontend", {
            "index.html": "<html>new ui</html>",
            "css/app.css": "body{}",
        })
        await manager.check()
        await manager.download(["frontend"])

    async def test_happy_path_replaces_frontend(self, paths, server, monkeypatch, keypair):
        old_index = paths / "frontend" / "index.html"
        old_index.parent.mkdir()
        old_index.write_text("<html>old ui</html>", encoding="utf-8")
        await self._check_and_stage(server, monkeypatch)

        result = await manager.install(["frontend"])
        assert result["results"]["frontend"] == "installed"
        assert (old_index.read_text(encoding="utf-8")
                == "<html>new ui</html>")
        # Old version preserved as a rollback backup.
        backups = list((config.UPDATE_ROLLBACK_DIR / "frontend").iterdir())
        assert len(backups) == 1
        assert (backups[0] / "index.html").read_text(encoding="utf-8") == "<html>old ui</html>"
        # User manifest records the new version; history records the install.
        user_manifest = json.loads(
            (config.USER_MANIFESTS_DIR / "frontend.json").read_text(encoding="utf-8")
        )
        assert user_manifest["version"] == "0.3.0"
        history = manager.history()
        assert history[-1]["result"] == "installed"
        assert history[-1]["from_version"] == "0.2.2"
        # Staged state cleared.
        assert manager.status()["staged"] == []

    async def test_health_check_failure_rolls_back(self, paths, server, monkeypatch, keypair):
        old_index = paths / "frontend" / "index.html"
        old_index.parent.mkdir()
        old_index.write_text("<html>old ui</html>", encoding="utf-8")
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        # Package has no index.html → health check must fail.
        _publish(server, "frontend", "0.3.0", "frontend", {"css/app.css": "body{}"})
        await manager.check()
        await manager.download(["frontend"])

        result = await manager.install(["frontend"])
        assert "health check failed" in result["results"]["frontend"]
        assert old_index.read_text(encoding="utf-8") == "<html>old ui</html>"
        assert manager.history()[-1]["result"] == "failed"

    async def test_staged_file_tampered_after_download_refused(
        self, paths, server, monkeypatch, keypair
    ):
        await self._check_and_stage(server, monkeypatch)
        staged_path = config.UPDATE_STAGING_DIR / "frontend-0.3.0.zip"
        staged_path.write_bytes(staged_path.read_bytes() + b"evil")

        result = await manager.install(["frontend"])
        assert "hash" in result["results"]["frontend"]

    async def test_core_type_stages_for_restart(self, paths, server, monkeypatch, keypair):
        """Core never dir-swaps; a proper core package stages for the
        external updater and applies on restart (full flow covered in
        test_core_update.py)."""
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        monkeypatch.setattr(config, "UPDATE_STAGING_DIR", paths.parent / "staging")
        _publish(server, "core", "0.3.0", "core", {"Soundloom.exe": "MZ fake exe"})
        await manager.check()
        await manager.download(["core"])
        result = await manager.install(["core"])
        assert "staged" in result["results"]["core"]


class TestInstallIntegration:
    async def test_happy_path_installs_module(self, paths, server, monkeypatch, keypair):
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "youtube", "0.2.3", "integration", {
            "adapter.py": "# updated adapter",
        })
        await manager.check()
        await manager.download(["youtube"])
        result = await manager.install(["youtube"])
        assert result["results"]["youtube"] == "installed"
        target = paths / "modules" / "youtube"
        assert (target / "adapter.py").read_text(encoding="utf-8") == "# updated adapter"
        assert (target / "package.json").exists()


class TestRestore:
    async def test_restores_previous_version(self, paths, server, monkeypatch, keypair):
        old_index = paths / "frontend" / "index.html"
        old_index.parent.mkdir()
        old_index.write_text("<html>old ui</html>", encoding="utf-8")
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        _publish(server, "frontend", "0.3.0", "frontend", {
            "index.html": "<html>new ui</html>",
        })
        await manager.check()
        await manager.download(["frontend"])
        await manager.install(["frontend"])

        result = manager.restore("frontend")
        assert result["component_id"] == "frontend"
        assert old_index.read_text(encoding="utf-8") == "<html>old ui</html>"
        assert manager.history()[-1]["result"] == "restored"

    async def test_restore_without_backup_refused(self, paths):
        with pytest.raises(manager.UpdateError, match="no rollback"):
            manager.restore("frontend")


class TestReconcile:
    async def test_completes_interrupted_install(self, paths, monkeypatch):
        package_json = json.dumps({
            "id": "youtube", "name": "Youtube", "version": "0.2.3",
            "type": "integration",
        })
        extract_dir = config.UPDATE_STAGING_DIR / "extract" / "youtube"
        extract_dir.mkdir(parents=True)
        (extract_dir / "package.json").write_text(package_json, encoding="utf-8")
        (extract_dir / "adapter.py").write_text("# new", encoding="utf-8")

        result = manager.reconcile()
        assert any("youtube" in action for action in result["actions"])
        assert (paths / "modules" / "youtube" / "adapter.py").exists()

    async def test_discards_extract_without_package_json(self, paths):
        extract_dir = config.UPDATE_STAGING_DIR / "extract" / "junk"
        extract_dir.mkdir(parents=True)
        (extract_dir / "loose.py").write_text("x", encoding="utf-8")

        result = manager.reconcile()
        assert any("discarded" in action for action in result["actions"])
        assert not extract_dir.exists()

    async def test_noop_when_consistent(self, paths):
        assert manager.reconcile() == {"actions": []}


class TestApi:
    async def test_status_endpoint(self):
        from backend.api.updates import updates_status
        body = await updates_status()
        assert body["core_version"] == config.APP_VERSION
        assert "components" in body and "staged" in body

    async def test_install_endpoint_refuses_unstaged(self):
        from backend.api.updates import updates_install, ComponentSelection
        body = await updates_install(ComponentSelection(component_ids=["youtube"]))
        assert "not staged" in body["results"]["youtube"]

    async def test_history_endpoint(self, tmp_path, monkeypatch):
        from backend.api.updates import updates_history
        monkeypatch.setattr(config, "UPDATES_DIR", tmp_path / "updates")
        body = await updates_history()
        assert body["history"] == []
