"""The control that calls an album a compilation, run rather than read.

Two states on one button, and what it sends is the opposite of what the server
last said. Read as text, a control that offered the withdrawal on an album
nobody had marked, or sent `true` from both states, is the same handful of
valid lines — so it is run, over a DOM holding only what it touches.

It also checks the crossing itself: the bridge call is positional, and the
seventh place is the one this gesture owns.

Skipped where `node` is not installed.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
from pathlib import Path

import pytest
from tests.ui.node_program import run_node

from diglibrary.application.api import LibraryApi

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

HARNESS = """
class Node {
  constructor(tag) {
    this.tag = tag;
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.listeners = {};
  }
  addEventListener(name, run) { this.listeners[name] = run; }
}
const document = { createElement: (tag) => new Node(tag) };
const STR = {
  planVariousMark: "Treat as a compilation",
  planVariousMarkTip: "mark tip",
  planVariousDrop: "Not a compilation",
  planVariousDropTip: "drop tip",
};
const sent = [];
const drawn = [];
let refreshed = 0;
const state = { openAlbum: { unit_id: 7 } };
const api = () => ({
  correct: async (...given) => { sent.push(given); return { ok: true, album: { unit_id: 7 } }; },
});
const whileCorrecting = (run) => run();
const renderAlbumDialog = (album) => drawn.push(album);
const refresh = async () => { refreshed += 1; };
const toastError = (text) => { throw new Error(text); };
"""


def _run(body: str) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    parts = []
    for name in ("variousControl", "sendVarious"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        parts.append(found.group(0))
    answer = run_node(HARNESS + "\n".join(parts) + body)
    return json.loads(answer.stdout.strip())


def test_an_unmarked_album_is_offered_the_mark_and_sends_it() -> None:
    said = _run("""
        const control = variousControl({ various_by_user: false });
        await control.listeners.click();
        console.log(JSON.stringify({
          text: control.textContent, tip: control.title, cls: control.className,
          sent, drawn: drawn.length, refreshed,
        }));
        """)

    assert said["text"] == "Treat as a compilation"
    assert said["tip"] == "mark tip"
    assert "link-button" in said["cls"].split()
    assert said["sent"] == [[7, None, None, None, None, None, True]]
    assert said["drawn"] == 1 and said["refreshed"] == 1, "redrawn from the server's answer"


def test_a_marked_album_is_offered_the_way_back_and_sends_that() -> None:
    said = _run("""
        const control = variousControl({ various_by_user: true });
        await control.listeners.click();
        console.log(JSON.stringify({ text: control.textContent, tip: control.title, sent }));
        """)

    assert said["text"] == "Not a compilation"
    assert said["tip"] == "drop tip"
    assert said["sent"] == [[7, None, None, None, None, None, False]]


def test_the_seventh_place_of_the_call_is_the_one_this_gesture_owns() -> None:
    """The bridge is positional, so the window's order has to be the server's."""
    names = list(inspect.signature(LibraryApi.correct).parameters)[1:]

    assert names.index("various") == 6, names


def test_the_window_draws_what_the_server_decided_and_never_works_it_out() -> None:
    """Whether the control is offered is the server's answer, read as given.

    The window is told `various_offered` and `various_by_user`; a second
    copy of the rule here, assembled from the release's artists or from what
    the source says, would be the worst pair of places for one condition to
    live.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    assert "album.various_by_user || album.various_offered" in source
    assert "album.various_by_source" not in source
    assert not re.search(r"various artists|\"various\"", source, flags=re.IGNORECASE)


def test_the_control_sits_under_the_folder_name_and_not_in_the_rows_last_cell() -> None:
    """The row ends in the narrow cell for a file's quality word.

    Appended to the row's last cell, the control was drawn past the edge of
    the dialog, where only its first two words could be read.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'folderRow.querySelector(".plan-after").append(variousControl(album))' in source
    assert "lastElementChild.append(variousControl" not in source
