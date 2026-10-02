"""The aside where the file's own spelling is on the table, run rather than read.

`spellingAside` is a handful of `createElement` calls with a state in
the middle of it, which is the shape no static guard over `ui/web/` can judge:
every line is valid whether the sentence agrees with the button beside it,
whether the button is wired at all, and — the one that matters here — whether
the way *out* of a decision is wired to the gesture that undoes it or to the one
that made it. Both states draw a link-button with a word in it, and the window
would look right either way.

The same approach `test_adopt_control.py` takes to the control beside it.

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

# Only what the aside touches. Anything it reaches for that is not here fails
# loudly, which is the point: a stub that answers everything proves nothing.
HARNESS = """
const made = [];
class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.type = "";
    this.listeners = {};
    made.push(this);
  }
  append(...kids) { this.children.push(...kids); }
  addEventListener(name, run) { this.listeners[name] = run; }
}
const document = { createElement: (tag) => new Node(tag) };
const STR = {
  planYours: (spelling) => `Your file says “${spelling}” — one word apart.`,
  planYoursBacked: (spelling, witness) =>
    `Your file says “${spelling}”, and ${witness} agrees with every title here.`,
  planYoursKeep: "Keep yours",
  planYoursKeepTip: "Write the spelling your file already carries.",
  planYoursStanding: (spelling) => `Kept: “${spelling}”.`,
  planYoursDrop: "Use the catalogue's",
  planYoursDropTip: "Withdraw your spelling and let the catalogue name this track again.",
  sourceName: (key) => ({ itunes: "iTunes" })[key] || key,
};
const kept = [];
const dropped = [];
function keepYourSpelling(position, spelling) { kept.push([position, spelling]); }
function dropYourSpelling(position) { dropped.push(position); }
const button = () => made.find((one) => one.tag === "button");
const press = () => button().listeners.click();
"""


def _run(body: str) -> dict:
    """Run `spellingAside` in node and report what it drew and what it called."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"function spellingAside\([^)]*\) \{[\s\S]+?\n\}", source)
    assert found, "no spellingAside() to run, so this guard is looking at the wrong file"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS + found.group(0) + body],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


_OFFER = '{ yours: "Druma Velto", yoursReason: "typo", yoursWitness: "", kept: "", position: 4 }'
_KEPT = '{ yours: "", yoursReason: "", yoursWitness: "", kept: "Druma Velto", position: 4 }'


def test_the_offer_says_both_spellings_and_writes_the_file_spelling_when_pressed() -> None:
    """The offer states the line the letters cannot settle, and gives no verdict."""
    said = _run(f"""
        spellingAside({_OFFER});
        press();
        console.log(JSON.stringify({{
          sentence: made.find((one) => one.tag === "span").textContent,
          label: button().textContent,
          kept,
          dropped,
        }}));
        """)

    assert said["sentence"] == "Your file says “Druma Velto” — one word apart."
    assert said["label"] == "Keep yours"
    assert said["kept"] == [[4, "Druma Velto"]], "the press writes the file's spelling on that line"
    assert said["dropped"] == []


def test_the_answered_line_says_what_stands_and_the_press_undoes_it() -> None:
    """The defect this was written for, and the one no reading would find.

    Both states draw a link-button holding a word, so an aside wired to
    `keepYourSpelling` in either state looks exactly like a working screen — the
    way back would rewrite the correction it was meant to withdraw, report
    success, and leave the album where it was.
    """
    said = _run(f"""
        spellingAside({_KEPT});
        press();
        console.log(JSON.stringify({{
          sentence: made.find((one) => one.tag === "span").textContent,
          label: button().textContent,
          kept,
          dropped,
        }}));
        """)

    assert said["sentence"] == "Kept: “Druma Velto”.", "it says what stands, not what was offered"
    assert said["label"] == "Use the catalogue's"
    assert said["dropped"] == [4], "and the press withdraws that line"
    assert said["kept"] == [], "it does not write the correction again"


def test_the_two_states_never_wear_each_others_clothes() -> None:
    """One area, two states, and nothing of one left standing in the other.

    A class the stylesheet reads to go quiet, a hover that explains the gesture
    on offer — both are written on every pass through here, because a property
    set in one branch and left standing in the other draws one state in the
    other's form.
    """
    said = _run(f"""
        const offering = spellingAside({_OFFER});
        const offeringButton = button();
        made.length = 0;
        const answered = spellingAside({_KEPT});
        console.log(JSON.stringify({{
          offering: offering.className,
          answered: answered.className,
          offeringTip: offeringButton.title,
          answeredTip: button().title,
          offeringButtonClass: offeringButton.className,
          answeredButtonClass: button().className,
        }}));
        """)

    assert said["offering"] == "plan-yours"
    assert said["answered"] == "plan-yours is-kept", "the stylesheet is told which state this is"
    assert said["offeringTip"] != said["answeredTip"], "each state explains its own gesture"
    assert "plan-yours-keep" in said["offeringButtonClass"]
    assert "plan-yours-drop" in said["answeredButtonClass"]
    assert "link-button" in said["answeredButtonClass"], "the way back is a link, never a button"


def test_the_witness_backed_offer_still_names_the_witness_and_not_the_word() -> None:
    """The second state leaves this alone: the evidence is named, never a verdict."""
    said = _run("""
        spellingAside({
          yours: "Zilda Porvim", yoursReason: "witness", yoursWitness: "itunes",
          kept: "", position: 4,
        });
        console.log(JSON.stringify({
          sentence: made.find((one) => one.tag === "span").textContent,
        }));
        """)

    assert said["sentence"] == (
        "Your file says “Zilda Porvim”, and iTunes agrees with every title here."
    )
