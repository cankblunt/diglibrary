"""Interfaces for explainable metadata matching, without a matching algorithm."""

from dataclasses import dataclass
from typing import Protocol

from diglibrary.metadata.models import MetadataQuery, ReleaseMetadata


@dataclass(frozen=True, slots=True)
class MetadataMatch:
    """A matcher-produced candidate association with an explicit explanation."""

    release: ReleaseMetadata
    confidence: float
    explanation: str


class MetadataMatcher(Protocol):
    """Evaluate canonical metadata candidates without fetching provider data."""

    def match(
        self, query: MetadataQuery, candidates: tuple[ReleaseMetadata, ...]
    ) -> tuple[MetadataMatch, ...]:
        """Return explainable candidate associations for a metadata query."""
