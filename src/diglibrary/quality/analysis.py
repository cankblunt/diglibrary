"""Measuring one audio file with ffmpeg, without deciding anything about it."""

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from diglibrary.quality.framing import FrameGridProbe
from diglibrary.quality.models import (
    BandEnergy,
    PassBand,
    SpectralProfile,
    TimeDomainStats,
    TrackAnalysis,
)

PROBE_FREQUENCIES = (13_000, 14_000, 15_000, 16_000, 17_500, 19_000, 20_000, 21_500)
"""Where to ask whether the audio still exists, in hertz.

These sit around the low-pass frequencies the common lossy encoders use —
roughly 16 kHz at 128 kbps, 18 kHz at 192, 19 kHz at 256 and 20 kHz at 320 — so
the step between two of them shows where the audio was cut off, whatever the
container now claims it is.

**Three rungs below 16 kHz, because a wall is only visible from underneath.**
In a textbook 128 kbps encode read from 16 kHz up, every band is already dead
and the steps between them are a couple of decibels at most, so a step rule
would let it pass. With a rung at 15 kHz the step is tens of decibels and the
wall is plain. The cost is roughly half as much time again per file.
"""

DEEP_PROBE_FREQUENCIES = (
    13_000,
    14_000,
    15_000,
    16_000,
    16_500,
    17_500,
    18_000,
    18_500,
    19_000,
    19_500,
    20_000,
    20_500,
    21_000,
    21_500,
)
"""The rungs used when one album is analyzed in depth rather than a library.

Fourteen instead of eight. Nothing below 128 kbps is measured, so the lowest
rungs sit where that bitrate cuts. The wall lands inside a 500 Hz interval
instead of a 1.5 kHz one.

It is a superset of ``PROBE_FREQUENCIES`` on purpose. The verdict reads the
16 kHz and 19 kHz rungs for its fall and the last rung for its ceiling, so a
deep measurement and an ordinary one produce numbers the same rule can compare —
which is what lets a fresh result be held up against a stored one.
"""

BAND_EDGES = tuple((low, low + 1_000) for low in range(15_000, 22_000, 1_000))
"""Where the band-pass readings are taken, in hertz.

One kilohertz wide, because the question is the *shape* of what is left rather
than how much: a floor is flat across neighbours and falling music is not.
"""

ANALYSIS_SECONDS = 60
"""How much audio to measure per file.

A minute is enough for the highest frequencies to appear if the recording has
any, and keeps a whole library measurable in one sitting. It is a deliberate
trade: a track whose only bright moment is in its final seconds can read as
quieter than it is, which is why a verdict weighs a whole album.
"""


KNOWN_FFMPEG_PATHS = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/local/bin/ffmpeg",
)
"""Where ffmpeg lives when PATH does not say.

An application launched from the Dock inherits no shell environment, so a
Homebrew ffmpeg in `/opt/homebrew/bin` is invisible to `shutil.which` even
though every terminal on the machine finds it. Searching the known locations
is what keeps the window's behaviour the same as the terminal's.
"""


def find_ffmpeg() -> str | None:
    """Return a usable ffmpeg, from PATH or from where it is normally installed."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    return next((path for path in KNOWN_FFMPEG_PATHS if os.access(path, os.X_OK)), None)


class CommandRunner(Protocol):
    """Run one external command and return what it wrote to standard error.

    Injected rather than called directly so that every test is deterministic
    and offline: ffmpeg reports its measurements on stderr, so a fake runner
    replays a recorded report without a subprocess or a media file.
    """

    def run(self, arguments: Sequence[str]) -> str:
        """Return the command's standard error, or raise ``OSError`` if it fails."""


class SubprocessCommandRunner:
    """Purpose: run ffmpeg as a child process and hand back its report.

    Responsibilities: invoke the binary, wait for it, and return stderr.
    Boundaries: it parses nothing and knows no ffmpeg argument — the analyzer
    builds the command. Dependencies: ``subprocess`` and an ffmpeg on PATH.
    Collaborators: ``FfmpegQualityAnalyzer``. Constraints: a non-zero exit is
    not an error by itself, because ffmpeg reports measurements and exits
    non-zero for conditions that still produced a usable report; an absent
    binary is an ``OSError`` and is allowed to propagate.
    """

    def __init__(self, executable: str = "ffmpeg", timeout_seconds: float = 120.0) -> None:
        """Create a runner for one ffmpeg executable."""
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    def run(self, arguments: Sequence[str]) -> str:
        """Return ffmpeg's standard error for one invocation."""
        completed = subprocess.run(
            [self._executable, *arguments],
            capture_output=True,
            # ffmpeg echoes the file's own tags into this report, and a tag
            # written in Latin-1 is not UTF-8. Decoding strictly would raise on
            # the first such file and end the whole survey. Nothing is lost by
            # replacing the byte: what is read from here is numbers.
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout_seconds,
            check=False,
        )
        return completed.stderr


