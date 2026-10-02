"""Unit tests for generic Provider Framework lifecycle infrastructure."""

import logging

import pytest

from diglibrary.providers.capabilities import ProviderCapabilities
from diglibrary.providers.manager import ProviderCapabilityError, ProviderManager
from diglibrary.providers.models import (
    ProviderCapability,
    ProviderHealth,
    ProviderMetadata,
    ProviderOperationResult,
    ProviderRequest,
    ProviderStatus,
)
from diglibrary.providers.registry import ProviderRegistrationError, ProviderRegistry
from diglibrary.providers.types import ProviderId


class FakeProvider:
    """In-memory provider used to observe framework orchestration without service behavior."""

    def __init__(self, metadata: ProviderMetadata, health: ProviderHealth | None = None) -> None:
        self._metadata = metadata
        self._health = health or ProviderHealth(ProviderStatus.READY)
        self.calls: list[str] = []

    @property
    def metadata(self) -> ProviderMetadata:
        """Return immutable fake metadata."""
        return self._metadata

    def initialize(self) -> None:
        """Record lifecycle initialization."""
        self.calls.append("initialize")

    def health(self) -> ProviderHealth:
        """Record and return configured health."""
        self.calls.append("health")
        return self._health

    def search(self, request: ProviderRequest) -> ProviderOperationResult:
        """Record a generic search operation."""
        self.calls.append("search")
        return ProviderOperationResult(True, {"target": request.target})

    def resolve(self, request: ProviderRequest) -> ProviderOperationResult:
        """Record a generic resolve operation."""
        self.calls.append("resolve")
        return ProviderOperationResult(True)

    def download(self, request: ProviderRequest) -> ProviderOperationResult:
        """Record a generic download contract invocation without download behavior."""
        self.calls.append("download")
        return ProviderOperationResult(True)

    def validate(self, request: ProviderRequest) -> ProviderOperationResult:
        """Record a generic validation operation."""
        self.calls.append("validate")
        return ProviderOperationResult(True)

    def shutdown(self) -> None:
        """Record lifecycle shutdown."""
        self.calls.append("shutdown")


def test_registry_registers_discovers_and_rejects_duplicate_identity() -> None:
    """Registry retains supplied instances and never invokes their lifecycle methods."""
    registry = ProviderRegistry()
    first = FakeProvider(_metadata("first", "First"))
    second = FakeProvider(_metadata("second", "Second"))

    assert registry.discover((second, first)) == (first, second)
    assert first.calls == []

    with pytest.raises(ProviderRegistrationError, match="identifier"):
        registry.register(FakeProvider(_metadata("first", "Other")))
    with pytest.raises(ProviderRegistrationError, match="name"):
        registry.register(FakeProvider(_metadata("third", "First")))

    assert registry.remove(ProviderId("first")) is first
    with pytest.raises(ProviderRegistrationError, match="Unknown"):
        registry.get(ProviderId("missing"))


def test_manager_initializes_and_shuts_down_enabled_providers_by_priority() -> None:
    """Manager is the sole lifecycle coordinator and skips disabled providers."""
    registry = ProviderRegistry()
    slow = FakeProvider(_metadata("slow", "Slow", priority=20))
    fast = FakeProvider(_metadata("fast", "Fast", priority=10))
    disabled = FakeProvider(_metadata("disabled", "Disabled", enabled=False))
    registry.discover((slow, fast, disabled))
    manager = ProviderManager(registry, logging.getLogger("test.providers"))

    states = manager.initialize()
    manager.shutdown()

    assert fast.calls == ["initialize", "shutdown"]
    assert slow.calls == ["initialize", "shutdown"]
    assert disabled.calls == []
    assert [str(state.metadata.identifier) for state in states] == ["disabled", "fast", "slow"]
    assert manager.state(ProviderId("disabled")).status is ProviderStatus.DISABLED


