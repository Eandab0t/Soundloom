"""Configuration management for Soundloom.

This module is the single path manager: every filesystem location the app
uses derives from the constants below, so no other module hardcodes paths.
"""
import json
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Frozen (PyInstaller) builds keep the exe in the install dir, while bundled
# read-only assets (frontend) live in _MEIPASS.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    _BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    _BUNDLE = ROOT

APP_VERSION = "0.2.2"

# --- Playlist sync defaults ---

PLAYLIST_SYNC_DEFAULT_NAME = "Soundloom Sync"


def _default_appdata_dir() -> Path:
    """%LOCALAPPDATA%/Soundloom, with a sane fallback outside Windows."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "Soundloom"


def _migrate_legacy_data(legacy: Path, target: Path) -> bool:
    """Copy the legacy exe-adjacent data/ directory to its new home.

    Copy, never move: the original directory is only ever removed by the
    user, by hand. ``logs/`` is excluded (fresh logs belong in the new
    location) along with __pycache__ debris. Returns True when the copy
    completed; on any failure the partial target is removed so the caller
    can fall back to the legacy directory without losing anything.
    """
    try:
        shutil.copytree(
            legacy,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "logs"),
        )
        logger.info(
            "Migrated user data from %s to %s (original left in place)",
            legacy, target,
        )
        return True
    except OSError as exc:
        logger.warning("Data migration to %s failed: %s", target, exc)
        shutil.rmtree(target, ignore_errors=True)
        return False


def _resolve_data_dir(root: Path, frozen: bool, env: dict) -> Path:
    """Decide where user data lives.

    Frozen builds use %LOCALAPPDATA%/Soundloom so user data survives
    application updates and never sits inside a replaceable install dir:

    1. SOUNDLOOM_DATA_DIR env var — explicit override, always wins.
    2. ``portable.flag`` inside the exe-adjacent data/ dir — keep the old
       portable layout (data next to the exe), no migration.
    3. AppData/Soundloom already exists — it is the active data dir.
    4. Legacy data/library.db exists next to the exe — one-time copy
       migration to AppData (original preserved); if the copy fails,
       keep using the legacy dir rather than risking data.
    5. Fresh install — use AppData.

    Development builds (not frozen) always use <repo>/data so the dev
    workflow and the test suite stay self-contained.
    """
    if not frozen:
        return root / "data"
    override = env.get("SOUNDLOOM_DATA_DIR")
    if override:
        return Path(override)
    legacy = root / "data"
    if (legacy / "portable.flag").exists():
        return legacy
    target = _default_appdata_dir()
    if target.exists():
        return target
    if (legacy / "library.db").exists() and not _migrate_legacy_data(legacy, target):
        return legacy
    return target


DATA_DIR = _resolve_data_dir(ROOT, getattr(sys, "frozen", False), dict(os.environ))
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "library.db"
COVERS_DIR = DATA_DIR / "covers"
LOGS_DIR = DATA_DIR / "logs"
TEMPLATES_DIR = ROOT / "templates"
EXPORTS_DIR = DATA_DIR / "exports"


def _resolve_frontend_dir(root: Path, bundle_root: Path) -> Path:
    """Prefer a loose frontend/ in the install dir over the bundled copy.

    The update system installs frontend updates as a loose directory next
    to the exe; the bundled copy under _MEIPASS remains as the fallback.
    In development both resolve to <repo>/frontend.
    """
    loose = root / "frontend"
    if (loose / "index.html").exists():
        return loose
    return bundle_root / "frontend"


FRONTEND_DIR = _resolve_frontend_dir(ROOT, _BUNDLE)

DEFAULTS = {
    "theme": "dark",
    "library_path": str(Path.home() / "Music"),
    "download_path": str(Path.home() / "Downloads" / "Music"),
    "folder_template": "{album_artist}\\{album}\\{track_number} - {title}.{format}",
    "default_format": "mp3",
    "default_quality": "balanced",
    "max_concurrent_downloads": 3,
    "max_retries": 3,
    "match_threshold": 70,
    # Recovering a file that already reached the library always happens.
    # Re-downloading an interrupted track costs bandwidth, so it is opt-in.
    "auto_resume_operations": False,
    "watch_enabled": True,
    "watched_check_interval": 3600,
    "watch_backfill_days": 30,
    "fetch_method": "musicbrainz",
    "fetch_confidence": 0.7,
    "fetch_overwrite": False,
    "fetch_batch_limit": 25,
    "musicbrainz_email": "",
    "acoustid_api_key": "",
    "discogs_token": "",
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "tag_case_mode": "off",
    "backup_before_write": True,
    # At startup, mark track rows whose file is no longer on disk as
    # missing (and restore rows whose file came back). Existence check
    # only - no tag reading - and it refuses to run at all when the
    # library folder is unavailable, so an unmounted drive cannot blank
    # the library. Turn off for a library on storage that is often
    # offline.
    "verify_library_on_startup": True,
    "backup_format": "json",
    # --- Playlist sync (Soundiiz-style) ---
    "sync_enabled": True,
    "sync_interval": 21600,          # seconds between scheduled sync passes (6h)
    "sync_max_tracks": 200,          # per playlist per pass
    # Default name for a freshly created playlist-sync intent.
    "playlist_sync_default_name": "Soundloom Sync",
    "spotify_redirect_uri": "http://127.0.0.1:5555/api/connections/callback",
    "spotify_user_token": "",        # {access, refresh, expiry} from the user OAuth flow
    "soundcloud_client_id": "",
    "soundcloud_client_secret": "",
    "soundcloud_redirect_uri": "http://127.0.0.1:5555/api/connections/callback",
    "soundcloud_user_token": "",     # {access, refresh, expiry} from the user OAuth flow
    # --- Audio source priority ---
    # Order in which search-based sources are tried for artist-title jobs
    # (sync, watch, search). Direct URLs always use their own source.
    "source_priority": ["youtube", "deezer", "soundcloud"],
    "source_fallback": True,         # failed search jobs try the next source
    "auto_update": "notify",
    "duplicate_policy": "keep_separate",
    "server_port": 5555,
    "server_host": "127.0.0.1",
    "auto_shutdown": True,
    "shutdown_timeout": 30,
}

_cache: dict | None = None

# The Spotify callback used to default to /static/callback.html, a page that
# never existed (the real OAuth handler is /api/connections/callback). Anyone
# who saved settings before that fix has the dead URL persisted; rewrite only
# that exact stale default — never a redirect URI the user chose themselves.
_LEGACY_REDIRECT_URIS = {
    "spotify_redirect_uri": "http://127.0.0.1:5555/static/callback.html",
}


def _repair_legacy_redirects(settings: dict) -> bool:
    changed = False
    for key, stale in _LEGACY_REDIRECT_URIS.items():
        if settings.get(key) == stale:
            settings[key] = DEFAULTS[key]
            changed = True
    return changed


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    _ensure_dirs()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            _cache = {**DEFAULTS, **stored}
            if _repair_legacy_redirects(_cache):
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(_cache, f, indent=2)
                logger.info("Repaired legacy redirect URI in settings.json")
        except Exception:
            logger.warning("Failed to load settings, using defaults")
            _cache = dict(DEFAULTS)
    else:
        _cache = dict(DEFAULTS)
    return _cache


def save_settings(data: dict) -> None:
    global _cache
    _ensure_dirs()
    _cache = {**DEFAULTS, **data}
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(_cache, f, indent=2)
    logger.info("Settings saved")


def get(key: str, default=None):
    return load_settings().get(key, default)


def get_all() -> dict:
    return dict(load_settings())


def update(partial: dict) -> dict:
    current = load_settings()
    current.update(partial)
    save_settings(current)
    return current
