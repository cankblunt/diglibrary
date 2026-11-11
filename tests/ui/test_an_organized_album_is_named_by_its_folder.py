"""An organized album is named on the shelf by its folder, and shows what was renamed.

The card's two lines, its hover, and the dialog's names-applied table are run in
node over stubs, with the window's own functions, because what is checked is
what they draw from a payload — which no reading of the code shows.
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
const done = {};
function make(tag) {
  return {
    tag, children: [], textContent: "", title: "", className: "", hidden: false,
    dataset: {},
    classList: { add() {} },
    addEventListener() {},
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    replaceChildren(...nodes) { this.children = nodes; },
  };
}
const document = { createElement: make };
const nodes = {};
function $(id) { return (nodes[id] = nodes[id] || make(id)); }
const state = { pointedOut: new Set(), covers: new Map() };
const coverObserver = { observe() {} };
function isWaiting() { return false; }
function openAlbum() {}
function albumMenu() {}
function forgetButton() { return make("forget"); }
function briefRating() { return make("rating"); }
function starStrip() { return make("stars"); }
function rateFromCard() {}
function badgeFor() { return ["", "", ""]; }
function markBox() { return make("mark"); }
function find(node, className) {
  if (node.className === className) return node;
  for (const child of node.children || []) {
    const found = find(child, className);
    if (found) return found;
  }
  return null;
}
const ORGANIZED = {
  unit_id: 1, organized: true, tracks: 9, year: 1977,
  artist: "Catalogue Credit", title: "Catalogue Title",
  folder: "Orchard Duo - Quiet Harbour Tapes (1977) [FLAC]",
  named_on_disk: {
    name: "Orchard Duo - Quiet Harbour Tapes (1977)",
    artist: "Orchard Duo", title: "Quiet Harbour Tapes", year: "1977",
  },
};
const WAITING = {
  unit_id: 2, organized: false, tracks: 9, year: 1975, named_on_disk: null,
  artist: "Catalogue Credit", title: "Catalogue Title", folder: "downloaded folder",
};
"""

_UNDER_TEST = (
    "fullNameFor",
    "onDiskName",
    "sameWords",
    "card",
    "renderNamesApplied",
)


def _function(source: str, name: str) -> str:
    found = re.search(rf"(?:async )?function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
    assert found, f"no {name}() to run, so this guard reads the wrong file"
    return found.group(0)


def _run(body: str) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    program = (
        STRINGS.read_text(encoding="utf-8").replace("export ", "")
        + HARNESS
        + "\n".join(_function(source, name) for name in _UNDER_TEST)
        + ";\n"
        + body
        + "\nconsole.log(JSON.stringify(done));"
    )
    ran = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(ran.stdout.strip().splitlines()[-1])


def test_the_card_of_an_organized_album_reads_its_folder() -> None:
    done = _run("""
      const drawn = card(ORGANIZED);
      done.title = find(drawn, "card-title").textContent;
      done.sub = find(drawn, "card-sub").textContent;
      done.hover = drawn.title;
    """)
    assert done["title"] == "Quiet Harbour Tapes"
    assert done["sub"] == "Orchard Duo · 1977"
    assert done["hover"] == "Orchard Duo - Quiet Harbour Tapes (1977) [FLAC]"


def test_an_album_not_yet_organized_keeps_the_catalogue_s_name() -> None:
    done = _run("""
      const drawn = card(WAITING);
      done.title = find(drawn, "card-title").textContent;
      done.sub = find(drawn, "card-sub").textContent;
    """)
    assert done["title"] == "Catalogue Title"
    assert done["sub"] == "Catalogue Credit · 1975"


def test_the_names_the_apply_gave_are_drawn_while_the_editor_is_hidden() -> None:
    done = _run("""
      $("plan-editor").hidden = true;
      renderNamesApplied({ names_applied: [
        { kind: "folder", was: "old folder", now: "New - Folder (1977) [FLAC]" },
        { kind: "file", was: "aaa.flac", now: "01. One.flac" },
      ] });
      done.hidden = $("names-applied-fold").hidden;
      const [head, body] = $("names-applied").children;
      done.head = head.children[0].children.map((cell) => cell.textContent);
      done.rows = body.children.map((row) => row.children.map((cell) => cell.textContent));
    """)
    assert done["hidden"] is False
    assert done["head"] == ["", "How it was", "", "How it is now"]
    assert done["rows"] == [
        ["Folder", "old folder", "→", "New - Folder (1977) [FLAC]"],
        ["", "aaa.flac", "→", "01. One.flac"],
    ]


def test_the_names_applied_give_way_to_the_editor_after_a_replan() -> None:
    done = _run("""
      $("plan-editor").hidden = false;
      renderNamesApplied({ names_applied: [{ kind: "file", was: "a", now: "b" }] });
      done.hidden = $("names-applied-fold").hidden;
      $("plan-editor").hidden = true;
      renderNamesApplied({ names_applied: [] });
      done.emptyHidden = $("names-applied-fold").hidden;
    """)
    assert done["hidden"] is True, "two tables of names would be on screen"
    assert done["emptyHidden"] is True
