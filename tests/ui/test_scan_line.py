"""The line that stays after a scan, composed rather than read.

The line is joined from up to three parts, and one of them is a clause, not a
sentence. Joined as three finished sentences it reads:

    Scan finished — 1 album identified. — without MusicBrainz.org.

No static reading of `strings.js` or `app.js` can see that: every
string is correct on its own, and the fault is only in the joining. So this runs
the composition — the real `renderScanDone`, over the real `STR` — and reads what
the label would say.

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

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

# The elements this function writes to, and nothing else. A `$` that invented
# elements would let the function ask for one that is not in the markup, which
# `test_web_assets.py` is the guard for — here it must fail loudly instead.
HARNESS = """
const drawn = {};
const $ = (id) => {
  if (!(id in drawn)) drawn[id] = { textContent: "", hidden: false };
  return drawn[id];
};
"""


def _line(setup: str) -> dict:
    """Run `renderScanDone` over a state and return what it drew."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    strings = (WEB / "strings.js").read_text(encoding="utf-8")
    strings = strings.replace("export const STR", "const STR")
    found = re.search(r"function renderScanDone\(\) \{[\s\S]+?\n\}", script)
    assert found, "no renderScanDone() to run, so this guard is looking at the wrong file"
    body = HARNESS + strings + found.group(0) + setup + "\nrenderScanDone();"
    body += '\nconsole.log(JSON.stringify({ label: drawn["scan-done-label"].textContent }));'
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", body],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def _state(**facts: object) -> str:
    base = {
        "scanning": False,
        "scanFinished": True,
        "albums": [{}],
        "tab": "library",
        "scanIdentified": 1,
        "scanUntouched": 0,
        "coversPlanned": 0,
        "sourcesDown": {},
        "reach": None,
    }
    base.update(facts)
    return f"const state = {json.dumps(base)};\n"


def test_what_a_run_was_decided_without_is_part_of_the_sentence() -> None:
    """The clause continues the sentence, so no stop may come before it."""
    said = _line(_state(sourcesDown={"musicbrainz": "unreachable"}))["label"]

    assert said == "Scan finished — 1 album identified — without MusicBrainz.org."
    assert ". —" not in said, "a clause is continuing a sentence that had already ended"


def test_the_covers_are_their_own_sentence_and_come_after() -> None:
    """Three facts, and only two of them belong to one sentence.

    The covers inserted between the run and its own clause would put a whole
    sentence between a subject and the thing said about it.
    """
    said = _line(_state(coversPlanned=3, sourcesDown={"musicbrainz": "unreachable"}))["label"]

    assert said == (
        "Scan finished — 1 album identified — without MusicBrainz.org. "
        "3 coverless albums carry a picture in the files."
    )


def test_an_album_left_alone_keeps_its_clause_too() -> None:
    """The other sentence this clause can land on ends in a stop as well."""
    said = _line(_state(scanUntouched=2, sourcesDown={"discogs": "no_key"}))["label"]

    assert said == (
        "Scan finished — 1 album identified; 2 albums were already organized "
        "and left exactly as they are — without Discogs.com."
    )


def test_a_run_that_missed_nothing_says_nothing_about_sources() -> None:
    """The clause exists only when something was missing, and takes no stop with it."""
    said = _line(_state())["label"]

    assert said == "Scan finished — 1 album identified."
