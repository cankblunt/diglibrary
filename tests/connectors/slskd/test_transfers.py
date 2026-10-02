"""Asking a source for a folder's files, and following what happens to them."""

import json
import logging
from collections.abc import Mapping

import pytest

from diglibrary.connectors.contracts import TransferConnector
from diglibrary.connectors.models import TransferFile, TransferRequest, TransferState
from diglibrary.connectors.slskd.client import SlskdClient, SlskdHttpResponse
from diglibrary.connectors.slskd.configuration import (
    SlskdAuthenticationMode,
    SlskdConfiguration,
)
from diglibrary.connectors.slskd.connector import SlskdConnector
from diglibrary.connectors.slskd.exceptions import SlskdConnectorError
from diglibrary.connectors.slskd.mapper import SlskdMapper


class FakeTransport:
    """Record request values and return queued responses without a network."""

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


def test_a_whole_folder_is_asked_for_in_one_request() -> None:
    """A Soulseek share is downloaded by folder, so the folder is the unit here."""
    transport = FakeTransport([_response({}), _response(_downloads())])
    connector = _connector(transport)

    transfers = connector.enqueue(
        TransferRequest(
            source="peer",
            files=(
                TransferFile("@@m\\Ledger\\01. Slow window turns.flac", 34771000),
                TransferFile("@@m\\Ledger\\02. Salt on the sill.flac", 27650700),
                TransferFile("@@m\\Ledger\\cover.jpg", 255489),
            ),
        )
    )

    assert isinstance(connector, TransferConnector)
    method, url, _, body, _ = transport.requests[0]
    assert (method, url) == ("POST", "http://slskd.test/api/v0/transfers/downloads/peer")
    assert json.loads(body or b"[]") == [
        {"filename": "@@m\\Ledger\\01. Slow window turns.flac", "size": 34771000},
        {"filename": "@@m\\Ledger\\02. Salt on the sill.flac", "size": 27650700},
        {"filename": "@@m\\Ledger\\cover.jpg", "size": 255489},
    ]
    assert len(transfers) == 3
    assert {transfer.directory for transfer in transfers} == {"@@m\\Ledger"}
    assert transfers[0].basename == "01. Slow window turns.flac"


def test_the_cover_travels_with_the_album() -> None:
    """A folder is asked for with every file in it, not only the audio extensions."""
    transport = FakeTransport([_response({}), _response(_downloads())])

    transfers = _connector(transport).enqueue(
        TransferRequest("peer", (TransferFile("@@m\\Ledger\\cover.jpg", 255489),))
    )

    assert [transfer.basename for transfer in transfers] == ["cover.jpg"]


def test_a_source_name_with_a_space_survives_the_url() -> None:
    """Soulseek names are not URL-shaped, and one wrong escape asks about nobody."""
    transport = FakeTransport([_response({}), _response({"directories": []})])

    _connector(transport).enqueue(TransferRequest("dj somebody", (TransferFile("a\\b.flac", 1),)))

    assert transport.requests[0][1].endswith("/transfers/downloads/dj%20somebody")


def test_every_state_slskd_words_is_read_by_its_outcome() -> None:
    """`Completed, Succeeded` and `Completed, Errored` share a first word.

    Reading the stage instead of the outcome would call a failed transfer done.
    """
    wordings = {
        "Completed, Succeeded": TransferState.COMPLETED,
        "Completed, Errored": TransferState.FAILED,
        "Completed, TimedOut": TransferState.FAILED,
        "Completed, Rejected": TransferState.FAILED,
        "Completed, Cancelled": TransferState.CANCELLED,
        "Completed, Aborted": TransferState.CANCELLED,
        "Queued, Remotely": TransferState.QUEUED,
        "Queued, Locally": TransferState.QUEUED,
        "InProgress": TransferState.IN_PROGRESS,
        "Requested": TransferState.QUEUED,
        "Something slskd Has Not Said Yet": TransferState.UNKNOWN,
    }
    for wording, expected in wordings.items():
        payload = {
            "directories": [
                {"directory": "d", "files": [{"id": "1", "filename": "a.flac", "state": wording}]}
            ]
        }
        transport = FakeTransport([_response(payload)])

        state = _connector(transport).transfers("peer")[0].state

        assert state is expected, wording


