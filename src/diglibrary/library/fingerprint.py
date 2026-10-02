"""Turning a file's audio into the fingerprint AcoustID answers to."""

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ALGORITHM = "chromaprint"
"""What produced the fingerprint, stored beside it so a later change is legible."""

KNOWN_FPCALC_PATHS = (
    "/opt/homebrew/bin/fpcalc",
    "/usr/local/bin/fpcalc",
    "/opt/local/bin/fpcalc",
)
"""Where fpcalc lives when PATH does not say.

The same trap ``find_ffmpeg`` exists for: an application launched from the Dock
inherits no shell environment, so a Homebrew binary is invisible to
``shutil.which`` while every terminal on the machine finds it.
"""


def find_fpcalc() -> str | None:
    """Return a usable fpcalc, from PATH or from where it is normally installed."""
    found = shutil.which("fpcalc")
    if found:
        return found
    return next((path for path in KNOWN_FPCALC_PATHS if os.access(path, os.X_OK)), None)


@dataclass(frozen=True, slots=True)
class AudioFingerprint:
    """Purpose: carry one file's acoustic fingerprint and the length it covers.

    Responsibilities: hold the compressed fingerprint, the duration AcoustID is
    told about, and which algorithm produced it. Boundaries: it identifies
    nothing on its own — a fingerprint becomes an answer only after a lookup.
    Dependencies: none. Collaborators: the fingerprinter that produces it and
    the AcoustID client that spends it. Constraints: ``duration_seconds`` is
    whole seconds because that is what the web service takes.
    """

    fingerprint: str
    duration_seconds: int
    algorithm: str = ALGORITHM


class FingerprintRunner(Protocol):
    """Run one external command and return what it wrote to standard output.

    Injected rather than called directly, for the reason every other binary in
    this project is: a fake runner replays a recorded answer, so the tests need
    neither a subprocess nor an audio file.
    """

    def run(self, arguments: Sequence[str]) -> str:
        """Return the command's standard output, or raise ``OSError`` if it fails."""


class SubprocessFingerprintRunner:
    """Purpose: run fpcalc as a child process and hand back what it printed.

    Responsibilities: invoke the binary, wait for it, and return stdout.
    Boundaries: it parses nothing and knows no fpcalc argument — the
    fingerprinter builds the command. Dependencies: ``subprocess`` and an fpcalc
    on disk. Collaborators: ``ChromaprintFingerprinter``. Constraints: fpcalc is
    invoked as a separate process and never linked, which is what keeps this
    project's own licence MIT; a non-zero exit means no fingerprint
    and raises, because unlike ffmpeg it reports nothing usable on failure.
    """

    def __init__(self, executable: str = "fpcalc", timeout_seconds: float = 120.0) -> None:
        """Create a runner for one fpcalc executable."""
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    def run(self, arguments: Sequence[str]) -> str:
        """Return fpcalc's standard output for one invocation."""
        completed = subprocess.run(
            [self._executable, *arguments],
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.strip() or "fpcalc failed")
        return completed.stdout


class ChromaprintFingerprinter:
    """Purpose: produce the acoustic fingerprint of one audio file.

    Responsibilities: build fpcalc's command, read its JSON, and return the
    fingerprint with the duration it covers. Boundaries: it reaches no network,
    consults no catalogue, and stores nothing — identification is somebody
    else's job. Dependencies: an injected runner. Collaborators: the acoustic
    identification workflow and the fingerprint store. Constraints: a file that
    cannot be read yields ``None`` rather than an exception, because a
    fingerprint is evidence this project can do without: acoustic
    identification never blocks organizing.
    """

    def __init__(
        self,
        runner: FingerprintRunner,
        logger: logging.Logger,
    ) -> None:
        """Create a fingerprinter over one way of running fpcalc."""
        self._runner = runner
        self._logger = logger

    def fingerprint(self, path: Path) -> AudioFingerprint | None:
        """Return this file's fingerprint, or ``None`` when it cannot be produced."""
        try:
            # Resolved so the positional argument is provably absolute: fpcalc
            # reads any leading-dash argument as one of its own options, and an
            # absolute path cannot start with a dash.
            output = self._runner.run(["-json", str(Path(path).resolve())])
        except (OSError, subprocess.SubprocessError):
            self._logger.warning(
                "A file could not be fingerprinted.",
                extra={"operation": "fingerprint.failure"},
            )
            return None
        try:
            reported = json.loads(output)
            # fpcalc reports a fractional duration; the web service takes whole
            # seconds, and rounding here keeps what is stored equal to what is
            # asked, so a cached fingerprint and a fresh one ask the same thing.
            return AudioFingerprint(
                fingerprint=str(reported["fingerprint"]),
                duration_seconds=round(float(reported["duration"])),
            )
        except (ValueError, KeyError, TypeError):
            self._logger.warning(
                "fpcalc reported something this version cannot read.",
                extra={"operation": "fingerprint.unreadable"},
            )
            return None
