"""The offer to plan an organized album again after a verdict, run rather than read.

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
const asked = [];
const replanned = [];
const nodes = {};
const $ = (id) => (nodes[id] ||= { textContent: "" });
const STR = { renameWaits: (album) => `waits ${album}` };
let answers = [];
async function askThrough() {
  asked.push($("rename-waits-message").textContent);
  return answers.shift();
}
async function replanFromMenu(unitId) { replanned.push(unitId); }
"""


def _function(name: str) -> str:
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


def _run(answers: list[bool], waiting: object) -> dict:
    program = (
        HARNESS
        + _function("offerReplanAfterWord")
        + f"\nanswers = {json.dumps(answers)};"
        + f"\nawait offerReplanAfterWord({json.dumps(waiting)});"
        + "\nconsole.log(JSON.stringify({ asked, replanned }));"
    )
    done = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(done.stdout.strip())


def test_accepting_plans_that_album_again() -> None:
    said = _run([True], [{"unit_id": 7, "album": "Vimbrel"}])

    assert said == {"asked": ["waits Vimbrel"], "replanned": [7]}


def test_declining_plans_nothing() -> None:
    said = _run([False], [{"unit_id": 7, "album": "Vimbrel"}])

    assert said == {"asked": ["waits Vimbrel"], "replanned": []}


def test_nothing_waiting_asks_nothing() -> None:
    assert _run([], []) == {"asked": [], "replanned": []}
    assert _run([], None) == {"asked": [], "replanned": []}
