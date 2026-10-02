"""Immutable configuration models."""

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from diglibrary.connectors.slskd.configuration import SlskdConfiguration
from diglibrary.library.naming import NamingPolicy
from diglibrary.providers.models import ProviderConfiguration
from diglibrary.providers.types import ProviderId


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Settings for the SQLite database."""

    path: Path
    # Where the copies taken as the window opens are kept. Its own folder and
    # its own word: `backup` in this application means the images a write
    # replaced, and that name is not given a second meaning.
    copies_directory: Path = Path("database-copies")


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Settings for structured application logging."""

    level: str
    directory: Path
    filename: str

    @property
    def path(self) -> Path:
        """Return the complete structured-log path."""
        return self.directory / self.filename


@dataclass(frozen=True, slots=True)
class ProvidersConfig:
    """Enabled provider types and their ordered default preference."""

    enabled: tuple[ProviderId, ...] = ()
    priority: tuple[ProviderId, ...] = ()
    settings: dict[ProviderId, ProviderConfiguration] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze generic per-provider settings after TOML validation."""
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))


@dataclass(frozen=True, slots=True)
class DiscogsConfig:
    """Authentication and rate-limit settings for Discogs metadata requests."""

    token_environment_variable: str
    requests_per_minute: int


@dataclass(frozen=True, slots=True)
class MusicBrainzConfig:
    """Identification, optional authentication, and rate-limit settings for MusicBrainz."""

    contact_environment_variable: str
    access_token_environment_variable: str
    requests_per_second: float


@dataclass(frozen=True, slots=True)
class MetadataConfig:
    """Configuration shared by all metadata infrastructure."""

    cache_directory: Path
    cache_ttl_seconds: int
    request_timeout_seconds: float
    max_attempts: int
    backoff_seconds: float
    discogs: DiscogsConfig
    musicbrainz: MusicBrainzConfig


@dataclass(frozen=True, slots=True)
class ArtworkConfig:
    """Cover-art settings: what to fetch, how big, and where the reversible copies live.

    Every field here is a setting that can be changed without touching code.
    ``staging_directory`` holds downloaded images before they are
    planned; ``backup_directory`` holds the images a write replaced.
    """

    enabled: bool
    include_back_cover: bool
    file_pixels: int
    embed_pixels: int
    parallel_downloads: int
    release_group_fallback: bool
    staging_directory: Path
    backup_directory: Path


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """All configuration required to initialize DigLibrary infrastructure."""

    database: DatabaseConfig
    logging: LoggingConfig
    providers: ProvidersConfig
    metadata: MetadataConfig
    slskd: SlskdConfiguration
    artwork: ArtworkConfig
    naming: NamingPolicy
