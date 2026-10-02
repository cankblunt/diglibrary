"""B2 — name the bitrate a laundered file was, by re-encoding it and listening back.

The frame grid (`quality/framing.py`) answers *was this an MP3*. It cannot
answer *at what rate*, because the grid is 576 samples wide whatever the
bitrate. This asks the second question a different way, and its answer is a
second, independent witness to the first.

**The idea.** An encoder is close to idempotent at its own rate. Audio that was
already quantized at 192 kbps is, in the encoder's own arithmetic, already
sitting on the lattice the 192 kbps encoder would put it on — so re-encoding it
at 192 changes it far less than re-encoding it at any other rate, and far less
than the same operation changes audio that never met an encoder. Audio that is
genuinely lossless has no such rate: its residual simply falls as the bitrate
rises, smoothly, with no dip anywhere.

So for each candidate rate the file is encoded, decoded, aligned against the
original, and the residual measured. Two numbers come out:

* **the rate of the deepest dip**, which is the bitrate the file was, and
* **how deep that dip is** against the smooth trend of its neighbours, which
  is what says whether there was a dip at all.

Nothing here concludes anything on its own; it is scored against the paired
arms of the benchmark, where the true rate of every file is known because it
was fabricated here.

**MEASURED, AND IT DOES NOT WORK. Kept so the idea is not tried again.**
Scored over the paired arms, three findings, each fatal on its own:

* **No file names its own rate.** Every curve is monotonic — there is no dip
  at the rate the file was encoded at, so the premise of encoder idempotence
  does not survive contact with a real encoder. Aligning the excerpt to the
  frame grid `quality/framing.py` finds changes the residual by a negligible
  amount.
* **It measures bandwidth, not history.** `cdfilter` — an honest master
  filtered at 20 kHz, never near an encoder — reads between `lame256` and
  `lame320`. That is the confound the `cdfilter` arm exists to expose.
* **It is blind where the frame grid is strongest.** `lame320-nolp` reads the
  same as the honest original.

The populations overlap completely.

One trap in the method: identical medians for all seven arms are a symptom,
not a finding, since the populations are known to differ. ffmpeg's
stereo-to-mono downmix carries a 1/sqrt(2) factor in the float path and not in
the WAV path, so a reference read one way and an excerpt read the other
measure the gap between the two readings rather than the encoder. The excerpt
written for the encoder is therefore also the reference.

    python3 benchmark/hypotheses/b2_recompression.py --from-list files.txt --out out.tsv

Needs `ffmpeg` and `lame` on PATH, and numpy. Never writes anywhere but --out
and a scratch directory it cleans up.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

RATES: tuple[int, ...] = (128, 160, 192, 256, 320)
"""The CBR ladder asked about. `160` sits between two neighbours on purpose:
a dip needs a shoulder on each side to be a dip rather than an edge."""

ALIGN_SEARCH = 4096
"""How far to look for the encoder's delay, in samples. LAME's own is about
1,105 and a decoder adds its own; this is generous and costs nothing."""

SECONDS = 20.0
SKIP = 20.0


def decode(
    path: Path, ffmpeg: str, seconds: float | None = None, skip: float = 0.0
) -> np.ndarray | None:
    """Return mono float64 PCM at the file's native rate, or ``None``."""
    arguments = [ffmpeg, "-nostdin", "-v", "error"]
    if skip:
        arguments += ["-ss", str(skip)]
    arguments += ["-i", str(path)]
    if seconds is not None:
        arguments += ["-t", str(seconds)]
    arguments += ["-vn", "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-"]
    try:
        done = subprocess.run(arguments, capture_output=True, timeout=300, check=False)
    except Exception:
        return None
    if done.returncode != 0 or len(done.stdout) < 8:
        return None
    return np.frombuffer(done.stdout, dtype=np.float32).astype(np.float64)


