"""Tests for backend.validation — path traversal, template validation, sanitization."""
import os
import pytest
from pathlib import Path
from backend.validation import (
    validate_path, validate_folder_template, sanitize_filename, validate_url,
    validate_absolute_path,
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


class TestValidateAbsolutePath:
    def test_windows_drive_absolute_passes(self):
        result = validate_absolute_path(r"C:\Users\Ean\Music")
        assert result == r"C:\Users\Ean\Music"

    def test_windows_unc_passes(self):
        result = validate_absolute_path(r"\\server\share\music")
        assert result == r"\\server\share\music"

    def test_drive_relative_is_rejected(self):
        # C:relative is NOT absolute on Windows - it resolves against the
        # CWD on the C drive. The old check let it through because of the :.
        with pytest.raises(ValidationError, match="must be an absolute path"):
            validate_absolute_path(r"C:relative\music")

    def test_relative_path_is_rejected(self):
        with pytest.raises(ValidationError, match="must be an absolute path"):
            validate_absolute_path("relative/dir/music")

    def test_just_drive_letter_is_rejected(self):
        with pytest.raises(ValidationError, match="must be an absolute path"):
            validate_absolute_path("C:")

    def test_empty_is_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            validate_absolute_path("")

    def test_null_byte_is_rejected(self):
        with pytest.raises(ValidationError, match="null bytes"):
            validate_absolute_path("C:\\music\\x\x00bad")

    def test_platform_truth(self):
        """The validator must agree with os.path.isabs on this platform."""
        samples = [
            r"C:\Music", r"\\server\share", r"C:relative",
            "relative/dir", "", "C:\\", "/absolute/posix/path",
        ]
        for s in samples:
            expected = bool(os.path.isabs(s)) if s else False
            if expected:
                validate_absolute_path(s)  # must not raise
            else:
                with pytest.raises(ValidationError):
                    validate_absolute_path(s)


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