def test_manager_reports_health_and_enforces_capabilities() -> None:
    """Health and operations pass only through capability-declared manager dispatch."""
    registry = ProviderRegistry()
    provider = FakeProvider(
        _metadata(
            "searcher", "Searcher", capabilities=frozenset({ProviderCapabilities.SEARCH_ALBUM})
        ),
        ProviderHealth(ProviderStatus.OFFLINE, "maintenance"),
    )
    registry.register(provider)
    manager = ProviderManager(registry, logging.getLogger("test.providers"))

    states = manager.health_check()
    result = manager.invoke(
        ProviderId("searcher"),
        ProviderCapabilities.SEARCH_ALBUM,
        "search",
        ProviderRequest("album"),
    )

    assert states[0].status is ProviderStatus.OFFLINE
    assert result.successful is True
    assert provider.calls == ["health", "search"]
    with pytest.raises(ProviderCapabilityError, match="lacks capability"):
        manager.invoke(
            ProviderId("searcher"),
            ProviderCapabilities.DOWNLOAD_ALBUM,
            "download",
            ProviderRequest("album"),
        )


def test_manager_can_disable_and_reenable_a_registered_provider() -> None:
    """Runtime enablement is manager-owned and preserves immutable provider metadata."""
    registry = ProviderRegistry()
    provider = FakeProvider(_metadata("toggle", "Toggle"))
    registry.register(provider)
    manager = ProviderManager(registry, logging.getLogger("test.providers"))

    assert manager.disable(ProviderId("toggle")).status is ProviderStatus.DISABLED
    manager.initialize()
    assert provider.calls == []
    assert manager.enable(ProviderId("toggle")).status is ProviderStatus.UNKNOWN
    manager.initialize()

    assert provider.calls == ["initialize"]


def test_provider_value_objects_are_immutable_and_extensible() -> None:
    """Capabilities and configuration values validate generic future extension safely."""
    capability = ProviderCapability("future_format")
    metadata = _metadata("future", "Future", capabilities=frozenset({capability}))

    assert capability in metadata.capabilities
    with pytest.raises(ValueError, match="Invalid provider capability"):
        ProviderCapability("not valid")
    with pytest.raises(AttributeError):
        metadata.priority = 1  # type: ignore[misc]


def _metadata(
    identifier: str,
    name: str,
    *,
    priority: int = 10,
    enabled: bool = True,
    capabilities: frozenset[ProviderCapability] = frozenset(),
) -> ProviderMetadata:
    return ProviderMetadata(
        identifier=ProviderId(identifier),
        name=name,
        version="1.0.0",
        author="DigLibrary",
        capabilities=capabilities,
        priority=priority,
        enabled=enabled,
    )


class _ReportingProvider(FakeProvider):
    """A provider that says what it observed while starting up."""

    def __init__(self, metadata: ProviderMetadata, observed: ProviderHealth) -> None:
        super().__init__(metadata)
        self._observed = observed

    def initialize(self) -> ProviderHealth:
        """Report the observation instead of only recording the call."""
        self.calls.append("initialize")
        return self._observed


def test_the_manager_records_what_a_provider_observed_while_starting() -> None:
    """`READY` is what was observed, not merely *did not raise*.

    A provider whose service answers that there is no key reports that as a
    state, so a manager that only watches for exceptions would file it as ready
    to use. A provider that reports nothing still gets `READY`, which is what
    returning without raising means for it.
    """
    registry = ProviderRegistry()
    reporting = _ReportingProvider(
        _metadata("keyless", "Keyless"),
        ProviderHealth(ProviderStatus.NO_CREDENTIAL, "No key was given."),
    )
    silent = FakeProvider(_metadata("quiet", "Quiet"))
    registry.discover((reporting, silent))
    manager = ProviderManager(registry, logging.getLogger("test.providers"))

    manager.initialize()

    assert manager.state(ProviderId("keyless")).status is ProviderStatus.NO_CREDENTIAL
    assert manager.state(ProviderId("keyless")).health is not None
    assert manager.state(ProviderId("quiet")).status is ProviderStatus.READY
