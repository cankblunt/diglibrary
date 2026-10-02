"""An album taken off the Library is off it, and no set-shaped read counts it.

The ✕ on a card clears the album from the shelf: the row keeps its history and
its files, and the Library stops showing it. A query that counts the whole
`album_units` table therefore answers with a number the screen does not show,
and a cleared album goes on influencing the application. Every query that
answers about *the library* has to leave cleared rows out, and this walks the
file rather than trusting that today's queries are the only ones there will be.

Read per statement and never per method: a method that filters in its first
query and forgets in its second would pass a check on its body, and that second
query is exactly the shape being looked for. The statements come from the
parser, so SQL written as several adjacent quoted lines arrives whole.

The exemptions are judgements, listed with their reasons, because each one is a
question about what clearing means for that read.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

STORE = Path(__file__).resolve().parents[2] / "src/diglibrary/database/library_store.py"

ANSWERS_ABOUT_MORE_THAN_THE_SHELF = {
    # What this app organized is left alone by a later scan, and a cleared
    # album is still organized on the disk. Asking the shelf here would plan the
    # catalogue's name over a folder renamed by hand as soon as its card is
    # taken off the screen.
    "ids_in_state",
    # History still answers for everything that happened to a cleared album:
    # a reverted run is a fact about the run.
    "plan_history",
    "change_log",
    # Roots, not albums: which folders this app has seen, for the picker.
    "known_parents",
    # Which folder each row holds, for the search that follows a moved album.
    # `folder_path` is UNIQUE over the whole table, so a folder a
    # cleared row still names is one no other row can be moved onto.
    "units_by_folder",
    # The highest id ever issued, which is how *recently added* is ordered.
    # A cleared row still used its number.
    "highest_unit_id",
    # Maps keyed by folder path, read by lookup and never counted: the export
    # report asks them about the albums a run touched, and the window asks them
    # about the album on screen. A key nobody asks for costs nothing, and a run
    # may have touched an album that has since been cleared.
    "album_witnesses",
    "album_sources",
}

# A read about one album that was pointed at is not a read about the library. Both
# spellings of that question count: a bound parameter, or a hole list built for
# one.
ABOUT_ONE_ALBUM = re.compile(
    r"(?:\bid|album_unit_id|unit_signature|folder_path)\s*(?:=|IN)\s*[?(]", re.IGNORECASE
)


def _queries() -> list[tuple[str, str]]:
    """Every SQL statement this store executes, paired with the method it is in."""
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not inner.args:
                continue
            called = inner.func
            if not isinstance(called, ast.Attribute) or called.attr != "execute":
                continue
            first = inner.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((node.name, first.value))
            elif isinstance(first, ast.JoinedStr):
                # An f-string builds the hole list; the words around it are what
                # this reads, and they carry the WHERE.
                found.append(
                    (
                        node.name,
                        "".join(
                            part.value
                            for part in first.values
                            if isinstance(part, ast.Constant) and isinstance(part.value, str)
                        ),
                    )
                )
    return found


def test_every_read_about_the_library_leaves_out_what_was_cleared() -> None:
    """A statement that reads the album table as a set and never says `cleared_at`."""
    statements = _queries()
    assert any("album_units" in sql for _, sql in statements), (
        "no statement in the store mentions the album table, so this guard is "
        "reading the wrong file or the parser stopped finding queries"
    )

    offenders = [
        f"{name}: {' '.join(sql.split())[:72]}"
        for name, sql in statements
        if "album_units" in sql
        and "SELECT" in sql.upper()
        and "cleared_at" not in sql
        and not ABOUT_ONE_ALBUM.search(sql)
        and name not in ANSWERS_ABOUT_MORE_THAN_THE_SHELF
    ]

    assert offenders == [], (
        "these read the album table as a set and count albums that were taken off "
        f"the Library, so a screen built on them says a number nobody can see: {offenders}"
    )


def test_the_exemptions_are_all_real_methods() -> None:
    """A name that no longer exists is an exemption protecting nothing.

    A method renamed out from under the list would leave the judgement
    standing over nothing and the new name unguarded.
    """
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    missing = sorted(ANSWERS_ABOUT_MORE_THAN_THE_SHELF - names)

    assert missing == [], f"exempted methods that no longer exist: {missing}"
