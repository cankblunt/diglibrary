"""The way back from the last gesture, run rather than read.

Three functions, and each fails in a way the source cannot show. `cancelLast`
reads the album out of the answer rather than rebuilding one, so a version that
redrew what was already on screen would look identical in the file and would
put the wrong album back. `redrawOpenAlbum` has to refuse while a field is being
typed in — one rule shared by the witness's reply and the scan's own answer,
and a shared rule has to hold in every caller. `markArrivals` is one line, and
what has to be proved is that every arrival reaches it: `test_web_assets.py`
reads the callers, and this reads the callee.

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

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

HARNESS = """
const drawn = {};
const dialog = { open: true, contains: () => inDialog, close: () => { closed = true; } };
let inDialog = false;
let closed = false;
const shown = [];
const $ = (id) => {
  if (id === "album-dialog") return dialog;
  if (!(id in drawn)) {
    drawn[id] = {
      textContent: "", hidden: false, disabled: false, dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
      showModal: () => shown.push(id),
      close: () => {},
    };
  }
  return drawn[id];
};
const scanned = [];
function scanBegan(units) { scanned.push(units); }
const relocated = [];
const forgotten = [];
async function relocateFromMenu(unitId) { relocated.push(unitId); }
async function forgetAlbum(unitId) { forgotten.push(unitId); }
function toastError(message) { errored.push(message); }
function toastRefusal(message) { errored.push(message); }
const document = { activeElement: { tagName: "BUTTON" } };
const rendered = [];
const toasted = [];
const errored = [];
let refreshed = 0;
let counted = 0;
const STR = {
  albumGone: "This album is not on screen.",
  cancelled: "Back to how it was before the last thing you asked for.",
  scanAgainBecause: (source) =>
    `${source} did not answer when this album was read. Asking again.`,
  cancelledKeepingWords:
    "Back to how it was before the last thing you asked for. "
    + "What you typed is kept, and comes back when you plan this album again.",
};
const state = { openAlbum: { unit_id: 7 }, marked: new Set() };
let answer = { ok: true, album: { unit_id: 7, organized: true, can_cancel: false } };
const api = () => ({
  cancel: async (unitId) => { asked.push(unitId); return answer; },
  scan_selected: async (units) => { asked.push(units); return {ok: true}; },
});
const asked = [];
function renderAlbumDialog(album) { rendered.push(album); }
function renderMarkCount() { counted += 1; }
function toast(message) { toasted.push(message); }
async function refresh() { refreshed += 1; }
// The real one puts a turning ring on the button and restores what it found;
// what matters here is that the work still runs and is awaited.
async function withSpinner(button, run) { return run(); }
"""

SAY = """
console.log(JSON.stringify({
  asked, rendered, toasted, errored, refreshed, counted, closed, shown, scanned,
  goneTitle: $("album-gone-title").textContent,
  goneMessage: $("album-gone-message").textContent,
  marked: [...state.marked],
}));
"""


def _run(body: str) -> dict:
    """Run the window's three functions in node and report what they did."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in (
        "canRedrawOpenAlbum",
        "redrawOpenAlbum",
        "markArrivals",
        "cancelLast",
        "closeAlbumOrAsk",
        "leaveTakingBack",
        "scanThisAlbum",
        "showAlbumGone",
    ):
        found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    # A newline and a semicolon between the last function and the call: a `}`
    # followed by `(` is one expression to the parser, and the whole program then
    # fails at the point where it looks most correct.
    program = (
        HARNESS
        + "let scanAgainAsked = false;\n"
        + "\n".join(pieces)
        + ";\n(async () => {\n"
        + body
        + "\n})();"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_the_album_it_draws_is_the_one_the_answer_carried() -> None:
    """What goes back on screen is decided on the other side of the bridge.

    The API knows what *before* was; the window does not, and a screen that
    reconstructed it would be a second rule for one thing.
    """
    said = _run("""
        answer = {ok: true, album: {unit_id: 7, organized: true, can_cancel: false}};
        await cancelLast();
        """ + SAY)

    assert said["asked"] == [7], "it asked about the album that is open"
    assert said["rendered"] == [{"unit_id": 7, "organized": True, "can_cancel": False}]
    assert said["toasted"] == ["Back to how it was before the last thing you asked for."]
    assert "kept" not in said["toasted"][0], "nothing was typed, so nothing is explained"
    assert said["refreshed"] == 1, "the shelf reads the state it just moved back"


def test_the_screen_says_when_a_typed_word_is_being_kept_out_of_sight() -> None:
    """The one moment `Cancel` looks like it threw away what was typed.

    A typed word is recorded against the album and comes back the next time it
    is planned, but the reading being put back never had it — so without
    a sentence, the screen is indistinguishable from one that dropped it. Which
    of the two sentences is true is decided by the API, which is the side that
    knows what was on record before the gesture.
    """
    said = _run("""
        answer = {ok: true, kept_words: true, album: {unit_id: 7}};
        await cancelLast();
        """ + SAY)

    assert said["toasted"] == [
        "Back to how it was before the last thing you asked for. "
        "What you typed is kept, and comes back when you plan this album again."
    ]


def test_a_refusal_says_so_and_draws_nothing() -> None:
    """An album with nothing to take back must not be redrawn as if it had been."""
    said = _run("""
        answer = {ok: false, error: "There is nothing to take back on this album."};
        await cancelLast();
        """ + SAY)

    assert said["errored"] == [
        "There is nothing to take back on this album."
    ], "a refusal is said, and said the way a refusal is said"
    assert said["rendered"] == []
    assert said["refreshed"] == 0
    assert said["toasted"] == []


def test_a_fresh_answer_never_lands_on_a_field_being_typed_in() -> None:
    """A reply that redraws the dialog would take away what is being typed.

    The scan's answer arrives while the dialog is open, as the witness's reply
    does, so this rule has two callers and has to hold in both.
    """
    over = _run("""
        inDialog = true;
        document.activeElement = {tagName: "INPUT"};
        redrawOpenAlbum({unit_id: 7});
        """ + SAY)
    assert over["rendered"] == [], "it waited for the next time the album is opened"

    beside = _run("redrawOpenAlbum({unit_id: 7});" + SAY)
    assert beside["rendered"] == [{"unit_id": 7}], "and drew it when nothing was being typed"

    other = _run("redrawOpenAlbum({unit_id: 99});" + SAY)
    assert other["rendered"] == [], "an answer about another album redraws nothing"


def test_leaving_an_album_with_nothing_waiting_just_closes() -> None:
    """The question is asked only where there is an answer to give.

    An album that was merely opened and looked at closes with one press. Asking
    there would be a confirmation about nothing.
    """
    said = _run("""
        state.openAlbum = {unit_id: 7, can_cancel: false};
        closeAlbumOrAsk();
        """ + SAY)

    assert said["closed"] is True
    assert said["shown"] == [], "nothing was asked"


def test_leaving_an_album_with_a_plan_waiting_asks_first() -> None:
    """The two ways out are named before either happens.

    The ✕, a click outside and `Cancel` would otherwise leave the album in
    different states with nothing saying which is which, so the album stays
    open until one is picked.

    The condition is `ask_before_leaving`, which is the server's answer rather
    than *anything can be taken back*: asked over a plain scan, the question
    would be a second click on the way out of the most common gesture.
    """
    said = _run("""
        state.openAlbum = {unit_id: 7, can_cancel: true, ask_before_leaving: true};
        closeAlbumOrAsk();
        """ + SAY)

    assert said["shown"] == ["leave-album-dialog"]
    assert said["closed"] is False, "the album is still there while the question stands"


def test_leaving_a_finished_album_planned_again_puts_it_back_without_asking() -> None:
    """Closing keeps the album as it was.

    `leaving_takes_back` is the server's answer, so every route out —
    the ✕, Escape and a click outside all call this — takes the re-plan back and
    closes, and the question is never drawn. Nothing on the shelf changes, so
    nothing is said; the album was organized and it still is.
    """
    said = _run("""
        state.openAlbum = {unit_id: 7, can_cancel: true, leaving_takes_back: true,
                           ask_before_leaving: false};
        closeAlbumOrAsk();
        await new Promise((done) => setTimeout(done, 0));
        """ + SAY)

    assert said["shown"] == [], "it asked the question this gesture no longer asks"
    assert said["closed"] is True
    assert said["asked"] == [7], "closing did not take the re-plan back"
    assert said["refreshed"] == 1, "the shelf was not redrawn from the album put back"
    assert said["toasted"] == [], "a way out that changes nothing the user can see said something"


def test_what_was_typed_is_still_said_when_leaving_puts_the_album_back() -> None:
    """The one sentence kept: the typed words are on record, and would look lost."""
    said = _run("""
        answer = { ok: true, kept_words: true, album: { unit_id: 7, organized: true } };
        state.openAlbum = {unit_id: 7, can_cancel: true, leaving_takes_back: true};
        closeAlbumOrAsk();
        await new Promise((done) => setTimeout(done, 0));
        """ + SAY)

    assert said["closed"] is True
    assert said["toasted"] == [
        "Back to how it was before the last thing you asked for. "
        "What you typed is kept, and comes back when you plan this album again."
    ]


def test_leaving_an_album_that_was_only_scanned_costs_one_press() -> None:
    """A scan alone does not make the dialog ask before closing.

    A scan arms the way back, so a question keyed on `can_cancel` would be asked
    over it. A scan writes nothing, and leaving its answer standing is the
    ordinary outcome — `Cancel` is still on the row for as long as the session
    holds it.
    """
    said = _run("""
        state.openAlbum = {unit_id: 7, can_cancel: true, ask_before_leaving: false};
        closeAlbumOrAsk();
        """ + SAY)

    assert said["shown"] == [], "closing an album that was only scanned still asks a question"
    assert said["closed"] is True, "and it did not close"


def test_a_rescan_states_its_cost_where_nothing_asked_for_it() -> None:
    """Scanning an album again costs catalogue calls and may change nothing.

    Where every catalogue answered when the album was read, a second reading
    would ask the same questions and wait for the same answers, so nothing runs
    until it is confirmed.

    `looked_at` is part of the state: an album no catalogue was asked about
    has no missing source either, and this question is not for it.
    """
    said = _run("""
        state.openAlbum = {unit_id: 7, looked_at: true, sources_missed: {}};
        await scanThisAlbum();
        """ + SAY)

    assert said["shown"] == ["scan-again-dialog"]
    assert said["scanned"] == [], "and no catalogue was asked anything"


def test_a_rescan_runs_and_says_why_when_a_source_was_missing() -> None:
    """The one thing on record that does ask for another reading.

    A source that did not answer is the reason a second look can differ, so it
    is named and the scan runs — the question would be asking for a decision
    the application can already make.
    """
    said = _run("""
        state.openAlbum = {unit_id: 7, sources_missed: {"MusicBrainz.org": "it did not answer"}};
        await scanThisAlbum();
        """ + SAY)

    assert said["shown"] == [], "nothing was asked"
    assert said["scanned"] == [[7]], "the album was read again"
    assert said["toasted"] == [
        "MusicBrainz.org did not answer when this album was read. Asking again."
    ]


def test_an_album_whose_folder_is_gone_gets_a_screen_of_its_own() -> None:
    """A card whose folder is missing opens a screen, not only a message.

    Everything drawn comes from the answer — the window works nothing out for
    itself, because the side that knows which folder is missing and where the
    other copy is, is the other side of the bridge.
    """
    said = _run("""
        showAlbumGone(42, {
          folder: "/Users/someone/Downloads/Orvalim/Vimbrel Da Quáltora 2/Vimbrel Da Quáltora 2",
          error: "…is not where it was recorded…",
        });
        """ + SAY)

    assert said["shown"] == ["album-gone-dialog"]
    assert said["goneTitle"] == "Vimbrel Da Quáltora 2", "named by its own folder"
    assert said["goneMessage"] == "…is not where it was recorded…", "and the API's sentence"


def test_an_arrival_marks_every_album_it_brought() -> None:
    """One rule for every way an album can arrive, read at the callee."""
    said = _run("markArrivals([3, 9, 3]);" + SAY)

    assert said["marked"] == [3, 9], "each album once, however often it is named"
    assert said["counted"] == 1, "and the button that counts what is marked is redrawn"

    empty = _run("markArrivals(undefined);" + SAY)
    assert empty["marked"] == [], "an arrival that carried no album marks nothing"
