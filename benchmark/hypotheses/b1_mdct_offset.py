"""B1 — does a decoded MP3 leave a frame-aligned trace an offset sweep can find?

The spectral silhouette is exhausted: above the trusted line an encoder's wall
and an honest converter's wall are the same shape, and a
naive count of quantization zeros — measured without frame alignment — showed
four identical distributions below any wall. This script tests the aligned
version of that idea, the one the forensics literature says is the point:

MP3 quantizes MDCT coefficients on a fixed 576-sample granule grid. Re-running
the encoder's own analysis (polyphase filterbank + MDCT + alias-reduction
butterflies, ISO 11172-3) recovers approximately the quantized coefficients —
but only at the alignment the encoder used. So sweep all 576 alignments
(32 polyphase phases x 18 MDCT phases) and, at each, measure how much of the
spectrum sits near zero. Audio that has been through MP3 shows a sharp peak at
one alignment; audio that never met an encoder is alignment-invariant, because
nothing in a converter's filter is framed. The discriminant is the peak's
z-score against the sweep's own spread — self-normalized, no absolute
threshold, and by construction blind to where the spectrum ends, which is what
keeps the honest dark master out of reach.

Per file it prints one row per channel view (left, mid): arm, peak z-score for
three relative thresholds, and the winning offset. Verdicts are NOT made here;
this is a measurement, to be scored against the paired arms of the benchmark.

    python3 benchmark/hypotheses/b1_mdct_offset.py --arms DIR --out results.tsv

Needs ffmpeg on PATH and numpy. Never writes anywhere but --out.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

SUBBANDS = 32
GRANULE = 576  # samples: 18 subband frames x 32 samples
# ISO 11172-3 Table C.1, from the application's own copy — one table, so this
# script and the shipped measurement can never drift apart. See
# `src/diglibrary/quality/enwindow_iso.txt` for its provenance.
try:
    from diglibrary.quality.framing import ENWINDOW
except ImportError:  # a checkout rather than an installed package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from diglibrary.quality.framing import ENWINDOW

assert ENWINDOW.shape == (512,)

# Analysis matrixing M[sb, k] = cos((2 sb + 1)(k - 16) pi / 64)  (dist10).
_sb = np.arange(SUBBANDS)[:, None]
_k = np.arange(64)[None, :]
MATRIX = np.cos((2 * _sb + 1) * (_k - 16) * np.pi / 64.0)  # (32, 64)

# Long-block MDCT with its sine window folded in:
# X[m] = sum_i win[i] z[i] cos(pi/72 (2i + 1 + 18)(2m + 1))   (dist10 mdct_sub)
_i = np.arange(36)[:, None]
_m = np.arange(18)[None, :]
MDCT = np.sin(np.pi / 36.0 * (np.arange(36) + 0.5))[:, None] * np.cos(
    np.pi / 72.0 * (2 * _i + 1 + 18) * (2 * _m + 1)
)  # (36, 18)

# Alias-reduction butterflies (encoder direction), ISO table B.9 coefficients.
_ci = np.array([-0.6, -0.535, -0.33, -0.185, -0.095, -0.041, -0.0142, -0.0037])
_CS = 1.0 / np.sqrt(1.0 + _ci * _ci)
_CA = _ci * _CS

ALPHAS = (1e-2, 1e-3, 1e-4)  # thresholds relative to the file's median |X|


def decode(path: Path, seconds: float, skip: float) -> np.ndarray | None:
    """Stereo float32 PCM at the file's NATIVE rate, shape (n, 2), via ffmpeg.

    Never resample here: the trace this script hunts is the encoder's
    576-sample granule grid, and any resampling smears it — the two 48 kHz
    sources in the first measurement scored flat on every arm until this
    stopped forcing 44.1 kHz.
    """
    args = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-ss",
        str(skip),
        "-i",
        str(path),
        "-t",
        str(seconds),
        "-ac",
        "2",
        "-f",
        "f32le",
        "-",
    ]
    try:
        out = subprocess.run(args, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or len(out.stdout) < 8:
        return None
    pcm = np.frombuffer(out.stdout, dtype=np.float32)
    return pcm.reshape(-1, 2)


def subband_frames(signal: np.ndarray, phase: int) -> np.ndarray:
    """All polyphase outputs S[t, sb] for frames ending at phase + 32 t.

    dist10 keeps the newest sample at x[0], so each frame's input is the
    previous 512 samples reversed, windowed by C, folded mod 64, matrixed.
    """
    usable = (signal.shape[0] - 512 - phase) // 32
    ends = 512 + phase + 32 * np.arange(usable)  # exclusive end of each window
    idx = ends[:, None] - 1 - np.arange(512)[None, :]  # newest first
    z = signal[idx] * ENWINDOW[None, :]
    y = z.reshape(usable, 8, 64).sum(axis=1)
    return y @ MATRIX.T  # (t, 32)


def granule_spectra(frames: np.ndarray) -> np.ndarray:
    """MDCT over every 36-frame window (hop 1), encoder-faithful.

    Returns |X| with shape (t - 35, 32, 18). Frequency inversion of odd
    subbands' odd samples and the alias-reduction butterflies are applied,
    matching dist10's mdct_sub.
    """
    inv = frames.copy()
    inv[1::2, 1::2] *= -1.0  # odd time sample of odd subband
    windows = np.lib.stride_tricks.sliding_window_view(inv, 36, axis=0)
    # windows: (t-35, 32, 36) -> einsum with (36, 18)
    x = np.einsum("tbi,im->tbm", windows, MDCT, optimize=True)
    # butterflies between adjacent bands
    lo = x[:, :-1, 17:9:-1].copy()  # X[b][17-k], k = 0..7
    hi = x[:, 1:, :8].copy()  # X[b+1][k]
    x[:, :-1, 17:9:-1] = lo * _CS + hi * _CA
    x[:, 1:, :8] = hi * _CS - lo * _CA
    return np.abs(x)


def sweep(signal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """T[alpha, 576] — fraction of |X| under alpha * median|X|, per offset."""
    fractions = np.zeros((len(ALPHAS), 32, 18), dtype=np.float64)
    median = None
    for p in range(32):
        mags = granule_spectra(subband_frames(signal, p))
        if median is None:
            median = np.median(mags)  # one scale for the whole file
        flat = mags.reshape(mags.shape[0], -1)  # (t', 576 coeffs)
        for a, alpha in enumerate(ALPHAS):
            small = (flat < alpha * median).mean(axis=1)  # per t'
            for q in range(18):
                fractions[a, p, q] = small[q::18].mean()
    return fractions.reshape(len(ALPHAS), -1), np.float64(median)


def peakiness(t: np.ndarray) -> tuple[float, int]:
    """Peak z-score of one sweep against its own spread, and the argmax."""
    med = np.median(t)
    mad = np.median(np.abs(t - med)) * 1.4826
    spread = max(mad, 1e-12)
    top = int(np.argmax(t))
    return float((t[top] - med) / spread), top


def measure(path: Path, seconds: float, skip: float) -> list[str]:
    pcm = decode(path, seconds, skip)
    if pcm is None:
        return [f"ERR\tdecode\t{path}"]
    views = {
        "left": pcm[:, 0].astype(np.float64),
        "mid": ((pcm[:, 0] + pcm[:, 1]) / np.sqrt(2.0)).astype(np.float64),
    }
    rows = []
    for view, signal in views.items():
        if signal.shape[0] < 512 + GRANULE * 40:
            rows.append(f"ERR\tshort\t{path}")
            continue
        t, _ = sweep(signal)
        cells = []
        for a in range(len(ALPHAS)):
            z, top = peakiness(t[a])
            cells.append(f"{z:.2f}\t{top}")
        rows.append(f"{path}\t{view}\t" + "\t".join(cells))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", help="folder of fabricated arms")
    parser.add_argument("--from-list", help="one audio path per line — real files, not arms")
    parser.add_argument("--out", required=True, help="TSV written (overwritten)")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--skip", type=float, default=4.0)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    parser.add_argument("--limit", type=int, default=0, help="files per arm, 0 = all")
    args = parser.parse_args()

    if args.from_list:
        listed = Path(args.from_list).read_text(encoding="utf-8").splitlines()
        files = [Path(line) for line in listed if line.strip()]
    elif args.arms:
        files = sorted(Path(args.arms).glob("*.flac"))
    else:
        print("give --arms or --from-list", file=sys.stderr)
        return 2
    if args.limit and args.arms:
        by_arm: dict[str, list[Path]] = {}
        for f in files:
            by_arm.setdefault(f.stem.rsplit("-", 1)[-1], []).append(f)
        files = [f for arm in by_arm.values() for f in arm[: args.limit]]
    if not files:
        print("nothing to measure", file=sys.stderr)
        return 2

    header = "name\tview\t" + "\t".join(f"z_{a:g}\ttop_{a:g}" for a in ALPHAS)
    lines = [header]
    with cf.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(measure, f, args.seconds, args.skip) for f in files]
        for done, future in enumerate(cf.as_completed(futures), 1):
            lines.extend(future.result())
            if done % 20 == 0:
                print(f"{done}/{len(files)}", file=sys.stderr)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(lines) - 1} rows over {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
