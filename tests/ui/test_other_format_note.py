"""The note on a file that is not in its album's format, run rather than read.

`planRow` draws the row of one file in the album dialog and `renderNamesApplied`
the table an organized album opens on. Whether the mark, the sentence and the
button reach the right-hand cell, and which sentence a verdict gets, is not
visible in the source: the lines are valid JavaScript whichever cell they
append to. So the real functions are run in node over a stand-in document, with
the real strings, and what they built is read back.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from tests.ui.node_program import run_node

WEB = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

REAL = ("otherFormatMark", "otherFormatNote", "hearTracks", "renderNamesApplied", "withSpinner")

HARNESS = """
function element(tag) {
  const node = {
    tag, className: "", textContent: "", title: "", hidden: false, disabled: false,
    children: [], dataset: {}, style: {}, listeners: {},
    classList: {
      add(name) { node.className += " " + name; },
      remove(name) { node.className = node.className.replace(" " + name, ""); },
      toggle() {},
    },
    append(...kids) { node.children.push(...kids.filter((kid) => kid && kid.tag)); },
    appendChild(kid) { node.children.push(kid); return kid; },
    replaceChildren(...kids) { node.children = kids; },
    addEventListener(type, run) { node.listeners[type] = run; },
    setAttribute() {}, closest() { return null; },
  };
  return node;
}
const document = { createElement: element };
const byId = {};
const $ = (id) => (byId[id] = byId[id] || element(id));
const state = { openAlbum: { unit_id: 7 } };
const asked = [];
const redrawn = [];
const refused = [];
let answer = { ok: true, album: { unit_id: 7 } };
const api = () => ({ hear_tracks: async (unitId) => { asked.push(unitId); return answer; } });
function renderAlbumDialog(album) { redrawn.push(album); }
function toastError(message) { refused.push(message); }
// The one helper answered for real: nothing to split the name around, so the
// row takes the branch that writes the name whole.
function editableParts() { return null; }
// Every other helper `planRow` calls, for the parts of a row this test is not
// about, answers with an empty element.
const NAMED = ["planRow", "STR", "document", "element", "editableParts", "row", "state", "api",
  "$", "byId", "asked", "redrawn", "refused", "answer", "renderAlbumDialog", "toastError",
  "flatten", "REAL_NAMES"];
