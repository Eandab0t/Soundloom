"""PyInstaller spec for E-Tuner standalone .exe.

Produces a single portable executable (like Vividl's portable zip).
Usage:  pyinstaller etuner_exe.spec
Output: dist/etuner.exe
"""
# -*- mode: python -*-
import os
import sys

block_cipher = None

# Ensure the project root is on the path so 'etuner' package is importable.
project_root = SPECPATH

a = Analysis(
    [os.path.join(project_root, "etuner", "exe_entry.py")],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "config.example.json"), "."),
        (os.path.join(project_root, "README.md"), "."),
    ],
    hiddenimports=["etuner.cli", "etuner.menu"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="etuner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
