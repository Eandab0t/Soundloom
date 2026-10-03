"""Tests for the settings API security + validation contract.

Two properties matter here:
1. GET /api/settings must never leak client secrets or OAuth tokens.
2. PUT /api/settings must validate input and must not let a blank password
   field wipe an already-configured secret.
"""
import pytest
from pydantic import ValidationError

from backend.api.settings import (
    SECRET_KEYS,
    SettingsUpdate,
    _clean_update,
    get_settings,
    public_settings,
    update_settings,
)


class StubConfig:
    """Minimal stand-in for backend.config with a dict-backed store."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get_all(self):
        return dict(self._values)

    def update(self, partial):
        self._values.update(partial)
        return dict(self._values)


@pytest.fixture
def stub_config(monkeypatch):
    import backend.api.settings as settings_api

    stub = StubConfig({
        "theme": "dark",
        "library_path": "C:/Users/you/Music",
        "max_retries": 3,
        "spotify_client_id": "public-id",
        "spotify_client_secret": "super-secret-value",
        "soundcloud_client_secret": "sc-secret",
        "spotify_user_token": {"access": "zz-oauth-access-value", "refresh": "zz-oauth-refresh"},
        "acoustid_api_key": "acoustic-key",
        "discogs_token": "discogs-token-value",
    })
    monkeypatch.setattr(settings_api.config, "get_all", stub.get_all)
    monkeypatch.setattr(settings_api.config, "update", stub.update)
    return stub


class TestSecretsNeverLeaveTheServer:
    async def test_public_settings_has_no_secret_values(self, stub_config):
        out = public_settings()
        for key in SECRET_KEYS:
            assert key not in out, f"{key} leaked to the client"

    async def test_get_settings_endpoint_is_redacted(self, stub_config):
        out = await get_settings()
        assert "spotify_client_secret" not in out
        assert "super-secret-value" not in str(out)
        assert "acoustic-key" not in str(out)
        assert "discogs-token-value" not in str(out)

    async def test_user_tokens_are_redacted(self, stub_config):
        out = await get_settings()
        assert "spotify_user_token" not in out
        assert "zz-oauth-access-value" not in str(out)
        assert "zz-oauth-refresh" not in str(out)

    async def test_client_id_stays_public(self, stub_config):
        """The client id is not a secret - the UI must still show it."""
        out = await get_settings()
        assert out["spotify_client_id"] == "public-id"

    async def test_configured_flags_replace_secret_values(self, stub_config):
        out = await get_settings()
        assert out["spotify_client_secret_configured"] is True
        assert out["soundcloud_client_secret_configured"] is True
        assert out["discogs_token_configured"] is True

    async def test_configured_flag_false_when_unset(self, stub_config):
        stub_config._values["acoustid_api_key"] = ""
        out = await get_settings()
        assert out["acoustid_api_key_configured"] is False

    async def test_non_secret_settings_still_returned(self, stub_config):
        out = await get_settings()
        assert out["theme"] == "dark"
        assert out["max_retries"] == 3
        assert out["library_path"] == "C:/Users/you/Music"


class TestBlankSecretDoesNotWipeStoredValue:
    async def test_blank_secret_is_dropped_from_update(self):
        payload = SettingsUpdate(spotify_client_secret="   ")
        assert _clean_update(payload) == {}

    async def test_omitted_secret_is_dropped_from_update(self):
        payload = SettingsUpdate(theme="light")
        assert _clean_update(payload) == {"theme": "light"}

    async def test_typed_secret_is_kept(self):
        payload = SettingsUpdate(spotify_client_secret="new-secret")
        assert _clean_update(payload) == {"spotify_client_secret": "new-secret"}

    async def test_saving_forms_keeps_existing_secret(self, stub_config):
        """The settings form sends a blank field for an untouched secret."""
        payload = SettingsUpdate(
            theme="light",
            spotify_client_secret="",
            soundcloud_client_secret="",
        )
        out = await update_settings(payload)
        assert stub_config.get_all()["spotify_client_secret"] == "super-secret-value"
        assert stub_config.get_all()["soundcloud_client_secret"] == "sc-secret"
        assert out["spotify_client_secret_configured"] is True
        assert "spotify_client_secret" not in out

    async def test_replacing_secret_works_and_still_hides_it(self, stub_config):
        payload = SettingsUpdate(spotify_client_secret="brand-new")
        out = await update_settings(payload)
        assert stub_config.get_all()["spotify_client_secret"] == "brand-new"
        assert "brand-new" not in str(out)
        assert out["spotify_client_secret_configured"] is True

    async def test_oauth_tokens_cannot_be_written_via_settings(self):
        """Read-only tokens belong to /api/connections, not this form."""
        with pytest.raises(ValidationError):
            SettingsUpdate(spotify_user_token={"access": "evil"})


class TestValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(not_a_real_setting=1)

    def test_concurrency_range_enforced(self):
        assert SettingsUpdate(max_concurrent_downloads=8).max_concurrent_downloads == 8
        with pytest.raises(ValidationError):
            SettingsUpdate(max_concurrent_downloads=0)
        with pytest.raises(ValidationError):
            SettingsUpdate(max_concurrent_downloads=9)

    def test_retries_range_enforced(self):
        assert SettingsUpdate(max_retries=0).max_retries == 0
        with pytest.raises(ValidationError):
            SettingsUpdate(max_retries=-1)
        with pytest.raises(ValidationError):
            SettingsUpdate(max_retries=11)

    def test_match_threshold_is_a_percentage(self):
        assert SettingsUpdate(match_threshold=100).match_threshold == 100
        with pytest.raises(ValidationError):
            SettingsUpdate(match_threshold=101)

    def test_confidence_is_zero_to_one(self):
        assert SettingsUpdate(fetch_confidence=0.9).fetch_confidence == 0.9
        with pytest.raises(ValidationError):
            SettingsUpdate(fetch_confidence=1.5)

    def test_bad_enum_rejected(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(theme="neon")
        with pytest.raises(ValidationError):
            SettingsUpdate(duplicate_policy="delete_everything")

    def test_wrong_type_rejected(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(max_retries="lots")

    def test_unknown_template_variable_rejected(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(folder_template="{evil_var}")

    def test_good_template_accepted(self):
        tmpl = "{album_artist}\\{album}\\{track_number} - {title}.{format}"
        assert SettingsUpdate(folder_template=tmpl).folder_template == tmpl

    def test_relative_library_path_rejected(self):
        with pytest.raises(ValidationError):
            SettingsUpdate(library_path="Music")

    def test_absolute_paths_accepted(self):
        assert SettingsUpdate(library_path="C:/Music").library_path == "C:/Music"
        assert SettingsUpdate(download_path="/home/me/Music").download_path == "/home/me/Music"

    def test_full_valid_form_passes(self):
        payload = SettingsUpdate(
            theme="light",
            library_path="C:/Music",
            download_path="C:/Downloads",
            folder_template="{artist}\\{title}.{format}",
            duplicate_policy="overwrite",
            max_concurrent_downloads=5,
            max_retries=2,
            sync_interval=7200,
            spotify_client_id="id",
            spotify_client_secret="secret",
            auto_shutdown=True,
            shutdown_timeout=45,
        )
        cleaned = _clean_update(payload)
        assert cleaned["theme"] == "light"
        assert cleaned["max_concurrent_downloads"] == 5
        assert cleaned["spotify_client_secret"] == "secret"


class TestSyncIntervalFloor:
    def test_sync_interval_must_be_at_least_an_hour(self):
        assert SettingsUpdate(sync_interval=3600).sync_interval == 3600
        with pytest.raises(ValidationError):
            SettingsUpdate(sync_interval=60)