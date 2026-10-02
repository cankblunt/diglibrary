# The paired transcode benchmark

A way to measure any transcode detector — this application's included — against
audio whose nature is **known**, because you fabricated it yourself from
lossless files you own. No audio is downloaded, none is distributed, and no
folder name is ever trusted: the recipe is what ships.

## Why paired, and why the filtered arm

A corpus that puts *genuine references* on one side and *labelled transcodes*
of other recordings on the other — different masters, different everything —
cannot say what its gap is made of: it mixes the encoder's fingerprint with the
music's.
Here every arm is **the same 60-second excerpt**, so any difference between two
arms of one index is the treatment and nothing else.

| arm | what it is | what a conviction on it means |
| --- | --- | --- |
| `original` | the excerpt, straight into FLAC | a false conviction |
| `cdfilter` | the same audio through a steep Butterworth at 20 kHz (8th order), **never through an encoder** | **a false conviction of an honest master** |
| `lame128` … `lame320` | LAME CBR, decoded back into FLAC — a laundered transcode | a detection |
| `lame320-nolp` | LAME 320 with `--lowpass -1`: no wall at all | a detection nobody has published a spectral rule for |

`cdfilter` is the arm that decides anything. An honest converter's
anti-aliasing filter puts a wall near 20 kHz on real records, so a detector
tuned until it catches every 320 has usually just learned to convict that wall.
A benchmark without this arm cannot see the difference, and this arm is what
this project's own rule was measured against when it **declined** to chase the
320.

## Running it

Three commands, on your own machine, against your own files. Needs `ffmpeg`
and `lame` on PATH, and `diglibrary` importable — a checkout works, and so does
an installed package; the scripts import only the analyzer and the rule.

```bash
# 1. sources.txt: one absolute path per line, lossless files you trust.
#    Prefer sources with energy above 21 kHz — a source that already stops
#    at 17 kHz cannot show what an encoder's low-pass would add.
python3 benchmark/build_arms.py --from-list sources.txt --outdir /tmp/arms

# 2. Read every arm with the application's own analyzer.
python3 benchmark/measure.py --arms /tmp/arms --results /tmp/results

# 3. Apply the shipped rule and print the table.
python3 benchmark/score.py --results /tmp/results
```

A few minutes and about 2.5 GB for 60 sources; both are linear in the count.
Scoring **another** detector needs no code from here: run it over the files in
`/tmp/arms` and count its convictions per arm — the arm is in every filename.

## Reading the table

`score.py` prints, for each arm, how many files the shipped rule convicts and
what a conviction on that arm means. No reference results are published here:
build the arms from files of your own and the table is yours.

The `cdfilter` row is the one to compare tools on. A rule that convicts by
where the floor begins cannot take in a 256 or a 320 without taking in the
honest filtered masters at the same rate, because all three floor at 20 kHz:
the line that spares a real converter's filter also spares an encoder that
stops beyond it.

Read the misses before the catches. **A 320 — with or without its low-pass —
goes free of the spectral rule.** Everything this benchmark scores is the
*spectral* rule: what a detector can conclude from where a file's energy dies.
The application also measures where the audio was cut into frames, which is a
different question and catches both of those arms (`quality/framing.py`); this
benchmark scores the spectral rule alone, because that is the rule other tools
publish and the only one they can be compared on. That the 320 arms read zero
is not a tuning gap: at 20 kHz a transcode and an honest filtered master are
the same shape, so a ceiling raised to catch one convicts the other. A detector
claiming high recall on 320s should be run against `cdfilter` before the claim
is believed — and `lame320-nolp` has no wall for any spectral rule to find, at
any threshold.

## What exists elsewhere

[flaccheck](https://github.com/dasunNimantha/flaccheck) publishes a
reproducible corpus (23 genuine references + 230 fabricated transcodes) and its
own scores over it, and reports `INCONCLUSIVE` on band-limited files rather
than convicting them — the same failure treated with the same respect. Its
transcodes are made from its own references, so it is paired as well, and it
covers AAC, Opus and Vorbis, which this benchmark does not. What it does not
have is the filtered-master arm as a first-class labelled population. Spectrogram viewers (Spek and kin) are manual inspection, not
detectors, and are not scoreable. Correct this account of the landscape if it
has aged.
