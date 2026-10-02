"""When a tooltip opens, and whether the window asks before it starts."""

import ast
from pathlib import Path

import pytest

from diglibrary.ui import tooltips

APP = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/app.py"


class _Defaults:
    """Keep what was registered, and how."""

    def __init__(self) -> None:
        self.registered: list[dict[str, int]] = []

    def registerDefaults_(self, values: dict[str, int]) -> None:  # noqa: N802
        self.registered.append(values)


def test_the_delay_is_registered_and_never_written() -> None:
    """A registered default loses to one somebody set for the whole machine.

    Writing the value would override that choice and leave it on disk after the
    application has gone, so the store is only ever asked to register.
    """
    defaults = _Defaults()

    assert tooltips.appear_sooner(defaults) is True

    assert defaults.registered == [{"NSInitialToolTipDelay": tooltips.DELAY_MILLISECONDS}]
    assert tooltips.DELAY_MILLISECONDS == 300


def test_a_platform_that_cannot_be_asked_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tooltips.sys, "platform", "win32")

    assert tooltips.appear_sooner() is False


def test_the_window_asks_before_it_starts() -> None:
    """The platform reads the delay when it first needs it, so order matters.

    Asked after `webview.start` it is asked after the window has closed, since
    that call blocks — valid code that does nothing.
    """
    run = next(
        node
        for node in ast.parse(APP.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    calls = [
        (node.lineno, ast.unparse(node.func))
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
    ]
    asked = [line for line, name in calls if name == "tooltips.appear_sooner"]
    started = [line for line, name in calls if name == "webview.start"]

    assert asked, "the window never asks for its tooltips to open sooner"
    assert started, "no webview.start to read, so this guard is looking at the wrong file"
    assert min(asked) < max(started), "asked only after the window has already closed"
