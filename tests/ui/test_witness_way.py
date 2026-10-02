"""The witness's way out of a wrong identification, run rather than read.

The screen offers a source that knows the record and may not name it,
and three separate rules decide what it says: whether there is a witness to
offer, whether what was measured lets the sentence be stated, and whether any of
it belongs on this album at all. Each of those is a branch, and a branch nobody
runs can take the whole window down — a guard that reads the source cannot know
that a name inside the function has gone away.

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

HARNESS = """
function element(id) {
  return {
    id,
    textContent: "",
    hidden: false,
    children: [],
    replaceChildren(...kids) { this.children = kids; },
    append(...kids) { this.children.push(...kids); },
  };
}
const screen = new Map();
const $ = (id) => {
  if (!screen.has(id)) screen.set(id, element(id));
  return screen.get(id);
};
const document = { createElement: (tag) => element(tag) };
const state = { openAlbum: null, witnessRecord: "a record left by another album" };
"""


def _strings() -> str:
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _functions(*names: str) -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in names:
        # `async` is part of the declaration, and cutting it off produces a
        # function whose `await` is a syntax error — which is a harness failing
        # to build rather than a defect, and reads like one if it is not said.
        found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))
    constant = re.search(r"const WITNESS_KNOWS_IT = [\d.]+;", source)
    assert constant, "no WITNESS_KNOWS_IT to run against, so the bar is written somewhere else"
    return constant.group(0) + "\n" + "\n".join(pieces)


def _run(program: str) -> dict:
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def _drawn(album: dict, wrong_record: bool) -> dict:
    """Run the real `renderWitnessWay` and report what the screen ended up with."""
    program = (
        HARNESS
        + _strings()
        + "\n"
        + _functions("witnessKnowsThisRecord", "witnessIsAvailable", "renderWitnessWay")
        + ";\nrenderWitnessWay("
        + json.dumps(album)
        + f", {json.dumps(wrong_record)});\n"
        + "console.log(JSON.stringify({"
        + " wayHidden: $('album-witness-way').hidden,"
        + " saidHidden: $('album-witness-said').hidden,"
        + " said: $('album-witness-said').textContent,"
        + " tracksHidden: $('album-witness-tracks').hidden,"
        + " read: $('album-witness-read').textContent,"
        + " held: state.witnessRecord }));"
    )
    return _run(program)


def _knows(album: dict) -> bool:
    program = (
        _functions("witnessKnowsThisRecord")
        + ";\nconsole.log(JSON.stringify(Boolean(witnessKnowsThisRecord("
        + json.dumps(album)
        + "))));"
    )
    return _run(program)  # type: ignore[return-value]


def _witness(**fields: object) -> dict:
    """An album carrying one iTunes position, in the shape the payload builds."""
    position = {"source": "itunes", "witness": True, "asked": True, "in_use": False}
    position.update(fields)
    return {"positions": [{"source": "discogs", "asked": True}, position]}


# An album as the witness answers for it when asked in the album's own words:
# eleven of its eleven names published by the record.
KNOWN = _witness(titles_agreeing=11, titles_compared=11, tracks=11)


def test_the_witness_that_publishes_every_name_knows_the_record() -> None:
    assert _knows(KNOWN) is True


def test_the_witness_that_publishes_none_of_them_does_not() -> None:
    """Testimony asked for in a candidate's words, not the album's own.

    Its numbers are about the winning candidate's record, which may be
    nobody's, and nothing may be stated from them.
    """
    assert _knows(_witness(titles_agreeing=0, titles_compared=4, tracks=4)) is False


def test_a_witness_that_compared_no_name_says_nothing_either() -> None:
    """`found nothing` and `found a record with no names to compare` both.

    Written as *did it clear the bar* rather than as a list of the ways it can
    fail, because a list of the ways it can fail leaves out the one nobody has
    met.
    """
    assert _knows(_witness(tracks=11)) is False
    assert _knows(_witness(asked=False)) is False
    assert _knows({"positions": [{"source": "discogs", "asked": True}]}) is False


def test_the_way_out_is_drawn_where_the_record_is_wrong() -> None:
    """The sentence, the controls, and the tracklist folded away to start."""
    screen = _drawn(KNOWN, wrong_record=True)

    assert screen["wayHidden"] is False
    assert screen["saidHidden"] is False
    assert "11 of 11" in screen["said"]
    assert screen["tracksHidden"] is True, "the tracklist costs two requests and was not asked for"
    assert screen["read"] == "Read its tracklist"


def test_an_album_the_catalogues_named_gets_none_of_it() -> None:
    """There is no silence to end on an album that was identified correctly."""
    assert _drawn(KNOWN, wrong_record=False)["wayHidden"] is True


def test_the_offer_stands_even_where_nothing_measured_backs_it() -> None:
    """The silence this ends is the one a low *stored* number would produce.

    An album can carry stored testimony about the candidate's
    record rather than its own, so the sentence cannot be stated — and the
    offer to ask must still be there, because the live lookup behind it is the
    only thing that knows.
    """
    screen = _drawn(_witness(titles_agreeing=0, titles_compared=4), wrong_record=True)

    assert screen["wayHidden"] is False, "the way out went away with the sentence"
    assert screen["saidHidden"] is True
    assert screen["said"] == ""


def test_a_witness_that_could_not_be_reached_is_not_offered() -> None:
    """Offering a way out through a source that is not answering is a dead end."""
    screen = _drawn(_witness(reached=False, titles_compared=0), wrong_record=True)

    assert screen["wayHidden"] is True


READING_HARNESS = """
const toasted = [];
const toast = (said) => toasted.push(said);
const toastError = (said) => toasted.push("error: " + said);
const withSpinner = async (button, run) => run();
const sourceLink = (url) => ({ id: "link", textContent: url, children: [] });
const api = () => ({ witness_record: async () => ANSWER });
const button = element("album-witness-read");
screen.set("album-witness-read", button);
// The state `renderWitnessWay` leaves behind, which is what this gesture always
// starts from: both folded away, and no record in hand from the last album.
$("album-witness-head").hidden = true;
$("album-witness-tracks").hidden = true;
state.witnessRecord = null;
state.openAlbum = { unit_id: 7 };
"""

RECORD = {
    "title": "Balo Quirema, Vol. 2",
    "artist": "Zuvane project, Ilda Morvex & Tavo Brenzali",
    "year": 2006,
    "track_count": 11,
    "url": "https://music.apple.com/us/album/balo-quirema-vol-2/100000001",
    "tracks": [
        {"position": 1, "name": "Zerbo", "duration_ms": 210800},
        {"position": 2, "name": "Quilvandra", "duration_ms": 250800},
    ],
}


def _read(answer: dict, presses: int = 1) -> dict:
    """Run the real `readWitnessTracklist` against a stubbed bridge."""
    program = (
        HARNESS
        + f"const ANSWER = {json.dumps(answer)};\n"
        + READING_HARNESS
        + _strings()
        + "\n"
        + _functions("readWitnessTracklist", "witnessRecordNow")
        # The word the boot writes into the button from its `data-str`, which is
        # what a press has to put back when the lookup answers nothing.
        + ";\nbutton.textContent = STR.witnessRead;\n(async () => {\n"
        + f"  for (let n = 0; n < {presses}; n += 1) await readWitnessTracklist(button);\n"
        + "  console.log(JSON.stringify({"
        + " headHidden: $('album-witness-head').hidden,"
        + " head: $('album-witness-head').children.map((k) => k.textContent ?? k),"
        + " tracksHidden: $('album-witness-tracks').hidden,"
        + " tracks: $('album-witness-tracks').children.map((k) => k.textContent),"
        + " read: button.textContent, toasted }));\n"
        + "})();"
    )
    return _run(program)


def test_reading_it_draws_what_the_record_is_and_then_its_songs() -> None:
    """The header first: the record exists, complete, at the witness.

    And the page it was read on, because a witness that contributed to a
    decision has to be one that can be checked.
    """
    screen = _read({"ok": True, "record": RECORD})

    assert screen["headHidden"] is False
    said = " ".join(str(part) for part in screen["head"])
    assert "Balo Quirema, Vol. 2" in said and "2006" in said and "11 tracks" in said
    assert "music.apple.com" in said, "the page it was read on is not offered"
    assert screen["tracks"] == ["1. Zerbo · 210.8s", "2. Quilvandra · 250.8s"]
    assert screen["read"] == "Hide its tracklist"


def test_pressing_it_again_folds_it_away() -> None:
    """And says so on the button, which is the only thing that says which it is."""
    screen = _read({"ok": True, "record": RECORD}, presses=2)

    assert screen["headHidden"] is True
    assert screen["tracksHidden"] is True
    assert screen["read"] == "Read its tracklist"


def test_a_record_with_no_songs_still_says_the_album_is_there() -> None:
    """An empty list drawn open is a blank gap that reads as a fold that failed."""
    screen = _read({"ok": True, "record": {**RECORD, "tracks": []}})

    assert screen["headHidden"] is False, "the album exists over there, and that is the fact"
    assert screen["tracksHidden"] is True


def test_a_witness_that_no_longer_answers_says_so_and_draws_nothing() -> None:
    """The first answer was bought at identification and this one is bought now."""
    screen = _read({"ok": True, "record": None})

    assert screen["headHidden"] is True
    assert screen["toasted"] == ["iTunes does not answer for this record now."]
    assert screen["read"] == "Read its tracklist", "the button went on saying it is open"


def test_a_witness_that_could_not_be_asked_reports_what_it_was_told() -> None:
    """The window carries the reason whole, whatever the reason turns out to be.

    The text here is one the route actually produces rather than one
    written for this test: a sentence no longer in the application, asserted
    against a stub that repeats it, is a guard agreeing with itself.
    """
    screen = _read({"ok": False, "error": "iTunes could not be asked: <urlopen error timed out>"})

    assert screen["toasted"] == ["error: iTunes could not be asked: <urlopen error timed out>"]
    assert screen["headHidden"] is True


def test_every_render_drops_the_record_the_last_album_left() -> None:
    """A verification source's words are never kept.

    And the second reason is the one a test catches: held across albums, the
    tracklist of one record draws under another album's notice.
    """
    assert _drawn(KNOWN, wrong_record=True)["held"] is None
    assert _drawn(KNOWN, wrong_record=False)["held"] is None
