"""The reason the witness fold reports is measured, or it is quoted.

`witness_record` must not answer every failure with one named cause. A `429`
is the witness answering, and a fault in this application's own code has
nothing to do with the network; reporting either as "could not be reached"
sends the reader to check a connection that is fine.

The metadata client already distinguishes the cases: `WitnessThrottledError`
is its own type, guarded in `tests/metadata/test_itunes.py`. These tests
assert that the route keeps that distinction instead of discarding it.

The window's own guard cannot cover this: it builds the answer by hand and
asserts it reaches the toast, which says nothing about what the bridge
decides to hand over.
"""

import logging
from types import SimpleNamespace

from diglibrary.application.api import LibraryApi
from diglibrary.library.hints import SearchHints
from diglibrary.metadata.itunes import WitnessThrottledError

ALBUM = 7


def _route(failure: Exception) -> dict[str, object]:
    """Run the real method over a witness that fails one way."""

    class Failing:
        def recognised(self, asked: object) -> object:
            raise failure

    state = SimpleNamespace(
        outcome=SimpleNamespace(
            hints=SearchHints(text="marlow marlow", artist="Marlow", album="Marlow", year=1974)
        ),
        unit=None,
        unit_id=ALBUM,
    )
    api = SimpleNamespace(
        _albums={ALBUM: state},
        _pipeline=SimpleNamespace(workflow=SimpleNamespace(verifier=Failing())),
        _logger=logging.getLogger("test.witness.route"),
    )
    return LibraryApi.witness_record(api, ALBUM)


def test_a_throttled_witness_is_not_reported_as_one_that_could_not_be_reached() -> None:
    """It answered. What it said was *stop asking*, and the remedy is to wait."""
    answer = _route(WitnessThrottledError("HTTP Error 429: Too Many Requests"))

    assert answer["ok"] is False
    reason = str(answer["error"])
    assert "could not be reached" not in reason, "the one cause that is measured says otherwise"
    assert "429" in reason, "what the witness actually said is the whole diagnosis"


def test_any_other_failure_quotes_what_it_was_told_and_names_no_cause() -> None:
    """A fault in this application's own code is not reported as a network failure."""
    answer = _route(TypeError("'NoneType' object is not subscriptable"))

    assert answer["ok"] is False
    reason = str(answer["error"])
    assert "could not be reached" not in reason
    assert "'NoneType' object is not subscriptable" in reason, "the text it was told is the reason"
