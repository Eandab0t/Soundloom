"""Pipeline stage interfaces (abstract base classes).

Each stage is a pluggable adapter. Implementations can be swapped
without changing the rest of the pipeline.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from .models import TrackMetadata, SourceCandidate, MatchResult, QualityProfile


class SourceResolver(ABC):
    """Resolves input (URL, search query, Spotify link) to metadata."""

    @abstractmethod
    async def resolve(self, input_str: str) -> TrackMetadata:
        """Resolve input to track metadata."""

    @abstractmethod
    def can_handle(self, input_str: str) -> bool:
        """Return True if this resolver can handle the input."""


class SearchProvider(ABC):
    """Searches for music across various sources."""

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[SourceCandidate]:
        """Search for candidates matching the query."""


class MatchProvider(ABC):
    """Matches candidates against desired metadata with confidence scoring."""

    @abstractmethod
    async def match(self, desired: TrackMetadata,
                    candidates: list[SourceCandidate]) -> list[MatchResult]:
        """Score and rank candidates against desired metadata."""

    @abstractmethod
    def confidence_threshold(self) -> float:
        """Minimum confidence for auto-accept."""


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


class Converter(ABC):
    """Converts audio between formats."""

    @abstractmethod
    async def convert(self, input_path: str, output_path: str,
                      profile: QualityProfile,
                      progress_callback=None) -> str:
        """Convert audio. Returns output path."""

    @abstractmethod
    def supported_formats(self) -> list[str]:
        """Return list of supported output formats."""


class TagProvider(ABC):
    """Reads and writes audio metadata tags."""

    @abstractmethod
    async def read_tags(self, file_path: str) -> dict:
        """Read tags from audio file."""

    @abstractmethod
    async def write_tags(self, file_path: str, metadata: TrackMetadata,
                         cover_path: str = None) -> bool:
        """Write tags to audio file. Returns success."""


class Organizer(ABC):
    """Organizes files into the library folder structure."""

    @abstractmethod
    async def organize(self, source_path: str, metadata: TrackMetadata,
                       library_root: str, template: str,
                       duplicate_policy: str = "keep_separate") -> str:
        """Move file to organized location. Returns final path."""


class ArtworkProvider(ABC):
    """Provides and processes cover art."""

    @abstractmethod
    async def get_artwork(self, metadata: TrackMetadata,
                          max_size: int = 1000) -> str | None:
        """Get best available artwork. Returns path or None."""

    @abstractmethod
    def normalize(self, image_path: str, output_path: str,
                  max_size: int = 1000) -> str:
        """Normalize image to JPEG at max_size. Returns output path."""
