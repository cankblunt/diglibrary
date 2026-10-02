"""Open provider identity, independent of concrete integrations."""

from dataclasses import dataclass

from diglibrary.identity import validate_identifier


@dataclass(frozen=True, slots=True, order=True)
class ProviderId:
    """Purpose: identify one acquisition provider by a stable, portable name.

    Responsibilities: validate and transport a provider identifier.
    Boundaries: it does not enumerate the providers this project ships, decide
    whether a provider is enabled, or reach a service. Dependencies: the shared
    identifier validation only. Collaborators: configuration,
    ``ProviderMetadata``, ``ProviderRegistry``, and ``ProviderManager``.
    Constraints: the set of providers is deliberately open — any value matching
    the identifier format is valid, so adding a third-party provider never
    requires a change to this package.
    """

    value: str

    def __post_init__(self) -> None:
        """Reject identifiers that are not portable across configuration and storage."""
        validate_identifier(self.value, "provider")

    def __str__(self) -> str:
        """Return the identifier as written in configuration, logs, and storage."""
        return self.value
