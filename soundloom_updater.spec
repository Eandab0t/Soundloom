# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the external core-update helper.
#
#   .venv/Scripts/python.exe -m PyInstaller soundloom_updater.spec
#
# Produces dist/SoundloomUpdater/SoundloomUpdater.exe (onefile, stdlib
# only). Ship it next to Soundloom.exe; backend/updater/core_update.py
# spawns it on shutdown when a core update is staged. It must stay tiny
# and dependency-free: it runs while the main app is not running.

a = Analysis(
    ["tools/updater_helper.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "pytest", "PyInstaller", "fastapi", "uvicorn",
        "pydantic", "aiohttp", "aiosqlite", "mutagen", "yt_dlp",
        "cryptography", "backend",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="SoundloomUpdater",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon="icon.ico",
)
