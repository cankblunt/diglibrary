"""Carrying a file's extended attributes across a rewrite, on macOS.

The Finder draws a small cover on an audio file's icon when the file carries a
**custom icon**: an icon resource in `com.apple.ResourceFork`, and the flag that
says to use it in `com.apple.FinderInfo`. It is not the embedded picture and it
is not `cover.jpg` — it is a third thing, and it is the only one of the three
the Finder shows for a FLAC.

A rewrite by copy-and-replace loses both attributes unless they are carried
by hand. One tag write is enough:

    before   com.apple.FinderInfo, com.apple.ResourceFork, com.apple.macl, …
    after    com.apple.provenance

`shutil.copy2` copies extended attributes only where CPython exposes the
`os.*xattr` calls, and that is Linux; on macOS they are absent and `copy2`
silently carries nothing. The picture inside the file survives such a write
and the icon drawn in the Finder does not.

The calls are reached through `ctypes` because CPython does not expose them
here. Everything is best effort by design: `com.apple.provenance` and
`com.apple.macl` are the kernel's own and refuse to be written, and a file that
loses one of those loses nothing a person can see.
"""

import ctypes
import ctypes.util
import sys
from pathlib import Path

_XATTR_NOFOLLOW = 0x0001
"""Act on the link itself, never on what it points at."""

_NOT_OURS = frozenset({"com.apple.provenance", "com.apple.macl", "com.apple.quarantine"})
"""Attributes the system owns: it writes them, refuses ours, and is right to."""


def _library() -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    name = ctypes.util.find_library("c")
    return ctypes.CDLL(name, use_errno=True) if name else None


_LIBC = _library()


def names(path: Path) -> tuple[str, ...]:
    """Return the extended attributes a file carries, or nothing when it cannot be asked."""
    if _LIBC is None:
        return ()
    encoded = str(path).encode()
    size = _LIBC.listxattr(encoded, None, 0, _XATTR_NOFOLLOW)
    if size <= 0:
        return ()
    buffer = ctypes.create_string_buffer(size)
    written = _LIBC.listxattr(encoded, buffer, size, _XATTR_NOFOLLOW)
    if written <= 0:
        return ()
    return tuple(name.decode() for name in buffer.raw[:written].split(b"\0") if name)


def _read(path: Path, name: str) -> bytes | None:
    assert _LIBC is not None
    encoded, key = str(path).encode(), name.encode()
    size = _LIBC.getxattr(encoded, key, None, 0, 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    if size == 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    read = _LIBC.getxattr(encoded, key, buffer, size, 0, _XATTR_NOFOLLOW)
    return buffer.raw[:read] if read >= 0 else None


def _write(path: Path, name: str, value: bytes) -> bool:
    assert _LIBC is not None
    return (
        _LIBC.setxattr(str(path).encode(), name.encode(), value, len(value), 0, _XATTR_NOFOLLOW)
        == 0
    )


_ICON = ("com.apple.ResourceFork", "com.apple.FinderInfo")
"""The two halves of a custom icon: the picture, and the flag that draws it."""

_ICON_TYPES = frozenset({b"icns", b"ICN#", b"ics#", b"il32", b"is32"})
"""Resource types that are an icon. `icns` is what the system writes today."""


def _holds_an_icon_resource(fork: bytes) -> bool:
    """Report whether a resource fork actually carries an icon.

    The fork's own type list is read, because the question is what is *in* it.
    An empty resource map is a valid fork holding nothing, and it is exactly
    what a stripped icon leaves behind: a fork of header and empty map under a
    `kHasCustomIcon` flag, so the Finder is told to draw an icon, goes
    looking, and finds no resource at all.

    The format is the classic one and it is fixed: a 16-byte header naming where
    the map begins, and 24 bytes into the map an offset to the list of types.
    Anything that does not parse is not an icon, which is the safe direction —
    a file wrongly said to have none is offered one, and offering only ever adds.
    """
    if len(fork) < 30:
        return False
    try:
        map_offset = int.from_bytes(fork[4:8], "big")
        map_length = int.from_bytes(fork[12:16], "big")
        if map_offset + map_length > len(fork) or map_length < 30:
            return False
        type_list = map_offset + int.from_bytes(fork[map_offset + 24 : map_offset + 26], "big")
        # `count - 1`, so an empty list is stored as 0xFFFF rather than as zero.
        stored = int.from_bytes(fork[type_list : type_list + 2], "big")
        if stored == 0xFFFF:
            return False
        for index in range(stored + 1):
            start = type_list + 2 + index * 8
            if start + 4 > len(fork):
                return False
            if fork[start : start + 4] in _ICON_TYPES:
                return True
    except (IndexError, ValueError):
        return False
    return False


def draws_its_own_icon(path: Path) -> bool:
    """Report whether the Finder already draws this file's own icon.

    Both halves have to be there *and* the fork has to hold an icon. Asked only
    whether the two attributes exist, a file wearing the flag over an empty
    fork answers yes: the application believes the icon is already drawn and
    plans nothing, while the Finder draws a blank icon.

    The same family as a picture block that declares `0x0` over a good JPEG:
    a declaration believed instead of the thing it describes.
    """
    carried = names(path)
    if not all(name in carried for name in _ICON):
        return False
    if _LIBC is None:
        return True
    return _holds_an_icon_resource(_read(path, "com.apple.ResourceFork") or b"")


def claims_an_icon(path: Path) -> bool:
    """Report whether the file *says* it has a custom icon, true or not.

    The two attributes being present, which is less than `draws_its_own_icon`
    asks. Kept as its own question because the gap between the two is a real
    state a file can be in — and one the system will not write over.
    """
    carried = names(path)
    return all(name in carried for name in _ICON)


def forget_own_icon(path: Path) -> None:
    """Take a custom icon away, leaving the file otherwise as it was.

    What undoes writing one. Only ever called on a file this application gave an
    icon to, which is why it can remove both halves without asking whose they
    were.
    """
    if _LIBC is None:
        return
    for name in _ICON:
        _LIBC.removexattr(str(path).encode(), name.encode(), _XATTR_NOFOLLOW)


def movable_names(path: Path) -> frozenset[str]:
    """Return the attributes a copy is expected to carry: all but the system's own."""
    return frozenset(names(path)) - _NOT_OURS


def carry_across(source: Path, destination: Path) -> tuple[str, ...]:
    """Copy *source*'s extended attributes onto *destination*, and say which landed.

    Called with the file about to be replaced and the copy about to take its
    place, so the custom icon, the Finder tags and the colour label survive a
    rewrite the way they survived an in-place save.
    """
    if _LIBC is None:
        return ()
    carried: list[str] = []
    for name in names(source):
        if name in _NOT_OURS:
            continue
        value = _read(source, name)
        if value is None:
            continue
        if _write(destination, name, value):
            carried.append(name)
    return tuple(carried)
