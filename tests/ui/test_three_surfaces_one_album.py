"""The card, the notice and the scan's question say one thing about one album.

The three are drawn by three different rules. The card and the dialog read
`looked_at`; if the scan's question reads the *absence of a missed source*
instead, it is true of the album that was read and equally true of the album
nobody ever asked about, and an album shown as not scanned is told it has
already been read. This runs the real `scanThisAlbum` over both albums and asks which
one gets the question.

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

HARNESS = """
const shown = [];
const said = [];
const scanned = [];
const $ = (id) => ({
  showModal() { shown.push(id); },
  close() {},
});
const STR = { scanAgainBecause: (source) => `because ${source}` };
const toast = (words) => said.push(words);
async function withSpinner(button, run) { return run(); }
const api = () => ({ scan_selected: async (ids) => { scanned.push(ids); return { ok: true }; } });
async function refresh() {}
function renderAlbumDialog() {}
function reportIfFollowed() {}
function scanBegan() {}
function toastError() {}
const state = { openAlbum: __ALBUM__ };
let scanAgainAsked = false;
__BODY__
scanThisAlbum().then(() => console.log(JSON.stringify({ shown, said, scanned })));
"""


def _press(album: dict) -> dict:
    """Press `Scan this album` over one album and report what happened."""
    source = SCRIPT.read_text(encoding="utf-8")
    body = re.search(r"async function scanThisAlbum\(event\) \{[\s\S]+?\n\}", source)
    assert body, "no scanThisAlbum() to run, so this guard is reading the wrong file"
    program = HARNESS.replace("__BODY__", body.group(0)).replace("__ALBUM__", json.dumps(album))
    finished = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def test_an_album_nobody_asked_about_is_never_told_it_was_already_read() -> None:
    """The three surfaces agree because all of them read one fact."""
    never = _press({"unit_id": 1, "looked_at": False, "sources_missed": {}})

    assert (
        never["shown"] == []
    ), "the album whose own card says NOT SCANNED was told it had already been read"
    assert never["scanned"] == [[1]], "and pressing Scan did not scan it"


def test_an_album_every_catalogue_answered_still_states_the_cost() -> None:
    """A re-scan of an album that was already read asks before it runs.

    Reading an album again costs dozens of catalogue calls and minutes of
    waiting, so the cost is stated first.
    """
    read_already = _press({"unit_id": 2, "looked_at": True, "sources_missed": {}})

    assert read_already["shown"] == ["scan-again-dialog"], "the cost stopped being stated"
    assert read_already["scanned"] == [], "and it scanned without asking"


def test_a_missing_source_is_the_reason_and_the_scan_runs() -> None:
    """A source missed on the last reading is a reason to read again, unasked."""
    missed = _press(
        {"unit_id": 3, "looked_at": True, "sources_missed": {"musicbrainz": "unreachable"}}
    )

    assert missed["shown"] == [], "it asked about a cost it had a reason to pay"
    assert missed["said"] == ["because musicbrainz"], missed["said"]
    assert missed["scanned"] == [[3]]
