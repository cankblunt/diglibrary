"""Look for the encoder's own frame grid in audio that claims to be lossless.

The spectrum cannot answer this one. Above the trusted line an encoder's wall
and an honest converter's anti-aliasing wall are the same shape, and an
encoder run with no low-pass leaves no wall at all — so a rule that reads
where the audio *ends* will never convict either. This module reads something
else: **where the audio was cut into frames**.

MPEG-1 Layer III quantizes on a fixed 576-sample granule grid. Re-running the
encoder's own analysis chain — the ISO polyphase filterbank, the long-block
MDCT, the alias-reduction butterflies — recovers approximately those quantized
coefficients, but only at the alignment the encoder used. So every one of the
576 alignments is tried, and each is scored by how much of the spectrum sits
near zero when read from there. Audio that went through MP3 shows one sharp
peak; audio that never met an encoder is alignment-blind, because nothing in a
converter's filter is framed.

Two independent things are measured, and the rule (in ``verdict``) asks for
both:

* **the peak's z-score** against the sweep's own spread — self-normalizing, so
  no absolute threshold has to travel between files;
* **how many of the six readings agree on which alignment won** — two channel
  views times three thresholds. Independent draws landing on the same one of
  576 alignments is not something honest audio does.

Measured on the paired benchmark under `benchmark/`: the honest arms peak low
and never get more than two readings to agree; every LAME arm from 128 to 320,
**including the no-low-pass 320 that no spectral rule reaches**, peaks well
above the highest honest file, and nearly every file in them agrees six times
out of six.

Nothing here concludes anything: this measures, ``verdict`` decides.
"""

from __future__ import annotations

import logging
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

SUBBANDS = 32
GRANULE = 576
"""Samples in one MP3 granule: 18 filterbank frames of 32 samples."""

ALIGNMENTS = GRANULE
"""How many alignments exist, and therefore how many are tried."""

ALPHAS: tuple[float, ...] = (1e-2, 1e-3, 1e-4)
"""Near-zero thresholds, relative to the file's own median coefficient.

Three rather than one because they are read as three independent witnesses:
the rule asks whether they agree on the winning alignment, and agreement is
only evidence when the readings could have disagreed.
"""

SECONDS = 8.0
"""How much audio one sweep reads. Eight seconds is 600-odd granules per
alignment, which was enough to separate every arm of the benchmark."""

SKIP_SECONDS = 4.0
"""Where the read starts. Not the first second: a fade-in is quiet, and a
quiet passage quantizes to near-zero everywhere, which flatters every
alignment equally and measures nothing."""

RATES_AN_ENCODER_WRITES_AT: frozenset[int] = frozenset(
    {8_000, 11_025, 12_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000}
)
"""Every sample rate MPEG-1, -2 and -2.5 Layer III define, and no others.

An encoder frames the audio at the rate it encoded, so a file at any other rate
arrived there by resampling — and **a resampler smears the grid away**, which is
the very thing ``FfmpegPcmReader`` is built never to cause. One 128 kbps
encode decoded and written twice shows it: at 44,100 Hz the peak stands far
above the threshold with all six readings agreeing, and the same audio at
96,000 Hz reads like audio that never met an encoder, with none agreeing.

The list is the standard's, not a record of the rates met so far, so a rate
nobody has seen yet falls on the side where an encoder could not have written.
"""


def rate_could_carry_a_grid(sample_rate: int | None) -> bool:
    """Report whether a reading taken at this rate could have found a grid at all.

    **One-sided, and the screen must say it that way.** A rate outside the list
    proves the audio was resampled and therefore that the sweep was looking for
    something no longer there; a rate inside it proves nothing in return, since
    44,100 Hz is also where a resampler lands. So this answers *could this
    reading mean anything*, and never *is this file honest* — a low reading at an
    unlisted rate is not evidence, and reading it as an acquittal is the mistake
    this exists to refuse.
    """
    return sample_rate in RATES_AN_ENCODER_WRITES_AT


_TABLE = Path(__file__).with_name("enwindow_iso.txt")

ENWINDOW = np.array(
    [
        float(line)
        for line in _TABLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ],
    dtype=np.float64,
)
"""The 512-tap analysis window C of ISO/IEC 11172-3, Table C.1."""

# Analysis matrixing M[sb, k] = cos((2 sb + 1)(k - 16) pi / 64), as dist10.
_SB = np.arange(SUBBANDS)[:, None]
_K = np.arange(64)[None, :]
MATRIX = np.cos((2 * _SB + 1) * (_K - 16) * np.pi / 64.0)

# Long-block MDCT with its sine window folded in, as dist10's mdct_sub:
# X[m] = sum_i win[i] z[i] cos(pi/72 (2i + 1 + 18)(2m + 1)).
_I = np.arange(36)[:, None]
_M = np.arange(18)[None, :]
MDCT = np.sin(np.pi / 36.0 * (np.arange(36) + 0.5))[:, None] * np.cos(
    np.pi / 72.0 * (2 * _I + 1 + 18) * (2 * _M + 1)
)

