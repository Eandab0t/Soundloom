"""End-to-end integration-module update: build, publish, install, activate.

Uses a synthetic module name (backend.sources.shadowtest_flat) so the real
download adapters are untouched. Proves the full Phase F chain:
tools/make_release packaging → signed manifest → manager install →
shadow loader activates the loose module at startup.
"""
import base64
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend import config, shadow
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
    monkeypatch.setattr(config, "ROOT", install_root)
    monkeypatch.setattr(config, "UPDATE_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(config, "UPDATES_DIR", tmp_path / "updates")
    monkeypatch.setattr(config, "UPDATE_ROLLBACK_DIR", tmp_path / "rollbacks")
    monkeypatch.setattr(config, "USER_MANIFESTS_DIR", tmp_path / "user_manifests")
    # give the component a bundled manifest to update from
    bundled_manifests = tmp_path / "bundled_manifests"
    bundled_manifests.mkdir()
    (bundled_manifests / "testadapter.json").write_text(json.dumps({
        "id": "testadapter",
        "name": "Test Adapter",
        "version": "0.2.2",
        "type": "integration",
        "description": "test",
        "maintainer": "Soundloom",
        "core_compatibility": ">=0.2.2",
        "dependencies": [],
    }), encoding="utf-8")
    monkeypatch.setattr(config, "MANIFESTS_DIR", bundled_manifests)
    return install_root


@pytest.fixture(autouse=True)
def reset_session_state():
    manager._last_check = None
    manager._staged.clear()
    import sys
    for name in [n for n in sys.modules if n.startswith("backend.sources.shadowtest")]:
        del sys.modules[name]
    yield
    manager._last_check = None
    manager._staged.clear()
    for name in [n for n in sys.modules if n.startswith("backend.sources.shadowtest")]:
        del sys.modules[name]


@pytest.fixture()
async def server(keypair):
    store = {"updates_json": b"", "packages": {}}

    async def manifest_handler(request):
        return web.Response(
            body=request.app["store"]["updates_json"], content_type="application/json"
        )

    async def package_handler(request):
        package = request.app["store"]["packages"].get(request.match_info["name"])
        if package is None:
            return web.Response(status=404, text="no such package")
        return web.Response(body=package, content_type="application/zip")

    app = web.Application()
    app["store"] = store
    app.router.add_get("/updates.json", manifest_handler)
    app.router.add_get("/packages/{name}", package_handler)
    test_server = TestServer(app)
    await test_server.start_server()
    yield {
        "base": str(test_server.make_url("/")),
        "key": keypair,
        "store": store,
    }
    await test_server.close()


MODULE_NAME = "backend.sources.shadowtest_flat"


def _publish_module_update(server: dict, version: str) -> None:
    package = make_release.build_module_package(
        "testadapter", version, provides={
            "backend/sources/shadowtest_flat.py": MODULE_NAME,
        },
    )
    # The repo source file must exist for build_module_package; create it.
    manifest = make_release.build_manifest(
        [(f"testadapter-{version}.zip", package)],
        app_version="0.3.0",
        core_compatibility=">=0.2.2",
        key=server["key"],
    )
    manifest["components"][0]["package_url"] = (
        server["base"] + f"packages/testadapter-{version}.zip"
    )
    manifest.pop("signature")
    manifest["signature"] = base64.b64encode(
        server["key"].sign(security.canonical_payload_bytes(manifest))
    ).decode("ascii")
    server["store"]["updates_json"] = json.dumps(manifest).encode("utf-8")
    server["store"]["packages"] = {f"testadapter-{version}.zip": package}


@pytest.fixture()
def module_source(monkeypatch, tmp_path):
    """make_release reads the repo source file; point it at a stub."""
    stub = tmp_path / "backend" / "sources" / "shadowtest_flat.py"
    stub.parent.mkdir(parents=True)
    stub.write_text("MARKER = 'from repo'\n", encoding="utf-8")
    monkeypatch.setattr(make_release, "REPO_ROOT", tmp_path)
    return stub


class TestModuleUpdateEndToEnd:
    async def test_install_then_shadow_activate(
        self, paths, server, monkeypatch, module_source
    ):
        _publish_module_update(server, "0.2.3")
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))

        checked = await manager.check()
        assert [u["id"] for u in checked["available"]] == ["testadapter"]
        staged = await manager.download(["testadapter"])
        assert staged["results"]["testadapter"] == "staged"
        installed = await manager.install(["testadapter"])
        assert installed["results"]["testadapter"] == "installed"

        module_dir = paths / "modules" / "testadapter"
        assert (module_dir / "package.json").exists()
        assert (module_dir / "shadowtest_flat.py").exists()

        # Startup-equivalent: the shadow loader activates the loose module.
        actions = shadow.load_shadows(paths / "modules")
        assert any("shadowtest_flat" in action for action in actions)
        import backend.sources.shadowtest_flat as module
        assert module.MARKER == "from repo"

        # The user manifest records the new version for future checks.
        user_manifest = json.loads(
            (config.USER_MANIFESTS_DIR / "testadapter.json").read_text(encoding="utf-8")
        )
        assert user_manifest["version"] == "0.2.3"

    async def test_restore_reverts_to_bundled(self, paths, server, monkeypatch, module_source):
        _publish_module_update(server, "0.2.3")
        monkeypatch.setattr(config, "get", lambda k, d=None: {
            "update_manifest_url": server["base"] + "updates.json",
        }.get(k, d))
        await manager.check()
        await manager.download(["testadapter"])
        await manager.install(["testadapter"])
        assert (paths / "modules" / "testadapter").exists()

        result = await manager.restore("testadapter")
        # No backup existed (first update): the loose override is removed.
        assert not (paths / "modules" / "testadapter").exists()
        assert manager.history()[-1]["result"] == "restored"
