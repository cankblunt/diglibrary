"""Walking the shelf from inside the album dialog, run rather than read.

Two buttons and a count, and every way they can be wrong is a way that reads as
working: a position counted from a list the screen does not show, an end that
quietly wraps to the beginning, a press on an album that is not on the shelf
opening whichever album happens to be first.

None of that is visible in the source — the lines are valid JavaScript whichever
list they walk — so this runs the two functions over stubs and reads what they
did, the same way `test_adopt_control.py` and `test_choose_cover.py` do.

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
const $ = (id) => {
  if (!(id in drawn)) drawn[id] = { textContent: "", hidden: false, disabled: false };
  return drawn[id];
};
const opened = [];
const STR = { albumPosition: (at, total) => `${at} of ${total}` };
let shelf = [];
const state = { openAlbum: null };
function visibleAlbums() { return shelf; }
async function openAlbum(unitId) { opened.push(unitId); return true; }
// The real one puts a turning ring on the button and restores what it found;
// what matters here is that the work still runs and is awaited.
async function withSpinner(button, run) { return run(); }
const cancelled = [];
const toasted = [];
let cancelAnswer = { ok: true };
const api = () => ({
  cancel: async (unitId) => {
    cancelled.push(unitId);
    opened.push(`cancel ${unitId}`);
    return cancelAnswer;
  },
});
function toast(message) { toasted.push(message); }
function toastRefusal(message) { toasted.push(message); }
"""


def _run(body: str) -> dict:
    """Run the two shelf-stepping functions in node and report what they did."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in ("renderAlbumSteps", "stepThroughShelf"):
        found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    # A newline and a semicolon between the last function and the call: a `}`
    # followed by `(` is one expression to the parser, and the whole program
    # then fails at the point where it looks most correct.
    program = HARNESS + "\n".join(pieces) + ";\n(async () => {\n" + body + "\n})();"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


# Read through `$` and not out of `drawn`: a step that never drew anything has
# no entry there, and reading it would throw — which reports a wiring fault as a
# broken test.
SAY = """
console.log(JSON.stringify({
  cancelled, toasted,
  position: $("album-position").textContent,
  hidden: $("album-position").hidden,
  previousOff: $("album-previous").disabled,
  nextOff: $("album-next").disabled,
  arrowsHidden: $("album-previous").hidden && $("album-next").hidden,
  opened,
}));
"""


def test_the_count_says_where_the_open_album_is_on_the_shelf() -> None:
    """`3 of 5`, and the ends are ends: neither arrow wraps around."""
    said = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}, {unit_id: 30}, {unit_id: 40}, {unit_id: 50}];
        renderAlbumSteps({unit_id: 30});
        """ + SAY)

    assert said["position"] == "3 of 5"
    assert said["hidden"] is False
    assert said["previousOff"] is False and said["nextOff"] is False


def test_the_first_and_the_last_album_stop() -> None:
    """A review pass has an end, and a control that starts over hides it."""
    first = _run("shelf = [{unit_id: 10}, {unit_id: 20}];\nrenderAlbumSteps({unit_id: 10});" + SAY)
    assert first["position"] == "1 of 2"
    assert first["previousOff"] is True and first["nextOff"] is False

    last = _run("shelf = [{unit_id: 10}, {unit_id: 20}];\nrenderAlbumSteps({unit_id: 20});" + SAY)
    assert last["position"] == "2 of 2"
    assert last["previousOff"] is False and last["nextOff"] is True


def test_an_album_the_shelf_is_not_showing_has_no_position() -> None:
    """The count is about the list on screen, so an album outside it says nothing.

    An album can be opened from a notice, or the search typed in after opening
    one: counting from a place that is not shown is worse than counting nothing.
    """
    said = _run("shelf = [{unit_id: 10}, {unit_id: 20}];\nrenderAlbumSteps({unit_id: 99});" + SAY)

    assert said["position"] == ""
    assert said["hidden"] is True and said["arrowsHidden"] is True


def test_one_album_on_the_shelf_is_not_a_walk() -> None:
    """Two arrows over a shelf of one are two controls that can never do anything."""
    said = _run("shelf = [{unit_id: 10}];\nrenderAlbumSteps({unit_id: 10});" + SAY)

    assert said["arrowsHidden"] is True and said["hidden"] is True


def test_the_step_opens_the_album_beside_this_one() -> None:
    """Forward and back, through the same door a click on a card goes through."""
    forward = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}, {unit_id: 30}];
        state.openAlbum = {unit_id: 20};
        await stepThroughShelf(1);
        """ + SAY)
    assert forward["opened"] == [30]

    back = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}, {unit_id: 30}];
        state.openAlbum = {unit_id: 20};
        await stepThroughShelf(-1);
        """ + SAY)
    assert back["opened"] == [10]


def test_walking_away_from_a_finished_album_planned_again_puts_it_back_first() -> None:
    """The arrows are a way out, and leaving takes the re-plan back.

    In that order — the album is put back before the next one opens — and only
    where the server says so: an album with nothing to take back is walked past
    exactly as before.
    """
    said = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}, {unit_id: 30}];
        state.openAlbum = {unit_id: 20, leaving_takes_back: true};
        await stepThroughShelf(1);
        """ + SAY)
    assert said["opened"] == ["cancel 20", 30], "put back first, then the next album"
    assert said["toasted"] == [], "nothing the user can see changed, so nothing is said"

    plain = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}, {unit_id: 30}];
        state.openAlbum = {unit_id: 20};
        await stepThroughShelf(1);
        """ + SAY)
    assert plain["cancelled"] == [] and plain["opened"] == [30]


def test_a_step_from_the_end_or_from_off_the_shelf_opens_nothing() -> None:
    """`shelf[-1 + 1]` is the first album, which is the wrong answer to a press.

    An album the shelf is not showing scores -1, and stepping forward from it
    would open whichever album happens to be first — a gesture about one record
    answering with another.
    """
    end = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}];
        state.openAlbum = {unit_id: 20};
        await stepThroughShelf(1);
        """ + SAY)
    assert end["opened"] == []

    outside = _run("""
        shelf = [{unit_id: 10}, {unit_id: 20}];
        state.openAlbum = {unit_id: 99};
        await stepThroughShelf(1);
        """ + SAY)
    assert outside["opened"] == []
