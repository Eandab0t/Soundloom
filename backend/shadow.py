"""Shadow-module loader: loose modules/ packages override bundled code.

PyInstaller freezes Python into the exe, so updated integration code
cannot be swapped inside it. Instead the Update Center installs loose
packages into ``<install dir>/modules/<id>/`` and this loader (run at
``backend`` import time, before anything imports a source adapter)
replaces the bundled modules in ``sys.modules`` with the loose ones.

Each loose package carries a ``package.json`` whose ``provides`` maps the
original repo file path to the dotted module name it replaces:

    {"provides": {
        "backend/sources/ytdlp_source.py": "backend.sources.ytdlp_source"
    }}

A package root (an entry whose repo path ends in ``__init__.py``) is
loaded with its search locations pointing at the loose directory, so its
submodules resolve from the loose copy too; ``provides`` entries under
that directory are then covered automatically.

Failure isolation is per component: a broken loose package is skipped
with a log line and the bundled copy stays in effect. Rollback (removing
or restoring the loose directory) therefore always returns the app to the
bundled behavior on the next start.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_parents(module_name: str) -> None:
    """Import parent packages before registering a shadow module.

    If only the full name lands in sys.modules, a later
    ``import backend.sources.x as y`` short-circuits without importing
    ``backend.sources`` and the binding fails. Importing the parents here
    keeps every later import form working.
    """
    parent, _, _ = module_name.rpartition(".")
    if parent:
        importlib.import_module(parent)


def _load_flat(module_dir: Path, module_name: str, file_name: str) -> None:
    _ensure_parents(module_name)
    spec = importlib.util.spec_from_file_location(module_name, module_dir / file_name)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_name} from {file_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


def _load_package(module_dir: Path, module_name: str, file_name: str) -> None:
    _ensure_parents(module_name)
    spec = importlib.util.spec_from_file_location(
        module_name,
        module_dir / file_name,
        submodule_search_locations=[str(module_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package {module_name} from {file_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


def _load_component(component_dir: Path) -> str:
    """Load one loose component; returns a description of what was replaced."""
    package_json = component_dir / "package.json"
    package = json.loads(package_json.read_text(encoding="utf-8"))
    provides: dict[str, str] = package.get("provides") or {}
    if not provides:
        return f"{component_dir.name}: nothing to shadow"

    # Package roots first; entries under their directory are covered by the
    # root's search locations and must not be loaded separately.
    replaced: list[str] = []
    roots: list[tuple[str, str]] = []
    for repo_path, module_name in provides.items():
        if repo_path.replace("\\", "/").endswith("__init__.py"):
            roots.append((repo_path, module_name))
    covered_prefixes = tuple(
        repo_path.rsplit("__init__.py", 1)[0] for repo_path, _ in roots
    )
    for repo_path, module_name in sorted(roots, key=lambda item: len(item[0])):
        _load_package(component_dir, module_name, Path(repo_path).name)
        replaced.append(module_name)
    for repo_path, module_name in sorted(provides.items()):
        if repo_path.replace("\\", "/").startswith(covered_prefixes):
            continue
        if (repo_path, module_name) in roots:
            continue
        _load_flat(component_dir, module_name, Path(repo_path).name)
        replaced.append(module_name)
    return f"{component_dir.name}: shadowed {', '.join(replaced)}"


def load_shadows(modules_root: Path) -> list[str]:
    """Load every loose module package under modules_root.

    Returns descriptions of what was replaced; broken components are
    logged and skipped so the bundled code keeps working.
    """
    actions: list[str] = []
    if not modules_root.is_dir():
        return actions
    for component_dir in sorted(modules_root.iterdir()):
        if not component_dir.is_dir():
            continue
        if not (component_dir / "package.json").exists():
            continue
        saved: dict[str, object] = {}
        try:
            # Snapshot sys.modules entries this component may replace, so a
            # mid-load failure leaves the bundled imports intact.
            package = json.loads(
                (component_dir / "package.json").read_text(encoding="utf-8")
            )
            for module_name in (package.get("provides") or {}).values():
                saved[module_name] = sys.modules.get(module_name)
            actions.append(_load_component(component_dir))
            logger.info("Loaded module shadow: %s", actions[-1])
        except Exception as exc:
            for module_name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(module_name, None)
                else:
                    sys.modules[module_name] = previous
            logger.warning(
                "Skipping module shadow %s (bundled copy stays active): %s",
                component_dir.name, exc,
            )
    return actions
