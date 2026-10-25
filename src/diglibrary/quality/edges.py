"""Look for an encoder's low-pass edge, moment by moment rather than on average.

The spectral pass reads the energy left *above* each of eight frequencies,
across a whole minute. Both halves of that reading lose an encoder's wall in a
real library. **Above** means whatever sits at the very top counts in every
probe: noise-shaped dither rising towards Nyquist, which an editor adds when it
saves a decoded MP3 at 16 bits, lifts all eight readings to one level and the
wall disappears from all of them. **Across a minute** means a passage that
carries content past the wall — a bright cymbal, an encoder that widened its
band for a few seconds — fills the average in, while the stretches either side
of it stop dead at one frequency.

This reads the other way. Each frame of a third of a second is asked where its
spectrum falls hardest, within a few hundred hertz; and the file is asked how
much of its time falls at **one** frequency. An encoder's low-pass sits where
the encoder put it, so its edge recurs at the same place frame after frame.
Music has no such place: a sharp fall in honest audio is a moment, and the next
one falls somewhere else.

Measured on the paired benchmark under `benchmark/`, and on a real library,
before any line was drawn: see ``EDGE_SHARE_TRUST`` in ``verdict``.

Nothing here concludes anything: this measures, ``verdict`` decides.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from diglibrary.quality.framing import PcmReader

EDGE_FRAME = 16_384
"""Samples per frame: 2.7 Hz per bin at 44.1 kHz, a third of a second long.

Fine enough that a fall of a few hundred hertz is many bins wide, and short
enough that a passage which carries content past the wall is its own frame
instead of being averaged into its neighbours.
"""

EDGE_SMOOTHING_HZ = 600.0
"""How wide a run of bins is averaged, in decibels, before a fall is read.

**Wide on purpose, and the reason is a sustained note.** A synthesizer or an
organ holding a chord draws its harmonics as fixed lines, and right above a
strong line the level drops by tens of decibels — at the same frequency, frame
after frame, which is exactly what this module counts. Read between two narrow
windows, such a line is a wall: measured on a real library, held synth chords
and drones were convicted that way while every honest file of the benchmark
was not. Averaged in decibels over six hundred hertz, one line is a few bins
among two hundred and barely moves the level, while a wall still empties the
whole window above it.
"""

EDGE_SPAN_HZ = 400.0
"""How far either side of a frequency the two windows are centred, in hertz.

With ``EDGE_SMOOTHING_HZ`` this compares 100 to 700 Hz below a frequency with
100 to 700 Hz above it. A low-pass is a couple of hundred hertz from pass to
stop, so it is the whole difference between the two; music running out of
treble takes kilohertz to fall as far.
"""

EDGE_FALL_DB = 30.0
"""How far a frame must fall across the span for that frame to have an edge.

Priced on the paired benchmark: at 30 dB no honest file — untouched, or through
a steep 20 kHz converter filter — has an edge in more than a tenth of its
frames, while 20 dB lets the transients of a bright master through.
"""

EDGE_FLOOR_HZ = 11_000
"""The lowest frequency an edge is looked for at.

Below the lowest wall a common encoder leaves at 44.1 kHz, so every rate a
transcode is worth naming is inside the search.
"""

EDGE_CEILING_HZ = 19_000
"""The highest frequency an edge is looked for at.

**The same line as ``CUTOFF_TRUST_HZ``**, and a test holds the two equal. Above
it an honest converter's anti-aliasing filter is an edge exactly like an
encoder's: measured on the paired benchmark, LAME 256 and a master filtered at
20 kHz fall within a decibel of each other there. Looked for above the line, the
honest filter would win the count on a file that also stops at 16 kHz in some
stretches, and hide the encoder.
"""

EDGE_TOLERANCE_HZ = 150.0
"""How close to the commonest edge another edge must fall to be the same one.

