"""Settings endpoints.

Security contract
-----------------
``GET /api/settings`` never returns credentials. Secret settings stay on the
server; the UI gets a ``<key>_configured`` boolean instead. That keeps client
secrets, OAuth tokens and API keys out of the browser (and out of any devtools
network log, screenshot or "copy response" a user might paste into an issue).

``PUT /api/settings`` accepts a validated subset of keys only. Anything else is
rejected, and empty strings for existing secrets mean "leave as-is" so a blank
password field cannot wipe a configured credential.
"""
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import config
from ..validation import validate_folder_template

router = APIRouter(prefix="/api/settings", tags=["settings"])

QUALITY_PRESETS = {
    "best": {"name": "Best Quality", "format": "flac", "bitrate": "lossless", "description": "Lossless audio, maximum quality"},
    "balanced": {"name": "Balanced", "format": "mp3", "bitrate": "320k", "description": "High quality, reasonable file size"},
    "ipod_saver": {"name": "iPod Saver", "format": "m4a", "bitrate": "128k", "description": "Small files, optimized for iPod storage"},
}

# Never sent to the browser. The UI learns only whether each one is set.
SECRET_KEYS = frozenset({
    "spotify_client_secret",
    "soundcloud_client_secret",
    "spotify_user_token",
    "soundcloud_user_token",
    "acoustid_api_key",
    "discogs_token",
})

# Secrets the settings form is allowed to write. Read-only tokens (the OAuth
# user tokens) are managed by /api/connections and deliberately excluded.
WRITABLE_SECRET_KEYS = frozenset({
    "spotify_client_secret",
    "soundcloud_client_secret",
    "acoustid_api_key",
    "discogs_token",
})

VALID_THEMES = ("dark", "light")
VALID_DUPLICATE_POLICIES = ("skip", "overwrite", "keep_separate")
VALID_TAG_CASE_MODES = ("off", "lower", "title", "upper")


def _configured_flags(settings: dict) -> dict:
    """{secret_key: "<key>_configured": bool} for every secret setting."""
    return {f"{key}_configured": bool(settings.get(key)) for key in SECRET_KEYS}


def public_settings() -> dict:
    """The settings view safe to serve to the browser: no secrets."""
    settings = config.get_all()
    return {
        key: value
        for key, value in settings.items()
        if key not in SECRET_KEYS
    } | _configured_flags(settings)


class SettingsUpdate(BaseModel):
    """Validated, allow-listed settings update.

    Unknown keys are rejected (``extra="forbid"``) so a typo or a stray field
    can never be silently persisted into settings.json.
    """

    model_config = ConfigDict(extra="forbid")

    theme: Literal["dark", "light"] | None = None
    library_path: str | None = None
    download_path: str | None = None
    folder_template: str | None = None
    duplicate_policy: Literal["skip", "overwrite", "keep_separate"] | None = None
    tag_case_mode: Literal["off", "lower", "title", "upper"] | None = None
    fetch_method: str | None = None
    auto_update: Literal["notify", "always", "never"] | None = None
    backup_format: Literal["json", "none"] | None = None
    max_concurrent_downloads: int | None = Field(default=None, ge=1, le=8)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    match_threshold: int | None = Field(default=None, ge=0, le=100)
    fetch_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    fetch_batch_limit: int | None = Field(default=None, ge=1, le=500)
    sync_interval: int | None = Field(default=None, ge=3600, le=604800)
    watched_check_interval: int | None = Field(default=None, ge=300, le=86400)
    watch_backfill_days: int | None = Field(default=None, ge=0, le=3650)
    shutdown_timeout: int | None = Field(default=None, ge=5, le=600)
    backup_before_write: bool | None = None
    watch_enabled: bool | None = None
    sync_enabled: bool | None = None
    source_fallback: bool | None = None
    auto_shutdown: bool | None = None
    spotify_client_id: str | None = None
    soundcloud_client_id: str | None = None
    # Secrets: blank/None means "keep the stored value".
    spotify_client_secret: str | None = None
    soundcloud_client_secret: str | None = None
    acoustid_api_key: str | None = None
    discogs_token: str | None = None

    @field_validator("folder_template")
    @classmethod
    def _check_template(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return validate_folder_template(v)
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("library_path", "download_path")
    @classmethod
    def _check_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v and not v.startswith(("\\\\", "//")) and ":" not in v and "/" not in v:
            raise ValueError("path must be absolute")
        return v


def _clean_update(payload: SettingsUpdate) -> dict:
    """Drop unset fields and treat blank secrets as 'keep existing'."""
    data: dict[str, Any] = {}
    for key, value in payload.model_dump().items():
        if value is None:
            continue
        if key in WRITABLE_SECRET_KEYS and isinstance(value, str) and not value.strip():
            continue  # empty password field: leave the stored secret alone
        data[key] = value
    return data


@router.get("")
async def get_settings():
    return public_settings()


@router.put("")
async def update_settings(payload: SettingsUpdate):
    data = _clean_update(payload)
    if not data:
        return public_settings()
    saved = config.update(data)
    return {k: v for k, v in saved.items() if k not in SECRET_KEYS} | _configured_flags(saved)


@router.get("/quality-presets")
async def get_quality_presets():
    return QUALITY_PRESETS