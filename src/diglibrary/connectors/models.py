"""Connector-neutral immutable models shared by acquisition search connectors."""

from dataclasses import dataclass
from enum import StrEnum


class SearchKind(StrEnum):
    """Classify an opaque connector search without interpreting music metadata.

    Purpose:
        Identifies the high-level search operation requested from any acquisition connector.
    Responsibilities:
        Distinguishes album and track searches for connector dispatch and observability.
    Architectural boundaries:
        Does not parse, validate, resolve, or rank musical entities.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SearchRequest`` and implementations of ``SearchConnector``.
    Constraints:
        Query interpretation remains a provider responsibility.
    """

    ALBUM = "album"
    TRACK = "track"


class SearchAvailability(StrEnum):
    """Represent a connector-reported resource availability observation.

    Purpose:
        Preserves a service availability signal in a connector-neutral vocabulary.
    Responsibilities:
        Distinguishes available, unavailable, and unknown results without policy.
    Architectural boundaries:
        Does not select candidates or cause transfers to be queued.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SearchResult`` and concrete connector mappers.
    Constraints:
        ``UNKNOWN`` must be used when a service does not supply an availability signal.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class SearchQueueState(StrEnum):
    """Represent an optional external-service queue observation without queue control.

    Purpose:
        Carries a broadly useful source readiness observation when a service provides one.
    Responsibilities:
        Expresses available, queued, or unknown state as immutable connector data.
    Architectural boundaries:
        Does not enqueue, cancel, prioritize, or download any resource.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SearchResult`` and concrete connector mappers.
    Constraints:
        Connectors for services without queues return ``UNKNOWN``.
    """

    AVAILABLE = "available"
    QUEUED = "queued"
    UNKNOWN = "unknown"


class ConnectorHealthState(StrEnum):
    """Represent the configured connector's local health-check outcome.

    Purpose:
        Supplies a provider-independent health vocabulary for external connectors.
    Responsibilities:
        Identifies disabled, ready, unauthenticated, and unavailable connector state.
    Architectural boundaries:
        Does not determine provider lifecycle state or perform recovery.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``ConnectorHealth`` and implementations of ``SearchConnector``.
    Constraints:
        It describes only the connector's external-service observation.

    A credential failure is its own observation rather than a kind of
    unavailability. Collapsed into ``UNAVAILABLE`` it would be reported as a
    server that is not answering, about a server that is answering and only
    wants a key, and the reader would go looking at the wrong thing. Two states
    rather than one because they are two different actions: a key has to be
    supplied, or a key that exists has to be corrected.
    """

    DISABLED = "disabled"
    READY = "ready"
    NO_CREDENTIAL = "no_credential"
    CREDENTIAL_REJECTED = "credential_rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SearchFile:
    """Represent one file a single source offers, exactly as the service names it.

    Purpose:
        Carries the per-file facts a provider needs to tell two copies apart.
    Responsibilities:
        Holds the service-reported name, size, bitrate, and duration.
    Architectural boundaries:
        Does not parse the name into artist, album, or track, split its directory,
        judge audio quality, or rank one file against another.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SearchSource`` and concrete connector mappers.
    Constraints:
        The name is preserved verbatim, including the backslash separators
        Soulseek reports; ``directory`` and ``basename`` read it without
        replacing it. Values a service omits remain ``None``.
    """

    name: str
    size_bytes: int | None
    bitrate_kbps: int | None
    duration_seconds: int | None
    sample_rate_hz: int | None = None
    bit_depth: int | None = None
    variable_bitrate: bool | None = None

    @property
    def directory(self) -> str:
        """Return the folder this file sits in, as the source spelled it.

        A Soulseek share is browsed by folder and downloaded by folder, so the
        folder is a column of its own wherever these results are shown.
        """
        return _folder_of(self.name)

    @property
    def basename(self) -> str:
        """Return the file's own name, without the folders above it."""
        return _leaf_of(self.name)


