"""Tests for the shadow-module loader (backend/shadow.py).

Uses synthetic module names (backend.sources.shadowtest_*) so real source
modules are never replaced mid-test-session.
"""
import json
import sys

import pytest

from backend import shadow


@pytest.fixture()
def modules_root(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def restore_sys_modules():
    """Undo any sys.modules replacement a test performs."""
    def _purge():
        for name in list(sys.modules):
            if name.startswith("backend.sources.shadowtest"):
                del sys.modules[name]
    saved = dict(sys.modules)
    _purge()
    yield
    _purge()
    for name, module in saved.items():
        if name.startswith("backend.sources.shadowtest") or name not in sys.modules:
            sys.modules[name] = module


def _write_component(root, component_id, provides, files):
    component = root / component_id
    component.mkdir(parents=True)
    (component / "package.json").write_text(
        json.dumps({"id": component_id, "provides": provides}), encoding="utf-8"
    )
    for name, content in files.items():
        (component / name).write_text(content, encoding="utf-8")
    return component


class TestFlatShadow:
    def test_replaces_module_in_sys_modules(self, modules_root):
        _write_component(
            modules_root, "testcomp",
            {"backend/sources/shadowtest_flat.py": "backend.sources.shadowtest_flat"},
            {"shadowtest_flat.py": "MARKER = 'shadowed'\n"},
        )
        actions = shadow.load_shadows(modules_root)
        assert any("shadowed" in action for action in actions)
        import backend.sources.shadowtest_flat as module
        assert module.MARKER == "shadowed"

    def test_no_modules_dir_is_noop(self, tmp_path):
        assert shadow.load_shadows(tmp_path / "nope") == []

    def test_dir_without_package_json_is_ignored(self, modules_root):
        stray = modules_root / "stray"
        stray.mkdir()
        (stray / "junk.py").write_text("x", encoding="utf-8")
        assert shadow.load_shadows(modules_root) == []


class TestPackageShadow:
    def test_package_root_covers_submodules(self, modules_root):
        _write_component(
            modules_root, "testpkg",
            {
                "backend/sources/shadowtest_pkg/__init__.py": "backend.sources.shadowtest_pkg",
                "backend/sources/shadowtest_pkg/helper.py": "backend.sources.shadowtest_pkg.helper",
            },
            {
                "__init__.py": "from . import helper\nVALUE = helper.HELPER\n",
                "helper.py": "HELPER = 'from shadow'\n",
            },
        )
        actions = shadow.load_shadows(modules_root)
        assert any("shadowtest_pkg" in action for action in actions)
        import backend.sources.shadowtest_pkg as package
        assert package.VALUE == "from shadow"
        # The helper came from the shadow directory, not a real module file.
        assert "modules" in str(getattr(package.helper, "__file__", ""))
        assert "testpkg" in str(getattr(package.helper, "__file__", ""))
        # The submodule provide entry was covered by the package root.
        assert not any("shadowtest_pkg.helper" in action for action in actions)


class TestFailureIsolation:
    def test_missing_provided_file_skips_component(self, modules_root):
        _write_component(
            modules_root, "broken",
            {"backend/sources/shadowtest_flat.py": "backend.sources.shadowtest_flat"},
            {},  # no actual file
        )
        actions = shadow.load_shadows(modules_root)
        assert actions == []
        assert "backend.sources.shadowtest_flat" not in sys.modules

    def test_broken_package_json_skips_component(self, modules_root):
        component = modules_root / "broken"
        component.mkdir(parents=True)
        (component / "package.json").write_text("{bad json", encoding="utf-8")
        assert shadow.load_shadows(modules_root) == []

    def test_execution_error_restores_previous_module(self, modules_root):
        # A previously-imported (bundled) module must survive a broken shadow.
        import types
        bundled = types.ModuleType("backend.sources.shadowtest_flat")
        bundled.MARKER = "bundled"
        sys.modules["backend.sources.shadowtest_flat"] = bundled

        _write_component(
            modules_root, "broken",
            {"backend/sources/shadowtest_flat.py": "backend.sources.shadowtest_flat"},
            {"shadowtest_flat.py": "raise RuntimeError('boom')\n"},
        )
        actions = shadow.load_shadows(modules_root)
        assert actions == []
        assert sys.modules["backend.sources.shadowtest_flat"].MARKER == "bundled"

    def test_one_broken_component_does_not_block_others(self, modules_root):
        _write_component(
            modules_root, "a_broken",
            {"backend/sources/shadowtest_a.py": "backend.sources.shadowtest_a"},
            {},  # missing file
        )
        _write_component(
            modules_root, "b_good",
            {"backend/sources/shadowtest_b.py": "backend.sources.shadowtest_b"},
            {"shadowtest_b.py": "MARKER = 'b'\n"},
        )
        actions = shadow.load_shadows(modules_root)
        assert any("shadowtest_b" in action for action in actions)
        assert not any("shadowtest_a" in action for action in actions)
        assert sys.modules["backend.sources.shadowtest_b"].MARKER == "b"


class TestStartupHook:
    def test_backend_init_contains_the_hook(self):
        """backend/__init__ activates shadows at import time (before any
        source adapter is imported). The functional behavior of the loader
        itself is covered above; this checks the wiring exists."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "backend" / "__init__.py").read_text(encoding="utf-8")
        assert "_activate_module_shadows" in source
        assert "load_shadows" in source
