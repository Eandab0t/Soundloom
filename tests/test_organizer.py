"""Tests for services.organizer — template rendering, file organization."""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.organizer import render_template, _sanitize, organize_file, preview_path


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestRenderTemplate:
    def test_basic(self):
        r = render_template("{artist}/{album}/{title}.mp3", {
            "artist": "Artist", "album": "Album", "title": "Song",
        })
        assert r == "Artist/Album/Song.mp3"

    def test_sanitizes_chars(self):
        r = render_template("{title}.mp3", {"title": 'My <Song> "Title"'})
        assert "<" not in r
        assert ">" not in r
        assert '"' not in r

    def test_unknown_fallback(self):
        r = render_template("{artist}/{title}.mp3", {"title": "Song"})
        assert "Unknown" in r

    def test_strips_trailing_dots(self):
        r = render_template("{title}.mp3", {"title": "Song..."})
        assert r.endswith("Song.mp3")


class TestSanitize:
    def test_removes_slash(self):
        assert "/" not in _sanitize("a/b")

    def test_removes_colon(self):
        assert ":" not in _sanitize("a:b")

    def test_removes_quotes(self):
        assert '"' not in _sanitize('a"b')

    def test_strips_dots_spaces(self):
        assert _sanitize("  file.mp3  ") == "file.mp3"

    def test_truncates_long(self):
        assert len(_sanitize("x" * 300)) <= 200

    def test_empty(self):
        assert _sanitize("") == ""
        assert _sanitize(None) == ""


class TestOrganizeFile:
    def test_moves_file(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "source.mp3"
            src.write_text("fake audio")
            lib = Path(td) / "library"
            lib.mkdir()

            result = _run(organize_file(
                str(src), {"artist": "A", "title": "T"}, str(lib),
                "{artist}/{title}.mp3",
            ))
            assert Path(result).exists()
            assert "A" in result
            assert "T" in result

    def test_duplicate_keep_separate(self):
        with tempfile.TemporaryDirectory() as td:
            src1 = Path(td) / "a.mp3"
            src1.write_bytes(b"audio1")
            src2 = Path(td) / "b.mp3"
            src2.write_bytes(b"audio2")
            lib = Path(td) / "lib"
            lib.mkdir()

            _run(organize_file(
                str(src1), {"title": "T"}, str(lib), "{title}.mp3",
            ))
            result = _run(organize_file(
                str(src2), {"title": "T"}, str(lib), "{title}.mp3",
            ))
            assert "(1)" in result

    def test_duplicate_skip(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "a.mp3"
            src.write_bytes(b"audio")
            lib = Path(td) / "lib"
            lib.mkdir()

            result1 = _run(organize_file(
                str(src), {"title": "T"}, str(lib), "{title}.mp3",
            ))
            src2 = Path(td) / "b.mp3"
            src2.write_bytes(b"audio2")
            result2 = _run(organize_file(
                str(src2), {"title": "T"}, str(lib), "{title}.mp3",
                duplicate_policy="skip",
            ))
            assert result1 == result2

    def test_source_not_found(self):
        try:
            _run(organize_file(
                "/nonexistent/file.mp3", {"title": "T"}, "/tmp", "{title}.mp3",
            ))
            assert False, "Should have raised"
        except FileNotFoundError:
            pass


class TestPreviewPath:
    def test_preview(self):
        p = preview_path({"artist": "A", "title": "T"}, "/music", "{artist}/{title}.mp3")
        assert "A" in p
        assert "T.mp3" in p