class FfmpegQualityAnalyzer:
    """Purpose: measure what an audio file actually contains, one file at a time.

    Responsibilities: read the stream's own properties, measure the energy left
    above each probe frequency, and collect the time-domain statistics that
    reveal damage. Boundaries: it concludes nothing — it never says a file is a
    transcode, because that is a rule and rules live in ``verdict``. It also
    never writes, renames, or tags. Dependencies: an injected ``CommandRunner``
    and ffmpeg's ``firequalizer`` and ``astats`` filters. Collaborators: the
    verdict rules and the application layer that walks a library. Constraints:
    probe frequencies above the stream's Nyquist limit are skipped rather than
    measured, because they would read as silence for every file and would make
    a 48 kHz master look like a 128 kbps transcode.
    """

    def __init__(
        self,
        runner: CommandRunner,
        logger: logging.Logger,
        frequencies: Sequence[int] = PROBE_FREQUENCIES,
        seconds: int | None = ANALYSIS_SECONDS,
        frame_grid: FrameGridProbe | None = None,
    ) -> None:
        """Create an analyzer for one set of probe frequencies.

        ``seconds`` of ``None`` measures the whole file. That is what the deep
        pass does, and it retires the caveat the minute-long default carries in
        its own docstring: a track whose only bright moment is in its last
        seconds reads quieter than it is.

        ``frame_grid`` is asked only of files whose container promises to have
        lost nothing, and only when one is given: it decodes audio rather than
        reading ffmpeg's report, so it is the expensive half of this pass and
        the fake runners of a hundred tests know nothing about it.
        """
        self._runner = runner
        self._logger = logger
        self._frequencies = tuple(sorted(frequencies))
        self._seconds = seconds
        self._frame_grid = frame_grid

    def pass_bands(
        self, path: Path, sample_rate: int, edges: Sequence[tuple[int, int]] = BAND_EDGES
    ) -> tuple[PassBand, ...]:
        """Return the energy inside each band, rather than above each frequency.

        This is the reading a spectrogram viewer is otherwise opened for: after
        the wall, a transcode leaves the encoder's noise floor and it is FLAT,
        while music that has simply run out of treble keeps falling. The
        cumulative bands the verdict uses cannot show it — they only ever
        descend.

        Nothing here concludes anything. No rule convicts on a flat floor: this
        measures and the reader decides, until such a rule has been calibrated
        against files of known origin.
        """
        nyquist = sample_rate / 2
        bands: list[PassBand] = []
        for low, high in edges:
            if low >= nyquist:
                break
            try:
                report = self._runner.run(self._pass_band_arguments(path, low, high))
            except OSError:
                break
            level = _rms_level(report)
            if level is None:
                break
            bands.append(PassBand(low_hertz=low, high_hertz=high, rms_db=level))
        return tuple(bands)

    def analyze(self, path: Path) -> TrackAnalysis | None:
        """Return what this file measures, or ``None`` when it cannot be read."""
        try:
            report = self._runner.run(self._statistics_arguments(path))
        except Exception as error:
            # Anything at all, deliberately. A timeout is a SubprocessError and
            # a report that will not decode is a ValueError; either one
            # escaping a narrower clause ends the survey of a whole library
            # over one file. The promise this method makes is already
            # "``None`` when it cannot be read".
            self._logger.warning(
                "Audio could not be measured; the file is reported as unanalyzed.",
                extra={
                    "operation": "quality.analyze.unreadable",
                    "file": str(path),
                    "error": str(error),
                },
            )
            return None
        stream = _stream_properties(report)
        if stream is None:
            self._logger.warning(
                "No audio stream was found to measure.",
                extra={"operation": "quality.analyze.no_stream"},
            )
            return None
        codec, sample_rate, bitrate = stream
        time_domain = _time_domain(report)
        if time_domain is None:
            return None
        analysis = TrackAnalysis(
            path=path,
            declared_codec=codec,
            sample_rate=sample_rate,
            declared_bitrate=bitrate,
            spectral=self._spectrum(path, sample_rate),
            time_domain=time_domain,
        )
        # **Only where the container claims to have lost nothing.** An MP3's
        # grid is its own and proves no lie; asking anyway would spend seconds
        # per file to learn that an MP3 is an MP3. A silent stream is skipped
        # for the reason the verdict skips it: near-zero everywhere flatters
        # every alignment equally.
        if self._frame_grid is None or not analysis.is_lossless_container or time_domain.is_silent:
            return analysis
        return replace(analysis, frame_grid=self._frame_grid.measure(path))

    def _spectrum(self, path: Path, sample_rate: int) -> SpectralProfile:
        nyquist = sample_rate / 2
        bands: list[BandEnergy] = []
        for frequency in self._frequencies:
            if frequency >= nyquist:
                break
            try:
                report = self._runner.run(self._band_arguments(path, frequency))
            except OSError:
                break
            level = _rms_level(report)
            if level is None:
                break
            bands.append(BandEnergy(hertz=frequency, rms_db=level))
        return SpectralProfile(bands=tuple(bands))

    def _limit(self) -> tuple[str, ...]:
        """Return ffmpeg's own way of saying how much to read, or nothing at all."""
        return () if self._seconds is None else ("-t", str(self._seconds))

    def _statistics_arguments(self, path: Path) -> tuple[str, ...]:
        return (
            "-nostdin",
            *self._limit(),
            "-i",
            str(path),
            "-af",
            "astats=metadata=1:reset=0",
            "-f",
            "null",
            "-",
        )

    def _pass_band_arguments(self, path: Path, low: int, high: int) -> tuple[str, ...]:
        # The same wall as the probes, twice: `between` keeps one band and
        # silences everything on either side of it, so what `astats` reports is
        # the energy inside that band and not the energy above its floor.
        return (
            "-nostdin",
            *self._limit(),
            "-i",
            str(path),
            "-af",
            f"firequalizer=gain='if(between(f,{low},{high}),0,-200)',astats=metadata=1:reset=0",
            "-f",
            "null",
            "-",
        )

    def _band_arguments(self, path: Path, frequency: int) -> tuple[str, ...]:
        # firequalizer builds an arbitrary FIR response, so the cut is a wall
        # rather than a slope. A biquad highpass leaks the loud content below
        # the cut into the measurement and cannot tell a 128 kbps transcode
        # from an untouched master.
        return (
            "-nostdin",
            *self._limit(),
            "-i",
            str(path),
            "-af",
            f"firequalizer=gain='if(lt(f,{frequency}),-200,0)',astats=metadata=1:reset=0",
            "-f",
            "null",
            "-",
        )


