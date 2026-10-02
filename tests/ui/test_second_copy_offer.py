"""A relocation refused because the folder is another album's, run rather than read.

When the folder a card is pointed at already
belongs to an album with the same audio, the card is a second copy, and the
window offers to let it go. Both ways to relocate — the card's menu and the
album's dialog — must make that offer, and neither may let the card go without
the user's yes. This runs the real functions over stubs and reads what they did.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

HARNESS = """
const nodes = {};
const $ = (id) => (nodes[id] ||= {
  hidden: false, textContent: "", open: false, close() { this.open = false; },
});
const STR = { relocated: (folder) => `moved to ${folder}` };
const state = { openAlbum: { unit_id: 7 } };
const did = [];
let answer = true;
let reply = {};
const api = () => ({
  choose_folder: async () => "/music/kept",
  relocate_album: async (unitId, folder) => {
    did.push(["relocate", unitId, folder]);
    return reply;
  },
});
async function withSpinner(button, run) { return run(); }
async function askThrough(dialog, yes, no) {
  did.push(["asked", $("second-copy-message").textContent]);
  return answer;
}
async function forgetAlbum(unitId) { did.push(["forgot", unitId]); }
function toast(message) { did.push(["toast", message]); }
function toastError(message) { did.push(["error", message]); }
function renderAlbumDialog() {}
async function refresh() {}
"""


def _function(name: str) -> str:
    """One whole function out of `app.js`, matched by its braces."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index(f"async function {name}(")
    depth = 0
    kept: list[str] = []
    for index, line in enumerate(source[start:].split("\n")):
        kept.append(line)
        depth += line.count("{") - line.count("}")
        if index and depth == 0:
            return "\n".join(kept)
    raise AssertionError(f"{name} never closes; this guard reads the wrong file")


def _run(body: str) -> list:
    program = (
        HARNESS
        + "\n".join(
            _function(name)
            for name in ("offerToLetSecondCopyGo", "relocateFromMenu", "relocateAlbum")
        )
        + ";\n(async () => {\n"
        + body
        + "\nconsole.log(JSON.stringify(did));\n})();"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


SECOND_COPY = """
reply = {
  ok: false,
  error: "“kept” is already on your shelf as another album with the same audio.",
  second_copy: { unit_id: 7, folder: "kept", twin_id: 3 },
};
"""


@pytest.mark.parametrize("gesture", ["relocateFromMenu(7)", "relocateAlbum()"])
def test_both_ways_to_relocate_offer_to_let_a_second_copy_go(gesture: str) -> None:
    did = _run(SECOND_COPY + f"answer = true; await {gesture};")

    asked = [entry for entry in did if entry[0] == "asked"]
    assert asked and "same audio" in asked[0][1], "the refusal was not put to the user"
    assert ["forgot", 7] in did, "a yes did not let the card go"
    assert not [entry for entry in did if entry[0] == "error"], "and it was also shown as a failure"


@pytest.mark.parametrize("gesture", ["relocateFromMenu(7)", "relocateAlbum()"])
def test_a_second_copy_is_never_let_go_without_a_yes(gesture: str) -> None:
    did = _run(SECOND_COPY + f"answer = false; await {gesture};")

    assert not [entry for entry in did if entry[0] == "forgot"], "the card went without a yes"


def test_any_other_refusal_is_still_said_as_one() -> None:
    did = _run(
        'reply = { ok: false, error: "holds a different album" }; await relocateFromMenu(7);'
    )

    assert ["error", "holds a different album"] in did
    assert not [entry for entry in did if entry[0] in ("asked", "forgot")]
