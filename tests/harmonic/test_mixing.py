"""What mixes with what: the wheel and the pitch fader, and neither alone.

A mix suggestion requires a compatible key on the Camelot wheel and a tempo the
fader can reach.
"""

import pytest

from diglibrary.harmonic.keys import _CAMELOT, camelot_neighbours
from diglibrary.harmonic.mixing import key_relation, mixes_with, tempo_relation


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("8A", "8A", "same key"),
        ("8A", "9A", "one step up"),
        ("8A", "7A", "one step down"),
        ("8A", "8B", "relative major"),
        ("8B", "8A", "relative minor"),
        # The wrap, which is where a table of pairs forgets a case.
        ("12A", "1A", "one step up"),
        ("1A", "12A", "one step down"),
        # And what does not mix at all.
        ("8A", "3A", None),
        ("8A", "9B", None),
    ],
)
def test_the_wheel_says_how_two_keys_are_related(source, target, expected) -> None:
    assert key_relation(source, target) == expected


def test_every_neighbour_has_a_name() -> None:
    """A pair the wheel allows but this cannot name would reach the screen as a
    suggestion with no reason beside it."""
    for code in _CAMELOT.values():
        for neighbour in camelot_neighbours(code):
            assert key_relation(code, neighbour) is not None, f"{code} → {neighbour} unnamed"


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (120.0, 120.0, "same speed"),
        (120.0, 125.0, "same speed"),  # inside the fader's reach
        (120.0, 240.0, "double time"),
        (120.0, 60.0, "half time"),
        (92.0, 184.0, "double time"),
    ],
)
def test_speeds_that_line_up(source, target, expected) -> None:
    meeting = tempo_relation(source, target)
    assert meeting is not None and meeting[0] == expected


def test_speeds_that_do_not_line_up() -> None:
    """The whole reason the key alone is not enough: 92 and 140 are compatible
    on the wheel and unmixable on a deck."""
    assert tempo_relation(92.0, 140.0) is None
    assert tempo_relation(120.0, 135.0) is None


def test_a_compatible_key_at_the_wrong_speed_is_not_offered() -> None:
    assert mixes_with("8A", 92.0, "9A", 140.0) is None


def test_a_compatible_key_at_a_workable_speed_is_offered_with_its_reason() -> None:
    candidate = mixes_with("8A", 120.0, "8B", 123.0)

    assert candidate is not None
    assert candidate.relation == "relative major"
    assert candidate.tempo_relation == "same speed"


def test_an_unmeasured_tempo_is_said_and_not_assumed() -> None:
    """Neither silently excluded nor silently allowed: the screen has to be
    able to say what it does not know."""
    candidate = mixes_with("8A", None, "9A", 120.0)

    assert candidate is not None
    assert candidate.tempo_relation == "speed unknown"


def test_a_wrong_key_is_refused_whatever_the_speed() -> None:
    assert mixes_with("8A", 120.0, "3A", 120.0) is None
