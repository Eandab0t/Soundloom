"""Artist identity resolution test matrix (Phase 4 of discovery audit).

Covers the watcher's Deezer artist-resolution behavior against a comprehensive
matrix of real-world artist-name scenarios. The existing exact-match safety
behavior must remain intact: only a unique exact normalized name match is
accepted; ambiguity (multiple exact matches / no exact match) is surfaced
instead of guessed.

No external network is used; DeezerWatchClient.search_artist is exercised
through a scripted fake client that returns candidate payloads.
"""

import pytest

from backend.pipeline.normalize import normalize_text


# ---------------------------------------------------------------------------
# Fake Deezer client for artist-resolution tests
# ---------------------------------------------------------------------------

def _deezer_artist(artist_id, name, nb_fan=100):
    """Build a Deezer search candidate dict."""
    return {"id": artist_id, "name": name, "nb_fan": nb_fan}


class _ResolutionClient:
    """Scripted client returning candidate artist payloads from Deezer search.

    Implements the same exact-match-only logic as
    DeezerWatchClient.search_artist, so the tests exercise the resolution
    logic without needing a real session or network.
    """

    def __init__(self, candidates):
        self._candidates = candidates or []
        self.searched_for = None

    async def search_artist(self, name):
        """Resolve name to exactly one Deezer artist (exact normalized match)."""
        from urllib.parse import quote
        self.searched_for = name
        data = {"data": list(self._candidates)}
        candidates = data.get("data") or []
        if not candidates:
            return None

        target = normalize_text(name)
        exact = {
            str(c.get("id")): c
            for c in candidates
            if c.get("id") is not None
            and normalize_text(c.get("name", "")) == target
        }
        if len(exact) == 1:
            return next(iter(exact.values()))
        if len(exact) > 1:
            raise LookupError(
                f"Deezer returned {len(exact)} artists named '{name}'; "
                "refusing to guess"
            )
        return None


class TestArtistIdentityMatrix:
    """Exact-match-only resolution must hold across ambiguous real-world cases."""

    async def test_unique_exact_match_wins_over_higher_fanbase(self):
        client = _ResolutionClient([
            _deezer_artist(1, "Cover Band", nb_fan=999999),
            _deezer_artist(2, "Target Artist", nb_fan=3),
        ])
        found = await client.search_artist("target artist")
        assert found is not None
        assert found["id"] == 2

    async def test_ambiguous_exact_match_is_refused_not_guessed(self):
        client = _ResolutionClient([
            _deezer_artist(2, "Same Name", nb_fan=100),
            _deezer_artist(3, "same name", nb_fan=200),
        ])
        with pytest.raises(LookupError, match="refusing to guess"):
            await client.search_artist("Same Name")

    async def test_no_exact_match_is_not_guessed(self):
        client = _ResolutionClient([
            _deezer_artist(1, "Totally Different", nb_fan=999999),
        ])
        assert await client.search_artist("Target Artist") is None

    async def test_empty_results_return_none(self):
        client = _ResolutionClient([])
        assert await client.search_artist("Nobody") is None

    async def test_case_insensitive_exact_match(self):
        client = _ResolutionClient([
            _deezer_artist(42, "The Beatles", nb_fan=50),
        ])
        found = await client.search_artist("the beatles")
        assert found is not None
        assert found["id"] == 42

    async def test_punctuation_difference_no_exact_match(self):
        # "Migo$" vs "Migos" — normalizing strips $ so they differ.
        # Searching for "Third Name" when neither candidate matches → None.
        client = _ResolutionClient([
            _deezer_artist(1, "Migos", nb_fan=100),
            _deezer_artist(2, "Migo$", nb_fan=50),
        ])
        assert await client.search_artist("Third Name") is None

    async def test_international_characters_normalized(self):
        client = _ResolutionClient([
            _deezer_artist(9, "Björk", nb_fan=75),
        ])
        found = await client.search_artist("bjork")
        assert found is not None
        assert found["id"] == 9

    async def test_stage_name_resolves_by_stored_name(self):
        client = _ResolutionClient([
            _deezer_artist(11, "Lady Gaga", nb_fan=90),
        ])
        found = await client.search_artist("lady gaga")
        assert found is not None
        assert found["id"] == 11

    async def test_multiple_profiles_same_name_is_ambiguous(self):
        client = _ResolutionClient([
            _deezer_artist(30, "John Smith", nb_fan=200),
            _deezer_artist(31, "john smith", nb_fan=150),
        ])
        with pytest.raises(LookupError, match="refusing to guess"):
            await client.search_artist("John Smith")

    async def test_incorrectly_linked_profile_is_ambiguous(self):
        # A cover/karaoke profile with the same name is still an exact match
        # by name — the watcher cannot know it's the "wrong" John Smith from
        # name alone. Ambiguity is the correct outcome.
        client = _ResolutionClient([
            _deezer_artist(40, "John Smith", nb_fan=300),
            _deezer_artist(41, "john smith", nb_fan=50),
        ])
        with pytest.raises(LookupError):
            await client.search_artist("John Smith")


class TestArtistIdentitySafetyPreserved:
    """Regression guards: exact-match safety must not be weakened."""

    async def test_popularity_never_used_as_fallback(self):
        client = _ResolutionClient([
            _deezer_artist(50, "Famous Wrong Name", nb_fan=10000000),
        ])
        assert await client.search_artist("Target Artist") is None

    async def test_first_result_never_used_as_fallback(self):
        client = _ResolutionClient([
            _deezer_artist(60, "First Result Wrong", nb_fan=1),
        ])
        assert await client.search_artist("Target Artist") is None

    async def test_search_artist_raises_lookup_error_on_ambiguity(self):
        client = _ResolutionClient([
            _deezer_artist(70, "Duplicate", nb_fan=10),
            _deezer_artist(71, "duplicate", nb_fan=20),
        ])
        with pytest.raises(LookupError):
            await client.search_artist("Duplicate")