An encoder's low-pass is one frequency, and the reading wanders across a
handful of bins as the music under the wall changes.
"""

EDGE_BIN_HZ = 50
"""The width of the bins edges are counted in when looking for the commonest."""

MINIMUM_EDGE_FRAMES = 4
"""Below this many frames a share is a coin toss, so nothing is reported."""


@dataclass(frozen=True, slots=True)
class FilterEdge:
    """Purpose: report how much of a file stops at one frequency, without judging it.

    Responsibilities: carry the share of frames whose edge falls at the
    commonest frequency, and that frequency. Boundaries: it convicts nothing —
    the rule that reads these numbers lives in ``verdict``. Dependencies: none.
    Collaborators: ``TrackAnalysis`` and the verdict rules. Constraints: a file
    too short or unreadable produces no instance at all, because a zero here
    would read as *measured and clean*.
    """

    share: float
    """The fraction of all frames read whose edge falls at ``hertz``, 0 to 1."""

    hertz: int | None
    """The commonest edge frequency, or ``None`` when no frame had an edge."""


def measure_edges(mono: np.ndarray, sample_rate: int) -> FilterEdge | None:
    """Return how much of this audio stops at one frequency, or ``None``.

    ``mono`` is float PCM at ``sample_rate``. Every frame counts in the
    denominator, including the quiet ones: a share of *the frames that had an
    edge* would read a file with one edge in a minute as a file that stops
    there all the time.
    """
    count = len(mono) // EDGE_FRAME
    if count < MINIMUM_EDGE_FRAMES:
        return None
    frequencies = np.fft.rfftfreq(EDGE_FRAME, 1.0 / sample_rate)
    step = frequencies[1]
    smoothing = max(1, int(EDGE_SMOOTHING_HZ / step))
    span = max(1, int(EDGE_SPAN_HZ / step))
    ceiling = min(EDGE_CEILING_HZ, sample_rate / 2 - EDGE_SPAN_HZ)
    searched = np.where((frequencies > EDGE_FLOOR_HZ) & (frequencies < ceiling))[0]
    if len(searched) == 0:
        return None
    window = np.hanning(EDGE_FRAME)
    kernel = np.ones(smoothing) / smoothing
    edges: list[float] = []
    for index in range(count):
        frame = mono[index * EDGE_FRAME : (index + 1) * EDGE_FRAME] * window
        power = np.abs(np.fft.rfft(frame)) ** 2
        level = np.convolve(10 * np.log10(power + 1e-20), kernel, "same")
        falls = level[searched - span] - level[searched + span]
        hardest = int(np.argmax(falls))
        if falls[hardest] >= EDGE_FALL_DB:
            edges.append(float(frequencies[searched[hardest]]))
    if not edges:
        return FilterEdge(share=0.0, hertz=None)
    binned = Counter(round(edge / EDGE_BIN_HZ) * EDGE_BIN_HZ for edge in edges)
    commonest = binned.most_common(1)[0][0]
    near = sum(1 for edge in edges if abs(edge - commonest) <= EDGE_TOLERANCE_HZ)
    return FilterEdge(share=near / count, hertz=commonest)


class FilterEdgeProbe:
    """Purpose: measure one file's filter edge, from disk to numbers.

    Responsibilities: read the same stretch the spectral pass reads and count
    its edges. Boundaries: it decides nothing and writes nothing. Dependencies:
    an injected ``PcmReader``. Collaborators: ``FfmpegQualityAnalyzer``, which
    carries the result into ``TrackAnalysis``. Constraints: ``None`` for
    anything that could not be measured, never a clean reading.
    """

    def __init__(self, reader: PcmReader, seconds: float, skip: float = 0.0) -> None:
        """Create a probe reading ``seconds`` of audio from ``skip`` onwards."""
        self._reader = reader
        self._seconds = seconds
        self._skip = skip

    def measure(self, path: Path, sample_rate: int) -> FilterEdge | None:
        """Return this file's edge reading, or ``None``."""
        pcm = self._reader.read(path, self._seconds, self._skip)
        if pcm is None:
            return None
        return measure_edges(pcm.mean(axis=1), sample_rate)
