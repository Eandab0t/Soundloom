"""Tests for pipeline.matcher — weighted scoring, confidence levels."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.pipeline.models import TrackMetadata, SourceCandidate, SourceType
from backend.pipeline.matcher import (
    score_candidate, rank_candidates, confidence_level,
    _strip_artist_from_title, has_comparable_intent,
)


class TestConfidenceLevel:
    def test_auto_accept(self):
        assert confidence_level(95) == "auto_accept"
        assert confidence_level(100) == "auto_accept"

    def test_accept_review(self):
        assert confidence_level(85) == "accept_review"
        assert confidence_level(94) == "accept_review"

    def test_ask_user(self):
        assert confidence_level(70) == "ask_user"
        assert confidence_level(84) == "ask_user"

    def test_reject(self):
        assert confidence_level(69) == "reject"
        assert confidence_level(0) == "reject"


class TestScoreCandidate:
    def test_perfect_match(self):
        desired = TrackMetadata(title="Song", artist="Artist", album="Album", duration=200)
        cand = SourceCandidate(url="yt", title="Song", artist="Artist", album="Album", duration=200)
        result = score_candidate(desired, cand)
        assert result.confidence >= 95

    def test_artist_stripped_from_title(self):
        desired = TrackMetadata(title="Song", artist="Artist", duration=200)
        cand = SourceCandidate(url="yt", title="Artist - Song (Official Video)", artist="Artist", duration=200)
        result = score_candidate(desired, cand)
        assert result.confidence >= 80

    def test_duration_mismatch_penalizes(self):
        desired = TrackMetadata(title="Song", artist="Artist", duration=200)
        cand = SourceCandidate(url="yt", title="Song", artist="Artist", duration=300)
        result = score_candidate(desired, cand)
        assert result.breakdown["duration"] < 1.0

    def test_wrong_artist_penalizes(self):
        desired = TrackMetadata(title="Song", artist="Artist A")
        cand = SourceCandidate(url="yt", title="Song", artist="Artist B")
        result = score_candidate(desired, cand)
        assert result.breakdown["artist"] < 0.95

    def test_variant_penalty(self):
        desired = TrackMetadata(title="Song", artist="Artist", duration=200)
        cand = SourceCandidate(url="yt", title="Song (Live)", artist="Artist", duration=200)
        result = score_candidate(desired, cand)
        assert any("variant" in w.lower() for w in result.warnings)

    def test_featured_artist_bonus(self):
        desired = TrackMetadata(title="Song", artist="A", featured_artists=["B"])
        cand = SourceCandidate(url="yt", title="Song", artist="A feat. B", duration=200)
        result = score_candidate(desired, cand)
        assert result.breakdown["artist"] >= 0.9

    def test_empty_candidate(self):
        desired = TrackMetadata(title="Song", artist="Artist")
        cand = SourceCandidate(url="")
        result = score_candidate(desired, cand)
        assert result.confidence < 50

    def test_explanation_strings(self):
        desired = TrackMetadata(title="Song", artist="A")
        cand = SourceCandidate(url="yt", title="Song", artist="A", duration=200)
        result = score_candidate(desired, cand)
        assert isinstance(result.explanation, str)
        assert len(result.explanation) > 0


class TestStripArtistFromTitle:
    def test_strips_prefix(self):
        r = _strip_artist_from_title("Artist - Song", "Artist")
        assert r == "song"

    def test_no_strip_needed(self):
        r = _strip_artist_from_title("Song", "Artist")
        assert "song" in r

    def test_case_insensitive(self):
        r = _strip_artist_from_title("ARTIST - Song", "artist")
        assert r == "song"


class TestRankCandidates:
    def test_orders_by_score(self):
        desired = TrackMetadata(title="Song", artist="A")
        c1 = SourceCandidate(url="1", title="Song", artist="A")
        c2 = SourceCandidate(url="2", title="Wrong", artist="B")
        ranked = rank_candidates(desired, [c2, c1])
        assert ranked[0].confidence >= ranked[1].confidence

    def test_empty(self):
        desired = TrackMetadata(title="Song")
        assert rank_candidates(desired, []) == []


class TestMissingEvidenceIsNeutral:
    """A field nobody stated is unknown, not wrong."""

    def test_source_without_an_album_is_not_a_wrong_album(self):
        desired = TrackMetadata(title="Lovefool", artist="The Cardigans",
                                album="Gran Turismo")
        cand = SourceCandidate(url="yt", title="Lovefool", artist="The Cardigans")
        result = score_candidate(desired, cand)
        assert result.breakdown["album"] == 0.5

    def test_a_genuinely_wrong_album_scores_below_neutral(self):
        desired = TrackMetadata(title="Lovefool", artist="The Cardigans",
                                album="Gran Turismo")
        cand = SourceCandidate(url="yt", title="Lovefool", artist="The Cardigans",
                              album="With My Friends")
        result = score_candidate(desired, cand)
        assert result.breakdown["album"] < 0.5

    def test_an_unstated_request_field_does_not_penalise_the_source(self):
        """A playlist job asks for title+artist; the source adding an album
        must not make a correct match look worse."""
        desired = TrackMetadata(title="Lovefool", artist="The Cardigans")
        cand = SourceCandidate(url="yt", title="Lovefool", artist="The Cardigans",
                              album="Gran Turismo")
        result = score_candidate(desired, cand)
        assert result.breakdown["album"] == 0.5
        assert result.confidence >= 80

    def test_an_identity_less_candidate_is_a_hard_zero(self):
        """It said nothing, so it cannot ride the neutral floor."""
        result = score_candidate(
            TrackMetadata(title="Lovefool", artist="The Cardigans"),
            SourceCandidate(url=""),
        )
        assert result.confidence == 0.0
        assert result.breakdown == {}

    def test_source_album_artist_is_preferred_over_reparsing_the_artist(self):
        desired = TrackMetadata(title="Song", artist="Artist",
                                album_artist="Various Artists")
        cand = SourceCandidate(
            url="yt", title="Song", artist="Artist",
            metadata={"album_artist": "Various Artists"})
        assert score_candidate(desired, cand).breakdown["album_artist"] == 1.0


class TestIntentAgainstSource:
    """The bug this file has to prevent.

    The downloader used to build the candidate from the resolved metadata and
    then score that metadata against itself, so every field matched itself and
    the confidence was a constant 80.0 whatever the source was. A constant
    above the default threshold means match_threshold can never reject
    anything - the setting is decorative.
    """

    WANTED = TrackMetadata(title="Lovefool", artist="The Cardigans")

    def _source(self, title, artist, **kw):
        return SourceCandidate(url="yt", title=title, artist=artist, **kw)

    def test_score_varies_with_the_source(self):
        right = score_candidate(self.WANTED, self._source(
            "Lovefool", "The Cardigans", album="Gran Turismo")).confidence
        wrong_track = score_candidate(self.WANTED, self._source(
            "Hounds of Love", "The Cardigans")).confidence
        wrong_artist = score_candidate(self.WANTED, self._source(
            "Lovefool", "Carly Simon")).confidence

        assert len({right, wrong_track, wrong_artist}) == 3, \
            "confidence is constant again - intent and source are the same data"

    def test_the_correct_track_clears_the_default_threshold(self):
        result = score_candidate(self.WANTED, self._source(
            "The Cardigans - Lovefool (Official Video)", "The Cardigans",
            album="Gran Turismo"))
        assert result.confidence >= 70, result.explanation

    def test_the_wrong_track_falls_below_the_default_threshold(self):
        result = score_candidate(self.WANTED, self._source(
            "Creep", "Radiohead", album="Pablo Honey"))
        assert result.confidence < 70, result.explanation

    def test_a_variant_is_rejected_when_a_variant_was_not_asked_for(self):
        """A live take is a different recording, not the track that was asked for."""
        result = score_candidate(self.WANTED, self._source(
            "Lovefool (Live)", "The Cardigans"))
        assert result.confidence < 70
        assert any("variant" in w.lower() for w in result.warnings)

    def test_a_reissue_of_the_same_recording_is_accepted(self):
        result = score_candidate(self.WANTED, self._source(
            "Lovefool (Remastered 2003)", "The Cardigans"))
        assert result.confidence >= 70, result.explanation


class TestHasComparableIntent:
    def test_a_stated_track_is_comparable(self):
        assert has_comparable_intent(TrackMetadata(title="Lovefool")) is True
        assert has_comparable_intent(TrackMetadata(artist="Cardigans")) is True
        assert has_comparable_intent(TrackMetadata(album="Gran Turismo")) is True

    def test_a_bare_url_request_is_not(self):
        """Scoring against a blank request lands on the neutral floor and says
        nothing about the source, so callers must be able to detect it."""
        assert has_comparable_intent(TrackMetadata()) is False
        assert has_comparable_intent(TrackMetadata(title="  ", artist="")) is False


class TestStripArtistFromTitleWithLeadingArticle:
    def test_strips_prefix_when_the_artist_has_a_leading_article(self):
        # normalize_artist drops 'The' but normalize_title does not, so a
        # plain startswith test misses every such artist.
        assert _strip_artist_from_title(
            "The Cardigans - Lovefool (Official Video)", "The Cardigans") == "lovefool"

    def test_still_strips_without_an_article(self):
        assert _strip_artist_from_title("Coldplay - Clocks", "Coldplay") == "clocks"

    def test_does_not_strip_a_word_that_only_looks_like_the_artist(self):
        r = _strip_artist_from_title("Cardigans (Live)", "The Cardigans")
        assert r == "cardigans"
