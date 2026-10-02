"""Which of the two buttons is the invitation, run rather than read.

One condition decides which button is filled, and it has three parts: the
filled button is `Scan this album again` while no catalogue has answered and
there is nothing to approve, it is never a button that is not on the row, and
it is `Approve & apply` once the tags have produced a plan. A screen that gets
any of them wrong invites the wrong gesture.

None of that is visible in the source: the lines are valid JavaScript whichever
button they light, and a guard that reads the text passes either way. So this
runs the real block over every combination of the three facts it reads and
looks at what it lit, the way `test_album_steps.py` does.

The block is extracted from `app.js` rather than copied, so a change to the
rule is a change to what this measures.

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
const marks = (holder) => ({
  toggle(name, on) {
    if (name !== "button-primary") return;
    holder.filled = Boolean(on);
  },
});

function decide(source, scanHidden, approveDisabled) {
  const album = { source };
  const drawn = {
    "album-scan-now": { hidden: scanHidden },
    "album-approve": { disabled: approveDisabled },
  };
  for (const id of Object.keys(drawn)) {
    drawn[id].filled = false;
    drawn[id].classList = marks(drawn[id]);
  }
  const $ = (id) => drawn[id];
__BLOCK__
  return { approve: drawn["album-approve"].filled, scan: drawn["album-scan-now"].filled };
}

const answers = [];
for (const source of [null, "tags", "discogs"]) {
  for (const scanHidden of [false, true]) {
    for (const approveDisabled of [false, true]) {
      answers.push({
        source,
        scanHidden,
        approveDisabled,
        ...decide(source, scanHidden, approveDisabled),
      });
    }
  }
}
console.log(JSON.stringify(answers));
"""


def _answers() -> list[dict]:
    """Run the real block over every combination and report what it lit."""
    source = SCRIPT.read_text(encoding="utf-8")
    block = re.search(
        r"  const noSourceAsked[\s\S]+?\$\(\"album-scan-now\"\)\.classList\.toggle\([^;]+;",
        source,
    )
    assert block, "no filled-button block to run, so this guard is reading the wrong file"
    program = HARNESS.replace("__BLOCK__", block.group(0))
    finished = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def _one(answers: list[dict], source: object, scan_hidden: bool, approve_disabled: bool) -> dict:
    for answer in answers:
        if (
            answer["source"] == source
            and answer["scanHidden"] is scan_hidden
            and answer["approveDisabled"] is approve_disabled
        ):
            return answer
    raise AssertionError("the matrix does not cover that state")


def test_two_filled_buttons_are_never_drawn_at_once() -> None:
    """Two filled buttons would be two invitations at once."""
    for answer in _answers():
        assert not (
            answer["approve"] and answer["scan"]
        ), f"both buttons are filled for {answer}, which asks for two things at once"


def test_the_amber_is_never_given_to_a_button_that_is_not_there() -> None:
    """An album whose scan is off the row still has to say what comes next."""
    for answer in _answers():
        if answer["scanHidden"]:
            assert not answer["scan"], f"the hidden scan is the filled button for {answer}"
        if not answer["approveDisabled"]:
            assert answer["approve"] or answer["scan"], (
                f"nothing at all is filled for {answer}, and the row says nothing "
                "about what comes next"
            )


def test_a_plan_from_the_tags_makes_approve_the_invitation() -> None:
    """Once a plan exists the question is whether to write it.

    Arranging an album by its own tags is the explicit answer to a catalogue
    that did not convince, so lighting the scan afterwards invites the gesture
    that was just declined. The invitation is `Approve & apply`.
    """
    answers = _answers()
    arranged = _one(answers, "tags", scan_hidden=False, approve_disabled=False)
    assert (
        arranged["approve"] and not arranged["scan"]
    ), "the album arranged by its own tags still invites the scan it just declined"
    never_looked_at = _one(answers, None, scan_hidden=False, approve_disabled=False)
    assert never_looked_at[
        "approve"
    ], "an album whose tags produced an approvable plan is the same case"


def test_the_scan_keeps_the_amber_while_there_is_nothing_to_approve() -> None:
    """No catalogue has answered, and there is no plan either.

    The scan is the more important gesture while it has not been made. That
    stops applying once the tags have answered the same question.
    """
    answers = _answers()
    for source in (None, "tags"):
        waiting = _one(answers, source, scan_hidden=False, approve_disabled=True)
        assert waiting["scan"] and not waiting["approve"], (
            f"the scan is not the invitation for an album with source {source!r} "
            "that has nothing to approve"
        )


def test_a_catalogue_that_answered_never_hands_the_amber_to_the_scan() -> None:
    """Where a catalogue answered, the look-up is done."""
    answers = _answers()
    for scan_hidden in (False, True):
        for approve_disabled in (False, True):
            answered = _one(answers, "discogs", scan_hidden, approve_disabled)
            assert not answered[
                "scan"
            ], f"the scan is filled for an album a catalogue answered: {answered}"
