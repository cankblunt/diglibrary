"""Repository interfaces reserved for future persistence implementations."""

from collections.abc import Mapping
from typing import Protocol, TypeVar

EntityT = TypeVar("EntityT", covariant=True)
IdentifierT = TypeVar("IdentifierT", contravariant=True)


class Repository(Protocol[EntityT, IdentifierT]):
    """Minimal persistence boundary for a single database-backed aggregate."""

    def get(self, identifier: IdentifierT) -> EntityT | None:
        """Return an entity by identifier, or ``None`` when it does not exist."""


Record = Mapping[str, object]


class AlbumRepository(Repository[Record, int], Protocol):
    """Persistence boundary for album records."""


class TrackRepository(Repository[Record, int], Protocol):
    """Persistence boundary for track records."""


class ProviderRepository(Repository[Record, int], Protocol):
    """Persistence boundary for provider records."""


class DownloadRepository(Repository[Record, int], Protocol):
    """Persistence boundary for download records."""


class ReportRepository(Repository[Record, int], Protocol):
    """Persistence boundary for report records."""


class SettingsRepository(Repository[Record, str], Protocol):
    """Persistence boundary for application settings."""