_STREAM_PATTERN = re.compile(
    r"Stream #\d+:\d+.*?: Audio: (?P<codec>[\w-]+).*?(?P<rate>\d+) Hz", re.DOTALL
)
_BITRATE_PATTERN = re.compile(r"bitrate: (\d+) kb/s")
_OVERALL_BITRATE_PATTERN = re.compile(r"Duration:.*?bitrate: (\d+) kb/s", re.DOTALL)


def _stream_properties(report: str) -> tuple[str, int, int | None] | None:
    """Return codec, sample rate, and declared bitrate from ffmpeg's own banner."""
    match = _STREAM_PATTERN.search(report)
    if match is None:
        return None
    sample_rate = int(match.group("rate"))
    if sample_rate <= 0:
        return None
    stream_bitrate = _BITRATE_PATTERN.search(report[match.start() :])
    overall = _OVERALL_BITRATE_PATTERN.search(report)
    bitrate = stream_bitrate or overall
    return match.group("codec").lower(), sample_rate, int(bitrate.group(1)) if bitrate else None


def _rms_level(report: str) -> float | None:
    """Return the overall RMS level ffmpeg measured, in dBFS."""
    return _last_measurement(report, "RMS level dB")


def _time_domain(report: str) -> TimeDomainStats | None:
    """Return the waveform statistics from one ``astats`` report."""
    peak = _last_measurement(report, "Peak level dB")
    rms = _last_measurement(report, "RMS level dB")
    if peak is None or rms is None:
        return None
    container, effective = _bit_depths(report)
    return TimeDomainStats(
        peak_db=peak,
        rms_db=rms,
        dc_offset=_last_measurement(report, "DC offset") or 0.0,
        flat_factor=_last_measurement(report, "Flat factor") or 0.0,
        clipped_samples=int(_last_measurement(report, "Abs Peak count") or 0.0),
        noise_floor_db=_last_measurement(report, "Noise floor dB") or 0.0,
        effective_bit_depth=effective,
        container_bit_depth=container,
        dynamic_range_db=_last_measurement(report, "Dynamic range"),
    )


_BIT_DEPTH_PATTERN = re.compile(r"Bit depth:\s*(\d+)/(\d+)/(\d+)/(\d+)")


def _bit_depths(report: str) -> tuple[int | None, int | None]:
    """Return the container's bit depth and the depth the samples really use.

    ffmpeg reports four numbers; the first is how many bits the samples occupy
    and the last is how many the container provides. A 24-bit file whose
    samples use 16 is a 16-bit recording in a larger box.
    """
    match = _BIT_DEPTH_PATTERN.search(report)
    if match is None:
        return None, None
    return int(match.group(4)), int(match.group(1))


def _last_measurement(report: str, label: str) -> float | None:
    """Return the last value ffmpeg printed for one ``astats`` label.

    The last one is the overall figure: ``astats`` prints per-channel blocks
    first and the summary afterwards, so reading the last occurrence gives the
    file's own number rather than one channel's.
    """
    values = re.findall(rf"{re.escape(label)}:\s*(-?[\d.]+|inf|-inf)", report)
    for raw in reversed(values):
        if raw in {"inf", "-inf"}:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None
