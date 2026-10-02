"""Measure what every album under a root actually is, without opening the window.

A library-sized survey is hours of decoding. The window is a poor place to keep
one: the thread dies with the app, and the machine must stay awake in front of
it. This runs the same ``QualitySurvey`` against the same database, prints one
line per album as it lands, and can be left running unattended.

    .venv/bin/python tools/run_quality_survey.py ~/Music

Nothing is renamed, tagged, or planned here — the survey writes measurements and
stops there, exactly as it does from the window.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from diglibrary.application.composition import create_application
from diglibrary.application.quality import AlbumQuality, QualitySurvey
from diglibrary.database.library_store import LibraryStore
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.scanner import LibraryScanner
from diglibrary.quality.analysis import (
    FfmpegQualityAnalyzer,
    SubprocessCommandRunner,
    find_ffmpeg,
)


def _say(message: str) -> None:
    """Print a progress line that survives being piped into a file."""
    print(message, flush=True)


def _elapsed(seconds: float) -> str:
    """Render a duration the way a person reads a long run."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def _build(config_path: Path, workers: int) -> QualitySurvey:
    """Assemble the survey over the real database, or refuse to pretend."""
    application = create_application(config_path)
    executable = find_ffmpeg()
    if executable is None:
        raise SystemExit("ffmpeg was not found; audio cannot be measured.")
    store = LibraryStore(application.database, application.logger)
    scanner = LibraryScanner(MutagenAudioProbe(), application.logger)
    analyzer = FfmpegQualityAnalyzer(SubprocessCommandRunner(executable), application.logger)
    return QualitySurvey(scanner, analyzer, application.logger, workers=workers, store=store)


def _survey_root(survey: QualitySurvey, root: Path, excluded: tuple[Path, ...]) -> None:
    """Measure one root, reporting each album as it is finished."""
    _say(f"\n=== {root} ===")
    started = time.monotonic()
    units = survey.albums(root, excluded)
    files = sum(len(unit.audio_files) for unit in units)
    _say(f"scan: {len(units)} albums, {files} files, {_elapsed(time.monotonic() - started)}")

    measured_files = 0
    began = time.monotonic()

    def report(index: int, total: int, finding: AlbumQuality) -> None:
        nonlocal measured_files
        measured_files += finding.tracks
        spent = time.monotonic() - began
        rate = measured_files / spent if spent else 0.0
        left = (files - measured_files) / rate if rate else 0.0
        mark = "SUSPECT" if finding.is_suspect else "ok     "
        _say(
            f"[{index}/{total}] {mark} {finding.verdict.encoding} "
            f"({finding.analyzed}/{finding.tracks} measured) "
            f"eta {_elapsed(left)} | {finding.folder_path.name}"
        )

    findings = survey.measure(units, on_album=report)
    suspect = sum(1 for finding in findings if finding.is_suspect)
    _say(
        f"done: {len(findings)} albums, {suspect} suspect, "
        f"{_elapsed(time.monotonic() - started)}"
    )


def main() -> None:
    """Parse the command line and measure every root it names."""
    parser = argparse.ArgumentParser(
        prog="run_quality_survey", description="Measure audio quality across a library."
    )
    parser.add_argument("roots", nargs="+", type=Path, help="Folders to survey.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        type=Path,
        help="A folder to leave out, pruned with its subtree (repeatable).",
    )
    arguments = parser.parse_args()

    survey = _build(arguments.config, arguments.workers)
    excluded = tuple(path.expanduser().resolve() for path in arguments.exclude)
    whole = time.monotonic()
    for root in arguments.roots:
        path = root.expanduser().resolve()
        if not path.is_dir():
            _say(f"skipped, not a folder: {path}")
            continue
        _survey_root(survey, path, excluded)
    _say(f"\nsurvey finished in {_elapsed(time.monotonic() - whole)}")


if __name__ == "__main__":
    main()
