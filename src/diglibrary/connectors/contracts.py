"""Provider-facing connector contracts without external-service implementation detail."""

from typing import Protocol, runtime_checkable

from diglibrary.connectors.models import (
    ConnectorHealth,
    SearchFile,
    SearchProgress,
    SearchResponse,
    Transfer,
    TransferRequest,
)


@runtime_checkable
class SearchWatch(Protocol):
    """Follow a search that is still running, and say when to abandon it.

    Purpose:
        A Soulseek search takes seconds that the caller has to be able to show
        and to interrupt, and neither is possible through a call that only
        returns at the end.
    Responsibilities:
        Receives progress observations as they are made, and answers whether the
        caller still wants the search.
    Architectural boundaries:
        Decides nothing about when a search has gathered enough — that is the
        connector's judgement — and never reads results.
    Dependencies:
        Depends only on ``SearchProgress``.
    Expected collaborators:
        The application layer implements it; connectors call it while waiting.
    Constraints:
        Both methods are called from the connector's own thread and must not
        block: whatever they touch has to be safe to touch from there.
    """

    def observed(self, progress: SearchProgress) -> None:
        """Receive what the service is holding right now."""

    def abandoned(self) -> bool:
        """Answer whether the caller has stopped wanting this search."""


@runtime_checkable
class SearchConnector(Protocol):
    """Define the provider-facing search boundary for acquisition connectors.

    Purpose:
        Lets every acquisition provider depend on one stable search abstraction rather than a
        concrete service connector.
    Responsibilities:
        Defines connector lifecycle, health, and opaque album and track search operations.
    Architectural boundaries:
        Excludes transport, HTTP, JSON, authentication, retries, provider lifecycle, candidate
        selection, downloads, metadata, Engines, and Decision Engine concerns.
    Dependencies:
        Depends only on connector-neutral immutable models in ``diglibrary.connectors.models``.
    Expected collaborators:
        Future provider implementations consume this contract; concrete connectors implement it.
    Constraints:
        Providers must not import concrete connector modules, and implementations must return
        only connector-neutral models through this interface.
    """

    def initialize(self) -> ConnectorHealth:
        """Initialize connector-owned resources and return its health observation."""

    def shutdown(self) -> None:
        """Release connector-owned resources without controlling provider lifecycle."""

    def health(self) -> ConnectorHealth:
        """Return an external-service health observation without service protocol details."""

    def search_album(self, query: str, watch: SearchWatch | None = None) -> SearchResponse:
        """Submit an opaque album-oriented query and return unranked connector results."""

    def search_track(self, query: str, watch: SearchWatch | None = None) -> SearchResponse:
        """Submit an opaque track-oriented query and return unranked connector results."""

    def directory(self, source: str, directory: str) -> tuple[SearchFile, ...]:
        """Return everything one source holds in one of its folders.

        A search answers with the files that matched the words, which is rarely
        the folder: the other tracks are in it, and so are the cover and the log
        that say where the rip came from. Asking the source is the only way to
        learn the rest, and a source that is no longer online cannot be asked.
        """

    def forget(self, request_id: str) -> None:
        """Drop one finished search from the external service's own list.

        A window that opens a tab per search would otherwise leave every one
        of them behind in the service's own list, which belongs to whoever runs
        that service.
        """


@runtime_checkable
class TransferConnector(Protocol):
    """Define the provider-facing boundary for fetching files a search found.

    Purpose:
        Lets every acquisition provider ask for files and follow them without
        learning one service's transfer protocol.
    Responsibilities:
        Defines enqueueing a set of files from one source, reading the state of
        that source's transfers, and cancelling one.
    Architectural boundaries:
        Excludes transport, HTTP, JSON, authentication, retries, provider
        lifecycle, candidate selection, where files land on disk, audio
        analysis, and library changes.
    Dependencies:
        Depends only on connector-neutral immutable models in
        ``diglibrary.connectors.models``.
    Expected collaborators:
        Provider implementations consume this contract; concrete connectors
        implement it.
    Constraints:
        A request never spans two sources, remote names are passed through
        unaltered, and an implementation never decides to retry a failure on the
        caller's behalf.
    """

    def enqueue(self, request: TransferRequest) -> tuple[Transfer, ...]:
        """Ask one source for the named files and return the transfers created."""

    def transfers(self, source: str) -> tuple[Transfer, ...]:
        """Return the current state of every transfer belonging to one source."""

    def all_transfers(self) -> tuple[Transfer, ...]:
        """Return every transfer this service holds, from every source.

        Separate from ``transfers`` because a screen showing what is downloading
        cannot name the sources in advance — it is asking precisely in order to
        find out which they are.
        """

    def cancel(self, source: str, identifier: str, remove: bool = False) -> None:
        """Stop one transfer, optionally dropping it from the service's list."""

    def clear_finished(self) -> None:
        """Drop every finished transfer from the service's list.

        It clears a list, not a disk: what finished moving stays where the
        service put it.
        """

    def destination(self) -> str | None:
        """Return where finished transfers land, as the service reports it.

        The service owns this setting, so asking it is the only way to be right
        about it; a copy kept here would go stale as soon as the folder was
        changed in the service. ``None`` when the service does not say.
        """