def test_a_state_nobody_has_met_yet_counts_as_still_moving() -> None:
    """The complement, written once on the enum and read everywhere else.

    If each consumer enumerates the states it believes exist —
    `{"completed", "failed", "cancelled"}` on one side and
    `{"queued", "in_progress"}` on the other — `UNKNOWN` belongs to neither. A
    wording slskd uses that this vocabulary does not cover then makes
    `any(... is moving)` answer False, and the machine is allowed to sleep
    while downloads are running, which `ui/wakefulness.py` exists to prevent.
    """
    assert TransferState.UNKNOWN.is_moving
    assert not TransferState.UNKNOWN.has_stopped
    # And the ends really are ends, so the fix is not "keep the machine awake
    # forever".
    assert all(
        state.has_stopped and not state.is_moving
        for state in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED)
    )
    assert all(state.is_moving for state in (TransferState.QUEUED, TransferState.IN_PROGRESS))


def test_every_state_this_vocabulary_has_answers_both_questions() -> None:
    """Written over the enum itself rather than over a list of members, so a
    state added later is covered by this test when it is added."""
    for state in TransferState:
        assert state.is_moving is not state.has_stopped, state


def test_progress_is_reported_while_a_file_is_still_moving() -> None:
    """The tree shows how far a download has got, so the bytes must survive."""
    payload = {
        "directories": [
            {
                "directory": "d",
                "files": [
                    {
                        "id": "7",
                        "filename": "a.flac",
                        "state": "InProgress",
                        "size": 1000,
                        "bytesTransferred": 250,
                    }
                ],
            }
        ]
    }
    transport = FakeTransport([_response(payload)])

    transfer = _connector(transport).transfers("peer")[0]

    assert (transfer.size_bytes, transfer.transferred_bytes) == (1000, 250)
    assert transfer.state is TransferState.IN_PROGRESS


def test_cancelling_names_the_transfer_and_says_whether_to_forget_it() -> None:
    """Stopping a download and dropping it from the list are different wishes."""
    transport = FakeTransport([_response(None), _response(None)])
    connector = _connector(transport)

    connector.cancel("peer", "7")
    connector.cancel("peer", "7", remove=True)

    assert transport.requests[0][0] == "DELETE"
    assert transport.requests[0][1].endswith("/transfers/downloads/peer/7?remove=false")
    assert transport.requests[1][1].endswith("/transfers/downloads/peer/7?remove=true")


def test_a_source_with_no_transfers_is_empty_rather_than_an_error() -> None:
    """slskd answers 404 for a source it has never fetched from.

    Measured against a running slskd: the whole download list answers 200 with
    `[]`, and one source with nothing answers 404. That is the ordinary state
    before the first transfer, and reading it as a missing route makes every
    such question an exception.
    """
    transport = FakeTransport([SlskdHttpResponse(status=404, body=b"")])

    transfers = _connector(transport).transfers("someone-we-never-asked")

    assert transfers == ()


def test_a_genuinely_unexpected_status_is_still_an_error() -> None:
    """Treating 404 as empty must not swallow every other protocol failure."""
    transport = FakeTransport([SlskdHttpResponse(status=418, body=b"{}")])

    with pytest.raises(SlskdConnectorError):
        _connector(transport).transfers("peer")


def test_a_disabled_connector_asks_for_nothing() -> None:
    """The connector ships disabled, and a disabled one must not reach the network."""
    transport = FakeTransport([])
    connector = _connector(transport, enabled=False)

    with pytest.raises(SlskdConnectorError, match="disabled"):
        connector.enqueue(TransferRequest("peer", (TransferFile("a.flac", 1),)))
    with pytest.raises(SlskdConnectorError, match="disabled"):
        connector.transfers("peer")
    assert transport.requests == []


