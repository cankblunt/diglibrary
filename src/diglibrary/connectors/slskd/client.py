"""Dedicated, standard-library HTTP client for the slskd connector."""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from diglibrary.connectors.contracts import SearchWatch
from diglibrary.connectors.models import SearchProgress, SearchRequest
from diglibrary.connectors.slskd.configuration import (
    SlskdAuthenticationMode,
    SlskdConfiguration,
)
from diglibrary.connectors.slskd.exceptions import (
    SlskdAuthenticationError,
    SlskdConnectorError,
    SlskdCredentialMissingError,
    SlskdMalformedResponseError,
    SlskdNotFoundError,
    SlskdProtocolViolationError,
    SlskdRetryExhaustedError,
    SlskdSearchAbandonedError,
    SlskdTimeoutError,
    SlskdUnavailableError,
)

type JsonValue = dict[str, Any] | list[Any]

_HANDOVER_STEP_SECONDS = 0.1
"""How long to leave between reads while a stopped search hands its answers over.

On a running slskd the responses are not served at the instant the stop returns;
they appear a few tens of milliseconds later.
"""

_HANDOVER_ALLOWANCE_SECONDS = 2.0
"""The longest to wait for a stopped search to serve what it says it holds.

A ceiling on an anomaly, not an ordinary wait: the read returns as soon as the
served count reaches the counted one, or stops growing short of it.
"""


@dataclass(frozen=True, slots=True)
class SlskdHttpResponse:
    """A raw HTTP result confined to the slskd client implementation boundary.

    Purpose:
        Allows injected transports to return status and bytes without depending on urllib.
    Responsibilities:
        Carries only the data the client needs for protocol validation and JSON parsing.
    Architectural boundaries:
        Is not returned by the client or connector public APIs.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SlskdHttpTransport`` implementations and ``SlskdClient``.
    Constraints:
        Must remain internal transport data and never be exposed to provider code.
    """

    status: int
    body: bytes


class SlskdHttpTransport(Protocol):
    """Protocol for one injectable slskd HTTP request implementation.

    Purpose:
        Separates request execution from slskd API behaviour for deterministic testing.
    Responsibilities:
        Sends one already-formed HTTP request and returns its status and byte body.
    Architectural boundaries:
        Does not authenticate, retry, decode JSON, or model slskd operations.
    Dependencies:
        Depends only on ``SlskdHttpResponse`` and standard collection abstractions.
    Expected collaborators:
        ``UrllibSlskdTransport`` in production and fakes in unit tests.
    Constraints:
        Callers must translate transport failures before they cross the connector boundary.
    """

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> SlskdHttpResponse:
        """Execute a single HTTP request."""


class UrllibSlskdTransport:
    """Standard-library HTTP transport used exclusively by ``SlskdClient``.

    Purpose:
        Performs the low-level HTTP operation without adding a runtime dependency.
    Responsibilities:
        Converts urllib's HTTP error response into a neutral response value.
    Architectural boundaries:
        Does not contain slskd routes, authentication, retries, JSON decoding, or mapping.
    Dependencies:
        Depends only on ``urllib`` and ``SlskdHttpResponse``.
    Expected collaborators:
        Constructed by the composition root and invoked by ``SlskdClient``.
    Constraints:
        Connection and timeout exceptions are deliberately left for client translation.
    """

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> SlskdHttpResponse:
        """Execute a request while preserving HTTP error statuses for the client."""
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return SlskdHttpResponse(status=response.status, body=response.read())
        except HTTPError as error:
            # Closed like the success path. `HTTPError` *is* a response object
            # holding a socket, and this client retries — so without the close,
            # a slskd answering 401 or 503 leaves one open socket per attempt.
            with error:
                return SlskdHttpResponse(status=error.code, body=error.read())


