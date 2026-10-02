"""What key a recording is in, and what that is called on the Camelot wheel.

The key is *measured from the audio*, never read from a tag and never asked of
a catalogue. Files rarely carry a key tag, and a verification source may not
contribute a written value.

The method is Krumhansl-Schmuckler: fold the spectrum into twelve pitch
classes, then correlate that against the twenty-four key profiles. It is the
classical approach, it needs no model, and its arithmetic is small enough to
read. What it gives back is a correlation, and that number is kept — a key
found at 0.55 and a key found at 0.92 are not the same claim, and printing
both as `D major` would present them as equally certain.

**The correlation is not what says how decided the reading was, though — the
distance to the runner-up is**, so both travel out of here.
"""

from dataclasses import dataclass

import numpy as np

PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Albrecht & Shanahan's profiles: how much of a key's music each pitch class
# turns out to be, counted over a corpus, in a major and in a minor key whose
# tonic is C.
#
# **Chosen by measurement against rekordbox's key analysis as the reference.**
# Krumhansl & Kessler's probe-tone profiles have a known failing in the mode:
# they answer major far more often than the reference does, calling
# major-for-minor. These profiles are much closer to the reference's share of
# major keys, and that is the whole of the difference: significantly more
# tracks land on the reference's key or on one that mixes with it.
#
# What cannot be promised is the exact key: the improvement in exact agreement
# was not statistically significant. Several published profiles were scored
# over the same chroma and this one agreed best; it was then confirmed on
# tracks that took no part in choosing it.
_MAJOR_PROFILE = np.array(
    [0.238, 0.006, 0.111, 0.006, 0.137, 0.094, 0.016, 0.214, 0.009, 0.080, 0.008, 0.081]
)
_MINOR_PROFILE = np.array(
    [0.220, 0.006, 0.104, 0.123, 0.019, 0.103, 0.012, 0.214, 0.062, 0.022, 0.061, 0.052]
)

# The wheel itself. Every key has one address on it, and neighbours on the
# wheel are the keys that mix — which is the whole reason DJs use these numbers
# instead of the names.
_CAMELOT: dict[tuple[str, str], str] = {
    ("B", "major"): "1B",
    ("F#", "major"): "2B",
    ("C#", "major"): "3B",
    ("G#", "major"): "4B",
    ("D#", "major"): "5B",
    ("A#", "major"): "6B",
    ("F", "major"): "7B",
    ("C", "major"): "8B",
    ("G", "major"): "9B",
    ("D", "major"): "10B",
    ("A", "major"): "11B",
    ("E", "major"): "12B",
    ("G#", "minor"): "1A",
    ("D#", "minor"): "2A",
    ("A#", "minor"): "3A",
    ("F", "minor"): "4A",
    ("C", "minor"): "5A",
    ("G", "minor"): "6A",
    ("D", "minor"): "7A",
    ("A", "minor"): "8A",
    ("E", "minor"): "9A",
    ("B", "minor"): "10A",
    ("F#", "minor"): "11A",
    ("C#", "minor"): "12A",
}

SAMPLE_RATE = 11025
"""Enough for everything a key is made of. The highest pitch this reads is
around 2 kHz, and decoding to this rate is what keeps a whole album to about
a second of work."""

_FRAME = 8192
_HOP = 4096
_LOWEST_HZ = 55.0
_HIGHEST_HZ = 2000.0


@dataclass(frozen=True, slots=True)
class MeasuredKey:
    """One reading of one recording's key, with how decided the reading was.

    ``margin`` is the winner's correlation less the runner-up's, and it is the
    number that says whether this was an answer or a coin toss. The correlation
    does not say that: measured against rekordbox's analysis of the same
    tracks, agreement rises with the margin band by band, while agreement
    across bands of the correlation is not even monotonic.

    The runner-up travels with it because a close call is two very different
    situations. Where the margin is under 0.05 and the runner-up is a
    neighbour on the wheel, the reading still mixes with the reference most of
    the time; where it is somewhere else on the wheel, it usually does not.
    """

    tonic: str
    mode: str
    confidence: float
    margin: float
    # `None` only when nothing else could be scored at all, which no profile in
    # this file produces — an absent runner-up is not the same fact as a
    # runner-up that lost by nothing.
    runner_up_tonic: str | None
    runner_up_mode: str | None

    @property
    def name(self) -> str:
        """The key as a musician writes it: `F# minor`."""
        return f"{self.tonic} {self.mode}"

    @property
    def camelot(self) -> str:
        """Where this key sits on the wheel: `11A`."""
        return _CAMELOT[(self.tonic, self.mode)]

    @property
    def runner_up_camelot(self) -> str | None:
        """Where the key this one beat sits on the wheel, if there was one."""
        if not self.runner_up_tonic or not self.runner_up_mode:
            return None
        return _CAMELOT.get((self.runner_up_tonic, self.runner_up_mode))


