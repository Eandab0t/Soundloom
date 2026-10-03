"""No-op fetcher. Always reports no match, never touches the network.

Used as the safe default when nothing is configured, and as a test double.
"""
from .base import FetchResult, MetadataFetcher
from ...pipeline.models import TrackMetadata


class LocalOnlyFetcher(MetadataFetcher):
    name = "local_only"
    description = "No lookup. Safe default."

    async def fetch(self, track: TrackMetadata, file_path: str = "") -> FetchResult:
        return FetchResult(matched=False, source=self.name,
                           notes=["Web lookup disabled."])

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return "Always unavailable - it is a stub."


__all__ = ["LocalOnlyFetcher"]
