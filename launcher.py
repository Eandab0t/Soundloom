"""Soundloom launcher - the .exe entry point.

Starts the FastAPI server inside this process, then opens the UI in *its own
window* powered by the default browser: Chromium-family browsers (Chrome,
Edge, Brave, Opera) support ``--app=<url>``, which opens a frameless,
single-page window with no tabs or address bar - it looks like a native app
but uses the browser the user already has, with nothing bundled. A dedicated
``--user-data-dir`` under data/ keeps the app window separate from the
user's normal browsing profile.

Exit semantics: the UI already heartbeats every 10 s and backend.main's
watchdog shuts the process down (``os._exit``) when no browser has
heartbeated for ``shutdown_timeout`` seconds. So "user closes the window"
naturally becomes "server exits" - in both the app-window path and the
plain-tab fallback. This launcher just stays alive while uvicorn runs.

A single-instance check stops a second copy from fighting over the port;
it opens a window at the running instance instead.
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# Frozen imports: config must resolve its paths before anything touches data.
if getattr(sys, "frozen", False):
    sys.path.insert(0, getattr(sys, "_MEIPASS"))

from backend import config


def _already_running(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_until_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


# Chromium-family browsers that support --app, most common first.
_APP_BROWSER_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "opera.exe")


def _find_app_browser() -> str | None:
    """Path to an installed Chromium-family browser (Windows registry, then PATH)."""
    import shutil
    if os.name != "nt":
        for name in ("google-chrome", "microsoft-edge", "brave-browser", "opera"):
            found = shutil.which(name)
            if found:
                return found
        return None

    import winreg
    for exe in _APP_BROWSER_EXES:
        for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                           (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                           (winreg.HKEY_CURRENT_USER, 0)):
            try:
                with winreg.OpenKey(
                    hive,
                    rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                    0,
                    winreg.KEY_READ | view,
                ) as key:
                    value, _typ = winreg.QueryValueEx(key, "")
                    if value and Path(value).is_file():
                        return value
            except OSError:
                continue
    return shutil.which("chrome") or shutil.which("msedge")


def _open_app_window(url: str) -> bool:
    """Open url in the default Chromium browser's app window. True on success."""
    exe = _find_app_browser()
    if not exe:
        return False
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [exe, f"--app={url}", f"--user-data-dir={config.DATA_DIR / 'browser-profile'}"],
            close_fds=True, creationflags=flags,
        )
        return True
    except OSError as e:
        print(f"Could not launch app window: {e}", file=sys.stderr)
        return False


def main() -> int:
    port = int(config.get("server_port", 5555))
    url = f"http://127.0.0.1:{port}"

    if _already_running(port):
        # A Soundloom server is already up; open a window at it and leave.
        if not _open_app_window(url):
            webbrowser.open(url)
        return 0

    import uvicorn
    from backend.main import app  # noqa: E402  (needs the path set up first)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", loop="asyncio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not _wait_until_ready(port):
        print("Server failed to start; see data/logs.", file=sys.stderr)
        return 1

    if not _open_app_window(url):
        webbrowser.open(url)  # plain tab; heartbeat auto-shutdown still applies

    # Stay alive while uvicorn runs. The in-app watchdog handles exit when
    # the window closes (no more heartbeats -> os._exit(0)).
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
