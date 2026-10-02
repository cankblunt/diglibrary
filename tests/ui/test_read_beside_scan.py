"""A folder read beside a scan, as the window hears it, run rather than read.

The server reads a folder while a scan runs, and three lines of the window
decide whether that can be seen working: whether the + borrows `scanning` and
the scan's bar, whether `opened` switches both off when the read ends, and
whether any `error` ends the scan. Each is valid JavaScript whichever way it is
wired, so this runs
`chooseFolder` and the event loop over stubs and reads what they did to a scan
that was already running.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

HARNESS = """
const nodes = {};
const $ = (id) => (nodes[id] ||= { hidden: false, textContent: "", style: {}, disabled: false });
const STR = { opening: "Reading that folder…" };
const state = {
  scanning: true, opening: 0, adopting: 0, marked: new Set(), polling: true,
  root: null, rootShown: false,
};
$("progress").hidden = false;
const did = [];
let queued = [];
const api = () => ({
  choose_folder: async () => "/music/new album",
  open_folder: async (folder) => { did.push(["open_folder", folder]); return { ok: true }; },
  events: async () => { const out = queued; queued = []; return out; },
});
async function withSpinner(button, run) { return run(); }
function renderRootPath() {}
function renderMarkCount() {}
function renderLibraryChrome() {}
function renderActivity() {}
function ensurePolling() { did.push(["poll"]); }
function markArrivals(ids) { did.push(["marked", ids]); }
async function refresh() { did.push(["refresh"]); }
function toastError(message) { did.push(["error", message]); }
function sayArrivedSameAudio(found) { if (found && found.length) did.push(["same audio", found]); }
function sayKnownAgain(names) { if (names && names.length) did.push(["known", names]); }
function worthPolling() { return false; }
function pollDelay() { return 0; }
"""


def _function(name: str) -> str:
    """One whole function out of `app.js`, matched by its braces."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index(f"async function {name}(")
    depth = 0
    kept: list[str] = []
    for index, line in enumerate(source[start:].split("\n")):
        kept.append(line)
        depth += line.count("{") - line.count("}")
        if index and depth == 0:
            return "\n".join(kept)
    raise AssertionError(f"{name} never closes; this guard reads the wrong file")


def _run(body: str) -> dict:
    program = (
        HARNESS
        + _function("chooseFolder")
        + "\n"
        + _function("pollEvents")
        + ";\n(async () => {\n"
        + body
        + "\nconsole.log(JSON.stringify({ did, scanning: state.scanning,"
        + " opening: state.opening, bar: !$('progress').hidden }));\n})();"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_a_folder_chosen_during_a_scan_waits_as_a_read_and_leaves_the_bar() -> None:
    said = _run("await chooseFolder();")

    assert said["did"][0] == ["open_folder", "/music/new album"]
    assert said["opening"] == 1, "the read has no wait of its own"
    assert said["scanning"] is True and said["bar"] is True, "the + took over the scan's bar"


def test_the_read_ending_does_not_end_the_scan_beside_it() -> None:
    said = _run("""
        await chooseFolder();
        queued = [{ type: "opened", payload: { albums: [9], folder: "/music/new album" } }];
        await pollEvents();
        """)

    assert ["marked", [9]] in said["did"], "what the folder held arrives marked"
    assert said["opening"] == 0, "the read's wait was not let go"
    assert said["scanning"] is True and said["bar"] is True, "the read switched the scan off"


def test_an_error_about_a_folder_is_not_the_end_of_the_scan() -> None:
    about_the_folder = _run("""
        queued = [{ type: "error", payload: { message: "x could not be read", reading: true } }];
        await pollEvents();
        """)
    assert ["error", "x could not be read"] in about_the_folder["did"], "and it is still said"
    assert about_the_folder["scanning"] is True and about_the_folder["bar"] is True

    about_the_run = _run("""
        queued = [{ type: "error", payload: { message: "the scan failed" } }];
        await pollEvents();
        """)
    assert about_the_run["scanning"] is False and about_the_run["bar"] is False
