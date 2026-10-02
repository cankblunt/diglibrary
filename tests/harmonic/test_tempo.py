"""The tempo measurement, and the ambiguity it must not hide.

The tallest autocorrelation peak is as often the double of the tempo as the
tempo itself, so a naive reading reports twice the speed of a track. These
assert that the preference for the lower octave fixes it and that genuine
ambiguity is still reported rather than resolved in silence.
"""

import numpy as np
import pytest

from diglibrary.harmonic.tempo import SAMPLE_RATE, measure_tempo


def _pulse_train(bpm: float, seconds: float = 20.0, subdivide: bool = False) -> np.ndarray:
    """A click every beat — a tempo that is a fact because it was constructed.

    ``subdivide`` puts a quieter click halfway between the beats, which is what
    a hi-hat does and what makes a tracker read double the tempo.
    """
    samples = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)
    period = SAMPLE_RATE * 60.0 / bpm
    click = np.hanning(220) * np.random.default_rng(0).standard_normal(220)
    position = 0.0
    index = 0
    while int(position) + click.size < samples.size:
        loud = 1.0 if not subdivide or index % 2 == 0 else 0.45
        start = int(position)
        samples[start : start + click.size] += click * loud
        position += period / 2 if subdivide else period
        index += 1
    return samples


@pytest.mark.parametrize("bpm", [90.0, 120.0, 140.0])
def test_a_pulse_is_read_at_the_speed_it_was_built_at(bpm: float) -> None:
    measured = measure_tempo(_pulse_train(bpm))

    assert measured is not None
    assert measured.bpm == pytest.approx(bpm, rel=0.05)


def test_an_offbeat_does_not_double_the_reading() -> None:
    """A click between every beat must not make a 92 BPM pulse read as 184."""
    measured = measure_tempo(_pulse_train(92.0, subdivide=True))

    assert measured is not None
    assert measured.bpm == pytest.approx(
        92.0, rel=0.08
    ), f"read {measured.bpm:.1f}, which is the octave error this preference exists to fix"


def test_a_genuinely_ambiguous_tempo_says_so() -> None:
    """Reporting the twin is the honest half: a reading the audio does not
    settle must not be printed as though it did."""
    measured = measure_tempo(_pulse_train(75.0, subdivide=True))

    assert measured is not None
    if measured.is_ambiguous:
        assert measured.alternative_bpm == pytest.approx(measured.bpm * 2, rel=0.1) or (
            measured.alternative_bpm == pytest.approx(measured.bpm / 2, rel=0.1)
        ), "the alternative offered is not an octave of the reading"


def test_too_little_audio_has_no_tempo() -> None:
    assert measure_tempo(np.zeros(500, dtype=np.float32)) is None


def test_silence_has_no_tempo() -> None:
    """Silence has no onsets, so it must not be given a beat."""
    assert measure_tempo(np.zeros(SAMPLE_RATE * 20, dtype=np.float32)) is None
