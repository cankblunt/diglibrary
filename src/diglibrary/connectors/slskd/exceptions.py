"""Connector-specific failures that hide HTTP-library implementation details."""


class SlskdConnectorError(RuntimeError):
    """Base exception for failures at the slskd connector boundary.

    Purpose:
        Provides one stable failure type for consumers of the connector.
    Responsibilities:
        Marks operational failures translated from transport or protocol conditions.
    Architectural boundaries:
        Does not expose urllib exceptions, provider concerns, or acquisition decisions.
    Dependencies:
        Depends only on the Python standard exception hierarchy.
    Expected collaborators:
        ``SlskdClient`` raises subclasses and ``SlskdConnector`` exposes them to callers.
    Constraints:
        Messages must not contain credentials or authentication tokens.
    """


class SlskdAuthenticationError(SlskdConnectorError):
    """Raised when slskd authentication is absent, rejected, or unavailable.

    Purpose:
        Distinguishes credential failures from service or protocol failures.
    Responsibilities:
        Signals that a caller must correct the external authentication configuration.
    Architectural boundaries:
        Does not resolve secrets or retain their values.
    Dependencies:
        Depends only on ``SlskdConnectorError``.
    Expected collaborators:
        ``SlskdClient`` and application configuration diagnostics.
    Constraints:
        Its text must never disclose a secret value.
    """


class SlskdCredentialMissingError(SlskdAuthenticationError):
    """Raised when no slskd API key exists to send at all.

    Purpose:
        Separates a key that was never supplied from a key the service refused.
    Responsibilities:
        Signals that the environment carries no credential for this connector.
    Architectural boundaries:
        Does not read, resolve, or store a secret value.
    Dependencies:
        Depends only on ``SlskdAuthenticationError``.
    Expected collaborators:
        ``SlskdClient`` raises it and ``SlskdConnector`` maps it to health.
    Constraints:
        A subclass rather than a sibling, so every existing handler of
        ``SlskdAuthenticationError`` keeps catching this case unchanged.

    The two are one gesture apart for whoever is reading the screen — supply a
    key, or correct the one already there — and telling them apart is the only
    reason this type exists.
    """


class SlskdServiceUnreachableError(SlskdConnectorError):
    """Raised when the configured service did not answer this request at all.

    Purpose:
        Names the family of failures that mean *nobody was there*, as one type.
    Responsibilities:
        Lets a caller separate a silent service from a service that answered
        badly, without listing the ways silence arrives.
    Architectural boundaries:
        Decides nothing about retries, health, or recovery.
    Dependencies:
        Depends only on ``SlskdConnectorError``.
    Expected collaborators:
        ``SlskdConnector.health`` maps it to an unavailable observation.
    Constraints:
        Every failure that means the service was not reached must inherit it,
        including ones added later.

    A base rather than a tuple at the catch site: `except (Timeout,
    Unavailable, RetryExhausted)` is a set written by listing its members, and
    a fourth way for a request to go unanswered would fall through it silently.
    """


class SlskdTimeoutError(SlskdServiceUnreachableError):
    """Raised when slskd does not respond before the configured timeout.

    Purpose:
        Gives callers a transport-neutral timeout signal.
    Responsibilities:
        Represents exhausted or non-retryable timeout failures.
    Architectural boundaries:
        Does not expose socket or urllib exception objects.
    Dependencies:
        Depends only on ``SlskdServiceUnreachableError``.
    Expected collaborators:
        ``SlskdClient`` retry handling and connector consumers.
    Constraints:
        The exception carries no request headers or sensitive endpoint data.
    """


class SlskdUnavailableError(SlskdServiceUnreachableError):
    """Raised when the configured slskd service cannot be reached.

    Purpose:
        Identifies service availability failures independently of a HTTP library.
    Responsibilities:
        Represents connection and transient server availability errors.
    Architectural boundaries:
        Does not make health decisions for providers.
    Dependencies:
        Depends only on ``SlskdServiceUnreachableError``.
    Expected collaborators:
        ``SlskdClient`` and the future provider health adapter.
    Constraints:
        It must not leak raw transport exceptions to connector consumers.
    """


