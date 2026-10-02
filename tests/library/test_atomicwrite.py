"""The file at the path is always whole: the old one, or the new one.

``mutagen``'s ``save()`` rewrites the container in place, and this project's
promise is that no write to the library can leave a file half-made. What is
proven here is the failure case, because the success case looks identical
either way and is not what this exists for.
"""

import shutil
import sys
from pathlib import Path

import pytest

from diglibrary.library.atomicwrite import replacing


def _file(path: Path, body: bytes = b"the original recording") -> Path:
    path.write_bytes(body)
    return path


def test_a_write_that_dies_leaves_the_original_untouched(tmp_path: Path) -> None:
    """The power cut, the force quit, the drive pulled out mid-album."""
    song = _file(tmp_path / "01. Zorvelim.flac")

    def ruin_it(copy: Path) -> None:
        copy.write_bytes(b"half a f")
        raise OSError("the drive went away")

    with pytest.raises(OSError, match="the drive went away"):
        replacing(song, ruin_it)

    assert song.read_bytes() == b"the original recording"
    assert list(tmp_path.iterdir()) == [song], "no half-written copy may be left beside the music"


def test_an_interruption_is_caught_too(tmp_path: Path) -> None:
    """A run stopped by hand is exactly when a stray copy must not survive.

    ``KeyboardInterrupt`` is not an ``Exception``, so a bare ``except Exception``
    here would clean up after every failure except the one a person causes.
    """
    song = _file(tmp_path / "01. Zorvelim.flac")

    def interrupted(copy: Path) -> None:
        copy.write_bytes(b"partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        replacing(song, interrupted)

    assert song.read_bytes() == b"the original recording"
    assert list(tmp_path.iterdir()) == [song]


def test_the_written_file_takes_the_original_s_place_and_keeps_its_mode(tmp_path: Path) -> None:
    """What lands is the copy, named and permissioned as the original was."""
    song = _file(tmp_path / "01. Zorvelim.flac")
    song.chmod(0o640)

    replacing(song, lambda copy: copy.write_bytes(b"the retagged recording"))

    assert song.read_bytes() == b"the retagged recording"
    assert song.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [song]


def test_the_copy_keeps_the_suffix_because_the_name_says_what_it_is(tmp_path: Path) -> None:
    """``mutagen`` decides the container partly by the name it is given.

    A copy called `.foo.tmp` would be opened as an unknown format, so the write
    would fail on every file rather than on none.
    """
    song = _file(tmp_path / "01. Zorvelim.flac")
    seen: list[str] = []

    def note(copy: Path) -> None:
        seen.append(copy.suffix)
        shutil.copy2(song, copy)

    replacing(song, note)
    assert seen == [".flac"]


def test_the_bytes_reach_the_disk_before_the_rename_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.replace`` is a promise about the directory entry and nothing else.

    Without a flush the entry can be on the disk while the bytes it points at
    are still in the page cache: the file exists, at full length, holding
    zeros — and the original is gone, because the copy was the only other one
    and the rename consumed it. That is the exact loss this module exists to
    prevent, on the exact hardware its docstring names, so the order is what is
    asserted: the content is flushed, *then* the rename happens.
    """
    song = _file(tmp_path / "01. Zorvelim.flac")
    order: list[str] = []
    real_fsync, real_replace = __import__("os").fsync, __import__("os").replace

    def note_fsync(descriptor: int) -> None:
        order.append("fsync")
        real_fsync(descriptor)

    def note_replace(source: object, destination: object) -> None:
        order.append("replace")
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr("diglibrary.library.atomicwrite.os.fsync", note_fsync)
    monkeypatch.setattr("diglibrary.library.atomicwrite.os.replace", note_replace)

    replacing(song, lambda copy: copy.write_bytes(b"the retagged recording"))

    assert order[0] == "fsync", "the bytes are put on the disk before the entry points at them"
    assert "replace" in order
    assert order.index("fsync") < order.index("replace")
    assert song.read_bytes() == b"the retagged recording"


def test_the_finder_icon_survives_a_rewrite(tmp_path: Path) -> None:
    """The small cover the Finder draws on an audio file is not in the audio file.

    It is a custom icon: an icon resource in `com.apple.ResourceFork` and the
    flag that says to use it in `com.apple.FinderInfo`. `shutil.copy2` copies
    extended attributes on Linux only, so on macOS a rewrite through a copy
    strips both unless they are carried across explicitly.
    """
    if sys.platform != "darwin":
        pytest.skip("Extended attributes are read through the macOS calls.")
    from diglibrary.library.xattrs import _write, names

    target = tmp_path / "track.flac"
    target.write_bytes(b"the recording")
    assert _write(target, "com.apple.ResourceFork", b"an icon lives here")
    assert _write(target, "com.apple.FinderInfo", b"\x00" * 8 + b"\x04" + b"\x00" * 23)

    replacing(target, lambda copy: copy.write_bytes(b"the recording, retagged"))

    assert target.read_bytes() == b"the recording, retagged"
    assert "com.apple.ResourceFork" in names(target), "the icon must outlive the write"
    assert "com.apple.FinderInfo" in names(target), "and the flag that draws it"
