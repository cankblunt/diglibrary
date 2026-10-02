"""An album arranged from its own tags does not claim nobody ever looked at it.

An album can be scanned, identified with low confidence, left in review, and
then arranged by its own tags. `source === "tags"` is then a fact about the
proposal on screen, not about the album's history, and a surface that reads it
as *never scanned* says something false. The card's badge and the confirmation
that stands in front of the write both have to read whether a catalogue was
asked, as the dialog's notice, its header and the scan's own question do.

This runs the real `badgeFor`, `groupOf` and `approveAlbum` over both albums —
the one a catalogue answered for and the one nobody ever asked about — and reads
which of them gets told it was never scanned.

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
const asked = [];
const written = {};
const $ = (id) => ({
  set textContent(words) { written[id] = words; },
  get textContent() { return written[id] || ""; },
  hidden: false,
  disabled: false,
  classList: { toggle() {} },
});
// Every string answers with its own key, so what the dialog was made to say is
// read as the name of the sentence rather than as the sentence.
const STR = new Proxy({}, { get: (target, key) => String(key) });
const state = { identifying: new Set(), openAlbum: null };
let correctionInFlight = null;
function editedCorrections() { return false; }
async function sendCorrections() { return true; }
async function withSpinner(button, run) { return run(); }
function toastError() {}
async function confirmApplyLeavesDoubtful() { return true; }
// Answers no, so `approveAlbum` returns before anything is applied: what is
// being read here is which question was asked, not what the answer led to.
// `?? null` rather than the bare value: a heading nothing wrote is `undefined`,
// which `JSON.stringify` drops entirely — and a key that is not there fails as
// a `KeyError` instead of as the sentence this guard exists to say.
function askThrough() {
  asked.push({ heading: written["apply-unscanned-heading"] ?? null });
  return false;
}
const api = () => ({ approve: async () => ({ ok: true }) });
"""

CASES = """
// Scanned, identified, left in review — and then arranged by its own tags.
const arrangedAfterAScan = {
  unit_id: 41,
  rejected: false, organized: false, folder_missing: false,
  looked_at: true, held: false, source: "tags", decision: "review",
  applicable: true, catalogue_asked: true, evidence: [],
};
// The other album, which must go on being told the truth: dropped on the
// window, arranged on the way in, never looked up by anything.
const neverScanned = {
  ...arrangedAfterAScan, unit_id: 999, looked_at: false, catalogue_asked: false,
};

const answer = {};
for (const [name, album] of [["scanned", arrangedAfterAScan], ["never", neverScanned]]) {
  asked.length = 0;
  state.openAlbum = album;
  await approveAlbum();
  answer[name] = {
    badge: badgeFor(album)[1],
    group: groupOf(album),
    applyHeading: asked.length ? asked[0].heading : null,
  };
}
console.log(JSON.stringify(answer));
"""


def _run() -> dict:
    """Run the three real functions over both albums and report what they said."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in ("badgeFor", "groupOf"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    for name in ("approveAlbum", "confirmApplyWithoutScan"):
        found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    # A newline between the last function and the call: a `}` followed by `(` is
    # one expression to the parser, and the program then fails where it looks
    # most correct.
    program = HARNESS + "\n" + "\n".join(pieces) + "\n;(async () => {" + CASES + "})();\n"
    finished = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=False)
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def test_the_card_of_a_scanned_album_does_not_say_it_was_never_scanned() -> None:
    """An album a catalogue answered for is not badged `not scanned`."""
    answer = _run()
    assert answer["scanned"]["badge"] != "badgeUnlooked", (
        "an album a catalogue answered for wears `not scanned` because it was "
        "arranged from its own tags"
    )
    # And the shelf's two readings of one album agree, which is what made this
    # visible: the card sat in the `review` group wearing the badge of the
    # `not scanned` one, so the filter that names the badge did not contain it.
    assert answer["scanned"]["badge"] == "badgeReview"
    assert answer["scanned"]["group"] == "review"


def test_the_album_nobody_asked_about_is_still_told_so() -> None:
    """The album no catalogue was asked about is the reason the badge exists."""
    answer = _run()
    assert answer["never"]["badge"] == "badgeUnlooked"
    assert answer["never"]["group"] == "unlooked"


def test_the_question_before_the_write_names_the_right_album() -> None:
    """The confirmation is asked either way; only its heading may differ."""
    answer = _run()
    assert answer["never"]["applyHeading"] == "applyWithoutScanHeading"
    assert answer["scanned"]["applyHeading"] == "applyInsteadOfScanHeading", (
        "the write's confirmation says this album has not been scanned, " "over one that was"
    )


GONE_CASES = """
// An organized album whose folder was deleted outside the application: the row
// is kept and marked as missing, and the card has to say so.
const organizedAndGone = {
  unit_id: 41, rejected: false, organized: true, folder_missing: true,
  looked_at: true, held: false, source: "tags", decision: "review",
  applicable: false, catalogue_asked: true, evidence: [],
};
const rejectedAndGone = { ...organizedAndGone, rejected: true, organized: false };
const organizedAndThere = { ...organizedAndGone, folder_missing: false };
const answer = {};
for (const [name, album] of [["organized", organizedAndGone], ["rejected", rejectedAndGone],
                             ["there", organizedAndThere]]) {
  answer[name] = { badge: badgeFor(album)[1], group: groupOf(album) };
}
console.log(JSON.stringify(answer));
"""


def _run_gone() -> dict:
    """Run the two shelf functions over an album whose folder is not there."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in ("badgeFor", "groupOf"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    program = HARNESS + "\n" + "\n".join(pieces) + "\n;" + GONE_CASES
    finished = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=False)
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def test_a_folder_that_is_not_there_outranks_every_other_word_about_the_album() -> None:
    """The missing folder is tested before `rejected` and `organized`.

    Tested after them, a finished album whose folder was then deleted draws
    exactly like a whole one, and the only signs are a cover that does not load
    and a card that opens onto a repair screen.

    Both facts are true of that album. Only one of them can be acted on, and
    nothing else about the album can be acted on until it is.
    """
    answer = _run_gone()
    assert (
        answer["organized"]["badge"] == "badgeFolderGone"
    ), "a finished album whose folder is gone still says it is finished"
    assert answer["rejected"]["badge"] == "badgeFolderGone"
    # And the album that is where it says it is keeps every word it had.
    assert answer["there"]["badge"] == "badgeOrganized"


def test_the_group_and_the_badge_answer_the_same_question() -> None:
    """A card wearing `not where it was` inside the `organized` filter is the
    shelf disagreeing with itself. The two functions move together or the
    filter loses the album.
    """
    answer = _run_gone()
    assert answer["organized"]["group"] == "review"
    assert answer["rejected"]["group"] == "review"
    assert answer["there"]["group"] == "organized"
