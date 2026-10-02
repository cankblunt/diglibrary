"""Arriving at the Library after moving an album, run rather than read.

A card wears `not where it was` when its folder does not answer at the moment
the shelf is drawn. When the album was only moved, `library_present` follows it
and corrects the row, and the card then has to be drawn again from the
corrected row.

The line between the two is what this guards. A function that redraws the shelf
only when the answer names something *missing* reads a sweep that found and
fixed everything as nothing to do: the albums are followed in the database and
the screen goes on showing where they used to be, until some other gesture
happens to refresh.

Run in node over stubs because that defect is not visible in the source: every
line of it is valid JavaScript that does exactly what it says.
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
const done = { markCounts: 0, asked: 0, said: null };
let answer = { ok: true, missing: [], followed: [], checked: 0 };
let throws = false;
const state = {
  scanning: false,
  restoring: false,
  adopting: false,
  marked: new Set(),
  identifying: new Set(),
};
const api = () => ({
  library_present: async () => {
    done.asked += 1;
    if (throws) throw new Error("the bridge said no");
    return answer;
  },
});
function renderMarkCount() { done.markCounts += 1; }
"""


def _run(body: str) -> dict:
    """Run the real `checkFoldersStillThere` in node and report what it did."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"async function checkFoldersStillThere\(\) \{[\s\S]+?\n\}", source)
    assert found, "no checkFoldersStillThere() to run, so this guard reads the wrong file"
    program = (
        HARNESS
        + found.group(0)
        + ";\n(async () => {\n"
        + body
        + "\nconsole.log(JSON.stringify({...done, marked: [...state.marked]}));\n})();"
    )
    ran = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(ran.stdout.strip())


def test_an_album_that_was_followed_leaves_the_marks_alone() -> None:
    """The row moved and the card did not; the marks are not part of that.

    `followed` is what tells the window a card it has drawn is now wrong. The
    redraw itself belongs to `refresh`, which is the call that draws the chip;
    what this must not do is take an album off the marks for having been found,
    which is the opposite of what a mark means.
    """
    did = _run(
        "state.marked = new Set([7]);"
        "\nanswer = {ok: true, missing: [], followed: [7], checked: 40};"
        "\nawait checkFoldersStillThere();"
    )

    assert did["asked"] == 1
    assert did["marked"] == [7], "it was found, so nothing about the mark changed"
    assert did["markCounts"] == 0


def test_a_library_where_nothing_moved_is_asked_and_nothing_else() -> None:
    """Every redraw asks this, so a quiet arrival must cost one round trip
    and no work at all."""
    did = _run("await checkFoldersStillThere();")

    assert did["asked"] == 1
    assert did["markCounts"] == 0


def test_an_album_that_could_not_be_found_loses_its_marks() -> None:
    """A marked album that is not there cannot be scanned, so the mark goes and
    the count says so."""
    did = _run(
        "state.marked = new Set([7, 8]);"
        "\nstate.identifying = new Set([7]);"
        "\nanswer = {ok: true, missing: [7], followed: [], checked: 40};"
        "\nawait checkFoldersStillThere();"
    )

    assert did["marked"] == [8]
    assert did["markCounts"] == 1


def test_found_and_lost_in_one_answer_are_told_apart() -> None:
    """A sweep that follows two albums and fails to find a third: only the third
    loses its mark, and the other two keep everything they had."""
    did = _run(
        "state.marked = new Set([7, 9]);"
        "\nanswer = {ok: true, missing: [9], followed: [7, 8], checked: 40};"
        "\nawait checkFoldersStillThere();"
    )

    assert did["marked"] == [7]
    assert did["markCounts"] == 1


def test_it_says_whether_a_card_on_screen_is_now_wrong() -> None:
    """The answer the two callers need, and they need different things from it.

    `refresh` is about to draw anyway and ignores it. Arriving at a tab draws
    nothing on its own — the shelf comes back exactly as it was left — so that
    one redraws only when this says a card has gone stale. Both early returns
    answer in the same word as the ordinary one: an early return is a second
    control flow, and it owes the caller the same answer.
    """
    quiet = _run("done.said = await checkFoldersStillThere();")
    assert quiet["said"] is False, "nothing moved, so nothing on screen is wrong"

    moved = _run(
        "answer = {ok: true, missing: [], followed: [7], checked: 40};"
        "\ndone.said = await checkFoldersStillThere();"
    )
    assert moved["said"] is True, "a followed album is a card drawn from a stale row"

    lost = _run(
        "answer = {ok: true, missing: [7], followed: [], checked: 40};"
        "\ndone.said = await checkFoldersStillThere();"
    )
    assert lost["said"] is True

    busy = _run("state.scanning = true;\ndone.said = await checkFoldersStillThere();")
    assert busy["said"] is False, "a run answers in the same word, not in silence"

    refused = _run("throws = true;\ndone.said = await checkFoldersStillThere();")
    assert refused["said"] is False


def test_a_run_in_progress_is_not_asked() -> None:
    """A scan reads folders it is about to record, so the two cannot be raced."""
    did = _run("state.scanning = true;\nawait checkFoldersStillThere();")

    assert did["asked"] == 0 and did["markCounts"] == 0


def test_a_bridge_that_refuses_leaves_the_shelf_alone() -> None:
    """Already named by the bridge proxy; a second sentence about it helps nobody."""
    did = _run("throws = true;\nawait checkFoldersStillThere();")

    assert did["asked"] == 1 and did["markCounts"] == 0


REFRESH_HARNESS = """
const done = { asked: 0, drew: 0, order: [] };
let answer = { ok: true, missing: [], followed: [], checked: 0 };
const state = {
  scanning: false, restoring: false, adopting: false,
  marked: new Set(), identifying: new Set(),
};
const api = () => ({
  library_present: async () => { done.asked += 1; done.order.push("asked"); return answer; },
  state: async () => { done.drew += 1; done.order.push("drew"); return {albums: [], history: []}; },
});
function renderMarkCount() {}
"""


def _refresh(body: str) -> dict:
    """Run the head of the real `refresh` far enough to see what it asks, and when.

    Only the opening of it: everything past the dashboard read is about panels
    this question has nothing to do with, and stubbing all of it would be a
    harness that tests the stubs.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(
        r"async function refresh\(\) \{[\s\S]+?\n  const snapshot = await api\(\)\.state\(\);",
        source,
    )
    assert found, "refresh() no longer opens the way this guard reads it"
    check = re.search(r"async function checkFoldersStillThere\([\s\S]*?\) \{[\s\S]+?\n\}", source)
    assert check, "no checkFoldersStillThere() to run"
    opening = found.group(0) + "\n}"
    program = (
        REFRESH_HARNESS
        + check.group(0)
        + "\nfunction collectDownloads() {}\n"
        + opening
        + ";\n(async () => {\n"
        + body
        + "\nconsole.log(JSON.stringify(done));\n})();"
    )
    ran = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(ran.stdout.strip())


def test_every_redraw_asks_whether_the_albums_are_still_there() -> None:
    """The call that draws the chip is the call that asks.

    `refresh()` is what draws `not where it was` on a card, and it is called
    from many places. If the check that clears the chip ran only on *arriving*
    at the Library, a window left standing on the Library would never arrive
    again: a moved album would keep the chip until the tab was changed and
    changed back.

    Measured on a real library, the sweep's own work is a small fraction of the
    redraw it precedes.
    """
    did = _refresh("await refresh();")

    assert did["asked"] == 1, "the redraw drew the chip without ever asking"
    assert did["drew"] == 1


def test_the_question_is_asked_before_the_shelf_is_read_not_after() -> None:
    """Otherwise the shelf drawn by this refresh is the stale one, and it takes
    a second redraw to say what was already known."""
    did = _refresh("await refresh();")

    assert did["order"] == ["asked", "drew"]


def test_a_run_in_progress_still_costs_a_redraw_nothing() -> None:
    """A scan redraws constantly for its progress, and the check stands aside
    for one — so during a run this must not even reach the bridge."""
    did = _refresh("state.scanning = true;\nawait refresh();")

    assert did["asked"] == 0 and did["drew"] == 1
