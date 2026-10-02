"""Engine interfaces that communicate through application-layer contracts."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from diglibrary.application.contracts import (
    MetadataQuery,
    MetadataSearchResult,
    ProviderCandidate,
    ResolutionRequest,
    ResolvedRequest,
)


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Ranked Decision Engine output with an explanation for the choice."""

    ranked_candidates: Sequence[ProviderCandidate]
    explanation: str


class ResolutionEngine(Protocol):
    """Resolve raw input into a canonical request."""

    def resolve(self, request: ResolutionRequest) -> ResolvedRequest:
        """Resolve an input request without invoking providers."""


class ProviderEngine(Protocol):
    """Coordinate configured providers through the application layer."""

    def collect(self, request: ResolvedRequest) -> Sequence[ProviderCandidate]:
        """Collect unranked candidate results from enabled providers."""


class DecisionEngine(Protocol):
    """Rank candidates and explain the resulting selection."""

    def decide(self, candidates: Sequence[ProviderCandidate]) -> DecisionResult:
        """Return a ranked, explainable decision without provider internals."""


class MetadataEngine(Protocol):
    """Search and retrieve canonical release metadata without making selections."""

    def search(self, query: MetadataQuery) -> Sequence[MetadataSearchResult]:
        """Return unranked canonical results from configured metadata sources."""


class QualityEngine(Protocol):
    """Assess technical media quality. No operation is defined on it yet."""


class LibraryEngine(Protocol):
    """Curate validated library content without touching Rekordbox's database."""


class ReportingEngine(Protocol):
    """Produce HTML, PDF, and JSON reports. No operation is defined on it yet."""
