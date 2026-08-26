"""Configuration management for VividlyMusicaly."""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "library.db"
COVERS_DIR = DATA_DIR / "covers"
LOGS_DIR = DATA_DIR / "logs"
TEMPLATES_DIR = ROOT / "templates"

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
    "auto_update": "notify",
    "duplicate_policy": "keep_separate",
    "server_port": 5555,
    "server_host": "127.0.0.1",
    "auto_shutdown": True,
    "shutdown_timeout": 30,
}

_cache: dict | None = None


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


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
