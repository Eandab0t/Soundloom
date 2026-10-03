"""Contract for metadata fetchers.

A fetcher looks a track up on an external service and returns candidate
metadata. Every implementation is async and uses aiohttp - the original
E-Tuner versions were synchronous `requests` calls, which cannot be used
directly from the FastAPI event loop.

Registered backends:

    local_only    no-op stub, safe default, never touches the network
    musicbrainz   free, no API key, ~1 req/s etiquette
    acoustid      audio fingerprinting; needs a key + local fpcalc binary
    discogs       release-level metadata; needs a free personal token
    spotify       needs a free client id/secret (Client Credentials flow)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ...pipeline.models import TrackMetadata

# Politeness delay between outbound calls to the same service.
API_DELAY = 0.5
DEFAULT_TIMEOUT = 15


@dataclass
class FetchResult:
    """Outcome of one lookup attempt.

    `matched` is only True when something usable came back. `confidence` is
    0-1 and is what the caller compares against `fetch_confidence` before
    accepting. `tags` is populated only when matched is True.
    """

    matched: bool = False
    confidence: float = 0.0
    source: str = ""
    tags: Optional[TrackMetadata] = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "confidence": self.confidence,
            "source": self.source,
            "tags": self.tags.to_dict() if self.tags else None,
            "notes": self.notes,
        }


class MetadataFetcher(ABC):
    """Interface every fetcher implements."""

    name: str = "base"
    #: What this backend is good at, shown in the UI.
    description: str = ""

    def __init__(self, settings: dict):
        self.settings = settings

    @abstractmethod
    async def fetch(self, track: TrackMetadata, file_path: str = "") -> FetchResult:
        """Look up metadata for a track. Never raises - report failures in notes."""

    def available(self) -> bool:
        """True when credentials and dependencies are present.

        An unavailable fetcher should still be constructible so the UI can
        explain *why* it cannot run.
        """
        return True

    def unavailable_reason(self) -> str:
        """Human-readable explanation when available() is False."""
        return "Not configured."


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["FetchResult", "MetadataFetcher", "API_DELAY", "DEFAULT_TIMEOUT"]
