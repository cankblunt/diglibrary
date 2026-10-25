"""A ✕ or a rating confirmed while the dashboard is being read stays on screen.

The dashboard read takes seconds on a large shelf, and `refresh` replaces the
shelf with whatever it returns. A read asked for before a ✕ is a picture from
before it: drawn, it put the card back after the server had taken the album
off, and every further ✕ on that card answered "Unknown album".

Run in node over stubs, because the defect is an ordering of two awaits and no
line of the source is wrong on its own.
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
const done = { reads: 0, toasts: [], errors: [] };
const state = { albums: [], marked: new Set(), shelfWrites: 0, openAlbum: null };
let shelf = [{ unit_id: 7, rating: null }, { unit_id: 8, rating: null }];
let release = null;
let hold = true;
let refuse = false;
const STR = { forgotten: "Taken off." };
const api = () => ({
  // The server's answer is the shelf as it stood when the read was asked for.
  state: () => {
    done.reads += 1;
    const picture = JSON.parse(JSON.stringify({ albums: shelf }));
    if (!hold) return Promise.resolve(picture);
    hold = false;
    return new Promise((resolve) => { release = () => resolve(picture); });
  },
  forget_album: async (unitId) => {
    if (refuse) return { ok: false, error: "Unknown album." };
    shelf = shelf.filter((album) => album.unit_id !== unitId);
    return { ok: true };
  },
  rate_album: async (unitId, value) => {
    shelf = shelf.map((album) => album.unit_id === unitId ? { ...album, rating: value } : album);
    return { ok: true };
  },
});
function renderGrid() {}
function toast(text) { done.toasts.push(text); }
function toastError(text) { done.errors.push(text); }
"""


_UNDER_TEST = ("readDashboard", "forgetAlbum", "applyRating")


def _function(source: str, name: str) -> str:
    found = re.search(rf"async function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
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


def test_a_card_taken_off_during_a_read_is_not_drawn_back() -> None:
    done = _run("""
      const reading = readDashboard();
      await forgetAlbum(7);
      release();
      done.drawn = (await reading).albums.map((album) => album.unit_id);
    """)
    assert done["drawn"] == [8], "the read from before the ✕ put the card back"
    assert done["reads"] == 2
    assert done["errors"] == []


def test_a_rating_given_during_a_read_is_not_drawn_away() -> None:
    done = _run("""
      const reading = readDashboard();
      await applyRating(8, 4);
      release();
      done.drawn = (await reading).albums.map((album) => album.rating);
    """)
    assert done["drawn"] == [None, 4]


def test_a_read_with_no_write_beside_it_is_asked_once() -> None:
    done = _run("""
      const reading = readDashboard();
      release();
      done.drawn = (await reading).albums.map((album) => album.unit_id);
    """)
    assert (done["drawn"], done["reads"]) == ([7, 8], 1)


def test_a_refused_write_does_not_send_the_read_round_again() -> None:
    """Only a write the server confirmed changes what the shelf is."""
    done = _run("""
      refuse = true;
      const reading = readDashboard();
      await forgetAlbum(7);
      release();
      done.drawn = (await reading).albums.map((album) => album.unit_id);
    """)
    assert done["drawn"] == [7, 8] and done["reads"] == 1
    assert done["errors"] == ["Unknown album."]