const helpers = new Proxy({}, {
  has: (_, name) => typeof name === "string" && !(name in globalThis)
    && !NAMED.includes(name) && !REAL_NAMES.includes(name),
  get: (_, name) => (name === Symbol.unscopables ? undefined : () => element("stub")),
});
function flatten(node, out = []) {
  out.push({ cls: node.className.trim(), text: node.textContent, title: node.title });
  for (const kid of node.children) flatten(kid, out);
  return out;
}
"""

FOUND = {
    "sample_rate": 96000,
    "bit_depth": 24,
    "album_sample_rate": 44100,
    "album_bit_depth": 16,
    "sharing": 8,
    "verdict": None,
    "title": None,
    "artist": None,
    "askable": False,
}


def _run(body: str) -> object:
    """Run the window's own functions in node and return what the body printed."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    pieces = []
    for name in (*REAL, "planRow"):
        # To the brace alone on its line: a parameter list may close with `}) {`.
        found = re.search(rf"(?:async )?function {name}\([\s\S]+?\n\}}\n", script)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    # `export` makes node read the whole program as a module, which is strict
    # and refuses the `with` the stand-in helpers are reached through.
    strings = (WEB / "strings.js").read_text(encoding="utf-8")
    assert "export const STR = {" in strings, "no STR object in strings.js to say the note with"
    strings = strings.replace("export const STR = {", "const STR = {", 1)
    program = (
        f"{strings}\nconst REAL_NAMES = {json.dumps(REAL)};\n{HARNESS}\n"
        f"with (helpers) {{\n{''.join(pieces)}\n(async () => {{\n{body}\n}})();\n}}"
    )
    done = run_node(program, module=False, check=False, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _row(found: dict | None) -> list[dict]:
    """Draw one unchanged file row and return every element of its plan column."""
    return _run(
        "const row = planRow({ key: 'track-8', label: '08', value: 'Jamming',"
        " before: '08. Jamming.flac', after: '08. Jamming.flac', hint: '', dataset: {},"
        f" position: 8, otherFormat: {json.dumps(found)} }});\n"
        "const after = row.children.find((kid) => kid.className.includes('plan-after'));\n"
        "console.log(JSON.stringify(flatten(after)));"
    )


def _said(found: dict) -> str:
    return _run(f"console.log(JSON.stringify(STR.planOtherFormat({json.dumps(found)})));")


def _tip(found: dict) -> str:
    return _run(f"console.log(JSON.stringify(STR.planOtherFormatTip({json.dumps(found)})));")


def test_a_file_in_another_format_is_marked_and_explained_in_the_plan_column() -> None:
    """The mark follows the name and the sentence sits under it, in the right-hand cell."""
    drawn = _row(FOUND)

    sentence = _said(FOUND)
    tip = _tip(FOUND)
    classes = [piece["cls"] for piece in drawn]
    assert classes[0] == "plan-after", "what is read back is the plan column and nothing else"
    mark = next(piece for piece in drawn if piece["cls"] == "plan-other-format-mark")
    assert (mark["text"], mark["title"]) == ("*", tip)
    note = classes.index("plan-other-format")
    assert drawn[note + 1]["text"] == f"* {sentence}"
    assert drawn[note + 1]["title"] == tip, "the numbers are reachable from the sentence too"
    assert note > classes.index("plan-cell"), "under the name, which stays first on the row"
    assert "link-button plan-other-format-ask" not in classes, "nothing to ask: not askable"


def test_a_file_in_its_albums_format_draws_no_note() -> None:
    """The ordinary row carries neither the mark nor the sentence."""
    classes = [piece["cls"] for piece in _row(None)]

    assert "plan-cell" in classes, "the row was drawn, so the absence below means something"
    assert not [name for name in classes if "other-format" in name]


def test_each_verdict_is_given_the_sentence_its_evidence_supports() -> None:
    """The format alone accuses nothing; only a foreign recording is called not the album's."""
    named = {**FOUND, "verdict": "foreign", "title": "Jamming", "artist": "Bob & The Band"}

    assert _said(named) == (
        'A different song, not one of this album\'s tracks: "Jamming" by Bob & The Band.'
    )
    assert _said({**named, "artist": None}) == (
        'A different song, not one of this album\'s tracks: "Jamming".'
    )
    assert _said({**named, "title": None}) == "A different song, not one of this album's tracks."
    assert _said({**FOUND, "verdict": "same"}) == (
        "The album's own song, from a different source. Nothing here says the audio is worse."
    )
    for unsettled in ("unrecognised", "no_album"):
        assert _said({**FOUND, "verdict": unsettled}) == (
            "This file came from a different source than the rest of the album. "
            "Nothing here says the audio is worse."
        ), f"{unsettled} knows no more about the song than the format does"
    # What tells those two apart is the tip, which is where the evidence is read.
    assert _tip({**FOUND, "verdict": "unrecognised"}) != _tip({**FOUND, "verdict": "no_album"})


def test_a_verdict_nobody_listed_says_only_what_the_format_says() -> None:
    """The fallback describes the one fact known in every case, and names no cause.

    A tip that ended in one of the listed answers would tell the reader the
    audio was put to a service that it was never put to, which is the shape of
    a screen diagnosing what it did not measure.
    """
    unlisted = {**FOUND, "verdict": "something new"}

    assert _said(unlisted) == _said(FOUND)
    assert "AcoustID" not in _tip(unlisted), "the fallback knows of no answer from any service"
    lossy = {**FOUND, "sample_rate": 48000, "bit_depth": None, "album_bit_depth": None}
    assert _tip({**lossy, "sharing": 1}).startswith(
        "This file is 48 kHz, and the other 1 track on this album is 44.1 kHz."
    ), "one sibling is spoken of in the singular, and a lossy file carries no depth"


def test_the_sentence_carries_no_number_and_the_tip_carries_both() -> None:
    """Two numbers were the whole sentence once, and they say nothing on their own.

    A reader who does not already know what a sample rate is learns from a pair
    of them that something is off and nothing about what. They belong where an
    audit looks, which is the tip.
    """
    for verdict in (None, "same", "unrecognised", "no_album", "foreign", "something new"):
        said = _said({**FOUND, "verdict": verdict})
        assert "kHz" not in said and "-bit" not in said, f"{verdict} says a format on the line"
        assert not any(letter.isdigit() for letter in said), f"{verdict} says a number on the line"
        tip = _tip({**FOUND, "verdict": verdict})
        assert "96 kHz / 24-bit" in tip and "44.1 kHz / 16-bit" in tip, f"{verdict} loses the proof"
        assert "the other 8 tracks" in tip, f"{verdict} loses what the file is being compared with"


def test_the_sentence_is_drawn_the_same_whatever_it_says() -> None:
    """One class for every verdict: the sentence is never dressed as part of the name."""
    foreign = [piece["cls"] for piece in _row({**FOUND, "verdict": "foreign", "title": "X"})]
    same = [piece["cls"] for piece in _row({**FOUND, "verdict": "same"})]

    assert "plan-other-format" in foreign and "plan-other-format" in same
    assert foreign == same


def test_the_button_asks_about_the_open_album_and_redraws_with_the_answer() -> None:
    """Pressing it reaches the bridge with the album's id and draws what came back."""
    done = _run(
        f"const note = otherFormatNote({json.dumps({**FOUND, 'askable': True})});\n"
        "const ask = note.children.find((kid) => kid.tag === 'button');\n"
        "await ask.listeners.click();\n"
        "const first = { text: ask.textContent, asked: [...asked], redrawn: redrawn.length,\n"
        "  enabled: !ask.disabled };\n"
        "answer = { ok: false, error: 'AcoustID was not asked.' };\n"
        "await ask.listeners.click();\n"
        "console.log(JSON.stringify({ first, asked, redrawn: redrawn.length, refused }));"
    )

    assert done["first"] == {"text": "Ask AcoustID", "asked": [7], "redrawn": 1, "enabled": True}
    assert done["asked"] == [7, 7]
    assert done["redrawn"] == 1, "a refusal redraws nothing"
    assert done["refused"] == ["AcoustID was not asked."]


def test_an_organized_album_says_it_under_the_name_the_file_carries_now() -> None:
    """The table of names applying gave carries the same mark and sentence."""
    album = {
        "names_applied": [
            {"kind": "folder", "was": "old folder", "now": "New Folder"},
            {"kind": "file", "was": "07 old.flac", "now": "07. Sniff.flac"},
            {"kind": "file", "was": "08 old.flac", "now": "08. Jamming.flac"},
        ],
        "evidence": [
            {"file": "07. Sniff.flac", "other_format": None},
            {"file": "08. Jamming.flac", "other_format": {**FOUND, "verdict": "same"}},
            {"file": None, "other_format": None},
        ],
    }
    rows = _run(
        "byId['plan-editor'] = element('plan-editor'); byId['plan-editor'].hidden = true;\n"
        f"renderNamesApplied({json.dumps(album)});\n"
        "const body = byId['names-applied'].children[1];\n"
        "console.log(JSON.stringify(body.children.map((row) => flatten(row.children[3]))));"
    )

    assert [len(cell) for cell in rows] == [1, 1, 4], "only the one file's cell holds a note"
    assert rows[2][0]["text"] == "08. Jamming.flac"
    assert [piece["cls"] for piece in rows[2][1:3]] == [
        "plan-other-format-mark",
        "plan-other-format",
    ]
    assert rows[2][3]["text"] == f"* {_said({**FOUND, 'verdict': 'same'})}"
