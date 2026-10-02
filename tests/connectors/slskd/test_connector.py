"""Unit tests for the slskd connector with fully mocked HTTP operations.

The payloads here are written by hand, from the shapes used by clients that talk
to slskd and work. That is a reading of other code, not a measurement: a fixture
that repeats the mapper's own misreading keeps the suite green over a mapper
that cannot parse a real payload.

``test_recorded_payloads.py`` is the one that answers to the running service.
Record with ``tools/capture_slskd_payloads.py`` and it stops skipping.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.error import URLError

import pytest

from diglibrary.connectors import (
    ConnectorHealthState,
    SearchAvailability,
    SearchConnector,
    SearchKind,
    SearchQueueState,
)
from diglibrary.connectors.models import SearchProgress
from diglibrary.connectors.slskd.client import SlskdClient, SlskdHttpResponse
from diglibrary.connectors.slskd.configuration import (
    SlskdAuthenticationMode,
    SlskdConfiguration,
)
from diglibrary.connectors.slskd.connector import SlskdConnector
from diglibrary.connectors.slskd.exceptions import (
    SlskdAuthenticationError,
    SlskdMalformedResponseError,
    SlskdRetryExhaustedError,
    SlskdSearchAbandonedError,
    SlskdTimeoutError,
)
from diglibrary.connectors.slskd.mapper import SlskdMapper


class FakeWatch:
    """Follow a search the way the window does, and abandon it on cue."""

    def __init__(self, abandon_after: int | None = None) -> None:
        """Create a watch that gives up after a set number of observations."""
        self.seen: list[SearchProgress] = []
        self._abandon_after = abandon_after
        self._asked = 0

    def observed(self, progress: SearchProgress) -> None:
        self.seen.append(progress)

    def abandoned(self) -> bool:
        self._asked += 1
        return self._abandon_after is not None and self._asked > self._abandon_after


class FakeTransport:
    """Record request values and return queued responses or raised transport failures."""

    def __init__(self, outcomes: list[SlskdHttpResponse | BaseException]) -> None:
        """Create a deterministic fake with one outcome per HTTP request."""
        self._outcomes = outcomes
        self.requests: list[tuple[str, str, Mapping[str, str], bytes | None, float]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> SlskdHttpResponse:
        """Return the next configured outcome without opening a network connection."""
        self.requests.append((method, url, headers, body, timeout_seconds))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_initialize_and_health_use_authenticated_application_request() -> None:
    """Initialization maps health and sends the configured API key without logging it."""
    transport = FakeTransport([_response({"version": "0.25.1"})])
    connector = _connector(transport)

    health = connector.initialize()

    assert isinstance(connector, SearchConnector)
    assert health.state is ConnectorHealthState.READY
    assert health.service_version == "0.25.1"
    assert transport.requests[0][0:2] == ("GET", "http://slskd.test/api/v0/application")
    assert transport.requests[0][2]["X-API-Key"] == "secret"


def test_disabled_connector_avoids_http_for_health() -> None:
    """A disabled connector returns a disabled health value without contacting slskd."""
    transport = FakeTransport([])
    connector = _connector(transport, enabled=False)

    assert connector.health().state is ConnectorHealthState.DISABLED
    assert transport.requests == []


def test_search_album_maps_the_shape_slskd_actually_answers_with() -> None:
    """One respondent per entry, and its files inside it.

    slskd answers a search with an object per user carrying a ``files`` array;
    the name of a file is never a field of the response itself. Reading
    ``filename`` off the response rejects every real payload as malformed.
    """
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "Completed, ResponseLimitReached"}),
            _response(
                [
                    {
                        "username": "peer",
                        "uploadSpeed": 456,
                        "queueLength": 0,
                        "hasFreeUploadSlot": True,
                        "files": [
                            {
                                "filename": "@@music\\Artist\\Album\\01.flac",
                                "size": 1234,
                                "bitRate": 1004,
                                "length": 245,
                            },
                            {"filename": "@@music\\Artist\\Album\\02.flac", "size": 4321},
                        ],
                    }
                ]
            ),
        ]
    )
    connector = _connector(transport)

    response = connector.search_album("Artist Album")

    assert response.request_id == "search-1"
    assert response.request.kind is SearchKind.ALBUM
    assert response.file_count == 2
    source = response.sources[0]
    assert source.identifier == "peer"
    assert source.availability is SearchAvailability.AVAILABLE
    assert source.queue_state is SearchQueueState.AVAILABLE
    assert source.files[0].name == "@@music\\Artist\\Album\\01.flac"
    assert source.files[0].bitrate_kbps == 1004
    assert source.files[0].duration_seconds == 245
    assert source.files[1].bitrate_kbps is None
    assert json.loads(transport.requests[0][3] or b"{}") == {
        "searchText": "Artist Album",
        "responseLimit": 100,
    }
    assert transport.requests[-1][1].endswith("/searches/search-1/responses")


def test_a_file_reports_its_folder_and_its_own_name_without_losing_the_path() -> None:
    """A share is browsed and downloaded by folder, so the folder is a column of its own.

    The name Soulseek reported stays the truth; the folder and the file name are
    read out of it.
    """
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "Completed"}),
            _response(
                [
                    {
                        "username": "peer",
                        "files": [
                            {"filename": "@@abc\\Music\\Dario Venn (1970)\\01 - Ocre.mp3"},
                            {"filename": "loose.mp3"},
                        ],
                    }
                ]
            ),
        ]
    )

    files = _connector(transport).search_album("query").sources[0].files

    assert files[0].name == "@@abc\\Music\\Dario Venn (1970)\\01 - Ocre.mp3"
    assert files[0].directory == "@@abc\\Music\\Dario Venn (1970)"
    assert files[0].basename == "01 - Ocre.mp3"
    assert files[1].directory == ""
    assert files[1].basename == "loose.mp3"


def test_responses_are_read_only_after_the_search_stops_gathering() -> None:
    """Peers answer over seconds, so reading immediately finds almost nothing.

    Without this wait a search that would have found a whole share finds
    nothing.
    """
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "InProgress"}),
            _response({"state": "InProgress"}),
            _response({"state": "Completed, ResponseLimitReached"}),
            _response([{"username": "peer", "files": [{"filename": "x.flac"}]}]),
        ]
    )

    response = _connector(transport).search_album("query")

    assert response.file_count == 1
    polls = [url for _, url, _, _, _ in transport.requests if url.endswith("/searches/search-1")]
    assert len(polls) == 3
    assert transport.requests[-1][1].endswith("/responses")


def test_a_search_is_ended_once_the_answers_stop_arriving() -> None:
    """Ending it is what makes it readable, and slskd is slow to end one itself.

    Measured on a running instance, a search can hold all of its respondents
    within a few seconds and stay ``InProgress`` for many times longer. Waiting
    that out buys a late peer at a multiple of the wait, so the search is
    stopped as soon as the counts stop moving.
    """
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "InProgress", "responseCount": 8, "fileCount": 40}),
            _response({"state": "InProgress", "responseCount": 20, "fileCount": 109}),
            _response({"state": "InProgress", "responseCount": 20, "fileCount": 109}),
            _response({"state": "InProgress", "responseCount": 20, "fileCount": 109}),
            _response({}),  # the PUT that ends it, which answers with no body
            _response(_peers(20)),
        ]
    )

    response = _connector(transport, search_quiet_seconds=2.0).search_album("query")

    assert response.file_count == 20
    stopped = [(method, url) for method, url, _, _, _ in transport.requests if method == "PUT"]
    assert stopped == [("PUT", "http://slskd.test/api/v0/searches/search-1")]
    assert transport.requests[-1][1].endswith("/responses")


def test_a_search_nobody_has_answered_is_never_cut_short_for_being_quiet() -> None:
    """ "Nothing yet" and "nothing coming" read the same, and only one is worth acting on.

    A popular search stays open longest, so stopping an empty one early fails
    in proportion to how much there was to find. Quietness only ends a search
    that has heard from someone.
    """
    transport = FakeTransport(
        [_response({"id": "search-1"})]
        + [_response({"state": "InProgress", "responseCount": 0}) for _ in range(3)]
        + [_response({}), _response([])]
    )

    response = _connector(
        transport, search_settle_seconds=3.0, search_quiet_seconds=1.0
    ).search_album("query")

    # It waited the whole allowance, then ended the search and read it honestly:
    # nobody answered is a finding, and it is only true once the waiting is done.
    assert response.sources == ()
    polls = [
        url
        for method, url, _, _, _ in transport.requests
        if method == "GET" and url.endswith("/searches/search-1")
    ]
    assert len(polls) == 3


def test_an_empty_first_read_after_stopping_is_not_taken_for_an_empty_network() -> None:
    """Ending a search flips its state before it serves what it gathered.

    Against a running slskd, a read made immediately after the stop can return
    nothing while a read made a moment later returns everything. Trusting
    the first read reports an empty network that is not empty, so the count the
    service reports for itself is what says an empty answer is false.
    """
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "InProgress", "responseCount": 1, "fileCount": 13}),
            _response({"state": "InProgress", "responseCount": 1, "fileCount": 13}),
            _response({}),  # the PUT
            _response([]),  # served nothing yet, though it counts one
            _response(_peers(1)),  # and here it is
        ]
    )

    response = _connector(transport, search_quiet_seconds=1.0).search_album("query")

    assert len(response.sources) == 1


def test_a_service_that_serves_fewer_than_it_counted_is_not_waited_on_forever() -> None:
    """A service may count more respondents than it serves; what it serves is the answer."""
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "Completed, TimedOut", "responseCount": 80}),
            _response(_peers(77)),
            _response(_peers(77)),
        ]
    )

    response = _connector(transport).search_album("query")

    # It read twice, saw the same 77, and stopped rather than burning the whole
    # allowance on three respondents this slskd was never going to hand over.
    assert len(response.sources) == 77
    assert len(transport.requests) == 4


def test_abandoning_a_search_stops_it_and_drops_it_and_reads_nothing() -> None:
    """Closing a tab has to stop costing the network something, or it closed nothing.

    It is dropped as well as stopped, because a window that opens a tab per
    search would otherwise leave a trail of them in the slskd instance.
    """
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "InProgress", "responseCount": 3}),
            _response({}),  # the PUT that ends it
            SlskdHttpResponse(status=204, body=b""),  # the DELETE that drops it
        ]
    )
    watch = FakeWatch(abandon_after=1)

    with pytest.raises(SlskdSearchAbandonedError):
        _connector(transport).search_album("query", watch)

    assert [
        (method, url) for method, url, _, _, _ in transport.requests if method in {"PUT", "DELETE"}
    ] == [
        ("PUT", "http://slskd.test/api/v0/searches/search-1"),
        ("DELETE", "http://slskd.test/api/v0/searches/search-1"),
    ]
    assert not any(url.endswith("/responses") for _, url, _, _, _ in transport.requests)


def test_the_watch_is_told_what_the_search_is_holding_while_it_runs() -> None:
    """A count is what the window shows instead of a mute spinner while it waits."""
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "InProgress", "responseCount": 8, "fileCount": 40}),
            _response({"state": "Completed, TimedOut", "responseCount": 20, "fileCount": 109}),
            _response(_peers(20)),
        ]
    )
    watch = FakeWatch()

    _connector(transport).search_album("query", watch)

    assert [(p.sources, p.files, p.finished) for p in watch.seen] == [
        (8, 40, False),
        (20, 109, True),
    ]


def test_a_folder_is_read_from_its_source_with_the_names_put_back_together() -> None:
    """A browsed folder states its name once and its files bare underneath it."""
    transport = FakeTransport(
        [
            _response(
                [
                    {
                        "name": "@@share\\Artist\\Album",
                        "fileCount": 2,
                        "files": [
                            {"filename": "01.flac", "size": 100},
                            {"filename": "cover.jpg", "size": 5},
                        ],
                    }
                ]
            )
        ]
    )

    files = _connector(transport).directory("some one", "@@share\\Artist\\Album")

    assert [file.name for file in files] == [
        "@@share\\Artist\\Album\\01.flac",
        "@@share\\Artist\\Album\\cover.jpg",
    ]
    # A name with a space in it has to survive the URL, or the request never
    # leaves: Python refuses a path with a raw space in it outright.
    assert transport.requests[0][1] == "http://slskd.test/api/v0/users/some%20one/directory"
    assert json.loads(transport.requests[0][3] or b"{}") == {"directory": "@@share\\Artist\\Album"}


def test_clearing_finished_transfers_clears_a_list_and_not_a_disk() -> None:
    """Measured on a running instance: 204, and the list comes back empty.

    What it cleared stays on disk: the download folder holds the same files
    afterwards. The route says `all/completed`, and nothing about it reaches a
    file.
    """
    transport = FakeTransport([SlskdHttpResponse(status=204, body=b"")])

    _connector(transport).clear_finished()

    assert transport.requests[0][0:2] == (
        "DELETE",
        "http://slskd.test/api/v0/transfers/downloads/all/completed",
    )


def test_forgetting_a_search_drops_it_from_the_service() -> None:
    """A window that opens a tab per search would otherwise litter the service."""
    transport = FakeTransport([SlskdHttpResponse(status=204, body=b"")])

    _connector(transport).forget("search-1")

    assert transport.requests[0][0:2] == ("DELETE", "http://slskd.test/api/v0/searches/search-1")


def test_forgetting_a_search_that_is_already_gone_is_not_a_failure() -> None:
    """The only reason to ask is to be rid of it, and it is."""
    transport = FakeTransport([SlskdHttpResponse(status=404, body=b"")])

    _connector(transport).forget("search-1")  # does not raise


def test_search_track_preserves_track_operation_without_parsing_query() -> None:
    """Track search differs only by its opaque connector operation label."""
    transport = FakeTransport(
        [
            _response({"id": "search-2"}),
            _response({"state": "Completed"}),
            _response({"responses": []}),
        ]
    )

    response = _connector(transport).search_track("  Artist - Track  ")

    assert response.request.kind is SearchKind.TRACK
    assert response.request.query == "  Artist - Track  "
    assert response.sources == ()


def test_a_source_that_offered_nothing_is_not_a_malformed_payload() -> None:
    """slskd reports respondents with no matching files, and they are simply empty."""
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "Completed"}),
            _response([{"username": "peer"}]),
        ]
    )

    response = _connector(transport).search_album("query")

    assert response.sources[0].files == ()
    assert response.file_count == 0


def test_malformed_search_payload_is_rejected() -> None:
    """A file without required remote-file data never becomes a connector model."""
    transport = FakeTransport(
        [
            _response({"id": "search-1"}),
            _response({"state": "Completed"}),
            _response([{"username": "peer", "files": [{"size": 10}]}]),
        ]
    )

    with pytest.raises(SlskdMalformedResponseError, match="file name"):
        _connector(transport).search_album("query")


def test_authentication_failure_is_translated() -> None:
    """A rejected API key raises the connector-specific authentication failure.

    Where nothing can report it, that is: a search has no state to answer with,
    so its only honest outcome is the exception. ``health`` is the one that
    answers instead — see below.
    """
    transport = FakeTransport([SlskdHttpResponse(status=401, body=b"{}")])

    with pytest.raises(SlskdAuthenticationError, match="rejected"):
        _connector(transport).search_album("query")


def test_health_tells_a_missing_key_from_a_refused_one_from_a_silent_server() -> None:
    """Three states, because they are three different things to do about it.

    Reported as one exception, all three become ``unreachable`` and the window
    says that the server is not answering — for a server that is answering and
    only wants a key, which points at the wrong thing to fix.
    """
    refused = _connector(FakeTransport([SlskdHttpResponse(status=401, body=b"{}")]))
    # One per attempt — `retry_limit` retries after the first try — because an
    # unreachable server is retried to the configured limit and only then is it
    # unavailable. A key is neither retried nor waited for.
    silent = _connector(FakeTransport([URLError("connection refused")] * 4))
    configuration = _configuration()
    keyless = SlskdConnector(
        configuration,
        # No key in the environment, which is not the same as a key nobody liked.
        SlskdClient(
            configuration,
            FakeTransport([]),
            _logger(),
            lambda _: None,
            sleep=lambda _: None,
        ),
        SlskdMapper(),
        _logger(),
    )

    assert refused.health().state is ConnectorHealthState.CREDENTIAL_REJECTED
    assert keyless.health().state is ConnectorHealthState.NO_CREDENTIAL
    assert silent.health().state is ConnectorHealthState.UNAVAILABLE


def test_missing_api_key_is_rejected_before_http() -> None:
    """Required credentials are resolved at request time and never stored in configuration."""
    transport = FakeTransport([])
    client = SlskdClient(
        _configuration(), transport, _logger(), lambda _: None, sleep=lambda _: None
    )

    with pytest.raises(SlskdAuthenticationError, match="not available"):
        client.health_payload()
    assert transport.requests == []


def test_timeout_is_retried_and_translated_after_retry_limit() -> None:
    """Transient timeouts are retried exactly as configured, then become retry exhaustion."""
    transport = FakeTransport([TimeoutError(), TimeoutError()])
    client = SlskdClient(
        _configuration(retry_limit=1),
        transport,
        _logger(),
        lambda _: "secret",
        sleep=lambda _: None,
    )

    with pytest.raises(SlskdRetryExhaustedError, match="exhausted"):
        client.health_payload()
    assert len(transport.requests) == 2


def test_single_timeout_is_translated_without_retry_wrapper() -> None:
    """A zero-retry timeout remains specifically distinguishable to connector consumers."""
    transport = FakeTransport([TimeoutError()])
    client = SlskdClient(
        _configuration(retry_limit=0),
        transport,
        _logger(),
        lambda _: "secret",
        sleep=lambda _: None,
    )

    with pytest.raises(SlskdTimeoutError):
        client.health_payload()


def test_network_failure_retries_without_leaking_urllib_error() -> None:
    """A transient URL failure is retried and a subsequent valid health payload succeeds."""
    transport = FakeTransport([URLError("offline"), _response({"version": "0.25.1"})])
    client = SlskdClient(
        _configuration(retry_limit=1),
        transport,
        _logger(),
        lambda _: "secret",
        sleep=lambda _: None,
    )

    assert client.health_payload()["version"] == "0.25.1"
    assert len(transport.requests) == 2


@pytest.mark.parametrize("base_url", ["ftp://slskd.test", "slskd.test"])
def test_configuration_rejects_invalid_slskd_url(tmp_path, base_url: str) -> None:
    """The configuration loader accepts only explicit HTTP(S) service endpoints."""
    from diglibrary.config.loader import ConfigurationError, load_configuration

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[database]
path = "database.sqlite3"
[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"
[providers]
enabled = ["youtube"]
priority = ["youtube"]
[connectors.slskd]
base_url = "{base_url}"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="base_url"):
        load_configuration(config_path)


def _connector(
    transport: FakeTransport,
    enabled: bool = True,
    search_settle_seconds: float = 20.0,
    search_quiet_seconds: float = 3.0,
) -> SlskdConnector:
    configuration = _configuration(
        enabled=enabled,
        search_settle_seconds=search_settle_seconds,
        search_quiet_seconds=search_quiet_seconds,
    )
    return SlskdConnector(
        configuration,
        SlskdClient(configuration, transport, _logger(), lambda _: "secret", sleep=lambda _: None),
        SlskdMapper(),
        _logger(),
    )


def _configuration(
    enabled: bool = True,
    retry_limit: int = 3,
    search_settle_seconds: float = 20.0,
    search_quiet_seconds: float = 3.0,
) -> SlskdConfiguration:
    return SlskdConfiguration(
        enabled=enabled,
        base_url="http://slskd.test",
        timeout_seconds=2.0,
        retry_limit=retry_limit,
        authentication_mode=SlskdAuthenticationMode.API_KEY,
        api_key_environment_variable="SLSKD_KEY",
        search_settle_seconds=search_settle_seconds,
        search_quiet_seconds=search_quiet_seconds,
        search_poll_seconds=1.0,
    )


def _peers(count: int) -> list[dict[str, Any]]:
    """Answer with as many respondents as the search said it was holding.

    The two have to agree: a stopped search that serves fewer than it counted is
    the handover race, and the client is right to keep reading through it.
    """
    return [
        {"username": f"peer-{index}", "files": [{"filename": f"@@share\\Album\\{index}.flac"}]}
        for index in range(count)
    ]


def _response(payload: dict[str, Any] | list[Any]) -> SlskdHttpResponse:
    return SlskdHttpResponse(status=200, body=json.dumps(payload).encode("utf-8"))


def _logger() -> logging.Logger:
    return logging.getLogger("test.slskd")
