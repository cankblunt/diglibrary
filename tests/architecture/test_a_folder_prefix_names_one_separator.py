"""A stored path is compared as text, and the separator is the platform's.

`audio_files.path` and `album_units.folder_path` are strings, so every question
of the form *is this file inside that folder* is answered by comparing the text
of one against the text of the other followed by a separator. That separator was
written as a literal `/` on ten lines — six in Python and four inside SQL —
which is correct on the system this grew up on and is nothing at all where paths
are written with a backslash.

Measured on a Windows runner: the prefix matched no row that has ever existed,
so every lookup of an album by its folder answered empty. What goes with it is
not small — finding an album renamed by hand, saying which files joined a folder
and which left it, recognising a second copy, keeping the word `organized` across
a rename, and carrying the rows when a folder moves — and none of it raised
anything. An empty answer is a valid answer to all of those questions.

So the separator is asked of the platform, and this refuses it being written down
again. In Python that is `os.sep`; in SQL it is a bound parameter, because a
query cannot call a function in this module.
"""

import os
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent.parent / "src" / "diglibrary" / "database"

#: `migrations.py` states the separator inside SQL that has already run. A
#: migration is a fact about the past: rewriting one changes nothing for a
#: database that applied it and is the riskier act for one that has not. The
#: statement it belongs to matches nothing on a fresh database anyway, because
#: the tables it reads are empty when it runs.
EXEMPT = {"migrations.py"}

SEPARATOR_WRITTEN_DOWN = re.compile(r"""(?:"/"|'/')""")


def test_no_stored_path_is_compared_against_a_separator_somebody_typed() -> None:
    """The separator comes from the platform, in Python and in SQL alike."""
    written: list[str] = []
    read = 0
    for source in sorted(PACKAGE.rglob("*.py")):
        if source.name in EXEMPT:
            continue
        read += 1
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if SEPARATOR_WRITTEN_DOWN.search(line):
                written.append(f"{source.name}:{number}")

    # A check that read no file passes, and proves nothing.
    assert read, f"no source was read under {PACKAGE}"
    assert written == [], (
        "these write the separator down, and a stored path is compared against "
        f"it as text: use `os.sep`, or bind it as a parameter in SQL: {written}"
    )


def test_a_folder_prefix_ends_in_the_separator_this_platform_writes() -> None:
    """Ending in it is what keeps one folder from taking its longer sibling.

    Without the separator, a prefix of `…/Name` also matches `…/Name Longer` —
    a different album, whose files would be weighed into this one and carried
    along when this one moved.
    """
    from diglibrary.database.library_store import _as_a_prefix

    base = str(Path("x") / "y")
    assert _as_a_prefix(base) == base + os.sep
    assert _as_a_prefix(base + os.sep) == base + os.sep, "a path already ending in it is unchanged"
    assert not _as_a_prefix(base).endswith(os.sep + os.sep)
