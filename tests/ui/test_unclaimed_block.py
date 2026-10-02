"""The sentence under the plan table, run rather than read.

`renderUnclaimed` runs inside `renderAlbumDialog`, so a name in it whose
declaration has gone — a loop left over a collection that is no longer built —
throws `Can't find variable` and takes the **whole window** down: the red band,
and every album card behind it drawn from a page that has stopped running.

Such a function does not read as broken. It is valid JavaScript, a test that
reads the source passes, and `ruff` and `black` have no opinion about `app.js`.
**The only thing that catches a name
whose declaration went away is running the function**, which is what this does.

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

WEB = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web"
SCRIPT = WEB / "app.js"
STRINGS = WEB / "strings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

HARNESS = """
const made = [];
function element(tag) {
  return {
    tag,
    textContent: "",
    className: "",
    hidden: false,
    children: [],
    replaceChildren(...kids) { this.children = kids; },
    append(...kids) { this.children.push(...kids); },
    appendChild(kid) { this.children.push(kid); },
  };
}
const document = {
  createElement: (tag) => {
    const one = element(tag);
    made.push(one);
    return one;
  },
};
const perch = element("div");
const $ = () => perch;
"""


def _strings() -> str:
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _drawn(album: dict, missing_tracks: int, paired: int) -> dict:
    """Run the real `renderUnclaimed` and report the block it built."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"function renderUnclaimed\([^)]*\) \{[\s\S]+?\n\}", source)
    assert found, "no renderUnclaimed() to run, so this guard is reading the wrong file"
    program = (
        HARNESS
        + _strings()
        + "\n"
        + found.group(0)
        + ";\nrenderUnclaimed("
        + json.dumps(album)
        + f", new Map(), {missing_tracks}, {paired});\n"
        + "console.log(JSON.stringify({ hidden: perch.hidden, "
        + "said: perch.children.map((kid) => kid.textContent) }));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


# An album identified as a record it is not, in the shape the route answers:
# four tracks of the release that found no file, eleven files that no track
# claimed.
NOTHING_MATCHED = {
    "evidence": [{"status": "unmatched_track", "track_position": n} for n in range(1, 5)]
    + [{"status": "unmatched_file", "file": f"track {n}.flac"} for n in range(1, 12)]
}


def test_an_album_nothing_matched_says_nothing_under_the_table() -> None:
    """The notice above the plan states this fact, so this block does not.

    A paragraph here for the album nothing matched would repeat the headline,
    the lists and the row sentences. The fact is said once, by the notice; this
    block is for the pairing problem, which is a different situation.
    """
    block = _drawn(NOTHING_MATCHED, missing_tracks=4, paired=0)

    assert block["hidden"] is True, (
        "the album nothing matched still gets a paragraph under the table, "
        "which is the notice's fact said a second time"
    )


def test_a_pairing_problem_gets_the_other_sentence() -> None:
    """Most of an album matching is not the same situation, and says so."""
    album = {
        "evidence": [
            {"status": "paired", "track_position": 1},
            {"status": "unmatched_track", "track_position": 2},
            {"status": "unmatched_track", "track_position": 3},
            {"status": "unmatched_file", "file": "b.flac"},
        ]
    }

    block = _drawn(album, missing_tracks=2, paired=1)

    said = " ".join(block["said"])
    assert "2 tracks found no file here" in said and "1 file was" in said
    assert "Nothing in this folder matched" not in said


def test_one_asking_track_keeps_its_files_on_its_own_row() -> None:
    """A single asking track lists its candidate files on its own row, not here."""
    block = _drawn(NOTHING_MATCHED, missing_tracks=1, paired=0)

    assert block["hidden"] is True, "the block drew where the track's own row says it"


def _rows(album: dict) -> list:
    """Run the real `releaseRowsToDraw` — and the condition it reads — as one."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in ("nothingMatchedHere", "releaseRowsToDraw"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))
    program = (
        "\n".join(pieces)
        + ";\nconsole.log(JSON.stringify(releaseRowsToDraw("
        + json.dumps(album)
        + ")));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


FOUR_TRACKS = [{"position": n, "title": f"t{n}"} for n in range(1, 5)]


def test_a_release_nothing_matched_draws_none_of_its_tracks() -> None:
    """Four rows reading `no file here` about a record that is not this album.

    When an album is identified as another record, the only rows in a column
    headed with what is on disk would be the tracks of that other record, each
    saying the folder has no file for it.
    """
    assert _rows({"release_tracks": FOUR_TRACKS, "evidence": []}) == []


def test_a_release_that_matched_keeps_every_track_row() -> None:
    """One pairing makes this record the album, and then a gap is a real gap.

    The row is also where a file is paired to a track, so taking it away from an
    album this record does describe would remove the answer along with the
    question.
    """
    drawn = _rows(
        {
            "release_tracks": FOUR_TRACKS,
            "evidence": [{"status": "paired", "track_position": 1}],
        }
    )

    assert [track["position"] for track in drawn] == [1, 2, 3, 4]


def test_an_album_with_no_release_draws_no_track_rows_either() -> None:
    """An arrangement from tags has no release to be missing anything from."""
    assert _rows({"evidence": []}) == []
