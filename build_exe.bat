@echo off
rem Build Soundloom.exe (one-dir) with PyInstaller.
rem Output: dist\Soundloom\Soundloom.exe  (ship the whole folder)

cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo No .venv found - create it and install requirements first.
  exit /b 1
)

.venv\Scripts\python.exe tools\make_icon.py icon.ico || exit /b 1
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean soundloom_exe.spec || exit /b 1

echo.
echo Built: dist\Soundloom\Soundloom.exe
echo Launch it directly - it starts the server and opens the UI window.
