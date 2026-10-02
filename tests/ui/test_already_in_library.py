"""The notice that something is already in the library, run rather than read.

A toast at the foot of the window is easy to miss, so this notice is a dialog
in the middle that waits for OK. It is called from the loop that drains every
event, so it must not wait there itself, and a second sentence arriving while
it is open must be added, not lost.

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
const made = [];
function element() {
  return {
    open: false, className: "", textContent: "", children: [],
    showModal() { this.open = true; made.push("modal"); },
    close() { this.open = false; },
    append(child) { this.children.push(child); },
    replaceChildren(...kids) { this.children = kids; },
  };
}
const nodes = {};
const $ = (id) => (nodes[id] ||= element());
const document = { createElement: () => element() };
const STR = {
  adoptedKnown: (names) => `known ${names.join(",")}`,
  arrivedSameAudio: (found) => `same ${found.map((entry) => entry.album).join(",")}`,
};
"""


def _function(name: str) -> str:
    """One whole function out of `app.js`, matched by its braces."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    depth = 0
    kept: list[str] = []
    for index, line in enumerate(source[start:].split("\n")):
        kept.append(line)
        depth += line.count("{") - line.count("}")
        if index and depth == 0:
            return "\n".join(kept)
    raise AssertionError(f"{name} never closes; this guard reads the wrong file")


def _run(body: str) -> dict:
    program = (
        HARNESS
        + "\n".join(
            _function(name)
            for name in ("sayAlreadyInLibrary", "sayArrivedSameAudio", "sayKnownAgain")
        )
        + "\n"
        + body
        + "\nconsole.log(JSON.stringify({ made, open: $('same-audio-dialog').open,"
        + " lines: $('same-audio-message').children.map((line) => line.textContent) }));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_it_opens_in_the_middle_and_waits() -> None:
    said = _run('sayKnownAgain(["Vimbrel"]);')

    assert said["made"] == ["modal"] and said["open"] is True
    assert said["lines"] == ["known Vimbrel"]


def test_a_second_sentence_while_it_is_open_is_added_not_lost() -> None:
    said = _run('sayKnownAgain(["Vimbrel"]); sayArrivedSameAudio([{ album: "Quáltora" }]);')

    assert said["made"] == ["modal"], "it was opened twice"
    assert said["lines"] == ["known Vimbrel", "same Quáltora"]


def test_nothing_to_say_opens_nothing() -> None:
    said = _run("sayKnownAgain([]); sayArrivedSameAudio(undefined);")

    assert said["made"] == [] and said["open"] is False
