"""Pipeline stage interfaces (abstract base classes).

Each stage is a pluggable adapter. Implementations can be swapped
without changing the rest of the pipeline.
"""
from abc import ABC, abstractmethod
from .models import TrackMetadata, SourceCandidate


class SourceResolver(ABC):
    """Resolves input (URL, search query, Spotify link) to metadata."""

    @abstractmethod
    async def resolve(self, input_str: str) -> TrackMetadata:
        """Resolve input to track metadata."""

    @abstractmethod
    def can_handle(self, input_str: str) -> bool:
        """Return True if this resolver can handle the input."""


class DownloadProvider(ABC):
    """Downloads audio from a source."""

    @abstractmethod
    async def download(self, candidate: SourceCandidate,
                       output_path: str,
                       progress_callback=None) -> str:
        """Download audio to output_path. Returns actual file path."""

    @abstractmethod
    async def download_thumbnail(self, candidate: SourceCandidate,
                                 output_path: str) -> str:
        """Download artwork. Returns path to downloaded image."""
