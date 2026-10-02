"""The window's script is a program, and this asks whether it parses as one.

A `const` declared twice in one scope is a **SyntaxError**: not a wrong answer
on one screen but a script that never runs, so the window comes up as a red band
over an empty Library. The other guards in this directory cannot see it, because
each of them cuts one function out with a regular expression and runs *that*;
none reads the file as a whole, and `ruff` and `black` have no opinion about
JavaScript.

**Asked as a module**, which is how `index.html` loads it: a `const` collision in
the top-level scope of a script is tolerated by the sloppy-mode parser that
`node --check` uses on a `.js` file, and the same file as a module is refused.
The window is a module, so the module's answer is the one that matters.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _parsed(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=source,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("name", ["app.js", "strings.js", "api.js", "finish.js"])
def test_every_script_this_window_loads_is_a_program(name: str) -> None:
    """Instant, offline, and it answers the one question no other guard asks."""
    script = WEB / name
    if not script.is_file():
        pytest.skip(f"{name} is not part of this window")

    answer = _parsed(script.read_text(encoding="utf-8"))

    assert (
        "SyntaxError" not in answer.stderr
    ), f"{name} does not parse, so this window draws nothing at all:\n{answer.stderr}"


def test_this_guard_refuses_the_defect_it_was_written_for() -> None:
    """A guard that cannot fail proves nothing, so this one is shown the defect.

    The collision itself: a second `const` of a name the same scope already
    holds.
    """
    twice = "function draw() {\n  const said = 1;\n  const said = 2;\n  return said;\n}\n"

    assert "SyntaxError" in _parsed(twice).stderr
