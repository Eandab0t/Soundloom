"""Data models shared across pipeline stages."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class JobState(Enum):
    PENDING = "pending"
    RESOLVING = "resolving"
    MATCHING = "matching"
    WAITING_CONFIRM = "waiting_for_confirmation"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    TAGGING = "tagging"
    ORGANIZING = "organizing"
    INDEXING = "indexing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    RETRYING = "retrying"


class SourceType(Enum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    DEEZER = "deezer"
    LOCAL = "local"
    SPOTIFY = "spotify"
    UNKNOWN = "unknown"


@dataclass
class TrackMetadata:
    """Canonical metadata for a track. Never mutated by normalization."""
    title: str = ""
    artist: str = ""
    primary_artist: str = ""
    featured_artists: list[str] = field(default_factory=list)
    display_artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_number: int = 0
    disc_number: int = 0
    year: int = 0
    genre: str = ""
    duration: float = 0.0

    @property
    def display_name(self) -> str:
        parts = []
        if self.display_artist:
            parts.append(self.display_artist)
        elif self.primary_artist:
            parts.append(self.primary_artist)
        if self.album:
            parts.append(self.album)
        return " - ".join(parts) if parts else self.title

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "primary_artist": self.primary_artist,
            "featured_artists": self.featured_artists,
            "display_artist": self.display_artist,
            "album_artist": self.album_artist,
            "album": self.album,
            "track_number": self.track_number,
            "disc_number": self.disc_number,
            "year": self.year,
            "genre": self.genre,
            "duration": self.duration,
        }


@dataclass
class SourceCandidate:
    """A potential source for downloading a track."""
    url: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    quality_notes: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class MatchResult:
    """Result of matching a candidate against desired metadata."""
    candidate: SourceCandidate = field(default_factory=SourceCandidate)
    score: float = 0.0
    confidence: float = 0.0
    breakdown: dict = field(default_factory=dict)
    explanation: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class DownloadPlan:
    """A planned download with all decisions made before execution."""
    id: int = 0
    source: SourceCandidate = field(default_factory=SourceCandidate)
    match: MatchResult = field(default_factory=MatchResult)
    output_format: str = "mp3"
    quality_profile: str = "balanced"
    folder_template: str = ""
    filename_template: str = ""
    estimated_size: int = 0
    needs_confirmation: bool = False
    state: JobState = JobState.PENDING


@dataclass
class QualityProfile:
    """Complete quality profile configuration."""
    name: str = "balanced"
    output_format: str = "mp3"
    codec: str = "libmp3lame"
    bitrate: str = "192k"
    sample_rate: str = "44100"
    channels: int = 2
    lossless: bool = False
    max_file_size: int = 0

    @classmethod
    def best(cls) -> "QualityProfile":
        return cls(name="best", output_format="flac", codec="flac",
                   lossless=True, sample_rate="44100")

    @classmethod
    def balanced(cls) -> "QualityProfile":
        return cls(name="balanced", output_format="mp3", codec="libmp3lame",
                   bitrate="320k", sample_rate="44100")

    @classmethod
    def ipod_saver(cls) -> "QualityProfile":
        return cls(name="ipod_saver", output_format="m4a", codec="aac",
                   bitrate="128k", sample_rate="44100")


QUALITY_PRESETS = {
    "best": QualityProfile.best(),
    "balanced": QualityProfile.balanced(),
    "ipod_saver": QualityProfile.ipod_saver(),
}
