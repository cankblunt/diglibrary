"""Read every arm with DigLibrary's own analyzer, and write what it measured.

The numbers are the application's numbers, not a hand-rolled approximation of
them: a second measuring grid can agree with itself about a rule that is wrong.
Runs offline and read-only; the only thing written is one TSV per arm in
``--results``.

    python3 benchmark/measure.py --arms /tmp/arms --results /tmp/results

Each TSV row is one file:

    name  cutoff_hz  wall_low_hz  floor_from_hz  cliff_db  decay_db  ceiling_db  steepest_db

Runs against an installed ``diglibrary`` (``pipx install diglibrary``) or from
a checkout, where it finds ``src/`` beside itself.
"""

import argparse
import concurrent.futures
import logging
import shutil
import sys
from pathlib import Path

try:
    from diglibrary.quality.analysis import FfmpegQualityAnalyzer, SubprocessCommandRunner
except ImportError:  # a checkout rather than an installed package
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from diglibrary.quality.analysis import FfmpegQualityAnalyzer, SubprocessCommandRunner

LOGGER = logging.getLogger("benchmark.measure")
LOGGER.addHandler(logging.NullHandler())

ARMS = ("original", "cdfilter", "lame128", "lame192", "lame256", "lame320", "lame320-nolp")


def measure_one(path: Path, ffmpeg: str) -> str:
    """One TSV line for one file — an error is data here, never a crash."""
    analyzer = FfmpegQualityAnalyzer(SubprocessCommandRunner(ffmpeg), LOGGER)
    try:
        spectrum = analyzer.analyze(path).spectral
    except Exception as error:  # a failed probe is a row, not a stop
        return f"{path.name}\tERR\t{error}"
    return (
        f"{path.name}\t{spectrum.cutoff_hertz}\t{spectrum.wall_low_hertz}\t"
        f"{spectrum.floor_from_hertz}\t{spectrum.cliff_db:.2f}\t{spectrum.decay_db:.2f}\t"
        f"{spectrum.ceiling_db:.2f}\t{spectrum.steepest_drop_db:.2f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", required=True, help="the folder build_arms.py wrote")
    parser.add_argument("--results", required=True, help="one TSV per arm is written here")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    args = parser.parse_args()

    if not args.ffmpeg:
        print("ffmpeg was not found on PATH; point --ffmpeg at it", file=sys.stderr)
        return 1

    arms_dir = Path(args.arms)
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)

    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for arm in ARMS:
            files = sorted(arms_dir.glob(f"*-{arm}.flac"))
            if not files:
                # An arm with no files would score as a perfect one. Say so.
                print(f"{arm}: no files found in {arms_dir}", file=sys.stderr)
                failures += 1
                continue
            lines = list(pool.map(lambda p: measure_one(p, args.ffmpeg), files))
            target = results / f"eng-{arm}.tsv"
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            errors = sum(1 for line in lines if "\tERR\t" in line)
            failures += errors
            suffix = f" ({errors} ERR)" if errors else ""
            print(f"{arm}: {len(lines)} measured -> {target}{suffix}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
