"""Build a paired ground truth for transcode detection, from files you own.

Every corpus this benchmark scores against is fabricated on your machine, from
your own lossless files — nothing is downloaded and no audio is distributed.
That is the point: you know what every file IS, because you made it, rather
than trusting what a folder name or an upload claims.

From each source FLAC, one 60-second excerpt is decoded once and becomes seven
files — the same audio, seven ways:

    original      the excerpt, straight back into FLAC: lossless, known
    cdfilter      through a steep Butterworth at 20 kHz (8th order), and NEVER
                  through an encoder. This is an honest converter's
                  anti-aliasing filter, and it is the arm that matters: a rule
                  that convicts it is convicting real records
    lame128..320  LAME CBR at 128, 192, 256 and 320 kbps, decoded back into a
                  lossless container — what a laundered transcode looks like
    lame320-nolp  LAME 320 with `--lowpass -1`: a transcode with no wall at
                  all, which no spectral ceiling detects

Pairing is what isolates the encoder: any difference between two arms of one
index is the treatment, never the music. A corpus of genuine references and
*different* fabricated transcodes cannot make that claim.

Writes only to ``--outdir``; every source is opened read-only. No prompt is
ever raised. The excerpt keeps the source's sample rate, and it and every arm
made from it are written at 16 bits.

    python3 benchmark/build_arms.py --from-list my-sources.txt --outdir /tmp/arms

``my-sources.txt`` is one absolute path per line, each a lossless file you
trust. Prefer sources with energy above 21 kHz — a source that already stops
at 17 kHz cannot show what an encoder's low-pass adds. Any count works, and the
scorer reports whatever it finds.
"""

import argparse
import concurrent.futures
import shutil
import subprocess
import sys
from pathlib import Path

# The honest master's filter: four chained 2-pole Butterworth low-passes at
# 20 kHz. Change this and every result is about a different corpus.
CD_FILTER = ",".join(["lowpass=f=20000:p=2"] * 4)

LAME_RATES = (128, 192, 256, 320)


def build_one(index: int, source: str, outdir: Path, ffmpeg: str, lame: str) -> bool:
    """Fabricate every arm for one source. Returns False when the source fails."""
    stem = f"{index:03d}"
    quiet = ["-v", "error", "-nostdin", "-y"]
    wav = outdir / f"{stem}.wav"

    # One 60 s excerpt, decoded once, is the common ancestor of every arm.
    if subprocess.run([ffmpeg, *quiet, "-t", "60", "-i", source, str(wav)]).returncode:
        return False
    subprocess.run([ffmpeg, *quiet, "-i", str(wav), str(outdir / f"{stem}-original.flac")])
    subprocess.run(
        [ffmpeg, *quiet, "-i", str(wav), "-af", CD_FILTER, str(outdir / f"{stem}-cdfilter.flac")]
    )

    encodings = [(f"lame{rate}", ["-b", str(rate), "--cbr"]) for rate in LAME_RATES]
    encodings.append(("lame320-nolp", ["-b", "320", "--cbr", "--lowpass", "-1"]))
    for arm, options in encodings:
        mp3 = outdir / f"{stem}-{arm}.mp3"
        target = outdir / f"{stem}-{arm}.flac"
        if subprocess.run([lame, "--quiet", *options, str(wav), str(mp3)]).returncode:
            return False
        # Decoded back into a lossless container: this is what a laundered
        # transcode looks like on a shelf.
        #
        # **Through a WAV, like every other arm, and never straight from the
        # MP3.** Handed the MP3 itself, a recent ffmpeg writes the FLAC in the
        # decoder's own terms: 24 bits, because an MP3 decodes to floating
        # point, and blocks of 47 samples where a FLAC ordinarily holds 4096.
        # Both are legal and neither is what a file on a shelf looks like. The
        # depth doubles the size and keeps a cleaner copy of the decoder's
        # output than a laundered file holds; the blocks make every later read
        # of the file some fifty times slower, so a measurement of minutes
        # takes hours. The ancestor is a 16-bit WAV, and so is this.
        decoded = outdir / f"{stem}-{arm}.wav"
        subprocess.run([ffmpeg, *quiet, "-i", str(mp3), str(decoded)])
        subprocess.run([ffmpeg, *quiet, "-i", str(decoded), str(target)])
        decoded.unlink(missing_ok=True)
        mp3.unlink()
    wav.unlink()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-list", required=True, help="one lossless source path per line")
    parser.add_argument("--outdir", required=True, help="written to; sources are read-only")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--lame", default=shutil.which("lame"))
    args = parser.parse_args()

    for name, binary in (("ffmpeg", args.ffmpeg), ("lame", args.lame)):
        if not binary:
            print(f"{name} was not found on PATH; point --{name} at it", file=sys.stderr)
            return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    listing = Path(args.from_list).read_text(encoding="utf-8")
    sources = [line for line in listing.splitlines() if line.strip()][: args.limit]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        outcomes = list(
            pool.map(
                lambda job: build_one(job[0], job[1], outdir, args.ffmpeg, args.lame),
                enumerate(sources),
            )
        )
    built = sum(outcomes)
    print(f"built {built} of {len(sources)} sources x {2 + len(LAME_RATES) + 1} arms -> {outdir}")
    # A corpus that silently lost members reads exactly like a complete one,
    # so a partial build is a failure, not a smaller success.
    return 0 if built == len(sources) else 1


if __name__ == "__main__":
    raise SystemExit(main())