class SlskdMalformedResponseError(SlskdConnectorError):
    """Raised when slskd returns invalid or structurally unusable JSON.

    Purpose:
        Separates payload-shape failures from service availability failures.
    Responsibilities:
        Signals response decoding and schema-boundary validation errors.
    Architectural boundaries:
        Does not attempt provider-specific recovery or candidate interpretation.
    Dependencies:
        Depends only on ``SlskdConnectorError``.
    Expected collaborators:
        ``SlskdClient`` and ``SlskdMapper``.
    Constraints:
        A malformed payload is never silently converted into business data.
    """


class SlskdRetryExhaustedError(SlskdServiceUnreachableError):
    """Raised after every permitted retryable slskd request has failed.

    Purpose:
        Lets callers distinguish retry exhaustion from an immediate terminal failure.
    Responsibilities:
        Preserves the high-level reason without exposing a raw HTTP exception.
    Architectural boundaries:
        Does not schedule retries outside one request execution.
    Dependencies:
        Depends only on ``SlskdServiceUnreachableError``.
    Expected collaborators:
        ``SlskdClient`` and application-layer error handling.
    Constraints:
        Retry counts remain bounded by ``SlskdConfiguration.retry_limit``.
    """


class SlskdProtocolViolationError(SlskdConnectorError):
    """Raised when slskd returns an unexpected HTTP or API protocol condition.

    Purpose:
        Represents non-authentication, non-transient protocol failures.
    Responsibilities:
        Hides HTTP status handling from all connector consumers.
    Architectural boundaries:
        Does not expose a raw response or make a provider decision.
    Dependencies:
        Depends only on ``SlskdConnectorError``.
    Expected collaborators:
        ``SlskdClient`` and operational diagnostics.
    Constraints:
        Its message contains status context only, never response bodies or secrets.
    """


class SlskdNotFoundError(SlskdProtocolViolationError):
    """Raised when slskd says the thing asked about does not exist.

    Purpose:
        Separates "there is nothing here" from "this route is wrong", which
        share a status code and mean opposite things.
    Responsibilities:
        Lets one caller treat an absence as ordinary while every other caller
        keeps seeing a protocol violation, since it remains one of those.
    Architectural boundaries:
        Does not decide what an absence means for any operation.
    Dependencies:
        Depends only on ``SlskdProtocolViolationError``.
    Expected collaborators:
        ``SlskdClient``, whose transfer list meets this for a source it has
        never fetched from.
    Constraints:
        Raised for 404 alone; any other unexpected status stays a plain
        protocol violation.
    """


class SlskdSearchAbandonedError(SlskdConnectorError):
    """Raised when the caller stopped wanting a search before it was read.

    Purpose:
        Separates "you closed this" from every failure, so the window can drop a
        tab in silence rather than explaining an error nobody suffered.
    Responsibilities:
        Names the one outcome that is a completed intention rather than a fault.
    Architectural boundaries:
        Does not decide when a caller has abandoned a search, nor clean up after
        one.
    Dependencies:
        Depends only on ``SlskdConnectorError``.
    Expected collaborators:
        ``SlskdClient`` raises it when the watch it was given says so.
    Constraints:
        The search is stopped at the service before this is raised, because the
        point of abandoning one is that it stops costing the network.
    """


class SlskdSearchUnsettledError(SlskdConnectorError):
    """Raised when a search is still gathering answers and cannot be read yet.

    Purpose:
        Keeps "the answers are not readable yet" from being mistaken for "nobody
        answered", which is the same empty list and the opposite meaning.
    Responsibilities:
        Names the one condition where waiting longer is the remedy.
    Architectural boundaries:
        Does not wait, retry, or decide how long is long enough.
    Dependencies:
        Depends only on ``SlskdConnectorError``.
    Expected collaborators:
        ``SlskdClient`` raises it; the window turns it into words a person can
        act on.
    Constraints:
        Raised only after the configured wait has elapsed with the search still
        in progress.
    """
