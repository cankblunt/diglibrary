"""The album whose tracks are open, and the gesture that reaches all of them.

With an album's tracks unfolded, the album's own `It is lossy` and `Not lossy`
have to stand out from everything else on screen. Three things decide that, and
none of them is visible in the source, because every line is valid whichever
way it draws:

* the list marks which row is open. `is-open` belongs on the row as well as on
  the card; without it a panel of tracks opens under a row drawn exactly like
  every row above it — one rule with two places that implement it;
* the chip that decides every track is larger than the chip that decides one,
  which stands once per track directly underneath;
* the proof that convicted the album is not repeated on every row, where one
  fact would stand on the screen once per track and once more for the album.

So this runs the functions over stubs and reads what they drew, the way
`test_album_steps.py` and `test_adopt_control.py` do. The CSS that gives the
band and the row their weight is guarded by `test_web_assets.py`, which refuses
a class no rule defines.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

# A node has a `classList` and a `className`, and the real DOM keeps the two in
# step. This one does not, so `classes()` below reads both — a guard that looked
# at one of them would pass an element wearing the class in the other.
HARNESS = """
function node(tag) {
  return {
    tag,
    className: "",
    textContent: "",
    title: "",
    hidden: false,
    disabled: false,
    colSpan: 0,
    type: "",
    dataset: {},
    style: {},
    children: [],
    classList: {
      _has: new Set(),
      add(...names) { names.forEach((name) => this._has.add(name)); },
      remove(...names) { names.forEach((name) => this._has.delete(name)); },
      toggle(name, on) { if (on) this._has.add(name); else this._has.delete(name); },
      contains(name) { return this._has.has(name); },
    },
    append(...kids) { for (const kid of kids) if (kid) this.children.push(kid); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids.filter(Boolean); },
    addEventListener() {},
  };
}
const document = { createElement: node };

function classes(el) {
  return String(el.className || "").split(/\\s+/).filter(Boolean)
    .concat([...el.classList._has]);
}
function walk(el, out = []) {
  out.push(el);
  for (const kid of el.children) walk(kid, out);
  return out;
}
function find(el, name) { return walk(el).filter((n) => classes(n).includes(name)); }
function tags(el, tag) { return walk(el).filter((n) => n.tag === tag); }

const STR = {
  qEveryTrack: (count) => `All ${count} tracks:`,
  qSayHonest: "Not lossy",
  qSayTranscoded: "It is lossy",
  qSayClear: "take it back",
  qMeasuring: "Measuring",
  qNotMeasured: "not measured",
  qMeasured: (done, total) => `${done}/${total}`,
  qProofGrid: "frame grid",
  qProofWall: "wall",
  qProofSpectrum: "spectrum",
  qSharedMeasurement: "shared measurement",
  qGridReadingsHead: "At least four of six readings agree.",
  qGridReadingsBody: "Not the same measurement repeated.",
  qAcousticCoverage: () => "coverage",
  qColFile: "TRACK", qColTrackVerdict: "MEASURED", qColHeard: "THE AUDIO",
  qColWall: "wall at", qColFall: "fall by", qColCeiling: "ceiling at", qColYours: "your word on it",
  qWallAt: (hz) => `${hz}`, qWallBetween: (lo, hi) => `${lo}-${hi}`,
  benchAnalyzeThis: "Analyze this album in depth",
  qBorderlineBadge: "borderline", qYourWord: "your word",
  confStrong: "STRONG", confFair: "FAIR", confWeak: "WEAK",
  tipSayEveryHonest: "", tipSayEveryTranscoded: "", tipSayEveryClear: "",
  tipProofGrid: "open the picture", tipProofWall: "", tipProofSpectrum: "",
  tipProofGridAndWall: "the picture shows it", tipSharedMeasurement: "",
  tipBenchAnalyze: "", tipBorderline: "", tipYourWord: "", tipConfidence: () => "",
  tipMark: "", tipWallOldRuler: "",
  qEverySaved: (n) => `${n}`,
};
const ENCODING_LABELS = {
  lossless: () => "LOSSLESS", transcoded: () => "WAS LOSSY", undecided: () => "UNDECIDED",
};
const FINDING_LABELS = { transcoded: () => "transcoded" };
const CLIC = "";
const state = {
  qualityTracks: new Map(),
  qualityOpen: new Set(),
  benchMarked: new Set(),
  spectrograms: new Map(),
  canMeasureAudio: true,
  qualityRunning: false,
};
const api = () => ({});
async function withSpinner(button, run) { return run(); }
function toast() {}
function toastError() {}
function refreshBench() {}
function startBenchAnalysis() {}
function toggleQualityTracks() {}
function benchRootMenu() {}
function renderQualitySummary() {}
// Stubbed because they are not what this is about; each returns a cell so the
// row it belongs to still has the right shape.
function heardCell() { return node("td"); }
function verdictControls() { return node("div"); }
function spectrogramButton() { return node("button"); }
function spectrogramRow() { return node("tr"); }
function spectrogramKey(album, track) { return `${album.path}::${track.file}`; }
"""

REAL = (
    "borderlineBadge",
    "spokenBadge",
    "confidenceBadge",
    "benchMarkBox",
    "proofChip",
    "sharedMeasurementChip",
    "everyTrackControls",
    "qualityEvidence",
    "qualityTracks",
    "qualityRow",
)


def _run(body: str) -> dict:
    """Run the real drawing functions over stubs and report what they drew."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in REAL:
        found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    program = HARNESS + "\n".join(pieces) + ";\n(() => {\n" + body + "\n})();"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # node's own message, not a wall of the program that produced it: a harness
    # that reports its failure as `CalledProcessError` hides the one line that
    # says which stub is missing.
    assert answer.returncode == 0, answer.stderr.strip().splitlines()[-3:]
    return json.loads(answer.stdout.strip())


