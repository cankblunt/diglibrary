"""The proof a line names, and the note that stands over the picture — run, not read.

A file the frame grid convicts has a spectrogram that looks full to the top,
the same shape as an honest file's, so a screen that says `was lossy` and
nothing else is hard to trust. The sentence that explains the verdict has to be
on the screen, not in a `title` nobody has a reason to open.

Every way this can be wrong is a way that reads as working. A chip built from a
map returns `undefined` for a proof nobody added and draws an empty box. A note
appended after the picture is valid JavaScript that arrives too late to do the
one thing it exists for. A verdict sentence read off a payload field that is
not there prints nothing at all, silently. So the functions are run over stubs
and what they drew is read back, the way `test_album_steps.py` does it.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web"
SCRIPT = WEB / "app.js"
STRINGS = WEB / "strings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

DOM = """
class Node {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.children = [];
    this.style = {};
    this.classList = {
      _node: this,
      add(name) { this._node.className += ` ${name}`; },
      toggle(name, on) { if (on) this.add(name); },
    };
  }
  append(...nodes) { for (const node of nodes) this.children.push(node); }
  addEventListener(name, run) { this.pressed = run; }
}
const document = { createElement: (tag) => new Node(tag) };
// What the window carries around these functions. `canMeasureAudio` decides
// whether a gesture is offered at all, so it is set per test.
const state = { canMeasureAudio: true, qualityRunning: false };
const analysesAsked = [];
function startBenchAnalysis(albums) { analysesAsked.push(albums); }
function flatten(node) {
  return {
    tag: node.tag,
    className: node.className.trim(),
    text: node.textContent,
    title: node.title,
    children: node.children.map(flatten),
  };
}
"""


def _strings() -> str:
    """Return the real strings object, so no sentence here is a copy of one."""
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR literal to read, so this guard is looking at the wrong file"
    return found.group(0)


def _run(names: list[str], body: str) -> dict:
    """Run these window functions in node and report what they drew."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in names:
        found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    program = DOM + _strings() + "\n" + "\n".join(pieces) + ";\n" + body
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def _texts(node: dict) -> list[str]:
    """Return every piece of text this node drew, in the order it drew it."""
    found = [node["text"]] if node["text"] else []
    for child in node["children"]:
        found.extend(_texts(child))
    return found


def test_a_line_says_which_reading_convicted_it() -> None:
    """`transcoded` alone cannot separate a proof you can see from one you cannot."""
    drawn = _run(
        ["proofChip"],
        "console.log(JSON.stringify({"
        ' grid: flatten(proofChip("grid")),'
        ' wall: flatten(proofChip("wall")),'
        ' spectrum: flatten(proofChip("spectrum")),'
        "}));",
    )
    assert drawn["grid"]["text"] == "frame grid"
    assert drawn["wall"]["text"] == "wall"
    assert drawn["spectrum"]["text"] == "spectrum"
    # Each carries its own explanation, and no two of them carry the same one:
    # a tooltip copied across three chips is three chips saying nothing.
    tips = {drawn[proof]["title"] for proof in ("grid", "wall", "spectrum")}
    assert len(tips) == 3
    assert all(tips)


def test_a_line_that_convicted_nothing_names_no_proof() -> None:
    """A file the app calls clean has no evidence to name.

    Drawing `spectrum` on it would be a claim about the reading that decided
    the opposite, and an empty chip is worse: a box with no word in it reads as
    a value that failed to load.
    """
    drawn = _run(
        ["proofChip"],
        'console.log(JSON.stringify({ none: proofChip(null), unknown: proofChip("nonsense") }));',
    )
    assert drawn["none"] is None
    assert drawn["unknown"] is None


