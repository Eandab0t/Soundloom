"""End-to-end update flow: build a signed package with tools/make_release,
serve it from a mock source, and install it through the manager.

This is the Phase E live-fire exercise for frontend updates, using the same
mock-server technique as test_updates_manager.py. Nothing touches the real
network.
"""
import base64
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend import config
from backend.updater import manager, security
from tools import make_release


@pytest.fixture()
def keypair(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        security, "UPDATE_SIGNING_PUBKEY",
        private_key.public_key().public_bytes_raw().hex(),
    )
    return private_key


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    install_root.mkdir()
    bundle_root = tmp_path / "_internal"
    bundle_root.mkdir()
    monkeypatch.setattr(config, "ROOT", install_root)
    monkeypatch.setattr(config, "_BUNDLE", bundle_root)
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


@pytest.fixture()
async def server(keypair, tmp_path):
    state = {}
    app_state = {"updates_json": b"", "packages": {}}

    async def manifest_handler(request):
        return web.Response(
            body=request.app["state"]["updates_json"],
            content_type="application/json",
        )

    async def package_handler(request):
        name = request.match_info["name"]
        package = request.app["state"]["packages"].get(name)
        if package is None:
            return web.Response(status=404, text="no such package")
        return web.Response(body=package, content_type="application/zip")

    app = web.Application()
    app["state"] = app_state
    app.router.add_get("/updates.json", manifest_handler)
    app.router.add_get("/packages/{name}", package_handler)
    test_server = TestServer(app)
    await test_server.start_server()
    state["base"] = str(test_server.make_url("/"))
    state["key"] = keypair
    state["store"] = app_state
    yield state
    await test_server.close()


def _publish_frontend_update(server: dict, frontend_dir, version: str) -> None:
    package = make_release.build_frontend_package(frontend_dir, version)
    manifest = make_release.build_manifest(
        [(f"frontend-{version}.zip", package)],
        app_version="0.3.0",
        core_compatibility=">=0.2.2",
        key=server["key"],
        release_notes="new UI",
    )
    # Rewrite package_url to the mock server.
    manifest["components"][0]["package_url"] = (
        server["base"] + f"packages/frontend-{version}.zip"
    )
    manifest.pop("signature")
    manifest["signature"] = base64.b64encode(
        server["key"].sign(security.canonical_payload_bytes(manifest))
    ).decode("ascii")
    server["store"]["updates_json"] = json.dumps(manifest).encode("utf-8")
    server["store"]["packages"] = {f"frontend-{version}.zip": package}


class TestFrontendUpdateEndToEnd:
    async def test_build_check_download_install_serves_new_ui(
        self, paths, server, monkeypatch
    ):
        # The running app serves the bundled frontend (no loose copy yet).
        bundled = config._BUNDLE / "frontend"
        bundled.mkdir(parents=True)
        (bundled / "index.html").write_text("<html>old ui</html>", encoding="utf-8")

        # Build an update from a "new" frontend source tree.
        new_frontend = paths / "new-frontend-src"
        (new_frontend / "css").mkdir(parents=True)
        (new_frontend / "index.html").write_text("<html>new ui</html>", encoding="utf-8")
        (new_frontend / "css" / "app.css").write_text("body{}", encoding="utf-8")
        _publish_frontend_update(server, new_frontend, "0.3.0")

        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))

        # check → download → install, the exact sequence the UI drives.
        checked = await manager.check()
        assert [u["id"] for u in checked["available"]] == ["frontend"]
        staged = await manager.download(["frontend"])
        assert staged["results"]["frontend"] == "staged"
        installed = await manager.install(["frontend"])
        assert installed["results"]["frontend"] == "installed"

        # The loose copy now exists and shadows the bundle...
        assert config.frontend_dir() == paths / "frontend"
        # ...and a fresh render of the resolution picks up the new UI.
        assert (config.frontend_dir() / "index.html").read_text(encoding="utf-8") == (
            "<html>new ui</html>"
        )
        # Rollback of the previous loose/bundled state exists; history written.
        assert manager.history()[-1]["result"] == "installed"
        assert manager.status()["rollbacks"] == {"frontend": "0.2.2"}

    async def test_restore_brings_bundle_state_back(self, paths, server, monkeypatch):
        bundled = config._BUNDLE / "frontend"
        bundled.mkdir(parents=True)
        (bundled / "index.html").write_text("<html>old ui</html>", encoding="utf-8")

        new_frontend = paths / "new-frontend-src"
        new_frontend.mkdir()
        (new_frontend / "index.html").write_text("<html>new ui</html>", encoding="utf-8")
        _publish_frontend_update(server, new_frontend, "0.3.0")
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))

        await manager.check()
        await manager.download(["frontend"])
        await manager.install(["frontend"])
        assert (paths / "frontend" / "index.html").read_text(encoding="utf-8") == (
            "<html>new ui</html>"
        )

        manager.restore("frontend")
        # The loose override is gone; resolution falls back to the bundle.
        assert config.frontend_dir() == config._BUNDLE / "frontend"
        assert manager.history()[-1]["result"] == "restored"

    async def test_frontend_dir_reResolution_after_install(self, paths):
        """config.frontend_dir() resolves on demand, not at import time."""
        bundled = config._BUNDLE / "frontend"
        bundled.mkdir(parents=True)
        (bundled / "index.html").write_text("old", encoding="utf-8")
        assert config.frontend_dir() == bundled
        loose = paths / "frontend"
        loose.mkdir()
        (loose / "index.html").write_text("new", encoding="utf-8")
        assert config.frontend_dir() == loose