def test_a_transfer_request_without_files_is_refused_before_the_network() -> None:
    """An empty request is a caller's mistake, not something to ask a peer about."""
    transport = FakeTransport([])

    with pytest.raises(ValueError, match="at least one file"):
        _connector(transport).enqueue(TransferRequest("peer", ()))
    assert transport.requests == []


def _downloads() -> dict[str, object]:
    return {
        "directories": [
            {
                "directory": "@@m\\Ledger",
                "files": [
                    {
                        "id": "1",
                        "filename": "@@m\\Ledger\\01. Slow window turns.flac",
                        "state": "Queued, Remotely",
                    },
                    {
                        "id": "2",
                        "filename": "@@m\\Ledger\\02. Salt on the sill.flac",
                        "state": "InProgress",
                    },
                    {"id": "3", "filename": "@@m\\Ledger\\cover.jpg", "state": "Requested"},
                ],
            }
        ]
    }


def _response(payload: object) -> SlskdHttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return SlskdHttpResponse(status=200, body=body)


def _connector(transport: FakeTransport, enabled: bool = True) -> SlskdConnector:
    configuration = _configuration(enabled=enabled)
    return SlskdConnector(
        configuration,
        SlskdClient(configuration, transport, _logger(), lambda _: "secret", sleep=lambda _: None),
        SlskdMapper(),
        _logger(),
    )


def _configuration(enabled: bool = True) -> SlskdConfiguration:
    return SlskdConfiguration(
        enabled=enabled,
        base_url="http://slskd.test",
        timeout_seconds=2.0,
        retry_limit=3,
        authentication_mode=SlskdAuthenticationMode.API_KEY,
        api_key_environment_variable="SLSKD_KEY",
    )


def _logger() -> logging.Logger:
    return logging.getLogger("test.slskd.transfers")


def test_one_file_the_service_refuses_does_not_cost_the_rest_of_the_folder() -> None:
    """A whole album is one request, and the request is all-or-nothing.

    A batch containing a name the service will not take — a file that is
    already queued, for instance — is refused entire, so the files it would
    have taken go with it and nothing says which was which.
    """
    transport = FakeTransport(
        [
            # The batch, refused.
            SlskdHttpResponse(status=409, body=b'{"message": "already queued"}'),
            # Then one request per file: the first is the duplicate, the rest take.
            SlskdHttpResponse(status=409, body=b'{"message": "already queued"}'),
            _response({}),
            _response({}),
            _response(_downloads()),
        ]
    )
    connector = _connector(transport)

    transfers = connector.enqueue(
        TransferRequest(
            source="peer",
            files=(
                TransferFile("@@m\\Ledger\\01. Slow window turns.flac", 34771000),
                TransferFile("@@m\\Ledger\\02. Salt on the sill.flac", 27650700),
                TransferFile("@@m\\Ledger\\cover.jpg", 255489),
            ),
        )
    )

    posts = [
        json.loads(body or b"[]")
        for method, _, _, body, _ in transport.requests
        if method == "POST"
    ]
    assert len(posts[0]) == 3, "the batch is still tried first: it is one question"
    assert [len(post) for post in posts[1:]] == [1, 1, 1], "then one file at a time"
    # And what the service holds afterwards is what comes back, which is the
    # whole point: two of the three, not none of them.
    assert len(transfers) == 3


def test_a_service_that_takes_nothing_at_all_still_fails() -> None:
    """The fallback must not turn a total refusal into a quiet success."""
    refusal = SlskdHttpResponse(status=409, body=b'{"message": "no"}')
    transport = FakeTransport([refusal, refusal, refusal])
    connector = _connector(transport)

    with pytest.raises(SlskdConnectorError):
        connector.enqueue(
            TransferRequest(
                source="peer",
                files=(
                    TransferFile("@@m\\Ledger\\01.flac", 1),
                    TransferFile("@@m\\Ledger\\02.flac", 2),
                ),
            )
        )
