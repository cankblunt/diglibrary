"""The Finder's tags on an album folder: what it carries, and every tag that exists.

A folder's tags are one extended attribute, `com.apple.metadata:_kMDItemUserTags`,
holding a binary property list of strings. A coloured tag is its name, a line
break and the colour's number (`listen\\n1` is grey); a tag with no colour is its
name alone. That is the form the Finder itself writes, measured on real folders,
and it is the form written here.

The Finder keeps no list of its tags that another application can read, so the
tags that exist are the tags in use: Spotlight answers which items carry any,
and each one's attribute says which, with its colour. A tag created and never
put on anything is therefore not offered — it exists in the Finder's sidebar and
nowhere a program can see.

**A name is compared composed.** One accented word can be stored as a single
character or as a letter followed by a combining accent; the screen draws both
the same, and the Finder treats them as two tags. Every name this module
returns, and every name it writes, is the composed form.

Nothing here is reached on a system without extended attributes: every reader
answers ``None`` there, and the window draws no Finder row.
"""

import plistlib
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from diglibrary.library.xattrs import read_attribute, remove_attribute, write_attribute
from diglibrary.library.xattrs import supported as attributes_supported

ATTRIBUTE = "com.apple.metadata:_kMDItemUserTags"

FINDER_PREFERENCES = Path.home() / "Library" / "Preferences" / "com.apple.finder.plist"
"""Where the Finder keeps the tags shown first in its menus, in their order."""

_SPOTLIGHT_QUERY = ("mdfind", "kMDItemUserTags == '*'")


@dataclass(frozen=True, slots=True)
class FinderTag:
    """One tag: its name, composed, and the Finder's colour number (0 is none)."""

    name: str
    color: int = 0

    @property
    def entry(self) -> str:
        """The string the attribute stores for this tag."""
        return f"{self.name}\n{self.color}" if self.color else self.name


class FinderTagError(OSError):
    """The folder's tags could not be written as asked."""


def composed(name: str) -> str:
    """Return the one form a tag's name is compared and written in."""
    return unicodedata.normalize("NFC", name)


def parse_tag(entry: str) -> FinderTag:
    """Read one stored string as a tag, composing its name."""
    name, _, color = entry.partition("\n")
    return FinderTag(composed(name), int(color) if color.isdigit() else 0)


def supported() -> bool:
    """Return whether this system can carry Finder tags at all."""
    return attributes_supported()


def read_finder_tags(folder: Path) -> tuple[FinderTag, ...] | None:
    """Return the tags a folder carries, in its order, or ``None`` when it cannot be asked.

    A folder with no tags answers an empty tuple; ``None`` is reserved for a
    system or a path where the question has no answer, so the two never read
    alike.
    """
    if not supported() or not folder.is_dir():
        return None
    raw = read_attribute(folder, ATTRIBUTE)
    if raw is None:
        return ()
    return _decoded(raw)


def write_finder_tags(folder: Path, tags: Sequence[FinderTag]) -> None:
    """Make the folder carry exactly ``tags``, and refuse unless it then reads back so.

    An empty list removes the attribute, which is what the Finder does when the
    last tag is taken off.
    """
    if not supported():
        raise FinderTagError("This system keeps no Finder tags.")
    if tags:
        value = plistlib.dumps([tag.entry for tag in tags], fmt=plistlib.FMT_BINARY)
        accepted = write_attribute(folder, ATTRIBUTE, value)
    else:
        accepted = remove_attribute(folder, ATTRIBUTE)
    if not accepted or read_finder_tags(folder) != tuple(tags):
        raise FinderTagError(f"The Finder tags of {folder} could not be written.")


def with_tags_changed(
    current: Sequence[FinderTag], add: Sequence[FinderTag], remove: Iterable[str]
) -> tuple[FinderTag, ...]:
    """Return ``current`` with ``remove`` taken off and ``add`` put after what stays.

    Every tag already there keeps its place and its colour; an added tag the
    folder already carries is not added twice.
    """
    gone = {composed(name) for name in remove}
    kept = [tag for tag in current if tag.name not in gone]
    present = {tag.name for tag in kept}
    return (*kept, *(tag for tag in add if tag.name not in present))


def tags_taken_back(
    current: Sequence[FinderTag],
    written: Sequence[FinderTag],
    before: Sequence[FinderTag],
) -> tuple[FinderTag, ...]:
    """Return what undoing a change leaves on a folder that may have moved on since.

    A folder still carrying exactly what was written gets back exactly what it
    had. One the user has tagged since keeps those tags: only what the change
    wrote comes off, and what it took off goes back where it stood. Both are
    read from what the folder held before and after the write, not from what
    the plan asked for, because a tag the plan chose and the Finder had already
    put on was not the change's to take back.
    """
    if tuple(current) == tuple(written):
        return tuple(before)
    had = {tag.name for tag in before}
    wrote = {tag.name for tag in written}
    added = wrote - had
    removed = had - wrote
    result = [tag for tag in current if tag.name not in added]
    for index, tag in enumerate(before):
        if tag.name in removed and all(other.name != tag.name for other in result):
            result.insert(min(index, len(result)), tag)
    return tuple(result)


def finder_tag_catalogue(
    folders: Iterable[Path] = (),
    *,
    search: Callable[[], Sequence[Path]] | None = None,
    preferences: Path = FINDER_PREFERENCES,
) -> tuple[tuple[FinderTag, ...], tuple[FinderTag, ...]]:
    """Return the tags that exist: the Finder's favourites in its order, then the rest.

    What exists is what Spotlight finds in use, together with what ``folders``
    carry — the album folders this application knows, which answer even where
    Spotlight is off. The rest are in alphabetical order, compared without case
    or accents.
    """
    if not supported():
        return (), ()
    paths = [*(search or _spotlight)(), *folders]
    seen: dict[str, FinderTag] = {}
    for path in paths:
        for tag in read_finder_tags(path) or ():
            known = seen.get(tag.name)
            if known is None or (not known.color and tag.color):
                seen[tag.name] = tag
    first = [seen[name] for name in _favourites(preferences) if name in seen]
    named = {tag.name for tag in first}
    rest = sorted(
        (tag for tag in seen.values() if tag.name not in named),
        key=lambda tag: (_folded(tag.name), tag.name),
    )
    return tuple(first), tuple(rest)


def _decoded(raw: bytes) -> tuple[FinderTag, ...]:
    try:
        entries = plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError):
        return ()
    if not isinstance(entries, list):
        return ()
    return tuple(parse_tag(entry) for entry in entries if isinstance(entry, str) and entry)


def _spotlight() -> tuple[Path, ...]:
    try:
        completed = subprocess.run(
            _SPOTLIGHT_QUERY, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(Path(line) for line in completed.stdout.splitlines() if line)


def _favourites(preferences: Path) -> tuple[str, ...]:
    try:
        with preferences.open("rb") as handle:
            names = plistlib.load(handle).get("FavoriteTagNames", ())
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return ()
    return tuple(composed(name) for name in names if isinstance(name, str) and name)


def _folded(name: str) -> str:
    stripped = unicodedata.normalize("NFD", name)
    return "".join(ch for ch in stripped if not unicodedata.combining(ch)).casefold()
