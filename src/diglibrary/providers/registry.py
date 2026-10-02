"""Non-instantiating registry for externally composed provider instances."""

from collections.abc import Iterable

from diglibrary.providers.contracts import Provider
from diglibrary.providers.types import ProviderId


class ProviderRegistrationError(ValueError):
    """Purpose: signal invalid provider registration without changing registry state.

    Responsibilities: communicate duplicate identity or identifier failures.
    Boundaries: this exception does not discover, instantiate, or recover providers.
    Dependencies: built-in exception behavior only. Collaborators: ``ProviderRegistry``
    and composition code. Constraints: messages must contain no provider secrets.
    """


class ProviderRegistry:
    """Purpose: retain externally constructed providers by validated stable identity.

    Responsibilities: register, remove, list, retrieve, and discover supplied
    provider instances. Boundaries: it never instantiates providers, invokes their
    lifecycle, ranks them, or performs network work. Dependencies: ``Provider``
    protocol and standard containers only. Collaborators: composition root and
    ``ProviderManager``. Constraints: identifiers and display names are unique,
    and discovery accepts instances rather than factories to preserve DI control.
    """

    def __init__(self) -> None:
        """Create an empty registry with no provider side effects."""
        self._providers: dict[ProviderId, Provider] = {}

    def register(self, provider: Provider) -> None:
        """Register one externally composed provider after duplicate validation."""
        metadata = provider.metadata
        if metadata.identifier in self._providers:
            raise ProviderRegistrationError(f"Duplicate provider identifier: {metadata.identifier}")
        if any(item.metadata.name == metadata.name for item in self._providers.values()):
            raise ProviderRegistrationError(f"Duplicate provider name: {metadata.name}")
        self._providers[metadata.identifier] = provider

    def remove(self, identifier: ProviderId) -> Provider:
        """Remove and return a provider without invoking its shutdown lifecycle."""
        try:
            return self._providers.pop(identifier)
        except KeyError as error:
            raise ProviderRegistrationError(f"Unknown provider identifier: {identifier}") from error

    def get(self, identifier: ProviderId) -> Provider:
        """Return a registered provider by stable identifier."""
        try:
            return self._providers[identifier]
        except KeyError as error:
            raise ProviderRegistrationError(f"Unknown provider identifier: {identifier}") from error

    def list(self) -> tuple[Provider, ...]:
        """Return registered providers in identifier order without lifecycle effects."""
        return tuple(self._providers[identifier] for identifier in sorted(self._providers))

    def discover(self, providers: Iterable[Provider]) -> tuple[Provider, ...]:
        """Register externally supplied discovered instances without constructing any provider."""
        for provider in providers:
            self.register(provider)
        return self.list()