ALBUM = """
const album = {
  id: 1, path: "/x/album", folder: "album", encoding: "transcoded", proved_by: "grid",
  reason: "This file was an MP3 before it was a FLAC.", borderline: false, spoken: false,
  suspect: true, bitrate: null, median_drop_db: 5.0, analyzed: 12, tracks: 12, findings: [],
  confidence: "strong", confidence_reasons: [],
};
const track = (file, proof) => ({
  file, encoding: "transcoded", transcoded: true, borderline: false, proved_by: proof,
  picture_shows_it: false, measured_from_this_file: true, override: null,
  decay_db: 5, ceiling_db: -101, wall_low: null, cutoff: null, heard: "",
});
"""


def test_the_open_album_marks_its_row() -> None:
    """The row carries `is-open`, as the card does."""
    said = _run(ALBUM + """
        state.qualityOpen.add("/x/album");
        const open = qualityRow(album);
        const shut = qualityRow({...album, id: 2, path: "/x/other"});
        console.log(JSON.stringify({
          open: classes(open).includes("is-open"),
          shut: classes(shut).includes("is-open"),
          openable: classes(open).includes("is-openable"),
        }));
        """)

    assert said["open"] is True, "the album whose tracks are unfolded is not marked"
    assert said["shut"] is False, "a closed album is marked open"
    assert said["openable"] is True, "the row stopped saying it can be opened"


def test_the_album_chips_are_larger_than_the_track_chips() -> None:
    """The band that reaches twelve tracks is drawn, captioned, and offers both words."""
    said = _run(ALBUM + """
        state.qualityTracks.set(album.path, {
          tracks: [track("a.flac", "grid"), track("b.flac", "grid")], acoustic: null,
        });
        const cell = qualityTracks(album);
        const band = find(cell, "q-every")[0];
        console.log(JSON.stringify({
          band: Boolean(band),
          caption: band ? find(band, "q-verdict-label")[0].textContent : "",
          words: band ? tags(band, "button").map((b) => b.textContent) : [],
        }));
        """)

    assert said["band"] is True, "the album-wide control is not on screen"
    assert said["caption"] == "All 2 tracks:"
    assert said["words"] == ["Not lossy", "It is lossy"], "the two words are not both offered"


def test_the_proof_is_said_once_where_every_track_agrees_with_the_album() -> None:
    """Twelve rows convicted by the album's own proof carry no proof chip."""
    said = _run(ALBUM + """
        const twelve = Array.from({length: 12}, (_, i) => track(`${i}.flac`, "grid"));
        state.qualityTracks.set(album.path, {tracks: twelve, acoustic: null});
        const cell = qualityTracks(album);
        console.log(JSON.stringify({
          chips: find(cell, "badge-proof").length,
          rows: tags(cell, "tr").length,
          reason: find(cell, "q-evidence-reason").length,
        }));
        """)

    assert said["chips"] == 0, "the album's own proof is repeated on every row"
    assert said["rows"] == 13, "the table lost rows"
    assert said["reason"] == 1, "the sentence that names the proof is gone"


def test_a_track_convicted_by_something_else_still_says_so() -> None:
    """Written as the complement: the row that differs is the row that speaks."""
    said = _run(ALBUM + """
        state.qualityTracks.set(album.path, {tracks: [
          track("same.flac", "grid"), track("other.flac", "wall"), track("none.flac", null),
        ], acoustic: null});
        const cell = qualityTracks(album);
        console.log(JSON.stringify({
          chips: find(cell, "badge-proof").map((c) => c.textContent),
        }));
        """)

    assert said["chips"] == ["wall"], "a track convicted by another proof went quiet"


def test_a_proof_nobody_has_met_yet_draws_itself() -> None:
    """The rule enumerates nothing, so a proof added later is not invisible."""
    said = _run(ALBUM + """
        const one = [track("new.flac", "spectrum")];
        state.qualityTracks.set(album.path, {tracks: one, acoustic: null});
        console.log(JSON.stringify({chips: find(qualityTracks(album), "badge-proof").length}));
        """)

    assert said["chips"] == 1


def test_what_six_readings_mean_is_offered_rather_than_served() -> None:
    """The lead stays; the paragraph waits behind a click, and is still there."""
    said = _run(ALBUM + """
        const holder = qualityEvidence(album);
        const fold = tags(holder, "details")[0];
        console.log(JSON.stringify({
          folded: Boolean(fold),
          lead: fold ? tags(fold, "summary")[0].textContent : "",
          body: fold ? find(fold, "q-evidence-body")[0].textContent : "",
        }));
        """)

    assert said["folded"] is True, "the paragraph is served rather than offered"
    assert said["lead"] == "At least four of six readings agree."
    assert said["body"].startswith("Not the same measurement"), "the explanation was lost"


def test_an_album_convicted_some_other_way_folds_nothing() -> None:
    """No grid, no grid note: the fold is about one proof and says so."""
    said = _run(ALBUM + """
        const holder = qualityEvidence({...album, proved_by: "wall"});
        console.log(JSON.stringify({folds: tags(holder, "details").length}));
        """)

    assert said["folds"] == 0
