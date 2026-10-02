"""Put a copy of the database back, or list the copies there are.

    diglibrary restore-copy            # list them
    diglibrary restore-copy --newest   # restore the newest
    diglibrary restore-copy <file.gz>  # restore that one

`take_copy` writes the copies; this is what reads one back. Without it, using a
copy means knowing it is gzip, knowing where the live database goes, and
stopping the application by hand. It lives inside the package rather than in
`tools/`, because `tools/` is not part of the wheel and an installation by name
needs it as much as a checkout does.

**Close DigLibrary first.** This refuses to run while the database looks open,
because replacing a file SQLite is holding is how a good copy becomes two broken
ones.

Nothing is deleted. The database being replaced is renamed aside, and its name is
printed, so undoing a restore is one `mv`.
"""

import argparse
from pathlib import Path

from diglibrary.config.home import resolve
from diglibrary.config.loader import load_configuration
from diglibrary.database.copies import (
    CopyUnreadableError,
    available_copies,
    restore_copy,
)


def _asked(given: list[str]) -> argparse.Namespace:
    """Read the command line, and refuse a word this command does not know.

    A word beginning with `-` is an option, an option this command does not
    define is refused **by name**, and only a word that is not an option can be
    the name of a copy. Treating everything except the known option as a file
    name would make `--help` a search for a copy called `--help`. `argparse`
    also answers `--help` from the description below.
    """
    parser = argparse.ArgumentParser(
        prog="diglibrary restore-copy",
        description=(
            "List the copies DigLibrary keeps of your database, or put one back. "
            "Close DigLibrary first: replacing a file SQLite is holding is how a "
            "good copy becomes two broken ones. Nothing is deleted — the database "
            "being replaced is renamed aside and its name is printed."
        ),
    )
    parser.add_argument(
        "copy",
        nargs="?",
        default=None,
        help=(
            "Which copy to put back, named as one of the files listed. "
            "Without one, and without --newest, the copies are listed."
        ),
    )
    parser.add_argument(
        "--newest", action="store_true", help="Put the most recent copy back, without naming it."
    )
    return parser.parse_args(given)


def restore(given: list[str]) -> int:
    """List or restore, and say what happened either way."""
    asked = _asked(given)
    arguments = [asked.copy] if asked.copy is not None else []
    newest = asked.newest
    config = load_configuration(resolve(None))
    database = config.database.path
    folder = config.database.copies_directory
    copies = available_copies(folder)

    print(f"database  {database}")
    print(f"copies    {folder}")
    if not copies:
        print(
            "\nThere are no copies here yet. One is taken when the window opens on "
            "something that changed, and before any schema upgrade."
        )
        return 1

    if not arguments and not newest:
        print(f"\n{len(copies)} copies, newest first:\n")
        for _, size, path in copies:
            print(f"  {path.name}   {size / 1_048_576:.1f} MB")
        print("\nRestore the newest with --newest, or name one of the files above.")
        return 0

    chosen = copies[0][2] if newest else Path(arguments[0]).expanduser()
    if not chosen.is_absolute():
        chosen = folder / chosen
    # The application's own write-ahead file is the signal that it is running.
    # A stale one is possible after a crash, which is why this says what it saw
    # rather than deciding for whoever is reading.
    if database.with_name(database.name + "-wal").is_file():
        print(
            f"\n{database.name}-wal is present, which usually means DigLibrary is "
            f"open. Quit it and run this again. (If it crashed, the file can be "
            f"left over — open the application once and quit it to settle it.)"
        )
        return 1

    print(f"\nrestoring {chosen.name}")
    try:
        displaced = restore_copy(chosen, database)
    except CopyUnreadableError as error:
        print(f"\nrefused: {error}\nNothing was changed.")
        return 1
    print(f"done. the database that was there is now {displaced.name}")
    print("open DigLibrary and check the shelf before deleting it.")
    return 0
