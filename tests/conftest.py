"""Shared pytest fixtures for Soundloom tests."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


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