def test_the_note_arrives_before_the_picture_and_not_after_it() -> None:
    """The note exists to be read before the spectrogram creates the doubt.

    Appended after the image it is still correct, still true, and useless: the
    picture is seen first, the application is judged wrong, and the sentence
    that answers it is below the fold. Order is the whole feature,
    and order is exactly what reading the source proves nothing about.
    """
    drawn = _run(
        ["spectrogramRow", "gridPictureNote", "wallInterval"],
        "const shown = { file: 'a.flac', image: 'data:,', top_hertz: 22050,"
        " cutoff: null, wall_low: null, encoder_walls: [] };"
        "const row = flatten(spectrogramRow(shown, { proved_by: 'grid', frame_grid_z: 41.2 }));"
        "const clean = flatten(spectrogramRow(shown, { proved_by: null, frame_grid_z: null }));"
        "console.log(JSON.stringify({ row, clean }));",
    )
    cell = drawn["row"]["children"][0]
    kinds = [child["className"] for child in cell["children"]]
    assert kinds[0] == "spec-grid-note", f"the note is not first in the cell: {kinds}"
    assert "spec" in kinds, "the picture is not in the cell at all"
    assert kinds.index("spec-grid-note") < kinds.index("spec")

    # And it is drawn only where the picture cannot answer. A file convicted by
    # its wall shows that wall, so the same note there would be false.
    clean_kinds = [child["className"] for child in drawn["clean"]["children"][0]["children"]]
    assert "spec-grid-note" not in clean_kinds


def test_the_note_explains_the_grid_rather_than_asserting_it() -> None:
    """A reader told only *we found something you cannot see* trusts the picture.

    The note is direct, and carries enough context that somebody who knows
    spectrograms and has never heard of a frame grid understands why a FLAC
    reaching past 20 kHz is still a lossy file.
    """
    drawn = _run(
        ["gridPictureNote"],
        "console.log(JSON.stringify(flatten(gridPictureNote({ frame_grid_z: 41.2 }))));",
    )
    said = " ".join(_texts(drawn))
    assert "The picture will not show this one." in said
    # The mechanism, in the words that make it checkable rather than magic.
    for word in ("576", "MP3 encoder", "320 kbps MP3 is full to the top too"):
        assert word in said, f"the note never says {word!r}, so it asks to be believed"
    # And the number behind the sentence, so nothing here has to be taken on trust.
    assert "41.2 deviations above" in said


def test_the_album_says_what_convicted_it_on_the_screen() -> None:
    """The sentence that names the evidence is drawn, not kept in a tooltip.

    A reason such as `Replayed from 5 measurements already on record.` names no
    evidence at all. The sentence has to be written, and it has to be drawn.
    """
    drawn = _run(
        ["qualityEvidence"],
        "const grid = flatten(qualityEvidence({ reason: '5 of 5 carry the grid.',"
        " proved_by: 'grid' }));"
        "const wall = flatten(qualityEvidence({ reason: '3 of 5 stop dead.',"
        " proved_by: 'wall' }));"
        "const none = flatten(qualityEvidence({ reason: '', proved_by: null }));"
        "console.log(JSON.stringify({ grid, wall, none }));",
    )
    assert "5 of 5 carry the grid." in _texts(drawn["grid"])
    said = " ".join(_texts(drawn["grid"]))
    assert "At least four of six readings agree." in said
    # The half that makes the agreement mean anything: six readings
    # of one measurement would agree with themselves and prove
    # nothing, so the sentence has to say they could have disagreed.
    assert "Not the same measurement repeated" in said
    assert "576" in said

    # A wall conviction gets its sentence and not the grid's: the picture shows
    # a wall, and saying it will not is the same falsehood in the other direction.
    wall_said = " ".join(_texts(drawn["wall"]))
    assert "3 of 5 stop dead." in wall_said
    assert "readings agree" not in wall_said

    # An album with nothing measured draws nothing rather than an empty heading.
    assert _texts(drawn["none"]) == []


