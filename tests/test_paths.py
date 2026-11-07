"""Tests for the centralized path manager in backend/config.py.

Covers the frozen-build data-dir resolution rules (env override, portable
flag, AppData adoption, one-time legacy migration with fallback) and the
frontend directory shadow resolution.
"""
import sys
from pathlib import Path

import pytest

from backend import config


def _frozen_env(tmp_path, monkeypatch):
    """Point LOCALAPPDATA at a scratch dir so tests never touch the real one."""
    appdata = tmp_path / "LocalAppData"
    appdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    return appdata


class TestResolveDataDir:
    def test_dev_mode_uses_repo_data(self, tmp_path):
        result = config._resolve_data_dir(tmp_path, frozen=False, env={})
        assert result == tmp_path / "data"

    def test_env_override_wins(self, tmp_path):
        override = tmp_path / "custom" / "data"
        result = config._resolve_data_dir(
            tmp_path, frozen=True, env={"SOUNDLOOM_DATA_DIR": str(override)}
        )
        assert result == override

    def test_portable_flag_keeps_legacy_layout(self, tmp_path, monkeypatch):
        _frozen_env(tmp_path, monkeypatch)
        legacy = tmp_path / "data"
        legacy.mkdir()
        (legacy / "portable.flag").touch()
        result = config._resolve_data_dir(tmp_path, frozen=True, env={})
        assert result == legacy

    def test_existing_appdata_is_adopted(self, tmp_path, monkeypatch):
        appdata = _frozen_env(tmp_path, monkeypatch)
        target = appdata / "Soundloom"
        target.mkdir()
        (target / "library.db").touch()
        result = config._resolve_data_dir(tmp_path, frozen=True, env={})
        assert result == target

    def test_legacy_db_triggers_migration(self, tmp_path, monkeypatch):
        appdata = _frozen_env(tmp_path, monkeypatch)
        legacy = tmp_path / "data"
        legacy.mkdir()
        (legacy / "library.db").write_bytes(b"sqlite")
        result = config._resolve_data_dir(tmp_path, frozen=True, env={})
        assert result == appdata / "Soundloom"
        assert (appdata / "Soundloom" / "library.db").read_bytes() == b"sqlite"
        # Original preserved, never moved.
        assert (legacy / "library.db").exists()

    def test_migration_failure_falls_back_to_legacy(self, tmp_path, monkeypatch):
        _frozen_env(tmp_path, monkeypatch)
        legacy = tmp_path / "data"
        legacy.mkdir()
        (legacy / "library.db").write_bytes(b"sqlite")
        # Make the copy fail: legacy contains an unreadable subdirectory.
        (legacy / "covers").mkdir()
        (legacy / "covers" / "locked").mkdir()
        monkeypatch.setattr(
            config.shutil, "copytree",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("disk full")),
        )
        result = config._resolve_data_dir(tmp_path, frozen=True, env={})
        assert result == legacy
        # No half-migrated target left behind.
        assert not (tmp_path / "LocalAppData" / "Soundloom").exists()

    def test_fresh_install_uses_appdata(self, tmp_path, monkeypatch):
        appdata = _frozen_env(tmp_path, monkeypatch)
        result = config._resolve_data_dir(tmp_path, frozen=True, env={})
        assert result == appdata / "Soundloom"


class TestMigrateLegacyData:
    def test_copies_tree_and_excludes_logs_and_pycache(self, tmp_path):
        legacy = tmp_path / "data"
        (legacy / "covers").mkdir(parents=True)
        (legacy / "logs").mkdir()
        (legacy / "__pycache__").mkdir()
        (legacy / "library.db").write_bytes(b"db")
        (legacy / "settings.json").write_text("{}", encoding="utf-8")
        (legacy / "covers" / "track_1.jpg").write_bytes(b"jpg")
        (legacy / "logs" / "app.log").write_text("log", encoding="utf-8")
        (legacy / "__pycache__" / "x.pyc").write_bytes(b"pyc")

        target = tmp_path / "newhome" / "Soundloom"
        assert config._migrate_legacy_data(legacy, target) is True

        assert (target / "library.db").read_bytes() == b"db"
        assert (target / "settings.json").exists()
        assert (target / "covers" / "track_1.jpg").read_bytes() == b"jpg"
        assert not (target / "logs").exists()
        assert not (target / "__pycache__").exists()
        # Source untouched.
        assert (legacy / "logs" / "app.log").exists()

    def test_failure_cleans_partial_target(self, tmp_path, monkeypatch):
        legacy = tmp_path / "data"
        legacy.mkdir()
        (legacy / "library.db").write_bytes(b"db")

        real_copytree = config.shutil.copytree

        def partial_copy(src, dst, **kwargs):
            real_copytree(src, dst, **kwargs)
            raise OSError("interrupted")

        monkeypatch.setattr(config.shutil, "copytree", partial_copy)
        target = tmp_path / "newhome" / "Soundloom"
        assert config._migrate_legacy_data(legacy, target) is False
        assert not target.exists()


class TestResolveFrontendDir:
    def test_loose_frontend_shadows_bundle(self, tmp_path):
        root = tmp_path / "install"
        loose = root / "frontend"
        loose.mkdir(parents=True)
        (loose / "index.html").touch()
        bundle_root = tmp_path / "_internal"
        assert config._resolve_frontend_dir(root, bundle_root) == loose

    def test_falls_back_to_bundle(self, tmp_path):
        root = tmp_path / "install"
        root.mkdir()
        bundle_root = tmp_path / "_internal"
        assert config._resolve_frontend_dir(root, bundle_root) == bundle_root / "frontend"

    def test_incomplete_loose_frontend_is_ignored(self, tmp_path):
        root = tmp_path / "install"
        (root / "frontend").mkdir(parents=True)  # exists but no index.html
        bundle_root = tmp_path / "_internal"
        assert config._resolve_frontend_dir(root, bundle_root) == bundle_root / "frontend"


class TestModuleConstants:
    """The module-level constants must derive from the resolution rules."""

    def test_dev_data_dir_sits_in_repo(self):
        # Not frozen during tests: data dir is the repo checkout's data/.
        assert not getattr(sys, "frozen", False)
        assert config.DATA_DIR == config.ROOT / "data"
        assert config.DB_PATH == config.DATA_DIR / "library.db"
        assert config.SETTINGS_FILE == config.DATA_DIR / "settings.json"

    def test_frontend_dir_is_the_repo_frontend_in_dev(self):
        assert config.FRONTEND_DIR == config.ROOT / "frontend"
