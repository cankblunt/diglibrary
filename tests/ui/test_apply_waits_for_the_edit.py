"""`Approve & apply` waits for whatever is changing the plan, run rather than read.

This is the one button in the window that writes to somebody's disk, and it
waits for an edit still in the air — a click landing before the answer applies
the plan that edit was about to change. Every gesture that changes the plan has
to announce itself for that to hold, not only the typed edit: keeping a typed
spelling, withdrawing it, withdrawing an album's titles, confirming one pairing,
confirming them all, adopting a track and dropping one.

The failure is a race and every line of it is valid, so no reading of `app.js`
finds it and no static guard can: `whileCorrecting` is a variable being set, and
whether it is set *before* the await that matters is a question only running the
thing answers. Here the bridge is made slow on purpose and the two presses land
in the order that loses when the apply does not wait.

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

# A bridge that answers on a later turn, which is the whole point: a fake that
# answers instantly never races its own state change, and this file is about
# a race. `order` is the assertion — what reached the API, in the
# order the application sent it.
HARNESS = """
const order = [];
// **What is recorded is when each call ANSWERS, not when it is made.** A call
// entering the bridge proves nothing about waiting: an unheld approve is sent
// while the withdrawal is still in the air, and both would be "in order". The
// question is whether the write landed after the change it was about.
//
// Different speeds on purpose too. Equal ones hide the case that matters: a
// quick gesture finishing while a slow one begun after it is still in the air.
const answer = (name, value, ms) =>
  new Promise((done) => setTimeout(() => { order.push(name); done(value); }, ms));
const bridge = {
  correct: () => answer("correct", { ok: true, album: {} }, 5),
  withdraw_spelling: () => answer("withdraw", { ok: true, album: {} }, 80),
  approve: () => answer("approve", { ok: true }, 5),
};
const pause = (ms) => new Promise((done) => setTimeout(done, ms));
const api = () => bridge;
const state = { openAlbum: { unit_id: 1, applicable: true, evidence: [], source: "discogs" } };
const STR = {
  planYoursKept: "", planYoursDropped: "", approvedToast: "", approvedPartToast: "",
  applyLeavesDoubtful: () => "", applyWithoutScan: "",
};
const noop = () => {};
const toast = noop, toastError = noop, refresh = async () => {};
// The redraw a gesture ends with, and the place a second press lands: this
// window reopens the dialog after every correction, so the moment one gesture
// is finishing is a moment the buttons are back under the cursor. `duringRedraw`
// is that press, fired once.
let duringRedraw = null;
const openAlbum = async () => {
  const next = duringRedraw;
  duringRedraw = null;
  if (next) next();
};
const renderAlbumDialog = noop;
const editedCorrections = () => false;
const sendCorrections = async () => true;
const confirm = () => true;
const findStatus = noop;
const $ = () => ({ disabled: false, classList: { add: noop, remove: noop }, close: noop });
async function withSpinner(button, run) { return run(); }
"""


def _run(body: str) -> dict:
    """Run the real gestures and the real approve, and report what reached the API."""
    source = SCRIPT.read_text(encoding="utf-8")
    wanted = ("whileCorrecting", "keepYourSpelling", "dropYourSpelling", "approveAlbum")
    pieces = []
    found = re.search(r"^let correctionInFlight = null;$", source, flags=re.MULTILINE)
    assert found, "the slot this guard is about is not where it expects it"
    pieces.append(found.group(0))
    for name in wanted:
        found = re.search(
            rf"^(?:async )?function {name}\(.*?\n\}}", source, flags=re.MULTILINE | re.DOTALL
        )
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS + "\n".join(pieces) + body],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_applying_straight_after_withdrawing_a_spelling_waits_for_it() -> None:
    """A withdrawal still in the air, on the button that renames files.

    `Use the catalogue's` is pressed and then Approve, without waiting. Unheld,
    the approve reaches the application first and it applies the plan that still
    carries the spelling just withdrawn: the files on disk get the word that was
    taken back, and the screen said the opposite.
    """
    said = _run("""
        dropYourSpelling(4, $());
        await approveAlbum();
        console.log(JSON.stringify({ order }));
        """)

    assert said["order"] == ["withdraw", "approve"], "the withdrawal lands before the apply"


def test_applying_straight_after_keeping_a_spelling_waits_for_it() -> None:
    """The same door in the other direction."""
    said = _run("""
        keepYourSpelling(4, "Quenta Vorim", $());
        await approveAlbum();
        console.log(JSON.stringify({ order }));
        """)

    assert said["order"] == ["correct", "approve"]


def test_a_gesture_that_has_finished_does_not_hand_back_a_slot_it_no_longer_holds() -> None:
    """The same race one layer in, and it needs two different speeds to see.

    A quick gesture starts, a slow one starts after it and takes the slot, and
    then the quick one finishes. Clearing the slot unconditionally there hands
    back a place that belongs to the gesture still in the air, and the next
    Approve goes straight past it. With equal speeds nothing is visible at all,
    so a test with equal speeds passes against the broken code.
    """
    said = _run("""
        keepYourSpelling(4, "Quenta Vorim", $());
        dropYourSpelling(5, $());
        await pause(30);
        await approveAlbum();
        console.log(JSON.stringify({ order }));
        """)

    assert said["order"] == [
        "correct",
        "withdraw",
        "approve",
    ], "the slow withdrawal still finished before the apply"


def test_a_press_landing_while_a_gesture_redraws_is_waited_for_too() -> None:
    """Waiting is itself a window, which is why the wait is a loop and not one await.

    Every gesture here ends by reopening the dialog, so the instant one finishes
    is an instant its buttons are back under the cursor. A press that lands
    there takes the slot *while Approve is already waiting on the previous one* —
    and a single `await` returns satisfied, having waited for something that is
    no longer what is in the air.
    """
    said = _run("""
        duringRedraw = () => dropYourSpelling(5, $());
        keepYourSpelling(4, "Quenta Vorim", $());
        await approveAlbum();
        console.log(JSON.stringify({ order }));
        """)

    assert said["order"] == [
        "correct",
        "withdraw",
        "approve",
    ], "the apply waited for the slow gesture that started while it was waiting"


def test_a_second_press_while_the_first_is_waiting_applies_nothing() -> None:
    """One press is one apply, however long the first one waits.

    A press waits for the re-plan, and while it waits the button is still live,
    so a second press would send a second apply right behind the first. The
    second would rehearse a plan the first had just carried out and report every
    file as gone. Once the first has its answer the button is a button again —
    the third press here is sent, because a guard that never lets go is a button
    that stops working.
    """
    said = _run("""
        dropYourSpelling(4, $());
        const first = approveAlbum();
        const second = approveAlbum();
        await Promise.all([first, second]);
        await approveAlbum();
        console.log(JSON.stringify({ order }));
        """)

    assert said["order"] == ["withdraw", "approve", "approve"], "two presses in the air, one apply"
