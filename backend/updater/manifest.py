"""Remote update manifest: validation and update planning.

The remote manifest (updates.json) lists every published component
version. Validation is structural; planning compares it against the
installed components and answers "what can be updated, and what else
must come with it".
"""
from __future__ import annotations

from urllib.parse import urlparse

from . import versions

MANIFEST_VERSION = 1
_COMPONENT_FIELDS = (
    "id", "version", "core_compatibility", "dependencies",
    "restart_required", "release_notes", "package_url", "sha256",
)


def is_loopback_url(url: str) -> bool:
    """True for 127.0.0.1/localhost/::1 — the only non-https hosts allowed.

    Loopback is exempt because there is no network between the app and a
    local mock/test server to intercept.
    """
    host = urlparse(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def _valid_package_url(url: str) -> bool:
    return url.startswith("https://") or is_loopback_url(url)


def _validate_component(component: dict, index: int) -> list[str]:
    errors = []
    label = f"components[{index}]"
    if not component.get("id"):
        errors.append(f"{label}: missing id")
    try:
        versions.parse_version(component.get("version", ""))
    except versions.VersionError:
        errors.append(f"{label}: version must be X.Y.Z")
    constraint = component.get("core_compatibility", "")
    try:
        if constraint:
            versions.satisfies("999.0.0", constraint)
    except versions.VersionError:
        errors.append(f"{label}: core_compatibility {constraint!r} is malformed")
    sha = component.get("sha256", "")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        errors.append(f"{label}: sha256 must be 64 hex chars")
    url = component.get("package_url", "")
    if not _valid_package_url(url):
        errors.append(f"{label}: package_url must be https")
    dependencies = component.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(dep, str) for dep in dependencies
    ):
        errors.append(f"{label}: dependencies must be a list of component ids")
    return errors


def validate_update_manifest(manifest: dict) -> list[str]:
    """Structural validation of a remote update manifest."""
    errors = []
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version must be {MANIFEST_VERSION}")
    try:
        versions.parse_version(manifest.get("app_version", ""))
    except versions.VersionError:
        errors.append("app_version must be X.Y.Z")
    if not isinstance(manifest.get("components"), list):
        errors.append("components must be a list")
        return errors
    seen = set()
    for index, component in enumerate(manifest["components"]):
        errors.extend(_validate_component(component, index))
        component_id = component.get("id")
        if component_id:
            if component_id in seen:
                errors.append(f"components[{index}]: duplicate id {component_id!r}")
            seen.add(component_id)
    return errors


def _compatible_with_core(component: dict, core_version: str) -> bool:
    try:
        return versions.satisfies(core_version, component["core_compatibility"])
    except versions.VersionError:
        return False


def available_updates(
    manifest: dict,
    installed: dict[str, str],
    core_version: str,
) -> tuple[list[dict], list[str]]:
    """Return (updatable components, problems).

    A component is updatable when its remote version is strictly newer
    than the installed one (downgrade protection) and its
    core_compatibility is satisfied by the running core. Components whose
    compatibility fails are reported as problems, never offered.
    """
    problems: list[str] = []
    updates: list[dict] = []
    for component in manifest.get("components", []):
        component_id = component.get("id", "?")
        installed_version = installed.get(component_id)
        if installed_version is None:
            problems.append(f"{component_id}: not installed on this machine")
            continue
        try:
            if not versions.is_newer(component["version"], installed_version):
                continue
        except versions.VersionError:
            continue
        if not _compatible_with_core(component, core_version):
            problems.append(
                f"{component_id}: requires core {component['core_compatibility']}, "
                f"running {core_version}"
            )
            continue
        updates.append(component)
    return updates, problems


def resolve_dependencies(
    selected_ids: list[str],
    available: list[dict],
) -> tuple[list[str], list[str]]:
    """Expand a selection into the full set of components to install.

    Returns (resolved ids in install order, problems). A dependency is
    added when it appears in the available list; a dependency that is
    already installed at a compatible version is not re-installed.
    Unknown or cyclic dependencies become problems.
    """
    by_id = {component["id"]: component for component in available}
    resolved: list[str] = []
    problems: list[str] = []

    def visit(component_id: str, chain: tuple[str, ...]) -> None:
        if component_id in chain:
            problems.append(f"circular dependency: {' -> '.join(chain + (component_id,))}")
            return
        if component_id in resolved:
            return
        component = by_id.get(component_id)
        if component is None:
            problems.append(f"{component_id}: no update available for this dependency")
            return
        for dependency in component.get("dependencies", []):
            visit(dependency, chain + (component_id,))
        if component_id not in resolved:
            resolved.append(component_id)

    for selected in selected_ids:
        visit(selected, ())
    return resolved, problems
