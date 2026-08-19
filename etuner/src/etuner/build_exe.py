#!/usr/bin/env python
"""Build E-Tuner as a standalone .exe using PyInstaller.

Usage:  python build_exe.py
Output: dist/etuner.exe  (~15 MB, single portable executable)

Like Vividl's portable zip — copy the .exe to any Windows folder and
double-click to launch the interactive menu.
"""
import subprocess
import sys


def main() -> int:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "etuner_exe.spec",
        "--clean",
        "--noconfirm",
    ]
    print("Building etuner.exe ...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nBuild failed (exit {result.returncode})")
        return 1
    print("\nBuild complete: dist/etuner.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
