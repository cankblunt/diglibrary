"""An album dropped or opened again while already on the shelf is brought into view.

It used to be named in a notice and nothing else, so an album whose row was
weeks old sat dozens of cards down a shelf sorted by date, and dropping it
looked like a gesture that did nothing. Now, under the date order, it floats
to the top with the arrivals; under an order the user chose it stays where
that order puts it; either way it is highlighted once the notice is closed,
scrolled to, and never marked. One kept off the screen by a filter or a search
is said to be hidden, and the filter is left alone.

Run in node over the real functions, because what matters is the order of
things on the shelf and the moment the highlight happens.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"
STRINGS = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/strings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

HARNESS = """
const done = { said: [], scrolled: [], timers: [] };
const state = { sort: "recent", pointedAt: [], pointedOut: new Set(), marked: new Set() };
function node(id) {
  const classes = new Set();
  return {
    dataset: { unitId: String(id) },
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      has: (c) => classes.has(c),
    },
    scrollIntoView: () => done.scrolled.push(id),
    classes,
  };
}
let drawn = [];
const albumContainer = () => ({ querySelectorAll: () => drawn });
const document = { querySelectorAll: (q) => drawn.filter((n) => n.classes.has("is-pointed-out")) };
let closeListener = null;
const notice = { open: false, addEventListener: (_, f) => { closeListener = f; } };
const $ = () => notice;
function sayKnownAgain(names) {
  if (names && names.length) done.said.push(STR.adoptedKnown(names));
}
function sayAlreadyInLibrary(sentence) { done.said.push(sentence); }
globalThis.setTimeout = (f) => { done.timers.push(f); };
const highlighted = () =>
  drawn.filter((n) => n.classes.has("is-pointed-out")).map((n) => Number(n.dataset.unitId));
"""


def _function(source: str, name: str) -> str:
    found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
    assert found, f"no {name}() to run, so this guard reads the wrong file"
    return found.group(0)


def _run(body: str) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    strings = STRINGS.read_text(encoding="utf-8")
    constant = re.search(r"const POINTED_OUT_MS = \d+;", source)
    assert constant, "no POINTED_OUT_MS"
    program = (
        strings.replace("export ", "")
        + HARNESS
        + constant.group(0)
        + "\n".join(
            _function(source, name)
            for name in ("sortAlbums", "floatToTheTop", "announceKnown", "pointOut")
        )
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
    return json.loads(ran.stdout.strip().splitlines()[-1])


SHELF = "const shelf = [1, 2, 3, 4, 5, 6].map((id) => ({ unit_id: id, artist: `Z${7 - id}` }));"


def test_under_the_date_order_the_albums_already_here_rise_to_the_top() -> None:
    done = _run(SHELF + """
        floatToTheTop([2, 4]);
        done.order = sortAlbums(shelf).map((a) => a.unit_id);
        state.sort = "artist";
        done.chosen = sortAlbums(shelf).map((a) => a.unit_id);
        done.marked = [...state.marked];
        """)
    assert done["order"] == [2, 4, 6, 5, 3, 1], "the dropped ones first, in the order given"
    assert done["chosen"] == [6, 5, 4, 3, 2, 1], "an order the user chose is left as chosen"
    assert done["marked"] == [], "nothing is marked for the next Scan"


def test_the_visible_ones_are_highlighted_after_the_notice_and_the_hidden_named() -> None:
    done = _run("""
        drawn = [node(9), node(5)];
        notice.open = true;
        announceKnown({ known_ids: [5, 7, 9], known_albums: ["Five", "Seven", "Nine"] });
        await Promise.resolve();
        done.before = highlighted();
        notice.open = false;
        closeListener();
        await new Promise((resolve) => setImmediate(resolve));
        done.during = highlighted();
        done.timers.shift()();
        done.after = highlighted();
        done.timers = [];
        """)
    assert done["before"] == [], "nothing pulses behind a notice that covers the shelf"
    assert done["during"] == [9, 5], "every visible one is highlighted"
    assert done["scrolled"] == [9], "the first in the shelf's own order is scrolled to"
    assert done["after"] == [], "and the highlight ends"
    assert done["said"][0].startswith("3 of these albums are already in your library")
    assert "highlighted on the shelf" in done["said"][0]
    assert done["said"][1].startswith("“Seven” is hidden by the filter or the search")
