"""Shared pytest fixtures for Soundloom tests."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolate_db_backups(tmp_path, monkeypatch):
    """Keep pre-migration snapshots out of the real data directory.

    Any test whose database is fresh has every migration pending, so
    run_migrations() takes a snapshot first. Left alone it writes into the
    user's real data/backups/db, where the keep-5 prune can evict genuine
    pre-migration copies. Redirect it for the duration of every test.
    """
    from backend.services import backup as backup_svc
    dest = tmp_path / "db_backups"
    dest.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_svc, "DB_BACKUP_DIR", dest)


@pytest.fixture
def tmp_library(tmp_path):
    """Create a temporary library directory with a few dummy files."""
    lib = tmp_path / "library"
    lib.mkdir()
    for name in ["song1.mp3", "song2.flac", "song3.m4a"]:
        (lib / name).write_bytes(b"\x00" * 1024)
    return lib


@pytest.fixture
def tmp_download(tmp_path):
    """Create a temporary download staging directory."""
    dl = tmp_path / "downloads"
    dl.mkdir()
    return dl


@pytest.fixture
def sample_metadata():
    """Return a TrackMetadata-like dict for testing."""
    return {
        "title": "Test Song",
        "artist": "Test Artist",
        "album": "Test Album",
        "album_artist": "Test Artist",
        "year": 2024,
        "genre": "Rock",
        "track_number": 1,
        "disc_number": 1,
        "format": "mp3",
    }


@pytest.fixture
def db_in_memory(tmp_path, monkeypatch):
    """Patch config.DB_PATH to use an in-memory SQLite database."""
    db_path = tmp_path / "test.db"
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    return db_path
