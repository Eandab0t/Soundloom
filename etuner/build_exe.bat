@echo off
REM Build script for E-Tuner standalone .exe
REM Run from the project root: build_exe.bat
REM
REM Produces dist/etuner.exe — a single portable executable (~15 MB).
REM Like Vividl's portable zip, you can copy the .exe to any Windows folder
REM and double-click to launch.

cd /d "%~dp0"

python build_exe.py

exit /b %ERRORLEVEL%
