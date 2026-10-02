"""Keeping the optional service keys where the environment can find them.

Some services need a key that cannot ship with an open-source application:
Discogs, whose token is personal, and AcoustID, whose key is registered per
application. Editing a shell profile or writing `~/.diglibrary/env` by hand is
a terminal step between installing the application and seeing it work, so the
window can write that file: connecting a service is pasting a key.

`~/.diglibrary/env` is sourced by the app bundle's launcher and documented in
the README. The application also reads it itself, through `load`: the window
writes the file however it was started, so the file has to be read however the
application was started, or a pasted key would be in force when launched from
the icon and absent when launched from a terminal.

A secret lives in an environment variable,
never in `config.toml`, never in a log or an exception message. This file *is*
the environment variable — it is the mechanism that supplies it — and nothing
here ever reads a value back out to the window: the only thing the interface is
ever told is whether a service is connected, never with what.

Written `0600`, and the folder `0700`, because a key in a world-readable file is
a key on a shared machine's disk.
"""

import os
import re
from pathlib import Path

ENV_FILE = Path("~/.diglibrary/env").expanduser()

DISCOGS = "DIGLIBRARY_DISCOGS_TOKEN"
ACOUSTID = "DIGLIBRARY_ACOUSTID_KEY"
SLSKD = "DIGLIBRARY_SLSKD_API_KEY"
SPOTIFY_ID = "DIGLIBRARY_SPOTIFY_CLIENT_ID"
SPOTIFY_SECRET = "DIGLIBRARY_SPOTIFY_CLIENT_SECRET"
"""The credentials of a Spotify application the user registers themselves.

A pasted link is read as words and nothing else — no value from Spotify is
stored, compared or written, so this key buys precision and never content.
Without it an album link still resolves, by its title alone; a track
link cannot resolve at all, because the public endpoint does not name the album
a track belongs to.
"""

SETTABLE = (DISCOGS, ACOUSTID, SLSKD, SPOTIFY_ID, SPOTIFY_SECRET)
"""What the window is allowed to write.

A closed list, so a name arriving from the page can never make this write an
arbitrary line into a file that a shell sources at launch.
"""

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=")


def connected() -> dict[str, bool]:
    """Report which services have a key, and never what the key is."""
    return {name: bool(os.environ.get(name)) for name in SETTABLE}


def store(name: str, value: str) -> None:
    """Write one key into the environment file, and into this process.

    Both, because they answer different moments: the file is what the next
    launch reads, and `os.environ` is what the request happening ten seconds
    from now reads. Writing only the file would mean a token that works
    tomorrow and does nothing today, which reads as a token that was rejected.

    An empty value removes the line — the way to disconnect a service is the
    same field, emptied.
    """
    if name not in SETTABLE:
        raise ValueError(f"Not a key this application stores: {name}")
    value = value.strip()

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(ENV_FILE.parent, 0o700)
    kept = [
        line
        for line in _existing()
        # Rewritten rather than appended: a file that accumulates three
        # generations of the same token is one where what is in force depends on
        # which line the shell read last.
        if not (_LINE.match(line) and _LINE.match(line).group(1) == name)
    ]
    if value:
        kept.append(f'export {name}="{_escaped(value)}"')

    # Written through a neighbouring file and moved into place, so an
    # interruption cannot leave a shell sourcing half a line at the next launch.
    temporary = ENV_FILE.with_name(f".{ENV_FILE.name}.new")
    temporary.write_text("\n".join(kept) + "\n" if kept else "", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, ENV_FILE)

    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def load() -> int:
    """Put what this file holds into this process, and say how many names moved.

    The window writes this file however the application was started, so it is
    read the same way. The app bundle's launcher `.`-sources it; a launch from a
    terminal has no launcher, and without this function a key pasted into
    Settings would work from the icon and be absent from a terminal.

    **A name already in the environment is left alone.** A variable exported in
    a shell is an explicit choice, and this file must not override it. It also
    makes this a no-op under the icon, where the launcher has already sourced
    the same file.

    **Every assignment is read, not only the names this application writes.**
    The launcher sources the whole file, so both routes have to load the same
    set of names — the README documents this file for a MusicBrainz contact
    address as well. `SETTABLE` bounds what the *window* may write, because
    that name arrives from a page; nothing arrives from a page here.

    Nothing is logged but a count. The caller says a number of keys was read and
    never which, and never one character of one.
    """
    brought = 0
    for line in _existing():
        assignment = _LINE.match(line)
        if assignment is None:
            continue
        name = assignment.group(1)
        if name in os.environ:
            continue
        os.environ[name] = _unquoted(line[assignment.end() :])
        brought += 1
    return brought


def _unquoted(value: str) -> str:
    """Read one value the way the shell that sources this file would read it.

    The exact inverse of ``_escaped``, and it has to be: this file is written to
    be `.`-sourced, so anything read differently here than by `sh` is a key that
    works by one route and not the other.

    Double quotes are what `store` writes — a backslash before `\\`, `"`, `$` or
    `` ` `` is an escape and any other backslash is a character. Single quotes
    are never written here but may be typed by hand, and inside them the shell
    unescapes nothing. Unquoted, there is nothing to undo.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote, value = value[0], value[1:-1]
        if quote == "'":
            return value
    else:
        return value
    read: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value) and value[index + 1] in '\\"$`':
            read.append(value[index + 1])
            index += 2
            continue
        read.append(character)
        index += 1
    return "".join(read)


def _existing() -> list[str]:
    try:
        return [line.rstrip("\n") for line in ENV_FILE.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return []


def _escaped(value: str) -> str:
    """Make one value safe to sit inside double quotes in a sourced shell file.

    This file is `.`-sourced by the bundle's launcher, so a value carrying a
    quote, a backslash, a `$` or a backtick would stop being data and start
    being shell. None of the four appears in a Discogs or AcoustID key today,
    which is why it is handled here rather than assumed.
    """
    for character in ("\\", '"', "$", "`"):
        value = value.replace(character, f"\\{character}")
    return value
