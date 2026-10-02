"""What a plan cell hides, cascaded from the real stylesheet rather than read.

The trap this exists for: `.plan-cell input { display: none }` is written for
the name fields, which wait behind their pencils — and it is every input a plan
cell will ever hold. The number offered beside an added track can be built
correctly, wired correctly, and invisible: Move then asks for *another number*
with nothing on screen to type it into.

No static guard over `ui/web/` can see this. The markup is right, the script is
right, the string is right; a rule written about one thing reaches another, and
only running the stylesheet over the element answers it.

The cascade here is deliberately small: `display` only, class and tag selectors
only, source order as the tie-break. It answers the one question the defect
turned on — *is this element drawn* — and claims nothing else. The element is
given as an explicit chain rather than parsed out of markup, because guessing
which element a selector targets is the mistake a guard makes most easily:
reading `.plan-cell` as a rule about the input inside it.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _display(chain: list[dict[str, object]]) -> str:
    """Return the `display` the window's stylesheet gives the last of ``chain``.

    ``chain`` runs outermost first; each entry is a tag and the classes that
    element carries.
    """
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    harness = f"""
    // Comments first, or a selector arrives carrying the prose written above
    // it and every word of that prose is read as part of the selector.
    const css = {json.dumps(css)}.replace(/\\/\\*[\\s\\S]*?\\*\\//g, " ");
    const chain = {json.dumps(chain)};
    const rules = [...css.matchAll(/([^{{}}]+)\\{{([^}}]*)\\}}/g)]
      .map(([, selector, body]) => {{
        const found = /(?:^|;)\\s*display\\s*:\\s*([^;]+)/.exec(body);
        return found ? {{ selector: selector.trim(), value: found[1].trim() }} : null;
      }})
      .filter(Boolean);
    const matches = (part, node) => {{
      const bits = part.split(".");
      const tag = bits[0];
      if (tag && tag !== node.tag) return false;
      return bits.slice(1).every((name) => (node.classes || []).includes(name));
    }};
    const target = chain[chain.length - 1];
    let display = "inline";
    for (const rule of rules) {{
      for (const one of rule.selector.split(",").map((s) => s.trim())) {{
        const parts = one.split(/\\s+/);
        if (!matches(parts[parts.length - 1], target)) continue;
        // Every earlier part must be satisfied by some ancestor, in order.
        let at = 0;
        const reached = parts.slice(0, -1).every((part) => {{
          while (at < chain.length - 1) {{
            if (matches(part, chain[at++])) return true;
          }}
          return false;
        }});
        if (reached) display = rule.value;
      }}
    }}
    console.log(display);
    """
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return answer.stdout.strip()


ADOPT_NUMBER = [
    {"tag": "td", "classes": ["plan-after"]},
    {"tag": "div", "classes": ["plan-cell"]},
    {"tag": "span", "classes": ["plan-adopt"]},
    {"tag": "select", "classes": ["plan-adopt-number"]},
]

NAME_FIELD = [
    {"tag": "td", "classes": ["plan-after"]},
    {"tag": "div", "classes": ["plan-cell"]},
    {"tag": "span", "classes": ["plan-field"]},
    {"tag": "input", "classes": []},
]


def test_the_number_beside_an_added_track_is_drawn() -> None:
    """The button asks for a number, so the field it is typed into must be drawn."""
    assert (
        _display(ADOPT_NUMBER) != "none"
    ), "the position field is hidden by a rule written for the name fields"


def test_a_name_field_still_waits_behind_its_pencil() -> None:
    """And the rule it was scoped away from still does its own job."""
    assert _display(NAME_FIELD) == "none", "an unopened name field must stay closed"
