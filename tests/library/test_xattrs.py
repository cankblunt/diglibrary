"""The custom icon the Finder draws, and telling one apart from the claim of one.

The picture inside a FLAC is not what macOS shows in a folder window: that is a
custom icon, an icon resource in `com.apple.ResourceFork` under a flag in
`com.apple.FinderInfo`. Both halves can be present over a fork that holds
nothing, which is what a stripped icon leaves behind, and that must not be read
as *this file already has one*.
"""

import sys
from pathlib import Path

import pytest

from diglibrary.library.xattrs import _holds_an_icon_resource, _write, draws_its_own_icon

_ON_MACOS = sys.platform == "darwin"


def _fork(types: tuple[bytes, ...]) -> bytes:
    """A resource fork in the classic layout, holding one entry per type.

    Written out by hand rather than taken from a file on disk, so the test says
    what the shape *is*: a header naming where the map begins, and a map whose
    type list is what `_holds_an_icon_resource` reads. An empty list is stored as
    `0xFFFF`, because the count is kept as `count - 1`.
    """
    data_offset, map_offset = 256, 256
    type_list_offset = 28
    entries = b"".join(kind + b"\x00\x00" + b"\x00\x00" for kind in types)
    stored = 0xFFFF if not types else len(types) - 1
    type_list = stored.to_bytes(2, "big") + entries
    body = (
        b"\x00" * 16  # the map's own copy of the header
        + b"\x00" * 4  # next handle
        + b"\x00" * 2  # file reference
        + b"\x00" * 2  # attributes
        + type_list_offset.to_bytes(2, "big")
        + (type_list_offset + len(type_list)).to_bytes(2, "big")
        + type_list
    )
    header = (
        data_offset.to_bytes(4, "big")
        + map_offset.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + len(body).to_bytes(4, "big")
    )
    return header + b"\x00" * (map_offset - len(header)) + body


def test_a_fork_that_holds_an_icon_is_an_icon() -> None:
    assert _holds_an_icon_resource(_fork((b"icns",)))


def test_an_empty_resource_fork_is_not_an_icon() -> None:
    """A fork of header and empty map under a `kHasCustomIcon` flag holds no icon.

    The Finder is told to draw an icon, looks, and finds no resource at all. A
    guard that answers *it already has one* makes the planner skip a file that
    is drawn with a blank icon.
    """
    assert not _holds_an_icon_resource(_fork(()))


def test_a_fork_of_other_resources_is_not_an_icon() -> None:
    """A fork is not an icon merely by existing: it may hold anything at all."""
    assert not _holds_an_icon_resource(_fork((b"STR ", b"vers")))


def test_a_fork_that_does_not_parse_is_not_an_icon() -> None:
    """The safe direction: a file wrongly said to have none is *offered* one, and
    offering only ever adds. Believing a truncated fork would be the other way."""
    assert not _holds_an_icon_resource(b"")
    assert not _holds_an_icon_resource(b"\x00" * 29)
    assert not _holds_an_icon_resource(_fork((b"icns",))[:40])


def test_a_file_wearing_the_flag_over_an_empty_fork_does_not_draw_its_own_icon(
    tmp_path: Path,
) -> None:
    """The guard itself, not the parser under it.

    `_holds_an_icon_resource` can be right while nothing asks it. This writes
    the two attributes onto a real file and asks the question the planner asks.
    """
    if not _ON_MACOS:
        pytest.skip("extended attributes are read through the platform's own calls")
    audio = tmp_path / "01. Quellan's Drift.flac"
    audio.write_bytes(b"not really a flac, and this question never opens it")
    # `kHasCustomIcon` set, over a fork holding nothing — what a stripped icon
    # leaves behind.
    _write(audio, "com.apple.FinderInfo", b"\x00" * 8 + b"\x04\x00" + b"\x00" * 22)
    _write(audio, "com.apple.ResourceFork", _fork(()))
    assert not draws_its_own_icon(audio), (
        "the flag is a claim about an icon; believing it over an empty fork "
        "would skip a file that is drawn with a blank icon"
    )

    _write(audio, "com.apple.ResourceFork", _fork((b"icns",)))
    assert draws_its_own_icon(audio), "and a file that really has one is left alone"
