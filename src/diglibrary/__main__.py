"""``diglibrary`` opens the window; two subcommands create the application icon
and restore a copy of the database."""

import argparse
import platform
import sys
from pathlib import Path

# The subcommands that mean something other than *open the window*. Anything
# else, including no argument at all and every option the window takes, goes to
# the window unchanged: a subcommand nobody typed must not change what happens.
COMMANDS = ("make-icon", "restore-copy")

NOT_MACOS = """\
DigLibrary runs on macOS, and this is {system}. Nothing was changed.

A package index cannot refuse an installation by platform, so this is the first
place that could tell you: the window's folder panel, the call that keeps your
Mac awake through a download, the Finder tags an album's cover is written into,
and the application bundle itself are all written against macOS, and no other
system has been written yet.

Windows and Linux are intended. They are not promised and there is no date.
If you would like to be the reason one of them arrives, the source is at
https://github.com/cankblunt/diglibrary — open an issue first and say which one,
so that nobody writes the same thing twice.\
"""
"""The message shown on any system other than macOS.

`pip install diglibrary` succeeds on every system, because a pure-Python wheel
has no platform to refuse by. Without this message the first symptom on another
system would be a window that does not open, with no error and no explanation.

The condition is *not macOS* rather than a list of known systems, and the name
shown comes from `platform.system()`.
"""


def main() -> None:
    """Parse the command line and hand over to the window, or to a command."""
    # Checked before the subcommand is read, because none of them works on
    # another system either. This is the only platform check: a second one in a
    # subcommand would be a second statement of the same rule to keep in step.
    if sys.platform != "darwin":
        print(NOT_MACOS.format(system=platform.system() or sys.platform), file=sys.stderr)
        raise SystemExit(1)

    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:
        raise SystemExit(_command(sys.argv[1], sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="diglibrary",
        description="Open the DigLibrary window.",
        epilog=(
            "Other things it can do: "
            "`diglibrary make-icon` puts DigLibrary in your Applications folder "
            "so you never need this terminal again; "
            "`diglibrary restore-copy` lists the copies of your database and "
            "puts one back."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to the configuration file. Without one: ./config.toml when it "
            "is there, otherwise ~/.diglibrary/config.toml, written on first run."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true", help="Open the window with developer tools enabled."
    )
    arguments = parser.parse_args()

    from diglibrary.ui.app import run

    # Resolved inside `run` rather than here, because choosing the file can fail
    # the same way reading it can — a directory holding a database and no
    # settings is refused — and there is one place that turns a configuration
    # failure into a window someone can read.
    run(arguments.config, debug=arguments.debug)


def _command(name: str, arguments: list[str]) -> int:
    """Run one of the subcommands and return its exit status."""
    if name == "make-icon":
        return _make_icon(arguments)
    return _restore_copy(arguments)


def _make_icon(arguments: list[str]) -> int:
    """Assemble the icon for whichever installation is running."""
    parser = argparse.ArgumentParser(
        prog="diglibrary make-icon",
        description=(
            "Put DigLibrary in your Applications folder, so you can open it "
            "without this terminal. The icon launches the copy of DigLibrary you "
            "are running this from, on this Mac only — it is not an application "
            "you can send to anybody."
        ),
    )
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        default=None,
        help="Where to put it. Default: ~/Applications",
    )
    chosen = parser.parse_args(arguments)

    # No platform check here: `main` refuses every subcommand on a system that
    # is not macOS before it reads which one was typed.

    from diglibrary.desktop.bundle import make_icon

    # Whether one was already there is answered by `make_icon` rather than here,
    # because the window's button has to say the same two words and a question
    # answered in two places is answered differently by one of them.
    bundle, replaced = make_icon(chosen.destination)
    # The same two sentences the window says, word for word (`iconCreated` and
    # `iconReplaced` in `strings.js`); a change to one is a change to the other.
    # Neither promises the Dock, which this application cannot write to.
    print(f"{'Replaced' if replaced else 'Created'} {bundle}")
    print("Open it from your Applications folder, and drag it to the Dock if you want it there.")
    return 0


def _restore_copy(arguments: list[str]) -> int:
    """List the copies of the database, or put one back."""
    from diglibrary.desktop.restore import restore

    return restore(arguments)


if __name__ == "__main__":
    main()
