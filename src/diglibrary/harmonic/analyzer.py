"""Read one file's audio and say what key and tempo it is in.

Nothing here writes, and nothing here asks a catalogue. Both answers are
measurements of the file's own audio, in the same sense as the quality
measurements: a verdict about this audio, carrying which audio it came from.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from diglibrary.harmonic.keys import SAMPLE_RATE, MeasuredKey, measure_key
from diglibrary.harmonic.tempo import MeasuredTempo, measure_tempo

DECODE_SECONDS = 1800
"""The most audio one decode may hand back — thirty minutes, ~79 MB of samples.

Without a ceiling this pipe is sized by the file's own claim about itself: a
container that declares hours of cheaply-decoded audio fills memory with
gigabytes of PCM well inside the subprocess timeout. No track the measurements
are meaningful for comes near thirty minutes, so the cap costs nothing real —
a longer file is measured over its first half hour, which for a mix that long
is as accurate as any single key or tempo could be."""


@dataclass(frozen=True, slots=True)
class Harmonics:
    """What one recording is, harmonically — and which recording that was.

    ``audio_key`` is here because `content_signature` identifies a *shape* —
    codec, length and format — and two different songs of the same exact length
    in the same format share one. A key measured from one recording must never
    be shown against another, so the measurement carries the audio it came from
    and the store keys on it.
    """

    audio_key: str
    content_signature: str
    key: MeasuredKey | None
    tempo: MeasuredTempo | None

    @property
    def measured_anything(self) -> bool:
        """Whether this reading found either answer worth recording."""
        return self.key is not None or self.tempo is not None


class AudioDecoder(Protocol):
    """Decode one file to mono float samples at the rate the analysis wants.

    Injected, like every other external process in this project, so the tests
    are deterministic and offline: a fake decoder hands back a signal whose key
    and tempo are facts by construction.
    """

    def decode(self, path: Path) -> np.ndarray:
        """Return mono samples, or an empty array when the file cannot be read."""


class FfmpegAudioDecoder:
    """Purpose: turn any file the library holds into samples this can measure.

    Responsibilities: run ffmpeg once, asking for mono float at the analysis
    rate. Boundaries: it measures nothing and writes nothing. Dependencies: an
    ffmpeg on PATH. Constraints: a file ffmpeg cannot read is an empty array
    and never an exception — one unreadable track must not stop an album.
    """

    def __init__(
        self,
        executable: str = "ffmpeg",
        timeout_seconds: float = 120.0,
        decode_seconds: int = DECODE_SECONDS,
    ) -> None:
        """Create a decoder for one ffmpeg executable."""
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._decode_seconds = decode_seconds

    def decode(self, path: Path) -> np.ndarray:
        """Return the file as mono float32 at `SAMPLE_RATE`."""
        try:
            completed = subprocess.run(
                [
                    self._executable,
                    "-nostdin",
                    "-v",
                    "quiet",
                    "-t",
                    str(self._decode_seconds),
                    "-i",
                    # Resolved so what reaches ffmpeg is provably a local path:
                    # a bare name that starts with a dash or a colon is an
                    # option or a protocol to ffmpeg, and an absolute path can
                    # be neither.
                    str(Path(path).resolve()),
                    "-ac",
                    "1",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-f",
                    "f32le",
                    "-",
                ],
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(completed.stdout, dtype=np.float32)


class HarmonicAnalyzer:
    """Purpose: answer what key and tempo one recording is in.

    Responsibilities: decode once and run both measurements over the same
    samples. Boundaries: it does not choose which files to read, does not
    store, and never writes to the file. Collaborators: the application layer,
    which asks for one album at a time, on request, and never sweeps the
    library. Constraints: both answers may be absent, and absent is reported as
    absent rather than filled in.
    """

    def __init__(self, decoder: AudioDecoder, logger: logging.Logger) -> None:
        """Create an analyzer over one decoder."""
        self._decoder = decoder
        self._logger = logger

    def analyze(self, path: Path, audio_key: str, content_signature: str) -> Harmonics | None:
        """Measure one file, or return ``None`` when there is nothing to measure."""
        try:
            samples = self._decoder.decode(path)
        except Exception:
            self._logger.exception(
                "A file could not be decoded for its key and tempo.",
                extra={"operation": "harmonic.decode.failure", "path": str(path)},
            )
            return None
        if samples.size == 0:
            return None
        # One decode, both questions. They want the same mono signal, and
        # decoding twice would double the only part of this that costs anything.
        harmonics = Harmonics(
            audio_key=audio_key,
            content_signature=content_signature,
            key=measure_key(samples),
            tempo=measure_tempo(samples),
        )
        return harmonics if harmonics.measured_anything else None
