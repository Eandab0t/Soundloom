@echo off
REM ======================================================================
REM  E-Tuner Application Launcher
REM ======================================================================
REM  Double-click this file to launch E-Tuner's interactive menu.
REM  It will auto-install dependencies if needed, then start.
REM
REM  Usage:
REM    Double-click launch_etuner.bat              -> interactive menu
REM    launch_etuner.bat scan "D:\Music" --dry-run -> any CLI command
REM
REM  All arguments after the script name are forwarded to the E-Tuner CLI.
REM ======================================================================
setlocal

REM Find Python — try common locations, then fall back to PATH.
set "PYEXE="
for %%p in (
    "py -3.12"
    "py -3.11"
    "py -3.10"
    "py"
    "python"
    "python3"
) do (
    if not defined PYEXE (
        where /q %%p 2>nul
        if !errorlevel! equ 0 (
            for /f "delims=" %%i in ('where %%p 2^>nul') do (
                if not defined PYEXE set "PYEXE=%%i"
            )
        )
    )
)

if not defined PYEXE (
    echo ERROR: Python 3.10 or newer was not found.
    echo Please install Python from https://python.org and try again.
    echo.
    pause
    exit /b 1
)

REM Run the launcher which handles dependency checks and starts E-Tuner.
cd /d "%~dp0"
"%PYEXE%" run_etuner.py %*

pause
endlocal
