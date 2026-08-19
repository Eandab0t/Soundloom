#!/usr/bin/env python3
r"""
E-Tuner Application Launcher
=============================

A single-entry-point launcher that makes E-Tuner feel like a real application:

  1. Verifies the Python version (3.10+ required).
  2. Checks for required runtime dependencies (mutagen, requests).
     If any are missing it tries to install them via pip.
  3. Ensures the E-Tuner package itself is importable (installs in
     editable mode if the source tree is present but not installed).
  4. By default launches the interactive menu — no flags needed.
     Pass CLI arguments to forward them to ``etuner`` directly, e.g.::

         run_etuner.py scan "D:\Music" --dry-run
         run_etuner.py fix "D:\Music" --yes --rename
         run_etuner.py --version

     With no arguments (or ``menu``), the guided interactive menu starts.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_PY = (3, 10)
REQUIRED = {"mutagen": ">=1.47", "requests": ">=2.31"}

# On Windows, set stdout/stderr to UTF-8 so banner characters display.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    os.system("chcp 65001 > nul 2>&1")


def _ansi(code: str, text: str) -> str:
    if os.environ.get("NO_COLOR") or os.environ.get("ETUNER_NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def _info(text: str) -> None:
    print(f"  {_ansi('36', '•')} {text}")


def _ok(text: str) -> None:
    print(f"  {_ansi('32', '✓')} {text}")


def _warn(text: str) -> None:
    print(f"  {_ansi('33', '!')} {text}")


def _error(text: str) -> None:
    print(f"  {_ansi('31', '✗')} {text}", file=sys.stderr)


def _check_python() -> bool:
    _info(f"Python {sys.version.split()[0]} (need {MIN_PY[0]}.{MIN_PY[1]}+)")
    if sys.version_info < MIN_PY:
        _error(f"Python {MIN_PY[0]}.{MIN_PY[1]} or newer is required.")
        return False
    _ok("Python version OK")
    return True


def _ensure_package_installed() -> bool:
    """Import-check etuner; install editable if missing."""
    try:
        import etuner  # noqa: F401
        _ok(f"E-Tuner {etuner.__version__} is importable")
        return True
    except ImportError:
        _warn("E-Tuner package not installed yet — installing in development mode...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-e", HERE],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _ok("E-Tuner installed")
            return True
        except subprocess.CalledProcessError as exc:
            _error(f"Failed to install E-Tuner: {exc}")
            return False


def _ensure_dependency(pkg: str, spec: str) -> bool:
    import importlib
    mod = pkg.replace("-", "_")
    try:
        importlib.import_module(mod)
        _ok(f"{pkg}{spec} already installed")
        return True
    except ImportError:
        _warn(f"Installing {pkg}{spec} ...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", f"{pkg}{spec}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _ok(f"{pkg}{spec} installed")
            return True
        except subprocess.CalledProcessError as exc:
            _error(f"Failed to install {pkg}: {exc}")
            return False


def _print_banner() -> None:
    banner = r"""
   ████████╗███╗   ███╗██████╗  ██████╗ ██╗██╗  ██╗
   ╚══██╔══╝████╗ ████║██╔══██╗██╔═══██╗██║╚██╗██╔╝
      ██║   ██╔████╔██║██║  ██║██║   ██║██║ ╚███╔╝
      ██║   ██║╚██╔╝██║██║  ██║██║   ██║██║ ██╔██╗
      ██║   ██║ ╚═╝ ██║██████╔╝╚██████╔╝██║██╔╝ ██║
      ╚═╝   ╚═╝     ╚═╝╚═════╝  ╚═════╝ ╚═╝╚═╝  ╚═╝
"""
    print(_ansi("36", banner))
    print(_ansi("2", "   Smart music metadata repair"))
    print(_ansi("2", "   " + "─" * 40))


def main() -> int:
    _print_banner()
    print()
    _info("Pre-flight checks")

    if not _check_python():
        return 1

    dep_ok = True
    for pkg, spec in REQUIRED.items():
        if not _ensure_dependency(pkg, spec):
            dep_ok = False

    if not dep_ok:
        _error("Missing dependencies. Install them manually:")
        _error(f"  pip install {' '.join(f'{p}{s}' for p, s in REQUIRED.items())}")
        return 1

    if not _ensure_package_installed():
        return 1

    print()
    _ok("Ready! Launching E-Tuner...")
    print()

    argv = sys.argv[1:]

    if not argv:
        # No args → interactive menu
        from etuner.menu import run_menu
        try:
            return run_menu()
        except KeyboardInterrupt:
            print("\n  Interrupted.")
            return 130

    if argv[0] == "--version":
        import etuner
        print(f"E-Tuner {etuner.__version__}")
        return 0

    if argv[0] == "--help":
        print(textwrap.dedent(r"""\
            E-Tuner — smart music metadata repair

            Usage:
              run_etuner.py                        Launch interactive menu
              run_etuner.py <folder>                 Launch menu with a folder pre-selected
              run_etuner.py scan <folder> [flags]    Preview changes (read-only)
              run_etuner.py fix <folder> [flags]     Clean tags (with confirm)
              run_etuner.py rename <folder> [flags]  Rename files by template
              run_etuner.py fetch <folder> [flags]   Look up metadata online
              run_etuner.py restore <file>           Restore tags from a backup
              run_etuner.py menu                     Interactive guided menu

            Examples:
              run_etuner.py "D:\Music"
              run_etuner.py scan "D:\Music" --dry-run
              run_etuner.py fix "D:\Music" --yes --rename
              run_etuner.py fetch "D:\Music" --method musicbrainz --yes
              run_etuner.py restore "D:\Music\etuner_backup_20240101_120000.json"

            All flags supported by the CLI are forwarded.
        """))
        return 0

    # If the first arg looks like a folder path, launch menu with that folder
    if os.path.isdir(argv[0]) and argv[0] not in ("scan", "fix", "rename", "fetch", "menu"):
        from etuner.menu import run_menu
        try:
            return run_menu(os.path.abspath(argv[0]))
        except KeyboardInterrupt:
            print("\n  Interrupted.")
            return 130

    # Otherwise, forward everything to the real CLI
    from etuner.cli import main as cli_main
    sys.argv = ["etuner"] + argv
    try:
        cli_main()
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