def test_re_reading_the_bench_re_reads_what_is_open_under_it() -> None:
    """Every path that re-reads the bench re-reads the open folds as well.

    After a deep measurement is adopted, the chip above the fold turns
    `LOSSLESS`; if the table underneath is drawn from a cache written before
    the adopt, it goes on showing `WAS LOSSY` chips. Clearing the cache on one
    of the paths that re-read the bench is not enough: the one that writes
    measurements needs it too.

    Run rather than read, because the failure is not in any line: every line
    is correct, and the defect is a call that is not made.
    """
    drawn = _run(
        ["refreshBench", "refreshOpenQualityFolds"],
        "const asked = [];"
        # The harness already declares `state`; a second `const` of the same
        # name is a SyntaxError, which is the whole program refusing to run.
        "Object.assign(state, { qualityOpen: new Set(['/A', '/B']),"
        " qualityTracks: new Map(), benchRoots: [], quality: [], canMeasureAudio: true });"
        "let draws = 0;"
        "function renderQuality() { draws += 1; }"
        "const api = () => ({"
        "  bench_state: async () => ({ roots: [], albums: [{ id: 1 }], can_measure: true }),"
        "  quality_tracks: async (path) => { asked.push(path);"
        "    return { ok: true, tracks: [{ file: 'x.flac' }] }; },"
        "});"
        "(async () => { await refreshBench();"
        " console.log(JSON.stringify({ asked, draws,"
        "  cached: [...state.qualityTracks.keys()],"
        "  stuck: [...state.qualityTracks.values()].some((held) => !held.tracks.length) }));"
        "})();",
    )
    assert sorted(drawn["asked"]) == ["/A", "/B"], (
        "re-reading the bench left an open fold drawn from rows written before "
        "the measurement changed"
    )
    # Cleared *and* filled. An emptied cache draws `Measuring…` under an album
    # nobody is measuring, which is a screen frozen on a status it never leaves.
    assert sorted(drawn["cached"]) == ["/A", "/B"]
    assert drawn["stuck"] is False
    # Twice: once when the rows arrive and once when the folds do. Drawing only
    # at the end would leave the shelf stale for the length of the re-read.
    assert drawn["draws"] == 2


def test_the_gesture_that_settles_a_verdict_stands_beside_it() -> None:
    """`Analyze in depth` is offered from the album's own open fold.

    A button in the top bar that does not exist until an album is marked is
    hard to find, and a way to settle a verdict that cannot be found is not a
    way to settle it. So it is offered where somebody is standing at the moment
    they disagree, and it needs nothing marked at all.

    Offered only where something can measure. An entry that always fails is
    worse than an absent one, which is the same reasoning the top bar button
    follows.
    """
    drawn = _run(
        ["qualityEvidence"],
        "const shown = flatten(qualityEvidence({ id: 7, reason: '5 of 5 carry the grid.',"
        " proved_by: 'grid' }));"
        "state.canMeasureAudio = false;"
        "const mute = flatten(qualityEvidence({ id: 7, reason: '5 of 5 carry the grid.',"
        " proved_by: 'grid' }));"
        "console.log(JSON.stringify({ shown, mute }));",
    )
    assert "Analyze this album in depth" in _texts(drawn["shown"])
    assert "Analyze this album in depth" not in _texts(drawn["mute"])


def test_the_note_is_not_drawn_over_a_picture_that_does_show_the_proof() -> None:
    """A note must not contradict the picture it stands over.

    A file can carry both proofs. `album_proof` says so in its own docstring —
    *one file can carry both a wall and a grid, and the grid is the finding a
    screen has to warn about* — and the warning it is made of asserts the
    picture will not show this one. Over a file that also stops at 14 kHz, with
    the band drawn across the spectrogram, that sentence is false about the
    picture beside it. Measured on a real library, a large share of the files
    the grid convicts also carry a wall at or below 19 kHz.

    The guard above this one states the rule in its comment — *a file convicted
    by its wall shows that wall, so the same note there would be false* — and
    asserts it against a file convicted by nothing at all, which is a different
    case entirely. This is the one it was describing.
    """
    shown = (
        "const shown = { file: 'a.flac', image: 'data:,', top_hertz: 22050,"
        " cutoff: 14000, wall_low: 13000, encoder_walls: [] };"
    )
    drawn = _run(
        ["spectrogramRow", "gridPictureNote", "wallInterval"],
        shown + "console.log(JSON.stringify({"
        " both: flatten(spectrogramRow(shown, { proved_by: 'grid', frame_grid_z: 78.5,"
        "   picture_shows_it: true })),"
        " grid_only: flatten(spectrogramRow(shown, { proved_by: 'grid', frame_grid_z: 78.5,"
        "   picture_shows_it: false })),"
        "}));",
    )
    both = [child["className"] for child in drawn["both"]["children"][0]["children"]]
    assert (
        "spec-grid-note" not in both
    ), "the note says the picture will not show this one, over a picture that does"
    # And the file the note was written for still gets it: the whole point is the
    # spectrum that reaches the top and looks healthy.
    alone = [child["className"] for child in drawn["grid_only"]["children"][0]["children"]]
    assert "spec-grid-note" in alone, "the file nobody can see the proof of lost its note"