def camelot_neighbours(code: str) -> tuple[str, ...]:
    """The codes that mix with this one, written as the wheel defines them.

    Three moves and no others: one step around the wheel either way, and the
    switch between the inner and outer ring at the same number — the relative
    minor or major. Anything else is a jump the ear hears.

    Derived from the code rather than looked up in a table of pairs, so every
    code is answered by the same rule and no pair can be missing from a list.
    """
    number, ring = int(code[:-1]), code[-1]
    before = 12 if number == 1 else number - 1
    after = 1 if number == 12 else number + 1
    other = "A" if ring == "B" else "B"
    return (f"{before}{ring}", f"{after}{ring}", f"{number}{other}")


def measure_key(samples: np.ndarray) -> MeasuredKey | None:
    """Return the key these samples are most like, or ``None`` for too little audio.

    ``None`` rather than a guess: a two-second file has no key to find, and an
    answer drawn from too little audio would look as confident as any other.
    """
    if samples.size < _FRAME * 2:
        return None
    chroma = _chroma(samples)
    if not chroma.any():
        return None
    scored: list[tuple[float, str, str]] = []
    for index, tonic in enumerate(PITCH_CLASSES):
        for mode, profile in (("major", _MAJOR_PROFILE), ("minor", _MINOR_PROFILE)):
            correlation = float(np.corrcoef(chroma, np.roll(profile, index))[0, 1])
            if not np.isnan(correlation):
                scored.append((correlation, tonic, mode))
    if not scored:
        return None
    # Sorted rather than tracked in the loop, because the second place is as
    # much of the answer as the first and a running best discards it.
    # Twenty-four rows.
    scored.sort(reverse=True)
    best, tonic, mode = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    return MeasuredKey(
        tonic=tonic,
        mode=mode,
        confidence=best,
        margin=best - runner_up[0] if runner_up else 0.0,
        runner_up_tonic=runner_up[1] if runner_up else None,
        runner_up_mode=runner_up[2] if runner_up else None,
    )


def _chroma(samples: np.ndarray) -> np.ndarray:
    """Fold the whole recording's spectrum into twelve pitch classes.

    Every bin is mapped to its pitch class once, outside the loop, because the
    mapping is a fact about the frame size and the sample rate and not about
    the audio — doing it per frame is the difference between an album costing a
    second and costing a minute.
    """
    window = np.hanning(_FRAME)
    frequencies = np.fft.rfftfreq(_FRAME, 1 / SAMPLE_RATE)
    with np.errstate(divide="ignore", invalid="ignore"):
        midi = 69 + 12 * np.log2(np.maximum(frequencies, 1e-9) / 440.0)
    usable = (frequencies > _LOWEST_HZ) & (frequencies < _HIGHEST_HZ)
    pitch_class = np.mod(np.rint(midi).astype(int), 12)
    # One boolean mask per pitch class, built once and reused every frame.
    masks = [usable & (pitch_class == pc) for pc in range(12)]

    totals = np.zeros(12)
    for start in range(0, samples.size - _FRAME + 1, _HOP):
        spectrum = np.abs(np.fft.rfft(samples[start : start + _FRAME] * window))
        for pc, mask in enumerate(masks):
            totals[pc] += spectrum[mask].sum()
    total = totals.sum()
    return totals / total if total else totals


def camelot_for(tonic: str, mode: str) -> str | None:
    """Where a key sits on the wheel, asked by name rather than by measurement.

    Public because what is stored is a tonic and a mode, and the screen needs
    the code — reading `_CAMELOT` from outside would be two modules sharing one
    dictionary instead of one owning it.
    """
    return _CAMELOT.get((tonic, mode))
