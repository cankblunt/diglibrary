"""Whether a name must change is a question about text, not about paths.

`Path == Path` answers *are these the same place*, and the answer is the
filesystem's: where case does not distinguish one entry from another, a name and
the same name in another case are one path. That is the right answer to the
question it is asked, and the wrong one to *does this have to be renamed* —
there the two names differ, and the whole of this project's naming standard is
about which case a name is written in.

Measured on a Windows runner: an album sitting under `Orquestra Da Serra`, with
a catalogue that says `Orquestra da Serra`, produced a plan with nothing in it.
Not a rename refused, not a rename that failed — no operation at all, for the
folder or for any of its files. On this project's own platform the same
comparison is case-sensitive, so the rule held and nothing here could say a word
about it.
"""

import ast
from pathlib import Path

PLANNER = (
    Path(__file__).resolve().parent.parent.parent / "src" / "diglibrary" / "library" / "planner.py"
)

#: Words that make an expression one of a path, near enough to ask about.
NAMES_A_PLACE = ("path", "folder", "destination", "source", "enclosing", "root", "file")

#: The comparisons that do ask *the same place*, where the filesystem's own
#: answer is the one wanted. Written as the function it sits in and the
#: expression, not as a line, so that moving one does not widen this — and not
#: as the expression alone, which is the mistake this very list made at first:
#: the loop over the files compares `destination == source` too, and an
#: exemption carrying only that text covered a second thing in silence. One
#: name, two meanings, found by running the guard against the code it was
#: written for before trusting it.
#:
#: `folder_name_is_free`: a name differing from the folder's own only in case
#: *is* that folder on a case-insensitive disk, and treating it as taken would
#: send an album to a pressing year instead of to its own corrected name. Its
#: docstring says so.
#:
#: `_disc_folder_renames`: whether a file sits directly in the album folder or
#: in a disc folder under it. One place or another, and nothing is renamed by
#: the answer.
EXEMPT = {
    ("folder_name_is_free", "destination == source"),
    ("_disc_folder_renames", "enclosing == album_folder"),
}


def _comparisons_between_two_places() -> list[str]:
    """Every `==`/`!=` in the planner with a path-looking name on both sides."""
    tree = ast.parse(PLANNER.read_text(encoding="utf-8"), filename=str(PLANNER))
    found = []
    for holder in ast.walk(tree):
        if not isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(holder):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            if not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
                continue
            sides = [node.left, node.comparators[0]]
            if not all(isinstance(side, (ast.Name, ast.Attribute)) for side in sides):
                continue
            written = ast.unparse(node)
            if (holder.name, written) in EXEMPT:
                continue
            if all(
                any(word in ast.unparse(side).lower() for word in NAMES_A_PLACE) for side in sides
            ):
                found.append(f"{holder.name}: {written}  (line {node.lineno})")
    return sorted(set(found))


def test_no_new_comparison_lets_the_filesystem_decide_whether_to_rename() -> None:
    """A path compared to a path, with neither of the two stated reasons."""
    found = _comparisons_between_two_places()

    assert found == [], (
        "these compare two paths directly, which asks the filesystem whether "
        "they are the same place: where the question is whether a name has to "
        f"change, ask `_already_named_that`: {found}"
    )


def test_a_name_in_another_case_is_not_the_name_the_catalogue_asks_for() -> None:
    """The helper itself, asked the question that was answered wrongly.

    Run everywhere, because the platform where this went wrong is not the one
    this is usually run on: the comparison it replaces gives the right answer
    here and the wrong one there, so a test that only exercised the behaviour
    would pass here however the comparison were written.
    """
    from diglibrary.library.planner import _already_named_that

    wanted = Path("/library/Trio - Orquestra da Serra")
    on_disk = Path("/library/Trio - Orquestra Da Serra")

    assert not _already_named_that(on_disk, wanted), (
        "an album written in another case is treated as already named the way "
        "the catalogue asks, so the rename that corrects it is never planned"
    )
    assert _already_named_that(wanted, wanted)
    assert not _already_named_that(
        Path("/library/Songs of the Sea"), Path("/library/Songs Of The Sea")
    )
