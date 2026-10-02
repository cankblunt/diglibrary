"""Two things the album dialog decides while drawing, run rather than read.

The note above the plan, and the slot that answers what proved this
identification. Both are chosen by a ternary that is valid JavaScript whichever
branch it picks, so neither is visible in the source.

With nothing to change, `What will change` can carry one fact three times: the
heading says `Nothing would change`, the note under it says `Nothing below
changes a name`, and the list under that says `Nothing to change — this album
is already exactly as it should be`. The note has two readings that both do it;
the second is `planNoteUnchanged`, which stands over the very same line.

The note is a caption for the rows beneath it, so it is drawn where there are
rows and nowhere else. None of that is visible in the source: the ternary is
valid JavaScript whichever sentence it picks, and the defect is that the element
is on screen at all. So this runs the real `renderAlbumDialog` over stubs and
reads what the fold ended up holding.

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

# Everything `renderAlbumDialog` reaches for and this question does not turn on.
# `renderPlanEditor` is the exception: whether the names are on screen is one of
# the two readings, so the stub is what decides it.
STUBS = """
const nodes = {};
const make = (id) => ({
  id, textContent: "", hidden: false, title: "", className: "", value: "",
  children: [], replaceChildren(...k) { this.children = k; },
  append(...k) { this.children.push(...k); },
  get childNodes() { return this.children; },
  classList: { add() {}, remove() {}, toggle() {} }, dataset: {}, style: {},
  setAttribute() {}, addEventListener() {}, showModal() {}, close() {},
});
const $ = (id) => (nodes[id] ||= make(id));
const document = { createElement: () => make("new") };
// `identifying` is the albums a scan is looking up right now. A re-scan leaves
// this dialog open, so the footer's two writing gestures read it, and a stub
// that omits it is a harness answering a question the real window would ask
// of something else.
const state = {
  openAlbum: null, covers: new Map(), shelf: [], marks: new Set(),
  identifying: new Set(),
};
let editorHidden = true;
function renderPlanEditor() { $("plan-editor").hidden = editorHidden; }
function renderAlbumSteps() {}
function renderEssentials() {}
function renderRating() {}
function drawDialogCover() {}
async function requestCover() {}
function renderSharedAudio() {}
function renderWitnessWay() {}
function candidateFacts() { return []; }
function matchBadge() { return make("badge"); }
function nothingMatchedHere() { return false; }
function evReleaseTracks() { return []; }
function foldTagWrites(x) { return x; }
function inReadingOrder(x) { return x; }
function operationLine(operation) {
  const e = make("op");
  e.textContent = operation.kind;
  return e;
}
function approvePart() { return false; }
function tagsDiffer() { return false; }
function seconds(ms) { return String(ms); }
"""

_EMPTY_ALBUM: dict[str, object] = {
    "unit_id": 1,
    "operations": [],
    "partial_operations": [],
    "blockers": [],
    "warnings": [],
    "positions": [],
    "candidates": [],
    "shared_audio": [],
    "extra_positions": {},
    "evidence": [],
    "tracks": 12,
    "source": "discogs",
    "proof": "audio",
}


def _strings() -> str:
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _dialog() -> str:
    """The whole of `renderAlbumDialog`, matched by its braces rather than by a
    regex: it is several hundred lines long, and a lazy `[\\s\\S]+?\\n\\}` stops
    at the first brace that opens a line."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("function renderAlbumDialog(album) {")
    depth = 0
    kept: list[str] = []
    for index, line in enumerate(source[start:].split("\n")):
        kept.append(line)
        depth += line.count("{") - line.count("}")
        if index and depth == 0:
            # The footer's Apply button is closed by one rule the dialog shares
            # with `approveAlbum`, so the rule travels with the dialog.
            rule = re.search(r"^function approveIsClosed\(album\) \{.*?\n\}", source, re.M | re.S)
            assert rule, "no approveIsClosed() beside the dialog; this guard reads the wrong file"
            return rule.group(0) + "\n" + "\n".join(kept)
    raise AssertionError("renderAlbumDialog never closes; this guard reads the wrong file")


def _fold(album: dict[str, object], names_on_screen: bool) -> dict[str, object]:
    program = (
        _strings()
        + STUBS
        + _dialog()
        + f"\neditorHidden = {json.dumps(not names_on_screen)};"
        + "\nrenderAlbumDialog("
        + json.dumps(album)
        + ");"
        + "\nconsole.log(JSON.stringify({"
        + ' noteHidden: $("plan-note").hidden,'
        + ' note: $("plan-note").textContent,'
        + ' operations: $("album-operations").children.map((c) => c.textContent),'
        + ' reason: $("album-reason").textContent,'
        + ' reasonHidden: $("album-reason").hidden,'
        + ' proofHidden: $("album-proof").hidden,'
        + ' hover: $("album-operations").children.map((c) => c.title) }));'
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_the_note_goes_away_where_the_plan_lists_nothing() -> None:
    """An organized album: no names to edit, nothing to write."""
    fold = _fold(_EMPTY_ALBUM, names_on_screen=False)

    assert fold["operations"] == [
        "Nothing to change — this album is already exactly as it should be."
    ]
    assert fold["noteHidden"] is True, (
        "the note captions the rows below it, and there are none — it said "
        f"{fold['note']!r} one line above the sentence that says the same thing"
    )


def test_the_other_reading_of_the_note_goes_away_too() -> None:
    """The sibling reading of the same ternary.

    `planNoteUnchanged` is drawn for the album whose names *are* listed and whose
    plan changes none of them, and it stands over the identical line. A rule
    applied to one reading of a ternary has to be applied to the other.
    """
    fold = _fold(_EMPTY_ALBUM, names_on_screen=True)

    assert fold["operations"] == [
        "Nothing to change — this album is already exactly as it should be."
    ]
    assert fold["noteHidden"] is True, f"the other reading is still drawn: {fold['note']!r}"
    assert fold["hover"] == [
        "The names below are shown the same way a change would be, so the claim can "
        "be checked. Each one is still editable: your word outranks the catalogue."
    ], "the hover that explained those rows has to travel with the sentence that stays"


def test_the_note_stays_where_there_are_rows_to_caption() -> None:
    """And it is a caption, so it is there whenever anything is listed."""
    album = dict(_EMPTY_ALBUM, operations=[{"kind": "rename_file"}])

    fold = _fold(album, names_on_screen=True)

    assert fold["noteHidden"] is False
    assert fold["note"].startswith("Every name below is editable")


def test_the_slot_answers_what_proved_this_and_not_what_the_folder_did() -> None:
    """The slot states the evidence on every album, not only where the plan has
    blockers.

    It is the line read to find out why this is the right record. Filled with
    the state sentence, it says `This album was organized by an earlier run…`
    on most organized albums: a sentence about the folder's history, in the
    place evidence goes.
    """
    album = dict(
        _EMPTY_ALBUM,
        evidence_line="Year agrees.",
        reason="This album was organized by an earlier run, so this scan left it as it is.",
    )

    fold = _fold(album, names_on_screen=False)

    assert fold["reason"] == "Year agrees.", (
        "the evidence is what this slot is for, and it was there all along — "
        f"the slot says {fold['reason']!r}"
    )
    assert fold["reasonHidden"] is False


def test_the_state_sentence_is_what_is_left_where_nothing_was_compared() -> None:
    """An album with no candidate has no evidence to state, and says so.

    Written as *whatever evidence there is, else the state* rather than as a list
    of the states that have none — the member nobody has met yet is invisible
    otherwise.
    """
    album = dict(_EMPTY_ALBUM, evidence_line="", explanation="", reason="Not looked up yet.")

    fold = _fold(album, names_on_screen=False)

    assert fold["reason"] == "Not looked up yet."


def test_the_slot_draws_nothing_rather_than_an_empty_line() -> None:
    """Some organized albums have nothing left to state once the three facts
    the rest of the dialog carries are subtracted."""
    album = dict(_EMPTY_ALBUM, evidence_line="", explanation="", reason="")

    fold = _fold(album, names_on_screen=False)

    assert fold["reasonHidden"] is True


def test_an_album_held_by_the_user_is_not_overwritten_by_evidence() -> None:
    """Holding is the user's decision, and no match measured it."""
    album = dict(
        _EMPTY_ALBUM,
        held=True,
        evidence_line="Year agrees.",
        reason="Held for review at the user's request.",
    )

    fold = _fold(album, names_on_screen=False)

    assert fold["reason"] == "Held for review at the user's request."


def test_the_proof_line_stops_deferring_to_a_sentence_that_lost_the_lengths() -> None:
    """The evidence sentence does not state the lengths, so this line is the one
    place they are spoken of — including where the plan has blockers.
    """
    album = dict(
        _EMPTY_ALBUM,
        blockers=["10 release tracks have no file."],
        release_id="r1",
        proof="audio",
        evidence_line="Year agrees.",
    )

    fold = _fold(album, names_on_screen=False)

    assert fold["proofHidden"] is False, "the lengths are said nowhere else on this screen"
