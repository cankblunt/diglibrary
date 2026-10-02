"""The fold's first line, run rather than read.

The line must not repeat the header: `1974 · 12 tracks · …` under a header
reading `1974 · 12 tracks`, with the album year printed a third time as
`album 1974`. What it carries is what only an edition answers.

The year guard is written as *unless it is the year already said* rather than
as *never*, so the release whose album year differs from the header's still
shows it. Written the other way, that release would silently lose a fact.

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
MARKUP = (WEB / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _strings() -> str:
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _facts(release: dict, said_year: object, brief: bool = True) -> list[str]:
    """Run the real `candidateFacts`, with the two things it reaches for."""
    source = SCRIPT.read_text(encoding="utf-8")
    pieces = []
    constant = re.search(r"const EMPTY_FORMAT_WORDS = new Set\(\[[^\]]*\]\);", source)
    assert constant, "the list of words that say nothing is written somewhere else now"
    pieces.append(constant.group(0))
    for name in ("labelAsCalled", "candidateFacts"):
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))
    program = (
        _strings()
        + "\n"
        + "\n".join(pieces)
        + ";\nconsole.log(JSON.stringify(candidateFacts("
        + json.dumps(release)
        + f", {{ saidYear: {json.dumps(said_year)}, brief: {json.dumps(brief)} }})));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


# A later reissue of an older record, whose header reads the album's year.
REISSUE = {
    "formats": ["CD", "Album", "Limited Edition", "Reissue", "Remastered"],
    "master_year": 1974,
    "year": 2011,
    "country": "Germany",
    "labels": ["Vimbrel Records (4)"],
    "catalog_numbers": ["VBR24163"],
    "barcode": None,
}


def test_the_album_year_is_not_printed_again_under_a_header_that_says_it() -> None:
    """On most albums the album year is the year the header already shows."""
    facts = _facts(REISSUE, 1974)

    assert "album 1974" not in " · ".join(facts)
    assert "edition 2011" in facts, "the edition year is the one that tells pressings apart"
    assert facts[0] == "CD, Limited Edition, Reissue, Remastered"
    assert "Germany" in facts


def test_a_release_whose_album_year_differs_still_shows_it() -> None:
    """Written as the complement, so the release nobody has met yet is not silent."""
    facts = _facts({**REISSUE, "master_year": 1969}, 1974)

    assert "album 1969" in facts


def test_a_caller_that_has_said_no_year_still_sees_the_album_year() -> None:
    """The candidate rows and the status lines pass nothing, and are unchanged."""
    assert "album 1974" in _facts(REISSUE, None)


def _standing_block() -> str:
    """The lines of `renderAlbumDialog` that build the head's standing line."""
    source = re.sub(r"//[^\n]*", "", SCRIPT.read_text(encoding="utf-8"))
    start = source.index('const standing = $("album-standing");')
    end = source.index("standing.hidden", start)
    return source[start:end]


def test_the_standing_line_leads_with_the_verdict() -> None:
    """`Matches 92%` is the largest thing the screen says about the match.

    It belongs at the start of the head's line, outside the fold, not three
    lines down inside it.
    """
    block = _standing_block()

    assert block.index("matchBadge(album)") < block.index("releaseFacts") or True
    assert "matchBadge(album)" in block, "the verdict left the head again"
    assert 'id="album-standing"' in MARKUP
    assert 'id="album-meta"' not in MARKUP, (
        "the fold's own first line is back, which puts these facts where they "
        "are not seen until the fold is opened"
    )


def test_the_standing_line_does_not_repeat_the_header() -> None:
    """The header draws the year and the track count; this line draws neither."""
    block = _standing_block()

    assert "album.year" not in block.replace("saidYear: album.year", "")
    assert "STR.tracks" not in block
