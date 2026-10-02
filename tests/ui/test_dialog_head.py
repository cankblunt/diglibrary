"""The album dialog's head, run rather than read.

The head carries three things: the match chip, the release's own facts, and how
many of the album's track names each source publishes. Three sources of text in
one line is where a separator goes missing or a score is drawn for a source
that compared nothing.

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
function element() {
  return {
    textContent: "", className: "", hidden: false, childNodes: [], children: [],
    replaceChildren(...k) { this.childNodes = k; },
    append(...k) { this.childNodes.push(...k); },
    addEventListener() {},
  };
}
const document = { createElement: () => element() };
const line = element();
const $ = () => line;
const reportIfRefused = () => {};
const api = () => ({});
const flat = (n) => n.map((k) => (typeof k === "string" ? k : k.textContent || "")).join("");
"""


def _strings() -> str:
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _head(album: dict) -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in ("sourceName", "sourceLink", "scoreOf", "renderEssentials"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))
    program = (
        HARNESS
        + _strings()
        + "\n"
        + "\n".join(pieces)
        + ";\nrenderEssentials("
        + json.dumps(album)
        + ");\nconsole.log(JSON.stringify(flat(line.childNodes)));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


# One album as the payload carries it: a source in use and a witness.
ALBUM = {
    "year": 1974,
    "tracks": 12,
    "source": "discogs",
    "url": "https://www.discogs.com/release/1",
    "positions": [
        {
            "source": "discogs",
            "asked": True,
            "in_use": True,
            "titles_agreeing": 10,
            "titles_compared": 12,
        },
        {
            "source": "itunes",
            "asked": True,
            "witness": True,
            "in_use": False,
            "titles_agreeing": 12,
            "titles_compared": 12,
        },
    ],
}


def test_the_head_carries_what_each_source_publishes_of_the_names() -> None:
    """Each source is followed by how many track names it agrees on.

    `Discogs.com (10/12)` beside `iTunes (12/12)` is the tell this line exists
    to make readable: a witness naming more of the files than the source in use.
    """
    said = _head(ALBUM)

    assert said == "1974 · 12 tracks · using Discogs.com (10/12) · witnesses: iTunes (12/12)"


def test_a_source_that_compared_no_name_is_given_no_score() -> None:
    """`(0/0)` about a source still being asked is a number that means nothing.

    Written as *did it compare anything* rather than as a list of the states
    that have none — unasked, unreachable, still out — because the state nobody
    has met yet would otherwise be given a score.
    """
    still_out = {
        **ALBUM,
        "positions": [
            ALBUM["positions"][0],
            {"source": "itunes", "asked": False, "witness": True, "asking": True},
        ],
    }

    assert "(0/0)" not in _head(still_out)
    assert _head(still_out).endswith(
        "using Discogs.com (10/12)"
    ), "a source that was never asked is not a witness with a score"


def test_an_arrangement_says_where_its_words_came_from_and_scores_nothing() -> None:
    """An arrangement makes no claim for a catalogue to have a score about."""
    said = _head({"year": 1984, "tracks": 9, "source": "tags", "positions": []})

    assert "(" not in said
    assert "9 tracks" in said


def test_an_album_identified_while_a_source_was_out_says_so_in_its_own_line() -> None:
    """A source that was out is reported in the dialog, not in a shelf notice.

    A clause in the same grey line, only on an album whose own identification
    went without the source — and nothing at all on the ordinary album, where
    the absence of a failure is not a fact worth a word.
    """
    touched = dict(ALBUM, sources_missed={"musicbrainz": "unreachable"})
    line = _head(touched)
    assert "without MusicBrainz.org \u2014 it did not answer" in line

    assert "without" not in _head(ALBUM), "the ordinary album says nothing"
