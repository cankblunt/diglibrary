"""A gesture told that an album's folder is gone puts that on the card.

`folder_missing` reaches the shelf only with a dashboard read. A folder deleted
in the Finder after the last read left its card wearing `✓ organized` while a
click on it answered that the folder is not there: the dialog said one thing
and the shelf behind it another, until something else happened to refresh it.

Run in node over stubs, with the window's own `badgeFor`, because the defect is
which copy of the shelf a gesture's answer reaches, and no line read alone
shows it.
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

HARNESS = """
const done = { draws: 0, errors: [], gone: [] };
const state = {
  albums: [{ unit_id: 7, organized: true }, { unit_id: 8, organized: true }],
  shelfWrites: 0,
  openAlbum: null,
  identifying: new Set(),
};
const STR = {
  badgeOrganized: "✓ organized",
  badgeFolderGone: "not where it was",
  tipFolderGone: "gone",
  albumGone: "Unknown album.",
};
const GONE = {
  ok: false,
  error: "not where it was recorded",
  folder_is_gone: true,
  folder: "/x/7",
};
const UNKNOWN = { ok: false, error: "Unknown album." };
let answer = GONE;
const api = () => ({
  album: async () => answer,
  open_album: async () => answer,
  send_to_bench: async () => answer,
});
function renderGrid() { done.draws += 1; }
function toastError(text) { done.errors.push(text); }
function showAlbumGone(unitId) { done.gone.push(unitId); }
function badges() { return state.albums.map((album) => badgeFor(album)[1]); }
"""

_UNDER_TEST = (
    "badgeFor",
    "noteFolderGone",
    "openAlbum",
    "openAlbumFolder",
    "sendToBench",
)


def _function(source: str, name: str) -> str:
    found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
    assert found, f"no {name}() to run, so this guard reads the wrong file"
    return found.group(0)


def _run(body: str) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    program = (
        HARNESS
        + "\n".join(_function(source, name) for name in _UNDER_TEST)
        + ";\n(async () => {\n"
        + body
        + "\nconsole.log(JSON.stringify(done));\n})();"
    )
    ran = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(ran.stdout.strip())


def test_a_click_on_a_card_whose_folder_is_gone_changes_its_badge() -> None:
    done = _run("""
      done.before = badges();
      done.opened = await openAlbum(7);
      done.after = badges();
      done.writes = state.shelfWrites;
    """)
    assert done["before"] == ["✓ organized", "✓ organized"]
    assert done["opened"] is False
    assert done["gone"] == [7], "the gone-folder screen is still the answer"
    assert done["after"] == ["not where it was", "✓ organized"]
    assert done["draws"] == 1
    assert done["writes"] == 1, "a read in flight would draw the old badge back"


@pytest.mark.parametrize("gesture", ["openAlbumFolder(7)", "sendToBench(7)"])
def test_every_gesture_told_the_folder_is_gone_marks_the_card(gesture: str) -> None:
    done = _run(f"await {gesture}; done.after = badges();")
    assert done["after"] == ["not where it was", "✓ organized"]
    assert done["errors"] == ["not where it was recorded"]


def test_a_refusal_that_is_not_about_the_folder_leaves_the_card_alone() -> None:
    done = _run("""
      answer = UNKNOWN;
      await openAlbum(7);
      await openAlbumFolder(7);
      await sendToBench(7);
      done.after = badges();
      done.writes = state.shelfWrites;
    """)
    assert done["after"] == ["✓ organized", "✓ organized"]
    assert (done["draws"], done["writes"]) == (0, 0)


def test_a_card_already_marked_is_not_drawn_again() -> None:
    done = _run("""
      await openAlbum(7);
      await openAlbum(7);
      done.writes = state.shelfWrites;
    """)
    assert (done["draws"], done["writes"]) == (1, 1)
