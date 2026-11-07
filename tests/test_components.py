"""Tests for the component registry (backend/components.py) and its API."""
import json

import pytest

from backend import components, config


def _write_manifest(directory, name, **overrides):
    manifest = {
        "id": name,
        "name": name.title(),
        "version": "1.0.0",
        "type": "integration",
        "description": "test component",
        "maintainer": "Soundloom",
        "core_compatibility": ">=0.2.2",
        "dependencies": [],
    }
    manifest.update(overrides)
    (directory / f"{name}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class TestValidate:
    def test_valid_manifest_has_no_errors(self):
        errors = components.validate({
            "id": "x", "name": "X", "version": "1.2.3", "type": "module",
        })
        assert errors == []

    @pytest.mark.parametrize("missing", ["id", "name", "version", "type"])
    def test_missing_required_field(self, missing):
        manifest = {"id": "x", "name": "X", "version": "1.0.0", "type": "core"}
        del manifest[missing]
        assert any(missing in e for e in components.validate(manifest))

    @pytest.mark.parametrize("bad", ["1.0", "1.0.0.0", "v1.0.0", "1.x.0", ""])
    def test_bad_version_rejected(self, bad):
        errors = components.validate({
            "id": "x", "name": "X", "version": bad, "type": "core",
        })
        assert any("version" in e for e in errors)

    @pytest.mark.parametrize("good", ["1.0.0", "0.2.2", "10.20.30"])
    def test_good_version_accepted(self, good):
        errors = components.validate({
            "id": "x", "name": "X", "version": good, "type": "core",
        })
        assert errors == []

    @pytest.mark.parametrize("bad", ["widget", "", None, 3])
    def test_unknown_type_rejected(self, bad):
        errors = components.validate({
            "id": "x", "name": "X", "version": "1.0.0", "type": bad,
        })
        assert any("type" in e for e in errors)

    @pytest.mark.parametrize("good", ["core", "module", "integration", "frontend"])
    def test_known_types_accepted(self, good):
        errors = components.validate({
            "id": "x", "name": "X", "version": "1.0.0", "type": good,
        })
        assert errors == []

    @pytest.mark.parametrize("bad", ["0.2.2", "~0.2.2", ">=x", ">=", ">=1.0"])
    def test_malformed_compat_rejected(self, bad):
        errors = components.validate({
            "id": "x", "name": "X", "version": "1.0.0", "type": "core",
            "core_compatibility": bad,
        })
        assert any("core_compatibility" in e for e in errors)

    @pytest.mark.parametrize("good", [">=0.2.2", "==1.0.0", "<2.0.0", ">1.0.0"])
    def test_wellformed_compat_accepted(self, good):
        errors = components.validate({
            "id": "x", "name": "X", "version": "1.0.0", "type": "core",
            "core_compatibility": good,
        })
        assert errors == []

    def test_dependencies_must_be_string_list(self):
        errors = components.validate({
            "id": "x", "name": "X", "version": "1.0.0", "type": "module",
            "dependencies": [1, 2],
        })
        assert any("dependencies" in e for e in errors)


class TestDiscover:
    def test_discovers_valid_manifests_sorted_by_id(self, tmp_path):
        _write_manifest(tmp_path, "beta")
        _write_manifest(tmp_path, "alpha")
        found = components.discover(tmp_path)
        assert [c["id"] for c in found] == ["alpha", "beta"]

    def test_skips_invalid_manifest(self, tmp_path):
        _write_manifest(tmp_path, "good")
        _write_manifest(tmp_path, "bad", version="not-a-version")
        found = components.discover(tmp_path)
        assert [c["id"] for c in found] == ["good"]

    def test_skips_unreadable_manifest(self, tmp_path):
        _write_manifest(tmp_path, "good")
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        found = components.discover(tmp_path)
        assert [c["id"] for c in found] == ["good"]

    def test_rejects_duplicate_ids(self, tmp_path):
        _write_manifest(tmp_path, "dup")
        (tmp_path / "dup2.json").write_text(json.dumps({
            "id": "dup", "name": "Second", "version": "2.0.0", "type": "module",
        }), encoding="utf-8")
        found = components.discover(tmp_path)
        assert len(found) == 1
        assert found[0]["version"] == "1.0.0"  # first file wins

    def test_missing_directory_returns_empty(self, tmp_path):
        assert components.discover(tmp_path / "nope") == []


class TestBundledManifests:
    """The manifests shipped with the app must all be valid and consistent."""

    def test_all_bundled_manifests_are_valid(self):
        found = components.discover(config.MANIFESTS_DIR)
        ids = [c["id"] for c in found]
        assert len(found) == 7
        assert len(ids) == len(set(ids))
        assert set(ids) == {
            "core", "frontend", "youtube", "deezer", "local", "metadata",
            "streaming-auth",
        }
        for manifest in found:
            assert manifest["version"] == config.APP_VERSION
            assert manifest["maintainer"] == "Soundloom"

    def test_only_core_is_type_core(self):
        found = components.discover(config.MANIFESTS_DIR)
        assert [c["id"] for c in found if c["type"] == "core"] == ["core"]


class TestComponentsApi:
    async def test_api_lists_components(self):
        from backend.api import system as system_api
        body = await system_api.list_components()
        assert body["core_version"] == config.APP_VERSION
        ids = {c["id"] for c in body["components"]}
        assert "core" in ids and "youtube" in ids