@dataclass(frozen=True, slots=True)
class SearchSource:
    """Describe one external source and everything it offered for a search.

    Purpose:
        Mirrors how an acquisition service actually answers: one respondent at a
        time, carrying its own files.
    Responsibilities:
        Preserves an opaque source identifier, its transfer observations, and the
        files it reported, without interpretation.
    Architectural boundaries:
        Does not represent a DigLibrary provider, account, artist, or candidate
        score, and never groups files into a release.
    Dependencies:
        Depends only on the neutral models in this module.
    Expected collaborators:
        ``SearchResponse`` and concrete connector mappers.
    Constraints:
        Availability and queue state belong to the source rather than to each of
        its files, which is where the service reports them. All values are
        observations only; unsupported values remain ``None``.
    """

    identifier: str
    transfer_rate: int | None
    queue_depth: int | None
    availability: SearchAvailability
    queue_state: SearchQueueState
    files: tuple[SearchFile, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Represent one opaque high-level search submitted through a connector.

    Purpose:
        Provides a reusable immutable request model without service protocol details.
    Responsibilities:
        Carries caller query text, search kind, and an optional bounded result limit.
    Architectural boundaries:
        Does not contain URLs, headers, authentication, payload fields, or music entities.
    Dependencies:
        Depends only on ``SearchKind``.
    Expected collaborators:
        ``SearchConnector`` implementations and concrete connector clients.
    Constraints:
        Query text is never normalized or interpreted at this abstraction boundary.
    """

    query: str
    kind: SearchKind
    result_limit: int


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """Represent the unranked sources that answered one connector search request.

    Purpose:
        Returns a stable connector-neutral result envelope to provider consumers.
    Responsibilities:
        Groups an opaque request correlation identifier with the original request
        and every source that answered it.
    Architectural boundaries:
        Does not expose HTTP, JSON, service payloads, selection, or transfer control.
    Dependencies:
        Depends only on ``SearchRequest`` and ``SearchSource``.
    Expected collaborators:
        ``SearchConnector`` implementations and future provider adapters.
    Constraints:
        The shape follows the service: sources answer, and each carries its own
        files. Results are a point-in-time observation and are not a
        decision-engine input directly.
    """

    request_id: str
    request: SearchRequest
    sources: tuple[SearchSource, ...]

    @property
    def file_count(self) -> int:
        """Return how many files the sources offered in total."""
        return sum(len(source.files) for source in self.sources)


@dataclass(frozen=True, slots=True)
class SearchProgress:
    """Report how much of a search has arrived while it is still running.

    Purpose:
        Lets a caller say "twenty answered so far" instead of showing a mute
        spinner for the seconds a Soulseek search takes.
    Responsibilities:
        Carries how many sources and files the service is holding, and whether
        it considers the search over.
    Architectural boundaries:
        Does not decide that a search has gathered enough, stop one, or read the
        results it counts.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SearchWatch`` implementations and concrete connector mappers.
    Constraints:
        These counts are the service's own, observed mid-flight; the files a
        search returns are read separately and may differ by the last arrival.
    """

    sources: int
    files: int
    finished: bool


class TransferState(StrEnum):
    """Represent where one requested file has got to, in connector-neutral words.

    Purpose:
        Lets a provider follow a transfer without learning a service's own state
        vocabulary.
    Responsibilities:
        Distinguishes waiting, moving, finished, failed, and cancelled.
    Architectural boundaries:
        Does not retry, cancel, reorder, or decide what a failure means.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``Transfer`` and concrete connector mappers.
    Constraints:
        A state a service words in a way this vocabulary does not cover becomes
        ``UNKNOWN`` rather than being forced into a neighbouring meaning.
    """

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def has_stopped(self) -> bool:
        """Whether this transfer has reached an end it will not move from.

        Three states are ends, and this is the only place that says so. Every
        caller that needs the other side asks for the complement rather than
        listing what it believes is moving. With two hand-written lists,
        `UNKNOWN` belongs to neither, and a state slskd words in a way this
        vocabulary does not cover would let the machine sleep in the middle of
        a download, which `ui/wakefulness.py` exists to prevent.
        """
        return self in _ENDED

    @property
    def is_moving(self) -> bool:
        """Whether something is still expected to arrive under this state.

        The complement, deliberately: a state not covered by this vocabulary
        counts as moving, so the cost of meeting one is a machine that stayed
        awake needlessly rather than one that slept through a download.
        """
        return not self.has_stopped


_ENDED = frozenset({TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED})
"""The ends. Named once, below the enum that owns them, and read by nothing but
the two properties above, so there is no second copy of this set to disagree."""


@dataclass(frozen=True, slots=True)
class TransferFile:
    """Name one file a caller wants fetched from one source.

    Purpose:
        Carries the minimum a service needs to enqueue a file.
    Responsibilities:
        Holds the remote name and the size the source reported for it.
    Architectural boundaries:
        Does not choose the file, decide where it lands, or start anything.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``TransferRequest`` and implementations of ``TransferConnector``.
    Constraints:
        The name must be the one the source published, unaltered, or the source
        will not recognise what is being asked of it.
    """

    name: str
    size_bytes: int | None = None

    @property
    def directory(self) -> str:
        """Return the folder this file sits in, as the source spelled it."""
        return _folder_of(self.name)

    @property
    def basename(self) -> str:
        """Return the file's own name, without the folders above it."""
        return _leaf_of(self.name)


@dataclass(frozen=True, slots=True)
class TransferRequest:
    """Ask one source for a set of its files.

    Purpose:
        Expresses the unit a caller actually acts on — in a Soulseek share, a
        folder's worth of files from one user.
    Responsibilities:
        Pairs a source identifier with the files requested from it.
    Architectural boundaries:
        Does not rank sources, split a request across sources, or retry.
    Dependencies:
        Depends only on ``TransferFile``.
    Expected collaborators:
        Implementations of ``TransferConnector``.
    Constraints:
        Every file must come from the named source; one request never spans two.
    """

    source: str
    files: tuple[TransferFile, ...]


@dataclass(frozen=True, slots=True)
class Transfer:
    """Report where one requested file has got to.

    Purpose:
        Gives a provider a stable way to follow a transfer it asked for.
    Responsibilities:
        Carries the service's handle for the transfer, its source, its remote
        name, its state, and how much has moved.
    Architectural boundaries:
        Does not act on the transfer, interpret a failure, or touch a file on disk.
    Dependencies:
        Depends only on ``TransferState``.
    Expected collaborators:
        Implementations of ``TransferConnector`` and future provider adapters.
    Constraints:
        The identifier is opaque and belongs to the service; it is the only way
        to name this transfer again.
    """

    identifier: str
    source: str
    name: str
    state: TransferState
    size_bytes: int | None = None
    transferred_bytes: int | None = None
    requested_at: str | None = None
    """When the service says this was asked for, in the service's own wording.

    Kept as the service spelled it and never parsed here: it exists to put a
    list of transfers in the order they were asked for, and comparing the
    strings does that for any format that sorts — which ISO 8601 does. A
    service that does not say goes last rather than being given a time.
    """

    average_speed: float | None = None
    """How fast this is moving, as the service measures it, in bytes per second."""

    remaining_time: str | None = None
    """How long the service thinks is left, in its own wording — never computed here."""

    place_in_queue: int | None = None
    """Where this waits in the source's queue, when the source says at all.

    Often it does not. A Soulseek peer answers this only while the transfer is
    actually queued with it, so absent is the ordinary case and not a fault.

    New fields go after ``requested_at``, not before it: this value is
    constructed positionally in places, and inserting ahead of it silently
    slides a timestamp into a float.
    """

    @property
    def directory(self) -> str:
        """Return the folder this file came from, as the source spelled it."""
        return _folder_of(self.name)

    @property
    def basename(self) -> str:
        """Return the file's own name, without the folders above it."""
        return _leaf_of(self.name)


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """Represent a connector-neutral external-service health observation.

    Purpose:
        Gives providers a typed readiness result without revealing service protocol details.
    Responsibilities:
        Records connector state and optional service version information.
    Architectural boundaries:
        Does not coordinate provider lifecycle or classify provider availability policy.
    Dependencies:
        Depends only on ``ConnectorHealthState``.
    Expected collaborators:
        ``SearchConnector.initialize`` and ``SearchConnector.health`` implementations.
    Constraints:
        A disabled connector returns ``DISABLED`` without invoking its external service.
    """

    state: ConnectorHealthState
    service_version: str | None = None


def _folder_of(name: str) -> str:
    """Return the folder part of a remote name, or nothing when it has none.

    Soulseek shares are Windows-shaped and separate folders with a backslash,
    which is an ordinary character in a name on the systems this project runs
    on. Reading it here keeps that knowledge in one place.
    """
    return name.rsplit("\\", 1)[0] if "\\" in name else ""


def _leaf_of(name: str) -> str:
    """Return the last segment of a remote name."""
    return name.rsplit("\\", 1)[-1]
