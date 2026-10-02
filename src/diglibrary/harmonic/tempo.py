"""How fast a recording is, and whether that answer has a twin.

**Tempo is ambiguous by nature, and the ambiguity is a factor of two.** A track
at 92 BPM has a beat on every other onset at 184, and an autocorrelation peak
sits at both. Taking the tallest peak reports double the tempo for a large
share of real recordings, and the screen would state it as fact.

So the peak is chosen with a preference for where music actually sits, and when
the other octave has nearly the same support **that is reported** rather than
resolved silently.
"""

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 11025
_FRAME = 1024
_HOP = 256

_SLOWEST = 60.0
_FASTEST = 200.0
# Where a preference peaks. Ellis's beat tracker uses a log-normal centred near
# 120 BPM for exactly this reason: without it, the tallest peak wins and the
# tallest peak is as often the double as the truth.
_PREFERRED_BPM = 120.0
_PREFERENCE_WIDTH = 0.9


@dataclass(frozen=True, slots=True)
class MeasuredTempo:
    """One reading of a recording's tempo, and the reading it could be confused with."""

    bpm: float
    confidence: float
    # The other octave, when it has nearly as much support. `None` when the
    # answer is not seriously ambiguous — which is the common case and must
    # not be dressed up as doubt.
    alternative_bpm: float | None = None

    @property
    def is_ambiguous(self) -> bool:
        """Whether half or double this is very nearly as good a reading."""
        return self.alternative_bpm is not None


def measure_tempo(samples: np.ndarray) -> MeasuredTempo | None:
    """Return how fast these samples are, or ``None`` when there is too little.

    ``None`` rather than a number: an eight-second file has no tempo worth
    stating, and a value is never invented to fill a column.
    """
    flux = _onset_strength(samples)
    if flux.size < 16:
        return None
    correlation = _autocorrelation(flux)
    frames_per_second = SAMPLE_RATE / _HOP
    shortest = max(int(frames_per_second * 60.0 / _FASTEST), 1)
    longest = int(frames_per_second * 60.0 / _SLOWEST)
    if longest >= correlation.size:
        longest = correlation.size - 1
    if longest <= shortest:
        return None

    lags = np.arange(shortest, longest + 1)
    strength = correlation[shortest : longest + 1]
    if strength.max() <= 0:
        return None
    candidates = 60.0 * frames_per_second / lags
    # The preference, applied to the strengths rather than to the answer: a
    # weighting decides which peak is read, and never invents a peak that the
    # audio does not have.
    weighted = strength * _preference(candidates)
    chosen = int(np.argmax(weighted))
    bpm = float(candidates[chosen])
    confidence = float(strength[chosen] / strength.max())

    return MeasuredTempo(
        bpm=bpm,
        confidence=confidence,
        alternative_bpm=_close_twin(bpm, candidates, strength, strength[chosen]),
    )


def _close_twin(
    bpm: float, candidates: np.ndarray, strength: np.ndarray, chosen_strength: float
) -> float | None:
    """Return half or double this tempo when the audio supports it nearly as well.

    Only the two octaves, because that is the ambiguity that exists: a tempo is
    not confusable with an unrelated one, and offering a third number would be
    inventing doubt rather than reporting it.
    """
    for twin in (bpm / 2.0, bpm * 2.0):
        if not _SLOWEST <= twin <= _FASTEST:
            continue
        nearest = int(np.argmin(np.abs(candidates - twin)))
        # Within a fifth of the chosen peak's support is close enough that the
        # audio is genuinely not deciding between them.
        if chosen_strength > 0 and strength[nearest] >= chosen_strength * 0.8:
            return float(candidates[nearest])
    return None


def _preference(bpm: np.ndarray) -> np.ndarray:
    """Weight each candidate by how ordinary a tempo it is."""
    return np.exp(-0.5 * (np.log2(bpm / _PREFERRED_BPM) / _PREFERENCE_WIDTH) ** 2)


def _onset_strength(samples: np.ndarray) -> np.ndarray:
    """How much the spectrum rises frame to frame — where the beats are.

    Only the rises count: energy falling away is a note ending, and a note
    ending is not an onset.
    """
    if samples.size < _FRAME * 4:
        return np.zeros(0)
    frames = np.lib.stride_tricks.sliding_window_view(samples, _FRAME)[::_HOP]
    if frames.shape[0] < 4:
        return np.zeros(0)
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(_FRAME), axis=1))
    flux = np.maximum(np.diff(spectrum, axis=0), 0.0).sum(axis=1)
    return flux - flux.mean()


def _autocorrelation(flux: np.ndarray) -> np.ndarray:
    """How much the onset pattern looks like itself, delayed — one beat period
    is the delay where it looks most like itself."""
    correlation = np.correlate(flux, flux, mode="full")[flux.size - 1 :]
    return correlation
