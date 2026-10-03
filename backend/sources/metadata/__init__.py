"""Metadata fetching registry.

Construct a fetcher with :func:`get_fetcher`, or ask :func:`describe_methods`
what is currently usable given the saved settings. :func:`pick_fetcher` picks
the best available backend automatically, which is what the scan uses.
"""
from __future__ import annotations

from .base import API_DELAY, DEFAULT_TIMEOUT, FetchResult, MetadataFetcher
from .acoustid import AcoustIDFetcher
from .discogs import DiscogsFetcher
from .local_only import LocalOnlyFetcher
from .musicbrainz import MusicBrainzFetcher
from .spotify import SpotifyFetcher

#: Order matters - this is the fallback order used by pick_fetcher.
#: AcoustID leads because it reads the audio and so survives bad tags;
#: MusicBrainz is first among tag-based ones because it needs no key.
_REGISTRY: dict[str, type[MetadataFetcher]] = {
    "acoustid": AcoustIDFetcher,
    "musicbrainz": MusicBrainzFetcher,
    "spotify": SpotifyFetcher,
    "discogs": DiscogsFetcher,
    "local_only": LocalOnlyFetcher,
}


def available_methods() -> list[str]:
    """Every registered backend name, in fallback order."""
    return list(_REGISTRY)


def get_fetcher(name: str, settings: dict) -> MetadataFetcher:
    """Build a fetcher by name. Raises ValueError for an unknown name.

    The result may still report available() == False if credentials or a
    system dependency are missing; check that before calling fetch().
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"unknown fetch method: {name!r} (choices: {', '.join(available_methods())})"
        )
    return cls(settings)


def describe_methods(settings: dict) -> list[dict]:
    """Report every backend with its availability, for the settings UI."""
    out = []
    for name in _REGISTRY:
        fetcher = get_fetcher(name, settings)
        out.append({
            "name": name,
            "description": fetcher.description,
            "available": fetcher.available(),
            "reason": "" if fetcher.available() else fetcher.unavailable_reason(),
        })
    return out


def pick_fetcher(settings: dict, preferred: str = "") -> MetadataFetcher:
    """Best currently-usable backend.

    Honours `preferred` when it is configured and available, otherwise walks
    the fallback order. Falls back to the local_only stub rather than
    returning None, so callers never have to null-check.
    """
    if preferred:
        fetcher = get_fetcher(preferred, settings)
        if fetcher.available():
            return fetcher
    for name in _REGISTRY:
        fetcher = get_fetcher(name, settings)
        if fetcher.available():
            return fetcher
    return LocalOnlyFetcher(settings)


__all__ = [
    "FetchResult",
    "MetadataFetcher",
    "AcoustIDFetcher",
    "MusicBrainzFetcher",
    "SpotifyFetcher",
    "DiscogsFetcher",
    "LocalOnlyFetcher",
    "available_methods",
    "get_fetcher",
    "describe_methods",
    "pick_fetcher",
]
