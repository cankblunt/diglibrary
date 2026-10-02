"""Unit tests for the AcoustID lookup, against its real answer shape."""

import logging
import urllib.parse
from collections.abc import Mapping
from typing import Any

from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.metadata.acoustid import REQUESTED_META, AcoustIdClient
from diglibrary.metadata.transport import MetadataRequestError


class _Recorded:
    """A JSON client that replays one answer and remembers the URL it was given."""

    def __init__(self, payload: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.payload = payload or {"status": "ok", "results": []}
        self.error = error
        self.urls: list[str] = []

    def get(self, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


class _Environment:
    """Credentials backed by a dictionary, so no test reads the real machine."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values

    def optional(self, name: str) -> str | None:
        return self._values.get(name) or None

    def required(self, name: str) -> str:
        return self._values[name]


def _logger() -> logging.Logger:
    return logging.getLogger("test.acoustid")


def _fingerprint() -> AudioFingerprint:
    return AudioFingerprint(fingerprint="AQADtIqoKXGU", duration_seconds=200)


# The shape the service answers in when asked for identifiers only: a result
# carries a score and recordings, and a recording carries its id
# and the release groups it belongs to.
_REAL_ANSWER: dict[str, Any] = {
    "status": "ok",
    "results": [
        {
            "id": "0a1b2c3d",
            "score": 0.9621242,
            "recordings": [
                {
                    "id": "11111111-2222-4333-8444-555555555555",
                    "releasegroups": [{"id": "rg-one"}, {"id": "rg-two"}],
                },
                {"id": "8f0b7d16-0000-4000-8000-000000000000", "releasegroups": [{"id": "rg-two"}]},
            ],
        },
        {"id": "1c2d3e4f", "score": 0.51, "recordings": [{"id": "rec-low"}]},
    ],
}


def _client(
    payload: Mapping[str, Any] | None = None,
    error: Exception | None = None,
    key: str = "k3y",
) -> tuple[_Recorded, AcoustIdClient]:
    recorded = _Recorded(payload, error)
    environment = _Environment({"DIGLIBRARY_ACOUSTID_KEY": key})
    return recorded, AcoustIdClient(recorded, environment, _logger())  # type: ignore[arg-type]


def test_a_fingerprint_returns_its_recordings_best_score_first() -> None:
    """The identifiers are the answer; no title or artist travels in one."""
    _, client = _client(_REAL_ANSWER)

    matches = client.lookup(_fingerprint())

    assert [round(match.score, 4) for match in matches] == [0.9621, 0.51]
    assert matches[0].recording_ids[0] == "11111111-2222-4333-8444-555555555555"
    # Release groups are collected across the recordings and de-duplicated,
    # because two recordings of one track commonly share an album.
    assert matches[0].release_group_ids == ("rg-one", "rg-two")


def test_the_lookup_asks_for_identifiers_and_nothing_else() -> None:
    """`meta` is space-separated — a literal `+` is accepted and answers without them.

    Against the running service, `recordingids+releasegroupids` comes back
    `status=ok` with no recordings at all, which is the worst kind of wrong
    answer because it looks like a track nobody knows.
    """
    recorded, client = _client(_REAL_ANSWER)

    client.lookup(_fingerprint())

    query = urllib.parse.parse_qs(urllib.parse.urlparse(recorded.urls[0]).query)
    assert query["meta"] == [REQUESTED_META]
    assert " " in REQUESTED_META and "+" not in REQUESTED_META
    assert query["duration"] == ["200"]
    assert query["fingerprint"] == ["AQADtIqoKXGU"]
    assert query["client"] == ["k3y"]


def test_without_a_key_the_feature_is_off_rather_than_broken() -> None:
    """The key is the user's own, so its absence is a normal state."""
    recorded, client = _client(key="")

    assert client.is_configured is False
    assert client.lookup(_fingerprint()) == ()
    assert recorded.urls == []


def test_a_service_that_cannot_be_reached_is_silence() -> None:
    """Acoustic identification never blocks organizing."""
    _, client = _client(error=MetadataRequestError("Network error", retryable=True))

    assert client.lookup(_fingerprint()) == ()


def test_a_refusal_is_not_read_as_an_empty_library() -> None:
    """`status` is what says the answer is an answer."""
    _, client = _client({"status": "error", "error": {"message": "invalid API key"}})

    assert client.lookup(_fingerprint()) == ()


def test_a_result_without_recordings_still_carries_its_score() -> None:
    """A fingerprint AcoustID knows but MusicBrainz has not mapped is a real state."""
    _, client = _client({"status": "ok", "results": [{"id": "abc", "score": 0.83}]})

    matches = client.lookup(_fingerprint())

    assert len(matches) == 1
    assert matches[0].recording_ids == ()
    assert matches[0].release_group_ids == ()


def test_the_key_never_reaches_a_log_record(caplog) -> None:
    """AcoustID is the first source here whose credential rides in the query string.

    Every other source authenticates in a header, so a URL was safe to mention.
    This one is not, and the failure path is where a URL is most tempting to
    log. Secrets live in environment variables, never in logs or exception
    messages.
    """
    secret = "s3cr3t-key"
    _, client = _client(error=MetadataRequestError("Network error", retryable=True), key=secret)

    with caplog.at_level(logging.DEBUG, logger="test.acoustid"):
        assert client.lookup(_fingerprint()) == ()

    written = "\n".join(record.getMessage() for record in caplog.records)
    written += "\n".join(str(record.__dict__) for record in caplog.records)
    assert secret not in written
    assert "api.acoustid.org" not in written
