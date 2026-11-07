"""Soundloom backend package.

On import, before any submodule loads, activate loose module shadows from
<install dir>/modules/ (installed by the Update Center). A no-op when the
directory does not exist, which is the normal case in development.
"""


def _activate_module_shadows() -> None:
    from pathlib import Path

    from . import config

    try:
        from .shadow import load_shadows

        load_shadows(config.ROOT / "modules")
    except Exception:  # pragma: no cover - never block startup on shadows
        import logging

        logging.getLogger(__name__).exception("Module shadow loading failed")


_activate_module_shadows()