def test_the_marks_on_a_picture_are_labels_and_never_a_stroke_across_it() -> None:
    """A line laid over a spectrum is read as part of the spectrum.

    A horizontal stroke is exactly what a low-pass filter leaves, so a ruler
    ruled across the picture shows a cut on a file that has none.
    Each mark is an anchor that places a label at a frequency and has no extent
    of its own — and the measured wall used to be given a height, so that its
    two borders bounded the interval.

    The rates are whatever the payload carries: the window keeps no list.
    """
    drawn = _run(
        ["spectrogramRow", "gridPictureNote", "wallInterval"],
        "const strokes = [];"
        "const seen = (node) => {"
        "  if (/spec-(wall|reference)$/.test(node.className.trim())) {"
        "    strokes.push({ kind: node.className.trim(), style: Object.keys(node.style),"
        "      top: node.style.top, label: node.children.map((c) => c.textContent),"
        "      tip: node.children.map((c) => c.title).join('') });"
        "  }"
        "  node.children.forEach(seen);"
        "};"
        "const shown = { file: 'a.flac', image: 'data:,', top_hertz: 22050,"
        " cutoff: 19000, probes: [16000, 17500, 19000, 20000],"
        " encoder_walls: [{ hertz: 16000, bitrate: 128 }, { hertz: 20000, bitrate: 320 }] };"
        "seen(spectrogramRow(shown, { proved_by: 'wall' }));"
        "const low = [];"
        "const lowest = { ...shown, cutoff: 16000, encoder_walls: [] };"
        "const seenLow = (node) => {"
        "  if (node.className.trim() === 'spec-wall') {"
        "    low.push({ style: Object.keys(node.style),"
        "      label: node.children.map((c) => c.textContent),"
        "      tip: node.children.map((c) => c.title) });"
        "  }"
        "  node.children.forEach(seenLow);"
        "};"
        "seenLow(spectrogramRow(lowest, { proved_by: 'wall' }));"
        "console.log(JSON.stringify({ strokes, low }));",
    )
    marks = drawn["strokes"]
    assert [mark["kind"] for mark in marks] == ["spec-reference", "spec-reference", "spec-wall"]
    assert [mark["label"] for mark in marks] == [
        ["128 kbps"],
        ["320 kbps"],
        ["stops 17.5\u201319.0 kHz"],
    ]
    for mark in marks:
        assert mark["style"] == ["top"], f"{mark['kind']} is given an extent: {mark['style']}"
        assert mark["tip"], f"{mark['kind']} lost the tooltip its caveats live in"
    # The interval's upper rung, which is where its label sits.
    assert marks[-1]["top"] == f"{(1 - 19000 / 22050) * 100}%"
    # The lowest rung has nothing probed beneath it: the same anchor, other words.
    (low,) = drawn["low"]
    assert low["style"] == ["top"]
    assert low["label"] == ["stops at or below 16.0 kHz"]
    # And other words in the tooltip too: one sentence for both readings called
    # this one an interval between two frequencies, and it has only the one.
    assert "between these two frequencies" in marks[-1]["tip"]
    assert "between" not in low["tip"][0]
    assert "or lower" in low["tip"][0]


