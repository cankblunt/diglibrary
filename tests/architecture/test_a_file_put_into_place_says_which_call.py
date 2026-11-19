"""`rename` means two things, and only one of them is what a caller wanted.

Putting a finished file into its final name is one gesture with two possible
answers when the name is already taken: replace what is there, or refuse. POSIX
`rename` replaces; Windows refuses with an error. `os.replace` replaces on both.
So `Path.rename` is a call whose behaviour is chosen by the machine rather than
by the person who wrote it, and whichever of the two they wanted, they only get
it on one of the platforms.

Measured on a Windows runner: a copy of the database is named for the second it
was taken in, two taken inside one second want the same name, and the second one
raised `FileExistsError` from inside the `try` that reports a failed copy. The
copy was lost and the live database was left with none from that moment — on the
one path this project describes as what makes every write to a library safe to
offer. On this project's own platform the same line had always replaced, quietly
and correctly, so nothing here could have said a word about it.

Where a file must *not* be overwritten, the refusal is not left to the platform:
it is checked, before the move, by whoever knows what the file is.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent.parent / "src" / "diglibrary"

#: Where `Path.rename` is the call that is meant, with the reason it is.
#:
#: `library/executor.py` moves the files of somebody's music library. Its
#: `_rename` refuses an existing destination itself, before the move and by
#: name, because overwriting one of those files is the one thing this project
#: must never do by accident — so the platform never gets to decide, and the
#: call it reaches is irrelevant.
#:
#: `database/copies.py` moves the live database aside before putting a restored
#: one in its place. The name it moves it to is looked for until one is free —
#: the database's and both of its tail files' — before anything moves at all,
#: so `rename` is reached with nothing there to overwrite. Keeping the call
#: that refuses a taken name is then the point: choosing and moving are two
#: steps, and on the platform whose `rename` replaces, a name taken in between
#: would be a database written over in silence.
EXEMPT = {"executor.py", "copies.py"}


def _renames(source: Path) -> list[int]:
    """Every `<something>.rename(...)` in one file, by line."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rename"
    ]


def test_a_file_put_into_place_does_not_leave_the_choice_to_the_platform() -> None:
    """`os.replace` where the name may be taken; an explicit refusal otherwise."""
    written: list[str] = []
    read = 0
    for source in sorted(PACKAGE.rglob("*.py")):
        if source.name in EXEMPT:
            continue
        read += 1
        written += [f"{source.name}:{line}" for line in _renames(source)]

    # A check that read no file passes, and proves nothing.
    assert read, f"no source was read under {PACKAGE}"
    assert written == [], (
        "these move a file with `rename`, which replaces what is there on one "
        "platform and refuses on another: use `os.replace` where replacing is "
        f"meant, or refuse the existing path by name before moving: {written}"
    )


def test_the_copy_of_the_database_is_put_into_place_by_the_call_that_replaces() -> None:
    """The line the Windows runner failed on, named so it cannot go back.

    An exemption covers the whole file, and this is the half of that file the
    measurement was about: the copy taken as the application opens, which has to
    land under its name whether or not a copy from the same second is there.
    """
    source = (PACKAGE / "database" / "copies.py").read_text(encoding="utf-8")
    taking = source[: source.index("def restore_copy")]

    assert "os.replace(partial, destination)" in taking, (
        "the finished copy is no longer put into place by the call that replaces, "
        "so on a platform whose `rename` refuses a taken name the copy is lost"
    )
    assert ".rename(" not in taking, (
        "something in the path that takes a copy moves a file with `rename` again: "
        "its behaviour when the name is taken is then the machine's to choose"
    )