def residual_db(original: np.ndarray, returned: np.ndarray) -> float | None:
    """Return how much of the original survives the round trip, in dB.

    The two are aligned first: an encoder prepends its own delay, so comparing
    sample against sample without finding that shift measures the shift and
    nothing else. Found by correlation over a window, once, at full strength.
    """
    length = min(original.size, returned.size)
    if length < 44_100:
        return None
    reference = original[:length]
    window = min(length, 200_000)
    best_lag, best_score = 0, -np.inf
    head = reference[:window]
    for lag in range(0, ALIGN_SEARCH):
        if lag + window > returned.size:
            break
        score = float(np.dot(head, returned[lag : lag + window]))
        if score > best_score:
            best_score, best_lag = score, lag
    shifted = returned[best_lag : best_lag + length]
    if shifted.size < length:
        reference = reference[: shifted.size]
    power = float(np.mean(reference**2))
    if power <= 0:
        return None
    difference = reference - shifted[: reference.size]
    return 10.0 * np.log10(max(float(np.mean(difference**2)), 1e-30) / power)


def ladder(path: Path, ffmpeg: str, lame: str) -> dict[int, float] | None:
    """Return the residual left by a round trip at each candidate rate.

    **The excerpt written for the encoder is also the reference.** Reading the
    original file twice — once as float for comparing, once as a WAV for
    encoding — is not reading the same audio: ffmpeg's stereo-to-mono downmix
    carries a 1/sqrt(2) factor in one path and not the other, so the reference
    peaked at 1.34 where the encoder's input peaked at 0.95 and 90% of the
    samples differed. Every residual then measured that gap instead of the
    encoder, and every arm of the benchmark came back identical to two decimal
    places — which is how this was caught.
    """
    answers: dict[int, float] = {}
    with tempfile.TemporaryDirectory(prefix="b2-") as scratch:
        room = Path(scratch)
        source = room / "source.wav"
        made = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-ss", str(SKIP), "-i", str(path),
             "-t", str(SECONDS), "-vn", "-map", "0:a:0", "-ac", "1", str(source)],
            capture_output=True, check=False,
        )  # fmt: skip
        if made.returncode != 0 or not source.exists():
            return None
        original = decode(source, ffmpeg)
        if original is None or original.size < 44_100:
            return None
        for rate in RATES:
            encoded = room / f"{rate}.mp3"
            done = subprocess.run(
                [lame, "--quiet", "-b", str(rate), "--cbr", str(source), str(encoded)],
                capture_output=True, check=False,
            )  # fmt: skip
            if done.returncode != 0 or not encoded.exists():
                continue
            returned = decode(encoded, ffmpeg)
            if returned is None:
                continue
            level = residual_db(original, returned)
            if level is not None:
                answers[rate] = level
    return answers or None


def dip_of(answers: dict[int, float]) -> tuple[int | None, float]:
    """Return the rate that stands out, and how far it stands out by.

    A rate is *out of line* when it sits below the straight line through its
    two neighbours. Lossless audio makes no such shape — its residual falls
    smoothly with the bitrate — so the depth is the whole evidence, and the
    ends of the ladder cannot be judged this way and are not.
    """
    rates = sorted(answers)
    best_rate, best_depth = None, 0.0
    for index in range(1, len(rates) - 1):
        low, middle, high = rates[index - 1], rates[index], rates[index + 1]
        expected = (answers[low] + answers[high]) / 2.0
        depth = expected - answers[middle]
        if depth > best_depth:
            best_rate, best_depth = middle, depth
    return best_rate, best_depth


def measure(path: Path, ffmpeg: str, lame: str) -> str:
    """One TSV line for one file — an error is data, never a stop."""
    answers = ladder(path, ffmpeg, lame)
    if not answers:
        return f"ERR\tunreadable\t{path}"
    rate, depth = dip_of(answers)
    cells = "\t".join(f"{answers[r]:.2f}" if r in answers else "" for r in RATES)
    return f"{path}\t{rate or ''}\t{depth:.2f}\t{cells}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-list", required=True, help="one audio path per line")
    parser.add_argument("--out", required=True, help="TSV written (overwritten)")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--lame", default="lame")
    args = parser.parse_args()

    listed = Path(args.from_list).read_text(encoding="utf-8").splitlines()
    files = [Path(line) for line in listed if line.strip()]
    if not files:
        print("nothing to measure", file=sys.stderr)
        return 2
    header = "name\tdip_rate\tdip_db\t" + "\t".join(f"r{rate}" for rate in RATES)
    lines = [header]
    with cf.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(measure, f, args.ffmpeg, args.lame) for f in files]
        for done, future in enumerate(cf.as_completed(futures), 1):
            lines.append(future.result())
            if done % 10 == 0:
                print(f"{done}/{len(files)}", file=sys.stderr)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(lines) - 1} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
