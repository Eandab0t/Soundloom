"""Tests for duplicate-removal rule."""
import pytest
from etuner.cleanup.duplicates import _dedupe

MIN = 3

@pytest.mark.parametrize("inp, expected", [
    ("Foo Bar - Foo Bar", "Foo Bar"),
    ("Artist - Artist - Track", "Artist - Track"),
    ("Drake - Drake - Nice For What", "Drake - Nice For What"),
    # should NOT collapse these:
    ("Yeah Yeah Yeahs", "Yeah Yeah Yeahs"),
    ("Na Na Na", "Na Na Na"),
    ("La La Land", "La La Land"),
])
def test_dedupe(inp, expected):
    assert _dedupe(inp, MIN) == expected
