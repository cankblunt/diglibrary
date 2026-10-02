"""Immutable value objects shared by the generic Provider Framework."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from diglibrary.identity import validate_identifier
from diglibrary.providers.types import ProviderId


class ProviderStatus(StrEnum):
    """Purpose: represent a provider's immutable runtime state.

    Responsibilities: provide the complete status vocabulary used by the
    framework. Boundaries: states carry no provider behavior or I/O.
    Dependencies: Python's immutable enum support only. Collaborators:
    ``ProviderManager`` and ``ProviderHealth``. Constraints: values are stable,
    serializable lowercase identifiers and must not encode provider-specific state.

    ``NO_CREDENTIAL`` and ``CREDENTIAL_REJECTED`` are provider-neutral for the
    same reason the rest of this vocabulary is: any service reached with a key
    can be missing one or be told its own is wrong, and those are two different
    things to do about it. Folded into ``OFFLINE``, both would be reported as
    a server that stopped answering while it is answering and only wants a key.
    """

    UNKNOWN = "unknown"
    INITIALIZING = "initializing"
    READY = "ready"
    BUSY = "busy"
    DISABLED = "disabled"
    ERROR = "error"
    NO_CREDENTIAL = "no_credential"
    CREDENTIAL_REJECTED = "credential_rejected"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True, order=True)
class ProviderCapability:
    """Purpose: name one extensible provider operation or content capability.

    Responsibilities: validate and transport a capability identifier. Boundaries:
    it does not decide whether an operation is invoked. Dependencies: standard
    dataclass and regular-expression validation only. Collaborators:
    ``ProviderMetadata`` and ``ProviderManager``. Constraints: capability names
    are immutable lowercase snake-case identifiers, enabling future values without
    changing an enum or introducing provider-specific conditionals.
    """

    name: str

    def __post_init__(self) -> None:
        """Reject non-portable capability identifiers at the configuration boundary."""
        validate_identifier(self.name, "provider capability")


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """Purpose: hold generic, immutable configuration for one provider instance.

    Responsibilities: expose enablement, priority, timeout, retries, and opaque
    future settings. Boundaries: it neither instantiates providers nor resolves
    secrets. Dependencies: standard immutable data structures only. Collaborators:
    composition code, ``ProviderMetadata``, and future provider factories.
    Constraints: identifiers are validated, common values are non-negative, and
    provider-specific settings remain immutable so the framework needs no changes.
    """

    identifier: ProviderId
    enabled: bool = True
    priority: int = 10
    timeout_seconds: float = 30.0
    retry_limit: int = 3
    settings: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate generic configuration while preserving opaque future settings."""
        if self.priority < 0 or self.timeout_seconds <= 0 or self.retry_limit < 0:
            raise ValueError("Provider priority, timeout, and retry values are invalid.")
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Purpose: describe a provider instance without exposing implementation details.

    Responsibilities: publish immutable identity, ownership, capabilities, and
    effective configuration. Boundaries: metadata does not perform lifecycle work
    or alter priorities. Dependencies: provider value objects only. Collaborators:
    registry, manager, documentation, and future provider implementations.
    Constraints: the identifier is globally unique and capabilities are immutable.
    """

    identifier: ProviderId
    name: str
    version: str
    author: str
    capabilities: frozenset[ProviderCapability]
    priority: int
    enabled: bool

    def __post_init__(self) -> None:
        """Validate public identity and freeze potentially mutable capability input."""
        if not all((self.name, self.version, self.author)):
            raise ValueError("Provider name, version, and author are required.")
        if self.priority < 0:
            raise ValueError("Provider priority must be non-negative.")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Purpose: report an immutable result of one provider health observation.

    Responsibilities: expose state and a human-readable diagnostic. Boundaries:
    it does not perform health checks or network I/O. Dependencies: ``ProviderStatus``.
    Collaborators: provider implementations and ``ProviderManager``. Constraints:
    status remains a framework vocabulary and diagnostics must not contain secrets.
    """

    status: ProviderStatus
    message: str = ""


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Purpose: carry generic provider-operation input without provider-specific types.

    Responsibilities: identify an operation target and immutable parameter values.
    Boundaries: it does not validate a service protocol or trigger I/O. Dependencies:
    standard mapping support only. Collaborators: ``ProviderManager`` and provider
    lifecycle methods. Constraints: parameters are copied into an immutable mapping.
    """

    target: str
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze operation parameters supplied by an application-layer caller."""
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ProviderOperationResult:
    """Purpose: return a generic lifecycle-operation outcome without download logic.

    Responsibilities: carry an immutable success flag, values, and explanation.
    Boundaries: it represents results but does not rank candidates, persist files,
    or interpret service payloads. Dependencies: standard mapping support only.
    Collaborators: lifecycle providers and ``ProviderManager``. Constraints:
    values are frozen and explanations must remain safe for structured logging.
    """

    successful: bool
    values: Mapping[str, object] = field(default_factory=dict)
    explanation: str = ""

    def __post_init__(self) -> None:
        """Freeze operation values to keep lifecycle results externally immutable."""
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
