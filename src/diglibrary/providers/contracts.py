"""Provider lifecycle contracts independent of concrete acquisition services."""

from typing import Protocol

from diglibrary.providers.models import (
    ProviderHealth,
    ProviderMetadata,
    ProviderOperationResult,
    ProviderRequest,
)


class Provider(Protocol):
    """Purpose: define the complete lifecycle boundary for an acquisition provider.

    Responsibilities: expose metadata and lifecycle operations for manager-led
    orchestration. Boundaries: implementations never coordinate peer providers,
    choose candidates, access the Decision Engine, or instantiate dependencies.
    Dependencies: generic provider value objects only. Collaborators:
    ``ProviderRegistry``, ``ProviderManager``, and composition code. Constraints:
    optional operations must be declared through capabilities; the framework
    itself performs no provider-specific network work.
    """

    @property
    def metadata(self) -> ProviderMetadata:
        """Return immutable provider metadata configured by the composition root."""

    def initialize(self) -> ProviderHealth | None:
        """Initialize provider-owned resources without coordinating other providers.

        Returns what it observed while doing so, or ``None`` when it observed
        nothing worth reporting. With no return value the manager could only
        record *it did not raise*, and since a credential failure is a state
        rather than an exception, that would read as ``READY`` for a provider
        that cannot be used at all.
        """

    def health(self) -> ProviderHealth:
        """Return a health observation without requiring framework-level policy."""

    def search(self, request: ProviderRequest) -> ProviderOperationResult:
        """Perform a capability-declared search operation."""

    def resolve(self, request: ProviderRequest) -> ProviderOperationResult:
        """Perform a capability-declared resolution operation."""

    def download(self, request: ProviderRequest) -> ProviderOperationResult:
        """Perform a capability-declared download operation."""

    def validate(self, request: ProviderRequest) -> ProviderOperationResult:
        """Perform a capability-declared validation operation."""

    def shutdown(self) -> None:
        """Release provider-owned resources without coordinating peer providers."""
