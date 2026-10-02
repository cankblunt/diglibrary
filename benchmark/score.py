"""Score a detector against the paired arms, and say what each number means.

Reads the TSVs ``measure.py`` wrote and applies DigLibrary's own per-track
rule to every row. Nothing here is a simulation: the function called is the
one the application ships, so a change to the rule changes this table on the
day it lands.

    python3 benchmark/score.py --results /tmp/results

The two arms that decide anything:

    cdfilter      a conviction here is a FALSE conviction of an honest master
                  — the failure this project refuses first
    lame128..320  a conviction here is the detector doing its job

``original`` guards the floor (convicting it is absurd), and ``lame320-nolp``
is the published limit: a transcode with no wall, which no spectral rule
reaches — its row is expected to read zero.

Another tool can score itself on the same corpus by running its own detector
over the files ``build_arms.py`` fabricated and counting convictions per arm;
the arm is in every filename.
"""

import argparse
import sys
from pathlib import Path

try:
    from diglibrary.quality.verdict import track_is_transcoded
except ImportError:  # a checkout rather than an installed package
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from diglibrary.quality.verdict import track_is_transcoded

# (arm, what a conviction on it means)
ARMS = (
    ("original", "false conviction"),
    ("cdfilter", "false conviction of an honest master"),
    ("lame128", "detection"),
    ("lame192", "detection"),
    ("lame256", "detection"),
    ("lame320", "detection (published miss)"),
    ("lame320-nolp", "detection (published miss)"),
)


def _number(field: str) -> int | None:
    return None if field == "None" else int(field)


def score_arm(tsv: Path) -> tuple[int, int, int]:
    """Return (convicted, measured, errors) for one arm's TSV."""
    convicted = measured = errors = 0
    for line in tsv.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            errors += 1
            continue
        _, cutoff, _, floor, _, decay, ceiling, _ = parts
        measured += 1
        if track_is_transcoded(_number(cutoff), float(decay), float(ceiling), _number(floor)):
            convicted += 1
    return convicted, measured, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", required=True, help="the folder measure.py wrote")
    args = parser.parse_args()

    results = Path(args.results)
    print(f"{'arm':<14} {'convicted':>9}   a conviction here is")
    print("-" * 66)
    missing = 0
    for arm, meaning in ARMS:
        tsv = results / f"eng-{arm}.tsv"
        if not tsv.is_file():
            # An absent arm scored as zero would read as a perfect arm.
            print(f"{arm:<14} {'—':>9}   NOT MEASURED — {tsv} is missing")
            missing += 1
            continue
        convicted, measured, errors = score_arm(tsv)
        note = f" ({errors} unreadable rows)" if errors else ""
        print(f"{arm:<14} {convicted:>4}/{measured:<4}   {meaning}{note}")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
