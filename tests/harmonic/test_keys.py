"""The key measurement, proved against signals whose key is a fact.

Ground truth cannot be assumed from a music library, where files rarely carry a
key tag. So the wiring is proved against chord progressions built here, where
the answer is known because it was constructed.
"""

import numpy as np
import pytest

from diglibrary.harmonic.keys import (
    _CAMELOT,
    PITCH_CLASSES,
    SAMPLE_RATE,
    camelot_neighbours,
    measure_key,
)


def _note_hz(name: str, octave: int = 4) -> float:
    semitone = PITCH_CLASSES.index(name) + 12 * (octave + 1)
    return 440.0 * 2 ** ((semitone - 69) / 12)


def _tone(hz: float, seconds: float = 1.0) -> np.ndarray:
    t = np.linspace(0, seconds, int(SAMPLE_RATE * seconds), endpoint=False)
    # Three harmonics, so this is shaped like an instrument rather than a sine:
    # a pure tone lands entirely in one bin and would flatter the measurement.
    return sum(np.sin(2 * np.pi * hz * h * t) / h for h in (1, 2, 3))


def _progression(chords: list[list[str]]) -> np.ndarray:
    return np.concatenate([sum(_tone(_note_hz(note)) for note in chord) for chord in chords])


@pytest.mark.parametrize(
    ("tonic", "mode", "chords"),
    [
        ("C", "major", [["C", "E", "G"], ["F", "A", "C"], ["G", "B", "D"], ["C", "E", "G"]]),
        ("A", "minor", [["A", "C", "E"], ["D", "F", "A"], ["E", "G", "B"], ["A", "C", "E"]]),
        ("G", "major", [["G", "B", "D"], ["C", "E", "G"], ["D", "F#", "A"], ["G", "B", "D"]]),
        (
            "F#",
            "minor",
            [["F#", "A", "C#"], ["B", "D", "F#"], ["C#", "E", "G#"], ["F#", "A", "C#"]],
        ),
    ],
)
def test_a_cadence_is_read_as_the_key_it_is_in(tonic: str, mode: str, chords: list) -> None:
    """I-IV-V-I says the key out loud; the measurement has to hear it."""
    measured = measure_key(_progression(chords))

    assert measured is not None
    assert (measured.tonic, measured.mode) == (tonic, mode)
    assert measured.confidence > 0.5


def test_too_little_audio_is_no_answer_rather_than_a_confident_one() -> None:
    """Audio too short to measure yields no key, not a key with a confidence."""
    assert measure_key(np.zeros(100, dtype=np.float32)) is None


def test_silence_is_not_a_key() -> None:
    """A silent file correlates with nothing, and must not be given a tonic."""
    assert measure_key(np.zeros(SAMPLE_RATE * 4, dtype=np.float32)) is None


def test_every_key_has_one_address_on_the_wheel() -> None:
    """Twenty-four keys, twenty-four codes, no code used twice.

    A duplicate here would put two unrelated keys at the same place on the
    wheel and make the mixing suggestions quietly wrong.
    """
    assert len(_CAMELOT) == 24, "a key lost its address on the wheel"
    assert len(set(_CAMELOT.values())) == 24, "two keys share one code"


def test_what_mixes_is_derived_and_not_listed() -> None:
    """Three moves and no others, and the wheel wraps at 12 to 1.

    Written from the code rather than from a table of pairs, so the wrap is
    not a special case that has to be added by hand.
    """
    assert set(camelot_neighbours("8A")) == {"7A", "9A", "8B"}
    assert set(camelot_neighbours("12B")) == {"11B", "1B", "12A"}
    assert set(camelot_neighbours("1A")) == {"12A", "2A", "1B"}


def test_the_wheel_is_symmetric() -> None:
    """If A mixes with B then B mixes with A. A wheel where that fails would
    recommend a transition in one direction and refuse it in the other."""
    for code in _CAMELOT.values():
        for neighbour in camelot_neighbours(code):
            assert code in camelot_neighbours(neighbour), f"{code} → {neighbour} is one-way"
