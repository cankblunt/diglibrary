"""Unit tests for producing an acoustic fingerprint from a file."""

import json
import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

from diglibrary.library.fingerprint import (
    ALGORITHM,
    KNOWN_FPCALC_PATHS,
    ChromaprintFingerprinter,
    find_fpcalc,
    why_fpcalc_does_not_run,
)


class _Recorded:
    """A runner that replays one fpcalc answer and remembers what it was asked."""

    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[Sequence[str]] = []

    def run(self, arguments: Sequence[str]) -> str:
        self.calls.append(list(arguments))
        if self.error is not None:
            raise self.error
        return self.output


def _logger() -> logging.Logger:
    return logging.getLogger("test.fingerprint")


def _answer(fingerprint: str = "AQADtIqoKXGU", duration: float = 186.49) -> str:
    return json.dumps({"duration": duration, "fingerprint": fingerprint})


def test_a_file_is_fingerprinted_with_the_length_the_service_takes() -> None:
    """fpcalc reports a fractional duration; AcoustID is told whole seconds.

    Rounding here rather than at the request keeps a stored fingerprint and a
    fresh one asking the same question, so a cache hit is a cache hit.
    """
    runner = _Recorded(_answer(duration=186.49))

    result = ChromaprintFingerprinter(runner, _logger()).fingerprint(Path("/music/a.flac"))

    assert result is not None
    assert result.fingerprint == "AQADtIqoKXGU"
    assert result.duration_seconds == 186
    assert result.algorithm == ALGORITHM
    assert runner.calls == [["-json", "/music/a.flac"]]


def test_a_file_that_cannot_be_read_yields_nothing_rather_than_raising() -> None:
    """Acoustic identification never blocks organizing."""
    runner = _Recorded(error=OSError("Could not open the input file"))

    assert ChromaprintFingerprinter(runner, _logger()).fingerprint(Path("/gone.flac")) is None


def test_a_timeout_is_an_absent_fingerprint_and_not_a_crash() -> None:
    """A long decode must not take the scan with it."""
    runner = _Recorded(error=subprocess.TimeoutExpired(cmd="fpcalc", timeout=120.0))

    assert ChromaprintFingerprinter(runner, _logger()).fingerprint(Path("/slow.flac")) is None


def test_an_answer_this_version_cannot_read_is_refused_quietly() -> None:
    """A future fpcalc that renames a field must not write a broken fingerprint."""
    runner = _Recorded(json.dumps({"duration": 186.49, "fp": "AQADtIqoKXGU"}))

    assert ChromaprintFingerprinter(runner, _logger()).fingerprint(Path("/music/a.flac")) is None


def test_the_binary_is_looked_for_where_the_dock_cannot_see() -> None:
    """Launched from the Dock the app inherits no PATH, so PATH alone is not enough.

    This is the trap `find_ffmpeg` already exists for, and the list is asserted
    rather than trusted: a Homebrew install is the normal case on macOS.
    """
    assert "/opt/homebrew/bin/fpcalc" in KNOWN_FPCALC_PATHS
    found = find_fpcalc()
    assert found is None or Path(found).name == "fpcalc"


def test_a_binary_that_is_found_and_does_not_run_says_why() -> None:
    """Being on disk is not running.

    A fingerprinter linked against a library that was upgraded since is found
    where it is installed and exits before reading a file, so it is asked for
    its version once and the first line of what it said is kept.
    """
    broken = _Recorded(error=OSError("dyld: Library not loaded: libavformat\n  Referenced from"))

    assert why_fpcalc_does_not_run(broken) == "dyld: Library not loaded: libavformat"
    assert broken.calls == [["-version"]], "asked without an audio file"


def test_a_binary_that_runs_has_nothing_to_say() -> None:
    """The ordinary case answers nothing, including when it merely timed out elsewhere."""
    assert why_fpcalc_does_not_run(_Recorded(output="fpcalc version 1.6.0")) is None
    timed_out = _Recorded(error=subprocess.TimeoutExpired("fpcalc", 120))
    assert why_fpcalc_does_not_run(timed_out) is not None
