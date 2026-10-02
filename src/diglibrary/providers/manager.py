"""The sole lifecycle coordinator for generic provider instances."""

import logging
from dataclasses import dataclass

from diglibrary.providers.contracts import Provider
from diglibrary.providers.models import (
    ProviderCapability,
    ProviderHealth,
    ProviderMetadata,
    ProviderOperationResult,
    ProviderRequest,
    ProviderStatus,
)
from diglibrary.providers.registry import ProviderRegistry
from diglibrary.providers.types import ProviderId


class ProviderCapabilityError(RuntimeError):
    """Purpose: signal a requested lifecycle operation absent from provider capabilities.

    Responsibilities: prevent invalid manager dispatch before provider invocation.
    Boundaries: it does not infer capabilities, perform fallback, or access a
    concrete service. Dependencies: built-in exception behavior only. Collaborators:
    ``ProviderManager`` callers. Constraints: errors reference only public provider
    identifiers and capability names.
    """


@dataclass(frozen=True, slots=True)
class ProviderRuntimeState:
    """Purpose: expose one provider's immutable manager-owned runtime snapshot.

    Responsibilities: pair provider metadata with its observed status and health.
    Boundaries: it does not mutate providers or perform health checks. Dependencies:
    provider value objects only. Collaborators: ``ProviderManager`` and reporting
    callers. Constraints: the manager replaces snapshots atomically; provider
    implementations must never own or mutate this framework state.
    """

    metadata: ProviderMetadata
    status: ProviderStatus
    health: ProviderHealth | None = None


