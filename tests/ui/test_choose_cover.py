"""What `Choose a cover…` does, and what the head of the dialog offers, run rather than read.

A finished album is not planned again before a cover is chosen. A cover names
nothing and asks no catalogue; a re-plan in front of it buys nothing and leaves
a proposal standing that draws the album back as `Auto`. So the gesture goes
straight to the picture panel on every album.

The stubs for a question and a re-plan are here on purpose, and they record: a
wiring that reached for either would show up in the list of what the gesture
did, which a static reading of `app.js` could not prove.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
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

# Only what the function touches. Every stub records, and nothing answers
# generously: a bridge that returned a plausible object for a call that should
# never have happened would hide exactly the defect this is for.
HARNESS = """
const did = [];
let askedAnswer = true;
let planAnswer = true;
let coverAnswer = { ok: true, album: { unit_id: 1 } };
const state = { openAlbum: null };
const api = () => ({
  choose_cover: async (unit_id) => {
    did.push(["choose_cover", unit_id]);
    return coverAnswer;
  },
});
async function confirmPlanAgain() {
  did.push(["asked"]);
  return askedAnswer;
}
async function planAnyway() {
  did.push(["plan_again"]);
  return planAnswer;
}
function toastError(text) { did.push(["error", text]); }
function renderAlbumDialog(album) { did.push(["drew", album && album.unit_id]); }
"""


def _run(body: str) -> dict:
    """Run `chooseCover` in node and report, in order, what it did."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"async function chooseCover\(\) \{[\s\S]+?\n\}", source)
    assert found, "no chooseCover() to run, so this guard is looking at the wrong file"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS + found.group(0) + body],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def _said(body: str) -> list:
    """What the gesture did, in order, for a body that awaits `chooseCover`."""
    return _run("(async () => {\n" + body + "\nconsole.log(JSON.stringify({ did }));\n})();")["did"]


def test_a_finished_album_goes_straight_to_the_picture_panel() -> None:
    """No question and no re-plan in front of the panel, finished or not."""
    for asleep in ("true", "false"):
        did = _said(f"""
            state.openAlbum = {{ unit_id: 7, asleep: {asleep} }};
            await chooseCover();
            """)

        assert did == [["choose_cover", 7], ["drew", 1]], f"asleep={asleep}"


def test_a_closed_panel_draws_nothing() -> None:
    """Cancelling the file panel is an answer, and the dialog stays as it was."""
    did = _said("""
        coverAnswer = { ok: true, cancelled: true };
        state.openAlbum = { unit_id: 7, asleep: true };
        await chooseCover();
        """)

    assert did == [["choose_cover", 7]]


def test_a_refusal_from_the_bridge_is_shown_and_nothing_is_drawn() -> None:
    """A file that is not an image is refused by the server, and said so."""
    did = _said("""
        coverAnswer = { ok: false, error: "That file is not an image this app can read." };
        state.openAlbum = { unit_id: 7, asleep: false };
        await chooseCover();
        """)

    assert did == [["choose_cover", 7], ["error", "That file is not an image this app can read."]]


def _head(body: str) -> dict:
    """Run `drawDialogCover` over stub nodes and read what the head shows."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"function drawDialogCover\(unitId\) \{[\s\S]+?\n\}", source)
    assert found, "no drawDialogCover() to run, so this guard is looking at the wrong file"
    program = (
        """
    const nodes = {};
    const $ = (id) => (nodes[id] ||= { hidden: false, textContent: "", src: "" });
    const STR = { chooseCover: "Choose a cover…", changeCover: "Change cover…" };
    const state = { covers: new Map(), coverless: new Set() };
    """
        + found.group(0)
        + body
        + """
    const read = (id) => ({ hidden: $(id).hidden, text: $(id).textContent, src: $(id).src });
    console.log(JSON.stringify({
      picture: read("album-cover"), empty: read("album-cover-empty"),
      gesture: read("album-choose-cover"),
    }));
    """
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_the_head_offers_to_change_a_picture_it_is_drawing() -> None:
    shown = _head('state.covers.set(7, "u://sleeve"); drawDialogCover(7);')

    assert shown["picture"] == {"hidden": False, "text": "", "src": "u://sleeve"}
    assert shown["empty"]["hidden"] is True
    assert shown["gesture"]["hidden"] is False
    assert shown["gesture"]["text"] == "Change cover…"


def test_an_album_with_no_picture_gets_an_empty_frame_and_the_first_words() -> None:
    shown = _head("state.coverless.add(7); drawDialogCover(7);")

    assert shown["picture"]["hidden"] is True
    assert shown["empty"]["hidden"] is False
    assert shown["gesture"]["hidden"] is False
    assert shown["gesture"]["text"] == "Choose a cover…"


def test_a_picture_not_asked_for_yet_is_not_called_absent() -> None:
    """Between opening and the answer, the head says nothing rather than guess.

    *Choose a cover…* over an album that has one — only because its card never
    scrolled into view — is the screen naming a cause it did not measure.
    """
    shown = _head("drawDialogCover(7);")

    assert shown["gesture"]["hidden"] is True, "it guessed there was no picture"
    assert shown["empty"]["hidden"] is False


def _sentence(files: int, icons: int) -> str:
    """Say the waiting cover with the window's own `strings.js`."""
    source = (SCRIPT.parent / "strings.js").read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    program = found.group(0).replace("export const STR", "const STR", 1) + (
        f"\nconsole.log(JSON.stringify(STR.coverWaiting({files}, {icons}, 1200, 1200)));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_the_waiting_cover_names_the_finder_icons_it_will_draw() -> None:
    """The icon is the place a cover is seen without opening anything."""
    assert _sentence(10, 10) == (
        "A cover you chose is waiting: 1200\u00d71200, for the folder, inside 10 files "
        "and on 10 Finder icons."
    )
    assert (
        _sentence(1, 0)
        == "A cover you chose is waiting: 1200\u00d71200, for the folder, inside 1 file."
    )
