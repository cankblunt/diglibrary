"""Every rebuild of a plan is handed what the audio measured, or the mark comes off.

`[Lossy]` on a track is the one thing in a name that warns that the file is not
what its container says, and it is carried into `refine` as `measured_rates`. A
rebuild that does not pass it drops the mark from a proven transcode — for
instance when only the folder's name is corrected — and applying the plan writes
that name to disk.

A call that omits the argument looks complete: `transcoded=` is still there, and
the missing one reads as if it only chose between a number and a word. So the
rule is enforced rather than remembered: every call to `workflow.refine` in the
application layer passes `measured_rates`, and the AST is what says so.
"""

from __future__ import annotations

import ast
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "src/diglibrary/application/api.py"


def _refine_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `<something>.refine(...)` in this module, wherever it sits."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "refine"
    ]


def test_every_replan_is_told_what_the_audio_measured() -> None:
    """The guard itself, over the real file."""
    tree = ast.parse(API.read_text(encoding="utf-8"))
    calls = _refine_calls(tree)

    assert len(calls) >= 4, "this guard is looking at the wrong file, or refine has moved"
    missing = [
        call.lineno
        for call in calls
        if not any(word.arg == "measured_rates" for word in call.keywords)
    ]

    assert missing == [], (
        "a plan rebuilt without the measurement loses `[Lossy]` from the track's "
        f"name, and that name gets written to disk — api.py lines {missing}"
    )


def test_the_guard_can_tell_a_call_that_is_missing_it() -> None:
    """A guard that cannot fail is the same shape as a bug that cannot be seen.

    Both arguments here are keywords on the same call, so a walk that found
    `refine` but read its keywords wrongly would pass over the real file for
    ever and say nothing. This is that walk, run against a call it must refuse.
    """
    forged = ast.parse(
        "workflow.refine(unit, candidate, transcoded=marked, measured_rates=rates)\n"
        "workflow.refine(unit, candidate, transcoded=marked)\n"
    )
    calls = _refine_calls(forged)

    assert len(calls) == 2
    missing = [
        call.lineno
        for call in calls
        if not any(word.arg == "measured_rates" for word in call.keywords)
    ]
    assert missing == [2], "the second call is the one that omits the measurement"
