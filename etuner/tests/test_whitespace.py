"""Tests for whitespace/formatting cleanup rule."""
import pytest
from etuner.cleanup.whitespace import _normalize


@pytest.mark.parametrize("inp, expected", [
    (" Drake ", "Drake"),
    ("Drake  -  Future", "Drake - Future"),
    ("Title ft. SZA", "Title feat. SZA"),
    ("Title featuring SZA", "Title feat. SZA"),
    ("Title ft SZA", "Title feat. SZA"),
    ("Title feat. SZA", "Title feat. SZA"),
    ("Title...", "Title."),
    ("Title\u2014Subtitle", "Title-Subtitle"),
    ("Artist\u00a0Name", "Artist Name"),
    ("  leading trailing  ", "leading trailing"),
    ("Song Pt. 2 ft. SZA", "Song Pt. 2 feat. SZA"),
    ("A.B.C. feat. SZA", "A.B.C. feat. SZA"),
])
def test_normalize(inp, expected):
    assert _normalize(inp) == expected