# Alias-reduction butterflies, ISO Table B.9.
_CI = np.array([-0.6, -0.535, -0.33, -0.185, -0.095, -0.041, -0.0142, -0.0037])
_CS = 1.0 / np.sqrt(1.0 + _CI * _CI)
_CA = _CI * _CS


READINGS = 6
"""Two channel views times three thresholds, which is what one sweep produces."""


@dataclass(frozen=True, slots=True)
class FrameGrid:
    """Purpose: report what one sweep found, without judging it.

    Responsibilities: carry the peak z-score, which alignment won, and **how
    many of the six readings landed on it**. Boundaries: it convicts nothing —
    the rule that reads these numbers lives in ``verdict``, so the threshold can
    move without this module knowing. Dependencies: none. Collaborators:
    ``TrackAnalysis`` and the verdict rules. Constraints: a file too short or
    unreadable produces no instance at all rather than a zero, because a zero
    here would read as *measured and clean*.

    **A count rather than a flag.** Asked as unanimity, one noisy reading
    answers for the other five: a transcode can put one alignment at the top of
    the two coarse readings of both views while the finest reading — the one at
    the lowest threshold, which is the one that picks up noise — lands
    somewhere different. Four of six agreeing would then read as *no
    agreement*.
    """

    peak_z: float
    offset: int
    agreeing_readings: int
    """How many of the six readings chose ``offset``. Six is unanimity."""

    @property
    def readings_agree(self) -> bool:
        """Report whether every one of the six readings chose ``offset``."""
        return self.agreeing_readings == READINGS


class PcmReader(Protocol):
    """Decode part of a file to PCM at its own sample rate.

    Injected for the same reason the analyzer's command runner is: a fake
    reader hands back synthesized audio, so every test of this is deterministic
    and offline.
    """

    def read(self, path: Path, seconds: float, skip: float) -> np.ndarray | None:
        """Return float PCM shaped (samples, channels), or ``None``."""


