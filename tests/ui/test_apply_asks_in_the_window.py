"""The two questions before an apply, asked and *awaited*, run rather than read.

They are not `confirm()`: the system draws that one, its buttons say OK, and
this webview is free not to show it at all — in which case `confirm()` answers
`false` and the apply silently does not happen. They go through `askThrough`,
like every other question the window asks.

Reading the source cannot tell whether that works. `askThrough` resolves off two
button listeners rather than off the dialog's `close` event, because this webview
does not fire one, and an `await` on a promise nothing settles is a
button that turns for ever. So this runs the real `approveAlbum`, the real
`askThrough` and the real question functions over stub elements, presses one of
the two buttons, and reports what reached the bridge.

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

# Elements that remember their listeners, so a test can press a button the way a
# person does — and a dialog that records having been shown, because a question
# that never opened is the failure this file guards against.
HARNESS = """
const shown = [];
const wrote = [];
const elements = {};
function $(id) {
  if (!(id in elements)) {
    const listeners = {};
    elements[id] = {
      id,
      open: false,
      textContent: "",
      disabled: false,
      addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
      removeEventListener(name, fn) {
        listeners[name] = (listeners[name] || []).filter((one) => one !== fn);
      },
      showModal() { this.open = true; shown.push(id); },
      close() { this.open = false; },
      press(name) { (listeners[name] || []).slice().forEach((fn) => fn()); },
    };
  }
  return elements[id];
}
const bridge = { approve: async () => { wrote.push("approve"); return { ok: true }; } };
const api = () => bridge;
let correctionInFlight = null;
const editedCorrections = () => false;
const sendCorrections = async () => true;
const withSpinner = async (button, run) => run();
const noop = () => {};
const toast = noop, toastError = noop;
const refresh = async () => {};
const state = { openAlbum: null };
const STR = {
  approvedToast: "", approvedPartToast: "",
  applyLeavesDoubtful: (count) => `${count} in doubt`,
};
const pause = (ms) => new Promise((done) => setTimeout(done, ms));
"""


def _run(body: str) -> dict:
    """Run the real apply gesture over stubs and report what it did."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in (
        "askThrough",
        "confirmApplyLeavesDoubtful",
        "confirmApplyWithoutScan",
        "approveAlbum",
    ):
        found = re.search(
            rf"^(?:async )?function {name}\(.*?\n\}}", source, flags=re.MULTILINE | re.DOTALL
        )
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))
    program = HARNESS + "\n".join(pieces) + ";\n(async () => {\n" + body + "\n})();"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


UNSCANNED = '{ unit_id: 1, applicable: true, evidence: [], source: "tags" }'
DOUBTFUL = (
    "{ unit_id: 1, applicable: false, partial_applicable: true, "
    'evidence: [{ suggested: true }, { suggested: true }], source: "discogs" }'
)


def test_an_unscanned_album_is_not_written_until_the_question_is_answered() -> None:
    """The apply waits on the window's own dialog, and the dialog is shown.

    Both halves matter. A question that is asked and not awaited writes while it
    is still on screen; a question that is awaited and never shown leaves the
    button turning with nothing to press.
    """
    said = _run(f"""
        state.openAlbum = {UNSCANNED};
        const going = approveAlbum();
        await pause(10);
        const before = wrote.slice();
        $("apply-unscanned-yes").press("click");
        await going;
        console.log(JSON.stringify({{ shown, before, wrote }}));
        """)

    assert said["shown"] == ["apply-unscanned-dialog"], "the question never opened"
    assert said["before"] == [], "the album was written while the question was on screen"
    assert said["wrote"] == ["approve"], "answering yes did not let the apply through"


def test_answering_no_writes_nothing_at_all() -> None:
    """The ✕ and Escape are that answer too — `askThrough` listens for both."""
    said = _run(f"""
        state.openAlbum = {UNSCANNED};
        const going = approveAlbum();
        await pause(10);
        $("apply-unscanned-no").press("click");
        await going;
        console.log(JSON.stringify({{ shown, wrote }}));
        """)

    assert said["shown"] == ["apply-unscanned-dialog"]
    assert said["wrote"] == [], "refusing the question still wrote to the files"


def test_escape_is_a_no_and_not_a_yes() -> None:
    """A dismissed question must never read as consent to write."""
    said = _run(f"""
        state.openAlbum = {UNSCANNED};
        const going = approveAlbum();
        await pause(10);
        $("apply-unscanned-dialog").press("cancel");
        await going;
        console.log(JSON.stringify({{ wrote }}));
        """)

    assert said["wrote"] == [], "dismissing the question was taken as a yes"


def test_the_other_question_names_how_many_files_are_being_left_alone() -> None:
    """The question about doubtful files says how many there are.

    The count is the whole of that question, and it is written into the dialog
    at the moment it is asked — a heading carrying a number would be true for
    one album and wrong for the next.
    """
    said = _run(f"""
        state.openAlbum = {DOUBTFUL};
        const going = approveAlbum();
        await pause(10);
        const message = $("apply-doubtful-message").textContent;
        $("apply-doubtful-yes").press("click");
        await going;
        console.log(JSON.stringify({{ shown, message, wrote }}));
        """)

    assert said["shown"] == ["apply-doubtful-dialog"]
    assert said["message"] == "2 in doubt", "the question stopped naming what it leaves alone"
    assert said["wrote"] == ["approve"]