class ProviderManager:
    """Purpose: coordinate all provider lifecycle operations through one application service.

    Responsibilities: initialize, health-check, invoke capability-declared operations,
    shut down, order by priority, and maintain immutable runtime state. Boundaries:
    it never constructs providers, performs provider-specific branching, selects
    candidates, downloads content, or invokes another Engine. Dependencies:
    ``ProviderRegistry``, provider contracts, and injected structured logger.
    Collaborators: composition root and application-layer workflows. Constraints:
    this is the sole lifecycle coordinator; disabled providers are never initialized
    or health-checked and all operations require declared capabilities.
    """

    def __init__(self, registry: ProviderRegistry, logger: logging.Logger) -> None:
        """Create a manager for already-registered providers without lifecycle side effects."""
        self._registry = registry
        self._logger = logger
        self._states: dict[ProviderId, ProviderRuntimeState] = {}
        self._enabled_overrides: dict[ProviderId, bool] = {}

    def enable(self, identifier: ProviderId) -> ProviderRuntimeState:
        """Enable one registered provider without instantiating or initializing it."""
        provider = self._registry.get(identifier)
        self._enabled_overrides[identifier] = True
        state = ProviderRuntimeState(provider.metadata, ProviderStatus.UNKNOWN)
        self._states[identifier] = state
        return state

    def disable(self, identifier: ProviderId) -> ProviderRuntimeState:
        """Disable one registered provider so manager orchestration skips it."""
        provider = self._registry.get(identifier)
        self._enabled_overrides[identifier] = False
        state = ProviderRuntimeState(provider.metadata, ProviderStatus.DISABLED)
        self._states[identifier] = state
        return state

    def initialize(self) -> tuple[ProviderRuntimeState, ...]:
        """Initialize enabled providers in ascending priority order."""
        for provider in self._ordered_providers():
            metadata = provider.metadata
            if not self._is_enabled(metadata):
                self._set_state(metadata, ProviderStatus.DISABLED)
                continue
            self._set_state(metadata, ProviderStatus.INITIALIZING)
            try:
                observed = provider.initialize()
            except Exception as error:
                self._set_state(
                    metadata, ProviderStatus.ERROR, ProviderHealth(ProviderStatus.ERROR, str(error))
                )
                self._logger.exception(
                    "Provider initialization failed.", extra={"operation": "provider.initialize"}
                )
                continue
            # What it says it observed, and only `READY` when it says nothing.
            # Recording `READY` for anything that merely did not raise would
            # file a provider that reports *no key* as a state as ready to use.
            if observed is not None:
                self._set_state(metadata, observed.status, observed)
                self._logger.info(
                    "Provider initialized.",
                    extra={"operation": "provider.initialize", "status": observed.status.value},
                )
                continue
            self._set_state(metadata, ProviderStatus.READY)
            self._logger.info("Provider initialized.", extra={"operation": "provider.initialize"})
        return self.states()

    def shutdown(self) -> tuple[ProviderRuntimeState, ...]:
        """Shut down enabled providers in reverse priority order."""
        for provider in reversed(self._ordered_providers()):
            metadata = provider.metadata
            if not self._is_enabled(metadata):
                continue
            try:
                provider.shutdown()
            except Exception as error:
                self._set_state(
                    metadata, ProviderStatus.ERROR, ProviderHealth(ProviderStatus.ERROR, str(error))
                )
                self._logger.exception(
                    "Provider shutdown failed.", extra={"operation": "provider.shutdown"}
                )
                continue
            self._set_state(metadata, ProviderStatus.UNKNOWN)
            self._logger.info("Provider shut down.", extra={"operation": "provider.shutdown"})
        return self.states()

    def health_check(self) -> tuple[ProviderRuntimeState, ...]:
        """Collect health results for enabled providers without issuing framework network calls."""
        for provider in self._ordered_providers():
            metadata = provider.metadata
            if not self._is_enabled(metadata):
                self._set_state(metadata, ProviderStatus.DISABLED)
                continue
            try:
                health = provider.health()
            except Exception as error:
                health = ProviderHealth(ProviderStatus.ERROR, str(error))
                self._logger.exception(
                    "Provider health check failed.", extra={"operation": "provider.health"}
                )
            self._set_state(metadata, health.status, health)
        return self.states()

    def invoke(
        self,
        identifier: ProviderId,
        capability: ProviderCapability,
        lifecycle_method: str,
        request: ProviderRequest,
    ) -> ProviderOperationResult:
        """Invoke one capability-declared lifecycle operation through the manager boundary."""
        provider = self._registry.get(identifier)
        metadata = provider.metadata
        if not self._is_enabled(metadata):
            raise ProviderCapabilityError(f"Provider is disabled: {identifier}")
        if capability not in metadata.capabilities:
            raise ProviderCapabilityError(
                f"Provider {identifier} lacks capability: {capability.name}"
            )
        operation = getattr(provider, lifecycle_method, None)
        if not callable(operation) or lifecycle_method not in {
            "search",
            "resolve",
            "download",
            "validate",
        }:
            raise ProviderCapabilityError(
                f"Unsupported provider lifecycle method: {lifecycle_method}"
            )
        self._set_state(metadata, ProviderStatus.BUSY)
        try:
            result = operation(request)
        except Exception as error:
            self._set_state(
                metadata, ProviderStatus.ERROR, ProviderHealth(ProviderStatus.ERROR, str(error))
            )
            self._logger.exception(
                "Provider operation failed.", extra={"operation": "provider.invoke"}
            )
            raise
        self._set_state(metadata, ProviderStatus.READY)
        return result

    def states(self) -> tuple[ProviderRuntimeState, ...]:
        """Return immutable runtime snapshots in manager priority order."""
        return tuple(
            self._states.get(provider.metadata.identifier, self._initial_state(provider))
            for provider in self._ordered_providers()
        )

    def state(self, identifier: ProviderId) -> ProviderRuntimeState:
        """Return one current runtime snapshot, creating no provider state side effects."""
        provider = self._registry.get(identifier)
        return self._states.get(identifier, self._initial_state(provider))

    def _ordered_providers(self) -> tuple[Provider, ...]:
        return tuple(
            sorted(
                self._registry.list(),
                key=lambda provider: (provider.metadata.priority, provider.metadata.identifier),
            )
        )

    def _initial_state(self, provider: Provider) -> ProviderRuntimeState:
        status = (
            ProviderStatus.DISABLED
            if not self._is_enabled(provider.metadata)
            else ProviderStatus.UNKNOWN
        )
        return ProviderRuntimeState(provider.metadata, status)

    def _is_enabled(self, metadata: ProviderMetadata) -> bool:
        return self._enabled_overrides.get(metadata.identifier, metadata.enabled)

    def _set_state(
        self,
        metadata: ProviderMetadata,
        status: ProviderStatus,
        health: ProviderHealth | None = None,
    ) -> None:
        self._states[metadata.identifier] = ProviderRuntimeState(metadata, status, health)