class FfmpegPcmReader:
    """Purpose: hand this module raw samples, at the file's own rate.

    Responsibilities: run ffmpeg for one excerpt and reshape its bytes.
    Boundaries: it measures nothing. Dependencies: an ffmpeg binary.
    Collaborators: ``FrameGridProbe``. Constraints: **it never resamples.** The
    trace this module hunts is a sample-rate-bound grid, and a resampler smears
    it: 48 kHz sources decoded at a forced 44.1 kHz read flat on every arm of
    the benchmark.
    """

    def __init__(
        self,
        executable: str = "ffmpeg",
        timeout_seconds: float = 120.0,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a reader for one ffmpeg executable.

        The logger is what keeps a silent failure from being invisible: this
        reader answers ``None`` for a file it could not decode, and ``None`` is
        also what an ordinary short file answers. Without a line in the log, a
        wrong binary or a blocked pipe would turn the whole measurement off
        across a library and read exactly like a library with nothing to find.
        """
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger("diglibrary")

    def read(self, path: Path, seconds: float, skip: float) -> np.ndarray | None:
        """Return stereo float32 PCM at the file's native rate, or ``None``."""
        arguments = [
            self._executable, "-nostdin", "-v", "error",
            "-ss", str(skip), "-i", str(path),
            "-t", str(seconds), "-ac", "2", "-f", "f32le", "-",
        ]  # fmt: skip
        try:
            done = subprocess.run(
                arguments, capture_output=True, timeout=self._timeout_seconds, check=False
            )
        except Exception as error:
            # Anything at all, for the same reason as in the analyzer: one
            # unreadable file must never end a survey of a whole library. Said
            # out loud, because this failure is otherwise indistinguishable
            # from a file with nothing to find.
            self._logger.warning(
                "Audio could not be decoded for the frame grid; the file is left unread.",
                extra={
                    "operation": "quality.framing.unreadable",
                    "file": str(path),
                    "error": str(error),
                },
            )
            return None
        if done.returncode != 0 or len(done.stdout) < 8:
            self._logger.warning(
                "Audio could not be decoded for the frame grid; the file is left unread.",
                extra={
                    "operation": "quality.framing.unreadable",
                    "file": str(path),
                    "returncode": done.returncode,
                    "bytes": len(done.stdout),
                },
            )
            return None
        pcm = np.frombuffer(done.stdout, dtype=np.float32)
        return pcm.reshape(-1, 2) if pcm.size >= 2 else None


def subband_frames(signal: np.ndarray, phase: int) -> np.ndarray:
    """Return the polyphase filterbank's output for frames ending at this phase.

    dist10 keeps the newest sample at ``x[0]``, so each frame's input is the
    previous 512 samples reversed, windowed by C, folded modulo 64, matrixed.
    """
    usable = (signal.shape[0] - 512 - phase) // 32
    ends = 512 + phase + 32 * np.arange(usable)
    index = ends[:, None] - 1 - np.arange(512)[None, :]
    windowed = signal[index] * ENWINDOW[None, :]
    folded = windowed.reshape(usable, 8, 64).sum(axis=1)
    return folded @ MATRIX.T


def granule_spectra(frames: np.ndarray) -> np.ndarray:
    """Return ``|X|`` for every 36-frame window, shaped (windows, 32, 18).

    Frequency inversion of the odd samples of odd subbands and the
    alias-reduction butterflies are applied, so what comes out is the encoder's
    own coefficients rather than a generic time-frequency picture — which is
    the whole point: a generic one is alignment-blind.
    """
    inverted = frames.copy()
    inverted[1::2, 1::2] *= -1.0
    windows = np.lib.stride_tricks.sliding_window_view(inverted, 36, axis=0)
    spectra = np.einsum("tbi,im->tbm", windows, MDCT, optimize=True)
    lower = spectra[:, :-1, 17:9:-1].copy()
    upper = spectra[:, 1:, :8].copy()
    spectra[:, :-1, 17:9:-1] = lower * _CS + upper * _CA
    spectra[:, 1:, :8] = upper * _CS - lower * _CA
    return np.abs(spectra)


def sweep(signal: np.ndarray, alphas: Sequence[float] = ALPHAS) -> np.ndarray:
    """Return, per threshold, the near-zero fraction at each of the 576 alignments."""
    fractions = np.zeros((len(alphas), SUBBANDS, 18), dtype=np.float64)
    median: float | None = None
    for phase in range(SUBBANDS):
        magnitudes = granule_spectra(subband_frames(signal, phase))
        if median is None:
            # One scale for the whole file, taken once: a per-alignment scale
            # would normalize away the very difference being looked for.
            median = float(np.median(magnitudes))
        flat = magnitudes.reshape(magnitudes.shape[0], -1)
        for index, alpha in enumerate(alphas):
            small = (flat < alpha * median).mean(axis=1)
            for granule_phase in range(18):
                fractions[index, phase, granule_phase] = small[granule_phase::18].mean()
    return fractions.reshape(len(alphas), -1)


def peak_of(scores: np.ndarray) -> tuple[float, int]:
    """Return how far the best alignment stands above the rest, and which it is.

    Measured in robust deviations — median and MAD — so a second strong
    alignment cannot inflate the spread and hide the first, and so the number
    means the same thing in a loud file and a quiet one.
    """
    middle = float(np.median(scores))
    spread = max(float(np.median(np.abs(scores - middle))) * 1.4826, 1e-12)
    best = int(np.argmax(scores))
    return (float(scores[best]) - middle) / spread, best


def measure(pcm: np.ndarray, alphas: Sequence[float] = ALPHAS) -> FrameGrid | None:
    """Return what the sweep found in this PCM, or ``None`` if there is too little.

    Two channel views, deliberately: the left channel alone, and the mid
    (L+R)/2. A joint-stereo encoder quantizes the mid signal, so a trace can be
    stronger there than in either channel — and two views that can disagree are
    what make their agreement worth anything.
    """
    if pcm.ndim != 2 or pcm.shape[0] < 512 + GRANULE * 40:
        return None
    left = pcm[:, 0].astype(np.float64)
    right = pcm[:, 1].astype(np.float64) if pcm.shape[1] > 1 else left
    views = (left, (left + right) / np.sqrt(2.0))
    peaks: list[float] = []
    winners: list[int] = []
    for position, view in enumerate(views):
        # **Both views are always swept.** Skipping the second view when the
        # first three readings already disagree is sound only if unanimity is
        # the question. For a count it is not: a file whose first view scatters
        # can still put four of six on one alignment, and the mid view is
        # exactly where a joint-stereo encoder's trace lives. The second sweep
        # doubles the cost of the measurement per file.
        _ = position
        scores = sweep(view, alphas)
        for index in range(len(alphas)):
            z, offset = peak_of(scores[index])
            peaks.append(z)
            winners.append(offset)
    if not peaks:
        return None
    # The alignment most readings chose, and how many chose it — not the
    # alignment of the loudest reading, which is one vote of six.
    votes = Counter(winners)
    chosen, agreeing = votes.most_common(1)[0]
    return FrameGrid(peak_z=max(peaks), offset=chosen, agreeing_readings=agreeing)


class FrameGridProbe:
    """Purpose: measure one file's frame grid, from disk to numbers.

    Responsibilities: read an excerpt and sweep it. Boundaries: it decides
    nothing and writes nothing. Dependencies: an injected ``PcmReader``.
    Collaborators: ``FfmpegQualityAnalyzer``, which carries the result into
    ``TrackAnalysis``. Constraints: ``None`` for anything that could not be
    measured — an unreadable file, a stream too short, a decoder that refused —
    because a measurement absent must never read as a measurement clean.
    """

    def __init__(
        self, reader: PcmReader, seconds: float = SECONDS, skip: float = SKIP_SECONDS
    ) -> None:
        """Create a probe reading one excerpt per file."""
        self._reader = reader
        self._seconds = seconds
        self._skip = skip

    def measure(self, path: Path) -> FrameGrid | None:
        """Return this file's frame grid reading, or ``None``."""
        pcm = self._reader.read(path, self._seconds, self._skip)
        if pcm is None:
            return None
        return measure(pcm)