class SlskdClient:
    """Execute authenticated, retried, validated slskd API requests.

    Purpose:
        Centralizes all HTTP infrastructure required by the reusable slskd connector.
    Responsibilities:
        Builds authenticated requests, applies bounded retries, enforces timeouts, validates
        HTTP and JSON responses, logs operations, and translates low-level failures.
    Architectural boundaries:
        Does not map payloads to connector models, know providers, or implement acquisition.
    Dependencies:
        Depends on an injected transport, immutable configuration, logger, and secret lookup.
    Expected collaborators:
        The composition root, ``SlskdConnector``, ``SlskdMapper``, and unit-test fakes.
    Constraints:
        Raw HTTP responses never leave this class; credentials are never logged or retained.
    """

    def __init__(
        self,
        configuration: SlskdConfiguration,
        transport: SlskdHttpTransport,
        logger: logging.Logger,
        environment: Callable[[str], str | None],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the client from injected infrastructure dependencies."""
        self._configuration = configuration
        self._transport = transport
        self._logger = logger
        self._environment = environment
        self._sleep = sleep

    def health_payload(self) -> Mapping[str, Any]:
        """Fetch the slskd application payload used by connector health mapping."""
        payload = self._request_json("GET", "/application", None, "connector.slskd.health")
        return _object_payload(payload)

    def search_payloads(
        self, request: SearchRequest, watch: SearchWatch | None = None
    ) -> tuple[Mapping[str, Any], JsonValue]:
        """Create a daemon search, wait for the answers to arrive, and read them.

        A Soulseek search is answered by peers over several seconds, and slskd
        serves none of those answers until the search is over — so the wait is
        the result rather than a delay in front of it. What ends the wait is
        described on ``_gather``.
        """
        created = self._request_json(
            "POST",
            "/searches",
            {"searchText": request.query, "responseLimit": request.result_limit},
            "connector.slskd.search",
        )
        created_object = _object_payload(created)
        search_id = _required_string(created_object, "id")
        expected = self._gather(search_id, watch)
        return created_object, self._read_responses(search_id, expected)

    def _read_responses(self, search_id: str, expected: int) -> JsonValue:
        """Read the responses, and do not mistake the handover for an empty network.

        Ending a search flips its state before its responses are served: a
        read made immediately after the stop sees the state ``Completed,
        Cancelled`` and no responses, and a read a moment later sees them. An
        empty answer at that instant would be reported as a search nobody
        answered, so what is served is compared with the count the service
        itself reports rather than trusted after a guessed pause. The wait ends
        when the two agree, or when the served count stops growing, because a
        service may serve slightly fewer responses than it counts.
        """
        payload = self._responses_payload(search_id)
        served = _served_count(payload)
        if served >= expected:
            return payload
        waited = 0.0
        while waited < _HANDOVER_ALLOWANCE_SECONDS:
            self._sleep(_HANDOVER_STEP_SECONDS)
            waited += _HANDOVER_STEP_SECONDS
            again = self._responses_payload(search_id)
            count = _served_count(again)
            if count >= expected or (count > 0 and count == served):
                return again
            payload, served = again, count
        self._logger.warning(
            "slskd served fewer responses than it counted.",
            extra={"operation": "connector.slskd.search"},
        )
        return payload

    def _responses_payload(self, search_id: str) -> JsonValue:
        return self._request_json(
            "GET",
            f"/searches/{quote(search_id, safe='')}/responses",
            None,
            "connector.slskd.search",
        )

    def stop_search(self, search_id: str) -> None:
        """End one search at the service, which is what makes it readable.

        On a running instance this answers 200 with no body, the state becomes
        ``Completed, Cancelled``, and the responses gathered so far are served
        from then on.
        """
        self._request_json(
            "PUT",
            f"/searches/{quote(search_id, safe='')}",
            None,
            "connector.slskd.search",
            allow_empty=True,
        )

    def forget_search(self, search_id: str) -> None:
        """Drop one search from the service's own list.

        The service answers 204 with no body, and a later read of the search
        answers 404. A search already forgotten is not an error here, because
        the only reason to ask is to be rid of it.
        """
        try:
            self._request_json(
                "DELETE",
                f"/searches/{quote(search_id, safe='')}",
                None,
                "connector.slskd.search",
                allow_empty=True,
            )
        except SlskdNotFoundError:
            return

    def search_state_payload(self, search_id: str) -> Mapping[str, Any]:
        """Fetch what the service is holding for one search, mid-flight."""
        return _object_payload(
            self._request_json(
                "GET", f"/searches/{quote(search_id, safe='')}", None, "connector.slskd.search"
            )
        )

    def enqueue_payload(self, source: str, files: Sequence[Mapping[str, object]]) -> JsonValue:
        """Ask one source for a set of files and return whatever slskd answered.

        slskd accepts the request and answers with no body of its own, so the
        caller reads the resulting transfers back through ``downloads_payload``.
        """
        return self._request_json(
            "POST",
            f"/transfers/downloads/{quote(source, safe='')}",
            list(files),
            "connector.slskd.transfer",
            allow_empty=True,
        )

    def options_payload(self) -> Mapping[str, Any]:
        """Fetch the settings slskd is running with, which own the download folder."""
        return _object_payload(
            self._request_json("GET", "/options", None, "connector.slskd.options")
        )

    def downloads_payload(self, source: str) -> JsonValue:
        """Fetch every download slskd currently holds for one source.

        A source this instance has never fetched from answers 404, which is the
        ordinary state before the first transfer and again once the list is
        cleared: the whole list answers 200 with ``[]``, and one unknown source
        answers 404. It means no transfers, not a missing route.
        """
        try:
            return self._request_json(
                "GET",
                f"/transfers/downloads/{quote(source, safe='')}",
                None,
                "connector.slskd.transfer",
            )
        except SlskdNotFoundError:
            return []

    def directory_payload(self, source: str, directory: str) -> JsonValue:
        """Ask one source for everything in one of its folders.

        This is what a Soulseek client does when it offers a folder: a search
        answers with the files that matched the words, and the folder holds the
        rest — the other tracks, the cover, the log. A peer may answer a
        search with a single file out of a folder holding a whole album.
        """
        return self._request_json(
            "POST",
            f"/users/{quote(source, safe='')}/directory",
            {"directory": directory},
            "connector.slskd.browse",
        )

    def all_downloads_payload(self) -> JsonValue:
        """Fetch every download slskd holds, from every source.

        A different shape from the per-source route: this answers an array of
        ``{username, directories}`` objects,
        where ``/transfers/downloads/{user}`` answers one bare ``{directories}``.
        """
        return self._request_json("GET", "/transfers/downloads", None, "connector.slskd.transfer")

    def clear_completed_downloads(self) -> None:
        """Drop every finished transfer from the service's list.

        The service answers 204 with no body, and the list comes back empty.
        It clears the *list*; the files it finished moving are on disk and are
        not touched.
        """
        self._request_json(
            "DELETE",
            "/transfers/downloads/all/completed",
            None,
            "connector.slskd.transfer",
            allow_empty=True,
        )

    def cancel_download(self, source: str, identifier: str, remove: bool) -> None:
        """Stop one transfer, optionally dropping it from the service's own list."""
        self._request_json(
            "DELETE",
            f"/transfers/downloads/{quote(source, safe='')}/{quote(identifier, safe='')}"
            f"?remove={'true' if remove else 'false'}",
            None,
            "connector.slskd.transfer",
            allow_empty=True,
        )

    def _gather(self, search_id: str, watch: SearchWatch | None) -> int:
        """Wait for the answers to arrive, then end the search so they can be read.

        Returns how many respondents the service says it is holding, which is
        what tells the reader afterwards whether an empty answer is real.

        **The wait is not an optimisation; it is the whole result.** slskd hands
        over a search's responses only once that search is over: while it is in
        progress the endpoint answers with an empty list, however many peers
        have already replied. In state ``InProgress`` the responses it holds
        are not returned; in state ``Completed`` they are.

        **Ending the search is what unlocks it**, and slskd is slow to end one
        by itself: a search can hold every respondent it will ever have within
        a few seconds and stay ``InProgress`` for many more. So the wait ends
        when the counts stop moving, and the search is stopped deliberately —
        nearly the same answer in a fraction of the time, at the cost of the
        few respondents that arrive late.

        Two things are left waiting deliberately. A search that has heard from
        nobody at all is never cut short on quietness, because "nothing yet" and
        "nothing coming" are the same reading and only one of them is worth
        acting on; it waits for the whole allowance. And a caller that has
        abandoned the search stops it at the service, because the point of
        closing a tab is that it stops costing the network something.

        A state that cannot be read at all is different, and left alone: the
        caller learns what was found rather than why one poll failed.
        """
        poll = self._configuration.search_poll_seconds
        quiet_needed = self._configuration.search_quiet_seconds
        deadline = self._configuration.search_settle_seconds
        waited = 0.0
        unchanged_for = 0.0
        held = (0, 0)
        while waited < deadline:
            if watch is not None and watch.abandoned():
                # Stopped and then dropped: a closed tab should cost the network
                # nothing and leave nothing behind in the slskd instance.
                self.stop_search(search_id)
                self.forget_search(search_id)
                raise SlskdSearchAbandonedError("The search was closed before it was read.")
            try:
                payload = self.search_state_payload(search_id)
            except SlskdConnectorError:
                self._logger.warning(
                    "slskd search state could not be read; reading responses anyway.",
                    extra={"operation": "connector.slskd.search"},
                )
                return held[0]
            counts = (_count(payload, "responseCount"), _count(payload, "fileCount"))
            state = payload.get("state")
            finished = not isinstance(state, str) or "InProgress" not in state
            if watch is not None:
                watch.observed(SearchProgress(counts[0], counts[1], finished))
            if finished:
                return counts[0]
            unchanged_for = unchanged_for + poll if counts == held else 0.0
            held = counts
            if counts[0] > 0 and unchanged_for >= quiet_needed:
                self._logger.info(
                    "slskd search went quiet; ending it to read what answered.",
                    extra={"operation": "connector.slskd.search"},
                )
                self.stop_search(search_id)
                return counts[0]
            self._sleep(poll)
            waited += poll
        self._logger.warning(
            "slskd was still gathering answers when the allowance ran out; ending it.",
            extra={"operation": "connector.slskd.search"},
        )
        self.stop_search(search_id)
        return held[0]

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | Sequence[Mapping[str, object]] | None,
        operation: str,
        allow_empty: bool = False,
    ) -> JsonValue:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers(body is not None)
        url = f"{self._configuration.base_url.rstrip('/')}{self._configuration.api_base_path}{path}"
        attempts = self._configuration.retry_limit + 1

        for attempt in range(1, attempts + 1):
            try:
                response = self._transport.request(
                    method, url, headers, body, self._configuration.timeout_seconds
                )
                return self._validated_json(response, operation, allow_empty)
            except SlskdConnectorError as error:
                if not _is_retryable(error) or attempt == attempts:
                    if _is_retryable(error) and attempts > 1:
                        raise SlskdRetryExhaustedError(
                            "slskd request retries were exhausted."
                        ) from error
                    raise
                self._logger.warning(
                    "slskd request failed; retrying.",
                    extra={"operation": "connector.slskd.retry"},
                )
                self._sleep(0.1 * attempt)
            except TimeoutError as error:
                failure = SlskdTimeoutError("slskd request timed out.")
                if attempt == attempts:
                    if attempts > 1:
                        raise SlskdRetryExhaustedError(
                            "slskd request retries were exhausted."
                        ) from error
                    raise failure from error
                self._log_retry_and_sleep(attempt)
            except URLError as error:
                failure = SlskdUnavailableError("slskd service is unavailable.")
                if attempt == attempts:
                    if attempts > 1:
                        raise SlskdRetryExhaustedError(
                            "slskd request retries were exhausted."
                        ) from error
                    raise failure from error
                self._log_retry_and_sleep(attempt)
        raise AssertionError("Retry loop exited unexpectedly.")

    def _headers(self, has_body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        if self._configuration.authentication_mode is SlskdAuthenticationMode.API_KEY:
            api_key = self._environment(self._configuration.api_key_environment_variable)
            if not api_key:
                # Its own type, because nothing was sent and nothing was refused:
                # the answer to this is to supply a key, and the answer to a
                # rejection is to correct one.
                raise SlskdCredentialMissingError(
                    "slskd API key is not available in the environment."
                )
            headers["X-API-Key"] = api_key
            self._logger.debug(
                "slskd API-key authentication configured.",
                extra={"operation": "connector.slskd.authentication"},
            )
        return headers

    def _validated_json(
        self, response: SlskdHttpResponse, operation: str, allow_empty: bool = False
    ) -> JsonValue:
        if response.status in {401, 403}:
            raise SlskdAuthenticationError("slskd rejected the configured authentication.")
        if response.status in {408, 504}:
            raise SlskdTimeoutError("slskd request timed out.")
        if response.status == 404:
            raise SlskdNotFoundError("slskd reports nothing at that address.")
        if response.status < 200 or response.status >= 300:
            if response.status == 429 or response.status >= 500:
                raise SlskdUnavailableError("slskd returned a transient service failure.")
            raise SlskdProtocolViolationError(
                f"slskd returned unexpected HTTP status {response.status}."
            )
        if allow_empty and not response.body.strip():
            # slskd accepts a queued transfer and a cancellation with no body of
            # its own; the caller reads the result back through the transfer list.
            self._logger.info("slskd request completed.", extra={"operation": operation})
            return {}
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SlskdMalformedResponseError("slskd returned invalid JSON.") from error
        if not isinstance(decoded, (dict, list)):
            raise SlskdMalformedResponseError("slskd returned an unsupported JSON payload.")
        self._logger.info("slskd request completed.", extra={"operation": operation})
        return decoded

    def _log_retry_and_sleep(self, attempt: int) -> None:
        self._logger.warning(
            "slskd request failed; retrying.", extra={"operation": "connector.slskd.retry"}
        )
        self._sleep(0.1 * attempt)


def _object_payload(payload: JsonValue) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise SlskdMalformedResponseError("slskd returned an object where an array was expected.")
    return payload


def _served_count(payload: JsonValue) -> int:
    """Count the respondents a responses payload actually carries."""
    if isinstance(payload, list):
        return len(payload)
    responses = payload.get("responses")
    return len(responses) if isinstance(responses, list) else 0


def _count(payload: Mapping[str, Any], key: str) -> int:
    """Read one of the running totals slskd keeps on a search, absent as nought."""
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SlskdMalformedResponseError(f"slskd response is missing required field {key!r}.")
    return value


def _is_retryable(error: SlskdConnectorError) -> bool:
    return isinstance(error, (SlskdTimeoutError, SlskdUnavailableError))
