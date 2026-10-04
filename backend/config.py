"""Configuration management for Soundloom."""
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Frozen (PyInstaller) builds keep the user-writable data directory next to
# the exe, while bundled read-only assets (frontend) live in _MEIPASS.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    _BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    _BUNDLE = ROOT

APP_VERSION = "0.2.2"

DATA_DIR = ROOT / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "library.db"
COVERS_DIR = DATA_DIR / "covers"
LOGS_DIR = DATA_DIR / "logs"
TEMPLATES_DIR = ROOT / "templates"
FRONTEND_DIR = _BUNDLE / "frontend"
EXPORTS_DIR = DATA_DIR / "exports"

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
    "backup_format": "json",
    # --- Playlist sync (Soundiiz-style) ---
    "sync_enabled": True,
    "sync_interval": 21600,          # seconds between scheduled sync passes (6h)
    "sync_max_tracks": 200,          # per playlist per pass
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
