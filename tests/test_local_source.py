"""Local-filesystem acquisitions.

`SourceType.LOCAL` was in the enum and the pipeline already branched on it
- the downloader skips the artwork fetch for a local source - but no code
produced it. `detect_source()` matches only remote URL patterns and returns
"unknown" for anything else, so a filesystem path fell through to
YtdlpResolver and yt-dlp failed on extraction rather than on anything the
user could act on.

These pin the two properties that matter for an acquisition run outside the
library: the fetch is a copy rather than a move (the staging tree is deleted
when the job ends, so a move would take the user's own file with it), and
detection is strict enough that a URL can never be handed to the copier.
"""
import shutil

import pytest

from backend.pipeline.models import SourceCandidate, SourceType
from backend.sources import local_source
from backend.sources.local_source import (
    LocalDownloader, LocalResolver, detect_local, is_local_path)


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "take.mp3"
    p.write_bytes(b"\x00" * 4096)
    return p


class TestDetection:
    def test_a_real_audio_file_is_local(self, audio):
        assert detect_local(str(audio)) == str(audio)
        assert is_local_path(str(audio))

    def test_a_url_is_never_local(self):
        """The copier would be handed a string, not a file."""
        for url in ("https://www.deezer.com/track/1",
                    "http://example.com/x.mp3",
                    "ftp://example.com/x.mp3"):
            assert detect_local(url) is None, url
            assert not is_local_path(url)

    def test_a_missing_path_is_not_local(self, tmp_path):
        assert detect_local(str(tmp_path / "nope.mp3")) is None

    def test_a_non_audio_file_is_not_a_track(self, tmp_path):
        """Handing a cover image to ffmpeg is not a useful outcome."""
        p = tmp_path / "cover.jpg"
        p.write_bytes(b"\xff\xd8\xff")
        assert detect_local(str(p)) is None

    def test_empty_and_blank_input(self):
        assert detect_local("") is None
        assert detect_local("   ") is None
        assert detect_local(None) is None

    def test_surrounding_whitespace_is_tolerated(self, audio):
        assert detect_local(f"  {audio}  ") == str(audio)

    def test_a_drive_letter_alone_is_not_a_scheme(self, tmp_path):
        """`E:` alone is a drive, and a one-character scheme never exists."""
        assert detect_local("E:") is None


class TestDownload:
    async def test_the_source_survives_the_fetch(self, audio, tmp_path):
        """_process_job deletes the whole staging tree in its finally block.
        A move would delete the user's file along with the job."""
        cand = SourceCandidate(url=str(audio), source_type=SourceType.LOCAL,
                               title="t", artist="a")
        out = tmp_path / "staging" / "dl_abc"
        out.parent.mkdir(parents=True)

        written, info = await LocalDownloader().download(cand, str(out))

        assert audio.is_file(), "the source file was moved, not copied"
        assert audio.read_bytes() == b"\x00" * 4096
        assert written == str(out) + ".mp3"
        assert open(written, "rb").read() == b"\x00" * 4096
        assert info is None, "no provider payload exists for a local copy"

    async def test_a_vanished_source_is_an_explicit_error(self, tmp_path):
        cand = SourceCandidate(url=str(tmp_path / "gone.mp3"),
                               source_type=SourceType.LOCAL,
                               title="t", artist="a")
        with pytest.raises(FileNotFoundError):
            await LocalDownloader().download(cand, str(tmp_path / "out"))

    async def test_progress_reaches_the_terminal_value(self, audio, tmp_path):
        seen = []
        cand = SourceCandidate(url=str(audio), source_type=SourceType.LOCAL,
                               title="t", artist="a")
        await LocalDownloader().download(cand, str(tmp_path / "dl"),
                                         progress_callback=seen.append)
        assert seen == [100], "a copy is not streamed; it is simply done"

    async def test_artwork_is_refused_rather_than_faked(self, audio, tmp_path):
        cand = SourceCandidate(url=str(audio), source_type=SourceType.LOCAL,
                               title="t", artist="a")
        with pytest.raises(NotImplementedError):
            await LocalDownloader().download_thumbnail(cand, str(tmp_path / "t"))


class TestResolve:
    async def test_an_untagged_file_still_resolves(self, audio):
        """Refusing it would make untagged local audio unusable rather than
        merely untidy."""
        meta = await LocalResolver().resolve(str(audio))
        assert meta.title == "take", "the filename stands in for a title"

    async def test_a_non_local_input_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            await LocalResolver().resolve(str(tmp_path / "nope.mp3"))

    def test_can_handle_agrees_with_detect(self, audio, tmp_path):
        r = LocalResolver()
        assert r.can_handle(str(audio))
        assert not r.can_handle(str(tmp_path / "nope.mp3"))
        assert not r.can_handle("https://example.com/x.mp3")


class TestRouting:
    def test_the_pipeline_knows_local_is_a_source_type(self):
        assert SourceType("local") is SourceType.LOCAL

    def test_a_local_job_would_skip_the_artwork_fetch(self):
        """The branch that made SourceType.LOCAL worth having."""
        cand = SourceCandidate(url=str(local_source.__file__),
                               source_type=SourceType.LOCAL)
        assert cand.source_type is not SourceType.YOUTUBE
