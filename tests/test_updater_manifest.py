"""Tests for backend/updater/manifest: validation and update planning."""
import pytest

from backend.updater import manifest


def _component(component_id="youtube", version="0.2.3", **overrides):
    component = {
        "id": component_id,
        "version": version,
        "core_compatibility": ">=0.2.2",
        "dependencies": [],
        "restart_required": True,
        "release_notes": "fixes",
        "package_url": "https://example.com/packages/x.zip",
        "sha256": "a" * 64,
    }
    component.update(overrides)
    return component


def _manifest(components=None, **overrides):
    body = {
        "manifest_version": 1,
        "app_version": "0.3.0",
        "released_at": "2026-11-08T00:00:00Z",
        "release_notes": "test",
        "components": components if components is not None else [_component()],
    }
    body.update(overrides)
    return body


class TestValidateUpdateManifest:
    def test_valid_manifest_has_no_errors(self):
        assert manifest.validate_update_manifest(_manifest()) == []

    def test_wrong_manifest_version(self):
        errors = manifest.validate_update_manifest(_manifest(manifest_version=2))
        assert any("manifest_version" in e for e in errors)

    def test_bad_app_version(self):
        errors = manifest.validate_update_manifest(_manifest(app_version="3"))
        assert any("app_version" in e for e in errors)

    def test_non_https_package_url_rejected(self):
        errors = manifest.validate_update_manifest(
            _manifest([_component(package_url="http://example.com/x.zip")])
        )
        assert any("https" in e for e in errors)

    def test_bad_sha256_rejected(self):
        errors = manifest.validate_update_manifest(_manifest([_component(sha256="abc")]))
        assert any("sha256" in e for e in errors)

    def test_bad_dependency_type_rejected(self):
        errors = manifest.validate_update_manifest(
            _manifest([_component(dependencies="youtube")])
        )
        assert any("dependencies" in e for e in errors)

    def test_duplicate_component_ids_rejected(self):
        errors = manifest.validate_update_manifest(
            _manifest([_component(), _component()])
        )
        assert any("duplicate" in e for e in errors)

    def test_components_not_a_list(self):
        errors = manifest.validate_update_manifest(_manifest(components="x"))
        assert any("components" in e for e in errors)


class TestAvailableUpdates:
    def test_newer_version_is_offered(self):
        updates, problems = manifest.available_updates(
            _manifest(), {"youtube": "0.2.2"}, core_version="0.2.2"
        )
        assert [u["id"] for u in updates] == ["youtube"]
        assert problems == []

    def test_same_or_older_version_not_offered(self):
        for installed in ("0.2.3", "1.0.0"):
            updates, _ = manifest.available_updates(
                _manifest(), {"youtube": installed}, core_version="0.2.2"
            )
            assert updates == []

    def test_incompatible_core_reported_not_offered(self):
        updates, problems = manifest.available_updates(
            _manifest([_component(core_compatibility=">=9.0.0")]),
            {"youtube": "0.2.2"},
            core_version="0.2.2",
        )
        assert updates == []
        assert problems and "requires core" in problems[0]

    def test_uninstalled_component_reported(self):
        updates, problems = manifest.available_updates(
            _manifest(), {}, core_version="0.2.2"
        )
        assert updates == []
        assert problems and "not installed" in problems[0]

    def test_mixed_batch(self):
        updates, problems = manifest.available_updates(
            _manifest([
                _component("youtube", "0.2.3"),
                _component("deezer", "0.2.9", core_compatibility=">=9.0.0"),
            ]),
            {"youtube": "0.2.2", "deezer": "0.2.2"},
            core_version="0.2.2",
        )
        assert [u["id"] for u in updates] == ["youtube"]
        assert len(problems) == 1 and "deezer" in problems[0]


class TestResolveDependencies:
    def test_no_dependencies_resolves_to_selection(self):
        resolved, problems = manifest.resolve_dependencies(
            ["youtube"], [_component("youtube")]
        )
        assert resolved == ["youtube"]
        assert problems == []

    def test_dependency_is_pulled_in_before_dependent(self):
        resolved, problems = manifest.resolve_dependencies(
            ["youtube"], [
                _component("youtube", dependencies=["metadata"]),
                _component("metadata"),
            ]
        )
        assert resolved == ["metadata", "youtube"]
        assert problems == []

    def test_transitive_dependencies(self):
        resolved, problems = manifest.resolve_dependencies(
            ["top"], [
                _component("top", dependencies=["mid"]),
                _component("mid", dependencies=["base"]),
                _component("base"),
            ]
        )
        assert resolved == ["base", "mid", "top"]

    def test_unknown_dependency_is_a_problem(self):
        resolved, problems = manifest.resolve_dependencies(
            ["youtube"], [_component("youtube", dependencies=["ghost"])]
        )
        assert resolved == ["youtube"]
        assert problems and "ghost" in problems[0]

    def test_circular_dependencies_detected(self):
        resolved, problems = manifest.resolve_dependencies(
            ["a"], [
                _component("a", dependencies=["b"]),
                _component("b", dependencies=["a"]),
            ]
        )
        assert problems and "circular" in problems[0]

    def test_unrelated_available_components_not_included(self):
        resolved, problems = manifest.resolve_dependencies(
            ["youtube"], [
                _component("youtube"),
                _component("deezer"),
            ]
        )
        assert resolved == ["youtube"]