def test_the_chip_does_not_point_to_a_note_that_is_not_there() -> None:
    """`tipProofGrid` ends by saying to open the picture and read the note.

    On a file that also carries a wall there is no note to read, because the
    scepticism it exists to get in front of has nothing to form around — the
    picture is not the healthy-looking one. The chip says what convicted and
    stops promising a sentence that was not drawn.
    """
    drawn = _run(
        ["proofChip"],
        "console.log(JSON.stringify({"
        ' alone: flatten(proofChip("grid", false)),'
        ' both: flatten(proofChip("grid", true)),'
        ' wall: flatten(proofChip("wall", true)),'
        "}));",
    )
    assert drawn["alone"]["text"] == drawn["both"]["text"], "the chip changed what it names"
    assert "read the note beside it" in drawn["alone"]["title"]
    assert "read the note beside it" not in drawn["both"]["title"]
    assert "the picture does show" in drawn["both"]["title"]
    # The other proofs are untouched by the second argument.
    assert drawn["wall"]["title"] == drawn["wall"]["title"]


def test_the_window_reads_the_fact_and_the_application_sends_it() -> None:
    """Both ends of the pair, in one assertion.

    `picture_shows_it` decides whether the note is drawn, and a field read but
    never sent is `undefined` — falsy, which here means *draw it*, which is the
    defect this exists to stop. A payload key is exactly the shape that goes
    missing without anything failing.
    """
    api = (Path(__file__).resolve().parents[2] / "src/diglibrary/application/api.py").read_text(
        encoding="utf-8"
    )
    assert '"picture_shows_it"' in api, (
        "the window asks whether the picture shows the proof and nothing answers, "
        "so the note comes back on every album"
    )
    assert "picture_shows_it" in SCRIPT.read_text(encoding="utf-8")
    quality = (
        Path(__file__).resolve().parents[2] / "src/diglibrary/application/quality.py"
    ).read_text(encoding="utf-8")
    assert (
        "def the_picture_shows_the_proof" in quality
    ), "the fact is computed somewhere other than beside the proof it qualifies"


def test_a_word_measured_from_another_file_says_so() -> None:
    """A `LOSSLESS` read from another file's row is marked as borrowed.

    A row written with no audio key, no cutoff and no grid may describe other
    audio of the same shape, while a fresh reading of the file itself finds a
    wall and a frame grid. The row is not stale — it is about other audio — so
    its word must not be drawn exactly like a word measured here.
    """
    drawn = _run(
        ["sharedMeasurementChip"],
        "console.log(JSON.stringify({"
        " borrowed: flatten(sharedMeasurementChip("
        '   {encoding: "lossless", measured_from_this_file: false})),'
        "}));",
    )

    assert drawn["borrowed"]["text"] == "another file's reading"
    assert "not taken from this file" in drawn["borrowed"]["title"]
    assert "measurement" not in drawn["borrowed"]["className"].split()[0]


def test_a_word_measured_here_carries_no_such_mark() -> None:
    """Or the warning is on every line and means nothing."""
    drawn = _run(
        ["sharedMeasurementChip"],
        "console.log(JSON.stringify({"
        ' mine: sharedMeasurementChip({encoding: "lossless", measured_from_this_file: true}),'
        "}));",
    )

    assert drawn["mine"] is None


def test_a_track_with_no_verdict_and_one_with_no_answer_are_left_alone() -> None:
    """`false`, never falsy, and never over a line that says nothing yet.

    A payload without the field answers `undefined`, which is what every track
    measured before the field existed answers — marking those would put the warning on the
    whole screen. And a file nobody has measured has no word for this to
    qualify: it already reads `not measured`, which says more.
    """
    drawn = _run(
        ["sharedMeasurementChip"],
        "console.log(JSON.stringify({"
        ' silent: sharedMeasurementChip({encoding: "lossless"}),'
        " unmeasured: sharedMeasurementChip({measured_from_this_file: false}),"
        " nothing: sharedMeasurementChip(null),"
        "}));",
    )

    assert drawn["silent"] is None
    assert drawn["unmeasured"] is None
    assert drawn["nothing"] is None
