"""What a stream that declares more range than a CD actually carries up there.

A file at 96 kHz has room for sound up to 48 kHz. One made at a lower rate and
lifted to 96 has nothing up there — not quiet sound, but an empty band, whose
level is the arithmetic floor of the samples rather than anything recorded.
A genuine recording at that rate has a **noise floor** above its music: the
converter's own, or the tape's, flat across tens of kilohertz. That is the
difference this module reads.

**It is read from decoded samples and not from ffmpeg's band filter**, and the
reason is a measurement. Held against pairs — thirty-six files of a real
library, each also taken down to 44.1 or 48 kHz and lifted back to 96 — the
band filter's reading separated nothing: the honest arm ran from -44 to -80 dB
below its reference band and the lifted arm from -64 to -105, so the two
overlapped by fifteen decibels, while every pair still ordered correctly. What
saturates is the filter, not the file. ``firequalizer`` builds a finite
response, so what it leaves in a band it was told to empty is its own leakage,
and the distance this question asks about is ten times the depth that filter
has. A window's sidelobes are far below it, so the same question answered bin
by bin has room to be answered.

Nothing here concludes anything: this measures, ``verdict`` decides.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from diglibrary.quality.framing import PcmReader
from diglibrary.quality.models import (
    DECLARES_MORE_THAN_A_CD_HERTZ,
    NOTHING_ABOVE_THE_REFERENCE_DB,
    HighBand,
)

TOP_BAND_FRAME = 65_536
"""Samples per window: 1.5 Hz per bin at 96 kHz.

Long on purpose. The reading is a level a hundred decibels under the music, and
a window's own spread is what sets how deep it can see; the sidelobes of this
one sit far below the floor being looked for, so what comes back is the file.
"""

TOP_BAND_WINDOWS = 10
"""How many windows are averaged, spread evenly across what was read.

Spread rather than consecutive, for the reason the edge probe gives: one bright
passage must not stand for the file, and one quiet one must not either.
"""

REFERENCE_BAND_HERTZ = (1_000, 5_000)
"""The band the top of a stream is held against, in hertz.

A level on its own says how loud the file is, and the question here is a
proportion. Low enough that every recording ever made is full here.
"""

TOP_BAND_OF_NYQUIST = (0.93, 0.99)
"""Where the top of a stream's own band is read, as a fraction of its Nyquist.

Stopping short of Nyquist on purpose: every converter rolls off before its own
limit, so the last hundredth is the filter rather than the recording, and
reading it would call every honest file empty.
"""

WALL_STEP_HERTZ = 1_000
"""How finely the frequency above which a stream holds nothing is reported."""

WALL_FLOOR_HERTZ = 20_000
"""The lowest frequency this module will name as a wall.

