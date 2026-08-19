@echo off
title Big Pickle - VividlyMusicaly
cd /d "%~dp0"

echo  ========================================
echo   Big Pickle - VividlyMusicaly
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
