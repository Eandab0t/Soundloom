@echo off
title Soundloom
cd /d "%~dp0"

echo  ========================================
echo   Soundloom
echo   Weave new music into your library.
echo  ========================================
echo.
echo  Server: http://127.0.0.1:5555
echo  Mode:   On-demand (auto-shuts down when browser closes)
echo.
echo  Press Ctrl+C to stop manually.
echo.

start "" "http://127.0.0.1:5555"
timeout /t 2 /nobreak >nul

call .venv\Scripts\python.exe -m backend.main
pause