Below it the question stops being about a rate that promises more than a CD
and becomes the question ``edges`` and the spectral pass already answer, where
a wall means something else entirely and is judged by other lines.
"""


def _spectrum(mono: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray] | None:
    """Return the averaged power spectrum in decibels, and each bin's frequency."""
    if mono.size < TOP_BAND_FRAME * 2:
        return None
    starts = np.linspace(0, mono.size - TOP_BAND_FRAME, TOP_BAND_WINDOWS).astype(int)
    window = np.hanning(TOP_BAND_FRAME)
    total = np.zeros(TOP_BAND_FRAME // 2 + 1)
    for start in starts:
        total += np.abs(np.fft.rfft(mono[start : start + TOP_BAND_FRAME] * window)) ** 2
    power = total / len(starts)
    return 10 * np.log10(np.maximum(power, 1e-30)), np.fft.rfftfreq(TOP_BAND_FRAME, 1 / sample_rate)


def _median_between(levels: np.ndarray, hertz: np.ndarray, low: float, high: float) -> float | None:
    """The middle level inside one band, or ``None`` when no bin falls in it.

    The middle rather than the mean, so that one tone inside the band does not
    stand for the band: what is being described is a floor.
    """
    inside = (hertz >= low) & (hertz < high)
    return float(np.median(levels[inside])) if inside.any() else None


def _wall(levels: np.ndarray, hertz: np.ndarray, sample_rate: int, quiet: float) -> int | None:
    """The frequency above which every band is quiet, in hertz.

    Walked down from the top rather than up from the bottom, because what is
    wanted is where the quiet *begins* and not the first quiet band — a dip
    between two loud bands is not a wall, and walking up would stop at it.

    **The answer is the top of the lowest quiet band and not its bottom**, and
    the difference is whether the sentence built on it is true. A band is
    judged by its middle, so one straddling the real edge reads quiet while its
    lower half still holds audio: naming its bottom would promise nothing above
    a frequency that has something above it. Measured on audio lifted from
    48 kHz, where the band from 23.5 kHz reads quiet although the stream runs
    to 24. Naming the top can only overstate, and an overstatement here is
    still true — nothing above 23.5 kHz means nothing above 24.5 either.
    """
    top = int(sample_rate / 2 * TOP_BAND_OF_NYQUIST[1])
    wall: int | None = None
    for low in range(top - WALL_STEP_HERTZ, WALL_FLOOR_HERTZ - 1, -WALL_STEP_HERTZ):
        level = _median_between(levels, hertz, low, low + WALL_STEP_HERTZ)
        if level is None or level > quiet:
            break
        wall = low + WALL_STEP_HERTZ
    return wall


def measure_top_band(mono: np.ndarray, sample_rate: int) -> HighBand | None:
    """Return what sits at the top of this stream's band, or ``None``.

    The wall is located only once the top has been found empty: a stream whose
    top is full has no such frequency, and saying one anyway would be a number
    describing nothing.
    """
    read = _spectrum(mono, sample_rate)
    if read is None:
        return None
    levels, hertz = read
    nyquist = sample_rate / 2
    low = int(nyquist * TOP_BAND_OF_NYQUIST[0])
    high = int(nyquist * TOP_BAND_OF_NYQUIST[1])
    reference = _median_between(levels, hertz, *REFERENCE_BAND_HERTZ)
    top = _median_between(levels, hertz, low, high)
    if reference is None or top is None:
        return None
    band = HighBand(reference_db=reference, top_db=top, top_low_hertz=low, top_high_hertz=high)
    if band.headroom_db > NOTHING_ABOVE_THE_REFERENCE_DB:
        return band
    quiet = reference + NOTHING_ABOVE_THE_REFERENCE_DB
    return replace(band, wall_hertz=_wall(levels, hertz, sample_rate, quiet))


class TopBandProbe:
    """Purpose: measure the top of one file's band, from disk to numbers.

    Responsibilities: read a stretch of audio and describe what is at the top
    of what the stream declares. Boundaries: it decides nothing and writes
    nothing, and it answers only for a stream that declares more than a CD —
    there is no band above a CD's ceiling to be empty. Dependencies: an
    injected ``PcmReader``. Collaborators: ``FfmpegQualityAnalyzer``, which
    carries the result into ``TrackAnalysis``. Constraints: ``None`` for
    anything that could not be measured, never a clean reading.
    """

    def __init__(self, reader: PcmReader, seconds: float, skip: float = 0.0) -> None:
        """Create a probe reading ``seconds`` of audio from ``skip`` onwards."""
        self._reader = reader
        self._seconds = seconds
        self._skip = skip

    def measure(self, path: Path, sample_rate: int) -> HighBand | None:
        """Return this file's top-of-band reading, or ``None``.

        The rate is asked before the audio is, so a library of CDs costs
        nothing here: the decode is the expensive half and a stream with no
        band above a CD's ceiling has nothing for it to read.
        """
        if sample_rate <= DECLARES_MORE_THAN_A_CD_HERTZ:
            return None
        pcm = self._reader.read(path, self._seconds, self._skip)
        if pcm is None:
            return None
        return measure_top_band(pcm.mean(axis=1), sample_rate)
