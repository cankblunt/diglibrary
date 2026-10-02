"""The control that gives an unlisted file a place, run rather than read.

A control built from a handful of `createElement` calls is exactly the shape no
static guard over `ui/web/` can judge: every line is valid whether the number
arrives filled or empty, whether the click carries the position that was picked
or the one that was offered, and whether the button is wired at all. The source
reads the same either way, and the fault would only show when it is pressed.

So this runs it, over a DOM small enough to be honest about — the same approach
`test_shelf_order.py` and `test_transfer_estimate.py` take to a comparator and to
arithmetic.

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

# A DOM with only what the control touches, and a `STR` with only the words it
# reads. Anything the function reaches for that is not here fails loudly, which
# is the point: a stub that answers everything proves nothing.
HARNESS = """
const made = [];
class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.listeners = {};
    made.push(this);
  }
  append(...kids) { this.children.push(...kids); }
  addEventListener(name, run) { this.listeners[name] = run; }
}
const document = { createElement: (tag) => new Node(tag) };
const STR = {
  planAdoptLabel: "Make this track",
  planAdopt: "Add",
  planAdoptTip: "Give this file a place on the tracklist.",
  planAdoptMoveDo: "Move",
  planAdoptMoveTip: "Put this track at another position.",
  planAdoptNumberTip: "The place this file takes on the tracklist.",
};
// Named rather than positional: the row once grew extra buttons and every test
// that reached for "the button" silently found the first of them.
const press = (text) => made.find((one) => one.tag === "button" && one.textContent === text)
  .listeners.click();
const field = () => made.find((one) => one.tag === "select");
const offered = () => field().children.map((one) => one.value);
const calls = [];
function adoptTrack(signature, position, from) { calls.push([signature, position, from]); }
"""


def _run(body: str) -> dict:
    """Run `adoptControl` in node and report what it built and what it called."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"function adoptControl\([^)]*\) \{[\s\S]+?\n\}", source)
    assert found, "no adoptControl() to run, so this guard is looking at the wrong file"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS + found.group(0) + body],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_the_offered_number_arrives_chosen_and_the_click_carries_it() -> None:
    """Ten tracks listed, and `11` arrives already chosen.

    The case that renumbers nothing is the one that must cost no gesture, so the
    fold arrives holding the position past the finished list's last and pressing
    Add without touching it adopts there.
    """
    said = _run("""
        adoptControl({ signature: "sig-bonus", position: 11, last: 11 });
        press("Add");
        console.log(JSON.stringify({
          value: field().value,
          offered,
          label: made.find((one) => one.tag === "label").textContent,
          calls,
        }));
        """.replace("offered,", "offered: offered(),"))

    assert said["value"] == "11", "the offered position arrives chosen"
    assert said["offered"][0] == "1", "there is no track zero"
    assert said["offered"][-1] == "11", "and nothing past the last place"
    assert said["calls"] == [["sig-bonus", 11, 11]], "the click carries the file and the number"


def test_a_number_that_is_picked_is_the_one_that_travels() -> None:
    """A file that is not the last reaches the API at the position picked.

    The fold holds a string and the position is a number all the way down; a
    control that handed `"5"` on would have looked identical on screen and
    compared as a different thing everywhere after.
    """
    said = _run("""
        adoptControl({ signature: "sig-middle", position: 11, last: 11 });
        field().value = "5";
        press("Add");
        console.log(JSON.stringify({ calls }));
        """)

    assert said["calls"] == [["sig-middle", 5, 11]], "picked, and travelling as a number"


def test_a_file_with_no_offer_still_gets_a_usable_fold() -> None:
    """A release nothing could be read from still offers position one, not zero."""
    said = _run("""
        adoptControl({ signature: "sig", position: 0, last: 1 });
        console.log(JSON.stringify({ value: field().value, offered: offered() }));
        """)

    assert said["value"] == "1"
    assert said["offered"] == ["1"]


def test_the_moving_control_has_no_loose_word_beside_its_button() -> None:
    """The moving control carries no label of its own.

    A word outside the button that acts on it reads as loose text. The
    row already says this is a track, so the moving control is the number and
    the verb and nothing else — while the unclaimed file keeps its label, which
    is what says the gesture exists at all.
    """
    said = _run("""
        adoptControl({ signature: "sig", position: 4, last: 12 }, { moving: true });
        const moving = made.map((one) => one.tag);
        made.length = 0;
        adoptControl({ signature: "sig", position: 4, last: 12 });
        console.log(JSON.stringify({ moving, adopting: made.map((one) => one.tag) }));
        """)

    assert "label" not in said["moving"], "nothing loose beside Move"
    assert "label" in said["adopting"], "the unclaimed file still says what the gesture is"


def test_pressing_move_without_changing_the_number_is_reported_as_a_stay() -> None:
    """A gesture whose whole effect can be *no visible change* has to speak.

    Re-adopting at the same position is correct and redraws an identical screen,
    which is indistinguishable from a button that is not wired. The call carries
    where it came from so the answer can say which of the two happened.
    """
    said = _run("""
        adoptControl({ signature: "sig", position: 9, last: 12 }, { moving: true });
        press("Move");
        console.log(JSON.stringify({ calls }));
        """)

    signature, to, came_from = said["calls"][0]
    assert to == came_from == 9, "the answer can tell a stay from a move"
    assert signature == "sig"


def test_the_fold_never_offers_a_place_the_album_cannot_hold() -> None:
    """Two unlisted files must not evict each other, seen from the control's side.

    If two unlisted files over a 15-track release are both offered 16 at most,
    choosing one throws the other out. `last` therefore arrives past the
    FINISHED list, with the tracks already adopted counted, and the fold simply
    lists the legal numbers: a place the album cannot honour is not an input to
    clamp, it is not on the menu. A moving track already holds one of those
    places, so its menu is one place shorter.
    """
    said = _run("""
        adoptControl({ signature: "sig-b", position: 17, last: 17 });
        const adding = offered();
        made.length = 0;
        adoptControl({ signature: "sig-a", position: 16, last: 17 }, { moving: true });
        const moving = offered();
        console.log(JSON.stringify({ adding, moving }));
        """)

    assert said["adding"][-1] == "17", "the second file reaches the place after the first"
    assert len(said["adding"]) == 17
    assert said["moving"][-1] == "16", "a mover's own place is not counted twice"
    assert len(said["moving"]) == 16
