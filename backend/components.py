"""Component registry: discovers and validates component manifests.

A component manifest (JSON) describes one independently versioned piece of
Soundloom — the core, the frontend, an integration adapter. The update
system compares these installed manifests against a remote update manifest;
this module only reads, validates and lists them.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

COMPONENT_TYPES = {"core", "module", "integration", "frontend", "dependency", "resource"}
_COMPAT_OPERATORS = (">=", "<=", "==", "!=", ">", "<")
_REQUIRED_FIELDS = ("id", "name", "version", "type")


def _is_version(text: str) -> bool:
    """Strict X.Y.Z: three dot-separated non-negative integers."""
    parts = text.split(".")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def _is_constraint(text: str) -> bool:
    """Version constraint like '>=0.2.2' or '==1.0.0'."""
    for operator in _COMPAT_OPERATORS:
        if text.startswith(operator):
            return _is_version(text[len(operator):])
    return False


def validate(manifest: dict) -> list[str]:
    """Return the schema problems in a manifest; an empty list means valid."""
    errors = []
    for field in _REQUIRED_FIELDS:
        if not manifest.get(field):
            errors.append(f"missing required field: {field}")
    version = manifest.get("version", "")
    if version and not _is_version(version):
        errors.append(f"version {version!r} is not X.Y.Z")
    if manifest.get("type") not in COMPONENT_TYPES:
        errors.append(f"unknown type: {manifest.get('type')!r}")
    compat = manifest.get("core_compatibility")
    if compat is not None and not _is_constraint(compat):
        errors.append(f"core_compatibility {compat!r} is malformed")
    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(dep, str) for dep in dependencies
    ):
        errors.append("dependencies must be a list of component ids")
    return errors


def discover(manifest_dir: Path) -> list[dict]:
    """Load every valid ``*.json`` manifest in the directory, sorted by id.

    Invalid manifests are logged and skipped — one bad file must never
    hide the components that are fine.
    """
    if not manifest_dir.is_dir():
        return []
    seen_ids: set[str] = set()
    components: list[dict] = []
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable manifest %s: %s", path.name, exc)
            continue
        errors = validate(manifest)
        if errors:
            logger.warning("Skipping invalid manifest %s: %s", path.name, "; ".join(errors))
            continue
        if manifest["id"] in seen_ids:
            logger.warning("Skipping duplicate component id %r in %s", manifest["id"], path.name)
            continue
        seen_ids.add(manifest["id"])
        components.append(manifest)
    return components
