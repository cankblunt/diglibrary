"""The Metadata Engine reaches a client in one place, so every answer is counted.

Whether a source answered decides a sentence on screen — that the source was not
consulted in a scan — so the count has to cover every way an answer arrives. A
count kept beside the search loop misses the others: when a search times out and
the acoustic path then fetches releases from the same source, the run would be
reported as decided without the source that decided it.

Counting inside `_ask` covers the paths that exist. This guard covers the next
one: a method that calls a client directly would answer correctly, pass every
test about what it returns, and stop the count. It is written as *no call on a
local*, not as a list of the methods known today.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[2] / "src/diglibrary/metadata/service.py"

# The one method allowed to call a client. Everything else hands the call to it.
THE_DOOR = "_ask"


def _locals_of(function: ast.FunctionDef) -> set[str]:
    """Names this function binds itself: assignments, `for` targets, `with … as`.

    A client only ever reaches a method's body through one of these — the loop
    over `self._sources`, or a `getattr` for an optional capability — so a call
    on one of them is a call on a source, and a call on anything else (a
    builtin, a module-level name) is not.
    """
    bound: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
    return bound


def test_only_one_method_of_the_engine_calls_a_source() -> None:
    """Any other method that calls a client is an answer nobody counted."""
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    engine = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "MetadataService"
    )
    methods = [node for node in engine.body if isinstance(node, ast.FunctionDef)]
    assert any(method.name == THE_DOOR for method in methods), (
        f"`{THE_DOOR}` is gone from the Engine, so this guard is watching a door "
        "that no longer exists"
    )

    offenders: list[str] = []
    for method in methods:
        if method.name == THE_DOOR:
            continue
        held = _locals_of(method)
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = None
            if isinstance(called, ast.Name):
                name = called.id
            elif isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name):
                name = called.value.id
            if name in held:
                offenders.append(f"{method.name}: line {node.lineno}")

    assert offenders == [], (
        "a method of the Metadata Engine calls a source directly instead of "
        f"through `{THE_DOOR}`, so that answer is never counted and the window "
        f"can report the source absent from a run it answered in: {offenders}"
    )


def test_the_count_is_written_in_that_one_place_too() -> None:
    """A second writer of the count is a second rule about what an answer is."""
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    writers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Subscript)
        and isinstance(inner.ctx, ast.Store)
        and isinstance(inner.value, ast.Attribute)
        and inner.value.attr == "_answers"
    }

    assert writers == {THE_DOOR}, f"the count of answers is written outside `{THE_DOOR}`: {writers}"
