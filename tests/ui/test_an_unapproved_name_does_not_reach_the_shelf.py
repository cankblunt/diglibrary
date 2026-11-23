"""The shelf's two naming functions, run rather than read.

A record this application proposed and was not confident enough to apply on its
own used to name the card exactly as a record it was sure of: a folder of loose
tracks appeared on the shelf as a record none of its files belong to. The server
decides this now and says it in one word, and the window names an album in
**two** functions — the line on the card and the tooltip spelled out — so a
guard that checks one of them checks half the rule.

Nothing here is visible in the source: both functions are valid JavaScript
whichever name they return. So they are extracted and run, the way
`test_album_steps.py` does.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from tests.ui.node_program import run_node

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

# `fullNameFor` asks `sameWords` whether the folder already says the name, so
# the real one comes along rather than a stub that would answer for it.
FUNCTIONS = ("fullNameFor", "onDiskName", "sameWords")


def _named(album: dict) -> dict:
    """Run both naming functions over one album and report what they drew."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    for name in FUNCTIONS:
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        pieces.append(found.group(0))
    program = (
        "\n".join(pieces)
        + ";\nconst album = "
        + json.dumps(album)
        + ";\nconsole.log(JSON.stringify({shelf: onDiskName(album), tip: fullNameFor(album)}));"
    )
    return json.loads(run_node(program).stdout.strip())


PROPOSAL = {
    "folder": "- a pile of singles (70s-80s) 5",
    "title": "Dusk",
    "artist": "Mickey Marlow",
    "year": 1978,
    "named_on_disk": None,
}


def test_an_unapproved_proposal_leaves_the_folder_on_the_card() -> None:
    """The card says what the album is called on the disk, and nothing more."""
    drawn = _named(PROPOSAL | {"name_is_unapproved": True})

    assert drawn["shelf"] == {
        "artist": "",
        "title": "- a pile of singles (70s-80s) 5",
        "year": "",
    }
    assert drawn["tip"] == "- a pile of singles (70s-80s) 5"
    assert "Dusk" not in json.dumps(drawn)


def test_a_record_this_application_stands_behind_still_names_the_card() -> None:
    """The refusal is about an unapproved proposal, not about every proposal.

    An album identified above the threshold is named on the shelf before it is
    organized — that is how the card says what it is about to become.
    """
    drawn = _named(PROPOSAL | {"name_is_unapproved": False})

    assert drawn["shelf"]["title"] == "Dusk"
    assert drawn["shelf"]["artist"] == "Mickey Marlow"
    assert drawn["tip"].startswith("Mickey Marlow — Dusk")


def test_an_organized_album_is_still_named_by_its_folder() -> None:
    """The older rule is untouched: what is on the disk wins over everything."""
    drawn = _named(
        {
            "folder": "Banda Zinco Pardo - Lenha Zorca (1974) [FLAC]",
            "title": "Lenha Zorca",
            "artist": "Banda Zinco Pardo",
            "year": 1974,
            "named_on_disk": {
                "artist": "Banda Zinco Pardo",
                "title": "Lenha Zorca",
                "year": "1974",
            },
            "name_is_unapproved": True,
        }
    )

    assert drawn["shelf"]["title"] == "Lenha Zorca"
    assert drawn["tip"] == "Banda Zinco Pardo - Lenha Zorca (1974) [FLAC]"
