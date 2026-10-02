"""High-level, provider-independent operations for a running slskd instance."""

import logging

from diglibrary.connectors.contracts import SearchConnector, SearchWatch, TransferConnector
from diglibrary.connectors.models import (
    ConnectorHealth,
    ConnectorHealthState,
    SearchFile,
    SearchKind,
    SearchRequest,
    SearchResponse,
    Transfer,
    TransferRequest,
)
from diglibrary.connectors.slskd.client import SlskdClient
from diglibrary.connectors.slskd.configuration import SlskdConfiguration
from diglibrary.connectors.slskd.exceptions import (
    SlskdAuthenticationError,
    SlskdConnectorError,
    SlskdCredentialMissingError,
    SlskdServiceUnreachableError,
)
from diglibrary.connectors.slskd.mapper import SlskdMapper


class SlskdConnector(SearchConnector, TransferConnector):
    """Expose typed slskd operations without carrying provider or business behaviour.

    Purpose:
        Is the reusable integration boundary consumed by the Soulseek provider.
    Responsibilities:
        Coordinates connector initialization, shutdown, health requests, and opaque album
        or track searches through the injected client and mapper.
    Architectural boundaries:
        Does not implement provider lifecycle, candidate selection, downloads, metadata,
        library changes, audio analysis, or decision-engine rules.
    Dependencies:
        Depends on an injected ``SlskdClient``, ``SlskdMapper``, configuration, and logger.
    Expected collaborators:
        The application composition root, future provider adapters, and no Engines directly.
    Constraints:
        Returns immutable connector transport models and never exposes raw HTTP responses.
    """

    def __init__(
        self,
        configuration: SlskdConfiguration,
        client: SlskdClient,
        mapper: SlskdMapper,
        logger: logging.Logger,
    ) -> None:
        """Create a connector from already-assembled infrastructure collaborators."""
        self._configuration = configuration
        self._client = client
        self._mapper = mapper
        self._logger = logger
        self._initialized = False

    def initialize(self) -> ConnectorHealth:
        """Verify the configured service when enabled and mark this connector initialized."""
        self._logger.info(
            "Initializing slskd connector.", extra={"operation": "connector.slskd.initialize"}
        )
        health = self.health()
        self._initialized = health.state is ConnectorHealthState.READY
        return health

    def shutdown(self) -> None:
        """Release connector-local lifecycle state without controlling the external service."""
        self._initialized = False
        self._logger.info(
            "slskd connector shut down.", extra={"operation": "connector.slskd.shutdown"}
        )

    def health(self) -> ConnectorHealth:
        """Return typed reachability information for the configured slskd instance.

        **What someone can act on is a state; what is simply broken still
        raises.** A credential failure and a silent service are ordinary
        configured conditions a screen has to explain — the same reason a
        disabled connector is one. Raised as exceptions, all three could only
        be reported under one word, and a running server that wanted a key
        would be reported as a server that could not be reached. A malformed
        payload or a violated protocol is nothing to act on from a settings
        screen, so it is still raised and still says what it was.
        """
        if not self._configuration.enabled:
            self._logger.info(
                "slskd connector is disabled.", extra={"operation": "connector.slskd.health"}
            )
            return ConnectorHealth(ConnectorHealthState.DISABLED)
        try:
            health = self._mapper.health(self._client.health_payload())
        except SlskdCredentialMissingError:
            self._logger.warning(
                "No slskd API key is available, so slskd cannot be asked anything.",
                extra={"operation": "connector.slskd.no_credential"},
            )
            return ConnectorHealth(ConnectorHealthState.NO_CREDENTIAL)
        except SlskdAuthenticationError:
            self._logger.warning(
                "slskd rejected the API key it was given.",
                extra={"operation": "connector.slskd.credential_rejected"},
            )
            return ConnectorHealth(ConnectorHealthState.CREDENTIAL_REJECTED)
        except SlskdServiceUnreachableError:
            self._logger.warning(
                "slskd did not answer.",
                extra={"operation": "connector.slskd.unavailable"},
            )
            return ConnectorHealth(ConnectorHealthState.UNAVAILABLE)
        except SlskdConnectorError:
            self._logger.error(
                "slskd health check failed.", extra={"operation": "connector.slskd.failure"}
            )
            raise
        self._logger.info(
            "slskd health check completed.", extra={"operation": "connector.slskd.health"}
        )
        return health

    def search_album(self, query: str, watch: SearchWatch | None = None) -> SearchResponse:
        """Submit an opaque album-oriented search query to slskd."""
        return self._search(query, SearchKind.ALBUM, watch)

    def search_track(self, query: str, watch: SearchWatch | None = None) -> SearchResponse:
        """Submit an opaque track-oriented search query to slskd."""
        return self._search(query, SearchKind.TRACK, watch)

    def directory(self, source: str, directory: str) -> tuple[SearchFile, ...]:
        """Return everything one source holds in one of its folders."""
        self._require_enabled()
        if not source.strip() or not directory.strip():
            raise ValueError("Reading a folder needs the source and the folder.")
        try:
            payload = self._client.directory_payload(source, directory)
        except SlskdConnectorError:
            self._logger.info(
                "slskd could not read that folder from its source.",
                extra={"operation": "connector.slskd.browse"},
            )
            raise
        return self._mapper.directory(payload, directory)

    def forget(self, request_id: str) -> None:
        """Drop one search from slskd's own list, so a tab leaves nothing behind."""
        self._require_enabled()
        self._client.forget_search(request_id)

    def _search(
        self, query: str, kind: SearchKind, watch: SearchWatch | None = None
    ) -> SearchResponse:
        if not self._configuration.enabled:
            raise SlskdConnectorError("slskd connector is disabled.")
        if not query.strip():
            raise ValueError("slskd search query must not be blank.")
        request = SearchRequest(query, kind, self._configuration.response_limit)
        try:
            created, responses = self._client.search_payloads(request, watch)
            mapped = self._mapper.search(request, created, responses)
        except SlskdConnectorError:
            self._logger.error(
                "slskd search failed.", extra={"operation": "connector.slskd.failure"}
            )
            raise
        self._logger.info("slskd search completed.", extra={"operation": "connector.slskd.search"})
        return mapped

    def enqueue(self, request: TransferRequest) -> tuple[Transfer, ...]:
        """Ask one source for the named files and report the transfers that resulted.

        The remote names are passed through exactly as the source published
        them: a Soulseek peer answers for the name it offered and for no other
        spelling of it.
        """
        self._require_enabled()
        if not request.source.strip():
            raise ValueError("A transfer needs the source that offered the files.")
        if not request.files:
            raise ValueError("A transfer needs at least one file.")
        payload = [{"filename": file.name, "size": file.size_bytes or 0} for file in request.files]
        try:
            self._client.enqueue_payload(request.source, payload)
        except SlskdConnectorError:
            # One file the service will not take must not cost the rest of the
            # folder. A whole album goes in one request because that is one
            # question to ask, but the request is all-or-nothing at the service
            # and a folder is not: a name already queued, or a file the peer no
            # longer offers, refuses the whole batch without naming the file.
            self._logger.warning(
                "slskd refused the batch; asking for one file at a time.",
                extra={"operation": "connector.slskd.transfer", "files": len(request.files)},
            )
            refused = self._enqueue_one_by_one(request)
            if refused == len(request.files):
                self._logger.error(
                    "slskd took none of the files.",
                    extra={"operation": "connector.slskd.failure"},
                )
                raise
        self._logger.info(
            "slskd transfer requested.", extra={"operation": "connector.slskd.transfer"}
        )
        try:
            queued = self._mapper.transfers(
                request.source, self._client.downloads_payload(request.source)
            )
        except SlskdConnectorError:
            self._logger.error(
                "slskd would not say what it queued.",
                extra={"operation": "connector.slskd.failure"},
            )
            raise
        wanted = {file.name for file in request.files}
        return tuple(transfer for transfer in queued if transfer.name in wanted)

    def _enqueue_one_by_one(self, request: TransferRequest) -> int:
        """Ask for each file on its own, and say how many were refused.

        Only reached when the batch was refused. Every name is tried, because
        which one the service objected to is not knowable from the refusal, and
        stopping at the first would again lose the files after it.
        """
        refused = 0
        for file in request.files:
            try:
                self._client.enqueue_payload(
                    request.source, [{"filename": file.name, "size": file.size_bytes or 0}]
                )
            except SlskdConnectorError:
                refused += 1
                self._logger.warning(
                    "slskd would not take one file of a folder.",
                    extra={"operation": "connector.slskd.transfer", "file": file.basename},
                )
        return refused

    def transfers(self, source: str) -> tuple[Transfer, ...]:
        """Return the current state of every transfer belonging to one source."""
        self._require_enabled()
        try:
            return self._mapper.transfers(source, self._client.downloads_payload(source))
        except SlskdConnectorError:
            self._logger.error(
                "slskd transfer list failed.", extra={"operation": "connector.slskd.failure"}
            )
            raise

    def all_transfers(self) -> tuple[Transfer, ...]:
        """Return every transfer slskd holds, whoever it is coming from."""
        self._require_enabled()
        try:
            return self._mapper.all_transfers(self._client.all_downloads_payload())
        except SlskdConnectorError:
            self._logger.error(
                "slskd download list failed.", extra={"operation": "connector.slskd.failure"}
            )
            raise

    def cancel(self, source: str, identifier: str, remove: bool = False) -> None:
        """Stop one transfer, optionally dropping it from the service's own list."""
        self._require_enabled()
        try:
            self._client.cancel_download(source, identifier, remove)
        except SlskdConnectorError:
            self._logger.error(
                "slskd transfer cancellation failed.",
                extra={"operation": "connector.slskd.failure"},
            )
            raise
        self._logger.info(
            "slskd transfer cancelled.", extra={"operation": "connector.slskd.transfer"}
        )

    def clear_finished(self) -> None:
        """Drop every finished transfer from slskd's list, leaving the files alone."""
        self._require_enabled()
        try:
            self._client.clear_completed_downloads()
        except SlskdConnectorError:
            self._logger.error(
                "slskd could not clear the finished transfers.",
                extra={"operation": "connector.slskd.failure"},
            )
            raise
        self._logger.info(
            "slskd finished transfers cleared.", extra={"operation": "connector.slskd.transfer"}
        )

    def destination(self) -> str | None:
        """Return the folder slskd drops a finished download into."""
        self._require_enabled()
        try:
            return self._mapper.destination(self._client.options_payload())
        except SlskdConnectorError:
            self._logger.error(
                "slskd settings could not be read.",
                extra={"operation": "connector.slskd.failure"},
            )
            raise

    def _require_enabled(self) -> None:
        if not self._configuration.enabled:
            raise SlskdConnectorError("slskd connector is disabled.")
