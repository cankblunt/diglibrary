"""What the Mixing screen says about a key reading, run rather than read.

The column is drawn from the margin *and the key it beat*, not from the
correlation — three words over five states, two of which draw quietly: a row
measured before the margin was kept, which is a reading taken with an earlier
profile, and a track with no key at all.

Every way this can be wrong is a way that reads as working. An old row given one
of the three words is a sentence about a scale that reading was never taken on;
a fresh row given the *stale* mark is the screen forgetting a measurement that
is right there; a number formatted through `mixOr` draws `or NaN`, and every one
of those lines is valid JavaScript. So this runs the function over stubs and
reads what it drew, the way `test_album_steps.py` does.

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

# The real strings, not stand-ins: half of what can go wrong here is a string
# reached with the wrong kind of argument, and a fake would answer anyway.
HARNESS = """
function element(tag) {
  return {
    tag,
    textContent: "",
    className: "",
    title: "",
    children: [],
    append(...kids) { this.children.push(...kids); },
    appendChild(kid) { this.children.push(kid); },
  };
}
const document = { createElement: element };
"""


def _strings() -> str:
    """`strings.js` as an object literal this harness can bind to `STR`."""
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _drawn(track: dict) -> dict:
    """Run `keySurenessCell` over one row and report the cell it built.

    `says` travels with the cell: the sentence each case would quote for this
    row, built by the window's own strings from the window's own table of cases,
    so a test names which sentence a tooltip is without copying its words.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for pattern in (
        r"const KEY_WEAK_BELOW = [\d.]+;",
        r"const KEY_STRONG_FROM = [\d.]+;",
        r"const KEY_CASES = \{[\s\S]+?\n\};",
        # The real wheel arithmetic, not a stand-in: whether the runner-up mixes
        # with the winner is half of what the word says, and a fake
        # `camelotNeighbours` would agree with whatever this test expected.
        r"function camelotNeighbours\(code\) \{[\s\S]+?\n\}",
        r"function runnerUpMixesWithIt\(track\) \{[\s\S]+?\n\}",
        r"function keyWasACloseCall\(track\) \{[\s\S]+?\n\}",
        r"function keySureness\(track\) \{[\s\S]+?\n\}",
        r"function keySurenessCell\(track\) \{[\s\S]+?\n\}",
    ):
        found = re.search(pattern, source)
        assert found, f"nothing in app.js matches {pattern}, so this guard is stale"
        pieces.append(found.group(0))
    program = (
        HARNESS
        + _strings()
        + "\n"
        + "\n".join(pieces)
        + ";\nconst row = "
        + json.dumps(track)
        + ";\nconst cell = keySurenessCell(row);"
        + "\nconst margin = row.key_margin === null ? null : row.key_margin.toFixed(2);"
        + "\ncell.says = {"
        + "\n  sure: Object.fromEntries(Object.entries(KEY_CASES).map("
        + "\n    ([name, one]) => [name, STR.mixKeySure(margin, one.howOften())])),"
        + "\n  twin: STR.mixKeyTwin(row.runner_up_camelot),"
        + "\n  apart: STR.mixKeyTwinApart(row.runner_up_camelot),"
        + "\n  stale: STR.mixKeyStale,"
        + "\n};"
        + "\nconsole.log(JSON.stringify(cell));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_a_decided_reading_wears_the_word_and_quotes_the_margin() -> None:
    """A margin of 0.20 is in the top band."""
    cell = _drawn({"camelot": "9A", "key_margin": 0.2, "runner_up_camelot": "4B"})

    assert len(cell["children"]) == 1, "a decided reading is not a close call and says nothing else"
    badge = cell["children"][0]
    assert badge["className"] == "badge badge-conf badge-conf-strong"
    assert "0.20" in badge["title"], "the tooltip stopped quoting the margin it is about"
    assert badge["title"] == cell["says"]["sure"]["strong"], "the tooltip is another band's"


def test_a_tie_between_two_keys_that_mix_is_not_the_worst_word() -> None:
    """9A and 8A are neighbours, so either answer mixes.

    A reading this close whose runner-up is adjacent agrees with a reference
    reading more often than the middle band does, and far more often than a tie
    between keys that do not mix. Giving it the worst word would be reading the
    margin without the runner-up stored right beside it.
    """
    cell = _drawn({"camelot": "9A", "key_margin": 0.01, "runner_up_camelot": "8A"})

    assert [kid["className"] for kid in cell["children"]] == [
        "badge badge-conf badge-conf-fair",
        "key-twin",
    ]
    badge = cell["children"][0]
    says = cell["says"]
    assert says["sure"]["mixableTie"] != says["sure"]["weak"], "the two ties are told one thing"
    assert (
        badge["title"] == says["sure"]["mixableTie"]
    ), "the tooltip says what holds for a different case than the row it is on"
    twin = cell["children"][1]
    assert twin["textContent"].strip() == "or 8A", "the second key drew as something else"
    assert "NaN" not in twin["textContent"], "a Camelot code went through a string that rounds"
    assert "8A" in twin["title"]
    assert twin["title"] == says["twin"] != says["apart"], "the sentence is the other tie's"


def test_a_tie_between_keys_that_do_not_mix_keeps_the_doubt() -> None:
    """9A and 3B are nowhere near each other, and this is the case that earns it.

    The sentence must not be the neighbour's: that one states what an adjacent
    runner-up measured, and quoting it here would be the screen citing a
    measurement of something else.
    """
    cell = _drawn({"camelot": "9A", "key_margin": 0.01, "runner_up_camelot": "3B"})

    assert [kid["className"] for kid in cell["children"]] == [
        "badge badge-conf badge-conf-weak",
        "key-twin",
    ]
    says = cell["says"]
    assert cell["children"][0]["title"] == says["sure"]["weak"]
    twin = cell["children"][1]
    assert twin["textContent"].strip() == "or 3B"
    assert "3B" in twin["title"]
    assert twin["title"] == says["apart"] != says["twin"], "the sentence is the neighbour's"


def test_a_reading_that_was_decided_enough_does_not_offer_a_second_key() -> None:
    """Above the bottom band the runner-up lost properly, and naming it is noise."""
    cell = _drawn({"camelot": "9A", "key_margin": 0.07, "runner_up_camelot": "8A"})

    assert [kid["className"] for kid in cell["children"]] == ["badge badge-conf badge-conf-fair"]


def test_a_key_measured_before_the_margin_existed_says_so() -> None:
    """The state this whole column turns on, and the one that draws quietly.

    A row with a key and no margin was measured with an earlier profile.
    Dressing it in one of the three words would be calibrating it on a scale it
    was never taken on.
    """
    cell = _drawn({"camelot": "9A", "key_margin": None, "runner_up_camelot": None})

    assert cell["children"] == [], "an old reading was given a badge"
    assert cell["textContent"] and cell["textContent"] != "—", (
        "an old reading draws exactly like a track with no key at all, which is "
        "two different facts wearing one mark"
    )
    assert cell["title"], "nothing on the screen says why it says nothing"
    assert cell["title"] == cell["says"]["stale"], "the reason given is another state's"


def test_a_track_with_no_key_is_not_an_old_reading() -> None:
    """Never measured and measured differently are two facts, and one mark each."""
    cell = _drawn({"camelot": None, "key_margin": None, "runner_up_camelot": None})

    assert cell["textContent"] == "—"
    assert cell["title"] == "", "a track that was never measured was told to measure again"
