"""Tests for backend.validation — path traversal, template validation, sanitization."""
import pytest
from pathlib import Path
from backend.validation import (
    validate_path, validate_folder_template, sanitize_filename, validate_url,
)
from backend.errors import ValidationError, PathTraversalError


class TestValidatePath:
    def test_valid_relative_path(self, tmp_path):
        result = validate_path("music/song.mp3", tmp_path)
        assert result == tmp_path / "music" / "song.mp3"

    def test_traversal_rejected(self, tmp_path):
        with pytest.raises(PathTraversalError):
            validate_path("../../../etc/passwd", tmp_path)

    def test_deep_traversal_rejected(self, tmp_path):
        with pytest.raises(PathTraversalError):
            validate_path("a/../../../b", tmp_path)

    def test_absolute_path_within_base(self, tmp_path):
        result = validate_path(str(tmp_path / "file.txt"), tmp_path)
        assert result == tmp_path / "file.txt"


class TestValidateFolderTemplate:
    def test_valid_template(self):
        result = validate_folder_template("{album_artist}\\{album}\\{title}")
        assert result == "{album_artist}\\{album}\\{title}"

    def test_all_valid_vars(self):
        validate_folder_template("{title} {artist} {primary_artist} {album} {year} {format}")

    def test_unknown_var_rejected(self):
        with pytest.raises(ValidationError, match="Unknown template"):
            validate_folder_template("{evil_var}")

    def test_dot_dot_rejected(self):
        with pytest.raises(ValidationError):
            validate_folder_template("../{title}")

    def test_empty_template(self):
        result = validate_folder_template("")
        assert result == ""


class TestSanitizeFilename:
    def test_removes_invalid_chars(self):
        result = sanitize_filename('file<>:"/\\|?*.txt')
        assert '<' not in result
        assert '>' not in result
        assert ':' not in result
        assert '"' not in result

    def test_strips_dots_and_spaces(self):
        result = sanitize_filename("  ...file...  ")
        assert result == "file"

    def test_collapses_whitespace(self):
        result = sanitize_filename("my   song   name")
        assert result == "my song name"

    def test_empty(self):
        assert sanitize_filename("") == ""
        assert sanitize_filename(None) == ""

    def test_truncation(self):
        long_name = "a" * 300
        result = sanitize_filename(long_name, max_length=50)
        assert len(result) == 50


class TestValidateUrl:
    def test_valid_url(self):
        assert validate_url("https://www.youtube.com/watch?v=abc") == "https://www.youtube.com/watch?v=abc"

    def test_strips_whitespace(self):
        assert validate_url("  https://x.com  ") == "https://x.com"

    def test_empty_raises(self):
        with pytest.raises(ValidationError):
            validate_url("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValidationError):
            validate_url("   ")

    def test_too_long(self):
        with pytest.raises(ValidationError, match="too long"):
            validate_url("https://x.com/" + "a" * 2100)

    def test_null_byte_rejected(self):
        with pytest.raises(ValidationError):
            validate_url("https://x.com/\x00bad")
