"""Every row written into `track_quality` names the ruler that took it.

A `StoredQuality` is what the next launch reads, and two of its fields are not
numbers about the audio but facts about *how it was measured*: `wall_low_hertz`,
the rung the wall was found from, and `floor_hertz`, where the audio stops
changing. Absent, the stored rule falls back to fall-and-emptiness, so a row
missing them is judged by the rule its own measurement was taken to replace: a
verdict that is right on screen reads back differently once it is written down.

Both fields default to `None`, so a writer that omits them looks complete. The
rule is therefore stated as a comparison between sibling calls rather than as a
list that has to be extended by hand whenever a writer is added.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src/diglibrary/application"

# Both facts are about the ruler rather than the audio, and both default to
# `None` on the dataclass — so omitting one is silent at every layer until a
# verdict flips on the next launch.
ABOUT_THE_RULER = {"wall_low_hertz", "floor_hertz"}


def _stored_quality_calls() -> list[tuple[Path, ast.Call]]:
    """Every `StoredQuality(...)` built in the application layer."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "StoredQuality"
            ):
                found.append((path, node))
    return found


def test_every_measurement_written_down_names_the_ruler_that_took_it() -> None:
    calls = _stored_quality_calls()
    assert calls, "no StoredQuality is built here any more — this guard has moved"
    missing = {
        f"{path.name}:{call.lineno}": sorted(
            ABOUT_THE_RULER - {keyword.arg for keyword in call.keywords if keyword.arg}
        )
        for path, call in calls
        if not {keyword.arg for keyword in call.keywords if keyword.arg} >= ABOUT_THE_RULER
    }
    assert not missing, (
        "a measurement is written down without saying which ruler took it, so the "
        f"stored rule will judge it by fall-and-emptiness: {missing}"
    )


def test_the_siblings_are_told_the_same_things() -> None:
    """No writer knows a fact about the audio that its siblings are not told.

    Gathers the keywords of every sibling call and shows whoever is short.
    Stated over the whole argument list rather than over two names, so a field
    added later is covered without being enumerated here.
    """
    calls = _stored_quality_calls()
    named = {
        f"{path.name}:{call.lineno}": {keyword.arg for keyword in call.keywords if keyword.arg}
        for path, call in calls
    }
    everything = set().union(*named.values())
    # `is_this_file` is read back from the database and never written, so a
    # writer that does not set it is not short of anything.
    everything -= {"is_this_file"}
    short = {where: sorted(everything - args) for where, args in named.items() if everything - args}
    assert not short, (
        "these writers of a measurement are not told what their siblings are told, "
        f"and a field absent here is a field that is NULL for good: {short}"
    )
