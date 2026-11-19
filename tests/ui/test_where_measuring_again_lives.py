"""Which menu the gesture that measures one album again is in, by running them both.

It was in the wrong one, and no guard here could have said so: it was in *a*
menu, with a label and a handler, and every check over these files passed. What
a check can hold is the rule the dialog's own code states — one gesture has one
home — against the home this one is meant to have, which is the dialog where
the lines it can change are drawn.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from tests.ui.node_program import run_node

WEB = Path(__file__).resolve().parent.parent.parent / "src" / "diglibrary" / "ui" / "web"
SCRIPT = WEB / "app.js"
STRINGS = WEB / "strings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _labels(album: dict) -> dict[str, list[str]]:
    """Run both menu builders over one album and return the labels each produced."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in ("albumActions", "albumMoreItems"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    program = (
        STRINGS.read_text(encoding="utf-8").replace("export const STR", "const STR", 1)
        + f"\nconst album = {json.dumps(album)};\n"
        + "const state = { openAlbum: album };\n"
        + "\n".join(pieces)
        + ";\nconsole.log(JSON.stringify({\n"
        + "  card: albumActions(album).map((item) => item.label),\n"
        + "  dialog: albumMoreItems().map((item) => item.label),\n"
        + "}));"
    )
    answer = run_node(program)
    return json.loads(answer.stdout.strip())


ORGANIZED = {
    "unit_id": 1,
    "organized": True,
    "applied": True,
    "looked_at": True,
    "decision": "automatic",
}


def test_measuring_an_album_again_is_behind_the_dialogs_three_dots() -> None:
    """Where the album is already pointed at, and where the lines it changes are."""
    menus = _labels(ORGANIZED)

    assert "Measure the audio again" in menus["dialog"]


def test_it_is_not_also_on_the_shelf_card_because_one_gesture_has_one_home() -> None:
    """The rule the dialog's own menu states, kept rather than quoted.

    Two homes for one gesture is two places for it to be changed by half, which
    this project has paid for more than once.
    """
    menus = _labels(ORGANIZED)

    assert "Measure the audio again" not in menus["card"]


def test_it_sits_beside_the_other_reading_of_this_albums_own_audio() -> None:
    """Key and tempo are read from the album in hand for the same reason.

    Beside it rather than anywhere in the list: the two are one kind of thing,
    and a reader looking for one should meet the other.
    """
    dialog = _labels(ORGANIZED)["dialog"]

    assert dialog.index("Measure the audio again") == dialog.index("Measure key and tempo") + 1
