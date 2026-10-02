"""Regression tests for the album review screen, one behaviour per test.

Each behaviour is also covered by the guard of its own area. This file drives
them end to end — the window's script run in node, the API over real files, the
stylesheet read as text — so that one run answers for the five together: which
button is filled, the folder cover of an arrangement by tags, an album moved on
disk, notices that leave on their own, and the colour of the way back.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.application.test_api import (
    FIXTURES,
    FakeSource,
    _api,
    _finish_reads,
    _library,
    _tagged_album,
)

WEB = Path(__file__).resolve().parents[1] / "src/diglibrary/ui/web"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


# --- 1. the filled button belongs to the gesture that comes next -----------


@needs_node
def test_the_arranged_album_lights_approve_and_not_the_scan() -> None:
    """An album arranged by its own tags fills Approve, not the scan button.

    The state is an album in review, arranged by its tags, with a plan that can
    be approved and the scan still on the row. The block is taken out of
    `app.js` and run, because which button is lit is not something reading the
    source can answer.
    """
    script = (WEB / "app.js").read_text(encoding="utf-8")
    block = re.search(
        r"  const noSourceAsked[\s\S]+?\$\(\"album-scan-now\"\)\.classList\.toggle\([^;]+;",
        script,
    )
    assert block, "the block that decides the filled button is not where this expects it"

    program = f"""
const lit = {{}};
const mark = (id) => ({{
  toggle: (name, on) => {{ if (name === "button-primary") lit[id] = on; }},
}});
const drawn = {{
  "album-scan-now": {{ hidden: false, classList: mark("scan") }},
  "album-approve": {{ disabled: false, classList: mark("approve") }},
}};
const $ = (id) => drawn[id];
const album = {{ source: "tags" }};
{block.group(0)}
console.log(JSON.stringify(lit));
"""
    finished = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert finished.returncode == 0, finished.stderr
    lit = json.loads(finished.stdout)

    assert lit["approve"] is True, "an arranged album does not fill the Approve button"
    assert lit["scan"] is False, "the scan is still the filled button on an arranged album"


# --- 2. arranging by tags plans the folder cover ---------------------------


def test_arranging_by_tags_plans_the_folder_cover(tmp_path: Path) -> None:
    """Arranging by tags also writes `cover.jpg` into the album folder.

    The picture is the one the files already carry, so this embeds one first and
    then asks for the arrangement. What must not appear is anything that writes
    into one of the audio files.
    """
    from tests.library.test_artwork_store import _drawable_jpeg

    from diglibrary.library.artwork import FilesystemArtworkStore

    library = tmp_path / "library"
    folder = _tagged_album(library / "an album")
    picture = tmp_path / "sleeve.jpg"
    picture.write_bytes(_drawable_jpeg())
    store = FilesystemArtworkStore()
    for track in sorted(folder.glob("*.flac")):
        store.embed(track, picture)

    api = _api(tmp_path, FakeSource())
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    album = api.album(api.state()["albums"][0]["unit_id"])["album"]

    covers = [row for row in album["operations"] if row["kind"] == "write_image"]
    kinds = {row["kind"] for row in album["operations"]}

    assert len(covers) == 1, f"no cover.jpg was planned; the arrangement planned {kinds}"
    assert Path(covers[0]["target"]).name == "cover.jpg", covers[0]["target"]
    assert "write_tags" not in kinds, "the arrangement wrote a tag, which is its whole promise"


# --- 3. an album moved on disk is followed to where it went ----------------


def test_a_moved_album_reaches_quality_and_the_file_manager(tmp_path: Path) -> None:
    """Send to Quality and Open folder both follow an album moved on disk.

    Both gestures in one test, because they depend on one lookup. The album is
    moved after being read, into a folder this application has been shown,
    which is the only place it searches.
    """
    library = _library(tmp_path)
    moving = library / "box" / "an album"
    moving.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    opened: list[str] = []
    api = _api(tmp_path, FakeSource())
    api._path_opener = opened.append
    api.open_folder(str(library))
    _finish_reads(api)
    moved = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}["an album"]

    shutil.move(str(moving), str(library / "an album"))

    to_quality = api.send_to_bench(moved)
    to_finder = api.open_album(moved)

    assert to_quality["ok"], f"Send to Quality refuses: {to_quality.get('error')}"
    assert to_finder["ok"], f"Open folder refuses: {to_finder.get('error')}"
    assert opened == [str(library / "an album")], "the old folder was opened"


# --- 4. a notice leaves the screen without being closed --------------------


@needs_node
def test_every_notice_leaves_on_its_own() -> None:
    """Every kind of notice arms a timer long enough for it to be read.

    All four ways this window speaks, in one run. A notice that arms no timer
    waits for a click.
    """
    from tests.ui.test_notice_stays import _armed

    armed = {
        "announcement": _armed('toast("the scan finished");'),
        "failure": _armed('toastError("that album is not where it was");'),
        "refusal": _armed('toastRefusal("a scan is already running");'),
        "instruction": _armed('toastToBeRead("go and look in this folder");'),
    }

    for kind, timers in armed.items():
        assert len(timers) == 1, f"the {kind} waits for a click"
        assert 5000 < timers[0] <= 20000, f"the {kind} earned {timers[0]} ms"

    assert (
        armed["failure"][0] > armed["announcement"][0]
    ), "a failure is given no longer to be read than an announcement"


# --- 5. Cancel is drawn as the way back, not as a disabled button ----------


def test_the_way_back_is_drawn_in_red() -> None:
    """Cancel takes the palette's red.

    Read from the stylesheet, which is where the answer is: the class the button
    wears has to take this palette's red, and `--bad` has to be a colour this
    stylesheet defines — a `var()` nothing defines makes the whole declaration
    invalid, so the button would simply have no colour at all.
    """
    styles = (WEB / "styles.css").read_text(encoding="utf-8")
    markup = (WEB / "index.html").read_text(encoding="utf-8")

    assert (
        'id="album-cancel"' in markup and "button-way-back" in markup
    ), "Cancel does not wear the class this is about"
    rule = re.search(r"\.button-way-back\s*\{([^}]*)\}", styles)
    assert rule, "nothing in the stylesheet draws the way back"
    assert "var(--bad)" in rule.group(1), f"the way back is not red: {rule.group(1).strip()}"
    assert re.search(r"--bad:\s*#", styles), "`--bad` is not defined, so the rule is inert"
