"""Writing tags and pictures into an audio file without ever half-writing it.

``mutagen``'s ``save()`` rewrites the container **in place**. For a tag that no
longer fits the padding, and always for an embedded cover, that means shifting
the audio stream itself: the file is opened, bytes are moved, and until the last
one lands the file on disk is neither the old one nor the new one.

Every write to the library is preceded by a reversible backup, and for tags
that backup is a record of the previous *values*, replayed to undo a change.
That serves a change that completed and is no use for one that did not: a power
cut, a force quit, or an external drive unplugged in the middle of an album
leaves a truncated file, and a dictionary of the tags it used to have cannot
rebuild it.

So the write happens on a copy beside the file and lands with ``os.replace``,
which within one directory is atomic: at every instant the path holds either the
whole original or the whole new file. An interruption costs the work and a
temporary file, never the recording.

The copy is what it costs, and it is less than it sounds. A tag write that
fits the padding rewrites only the header in place, so the copy is the whole
added cost there; a cover embed moves the audio stream either way, so the two
routes cost nearly the same. On APFS ``copy2`` clones rather than reads and
writes, so the bytes are shared until one side is written to. On a filesystem
without cloning it is one sequential copy, which is still the cheapest thing in
an apply that has already decoded the audio to measure it.
"""

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from diglibrary.library.xattrs import carry_across


def replacing(path: Path, write: Callable[[Path], None]) -> None:
    """Run *write* against a copy of *path*, then put the result in its place.

    The temporary file is made in the same directory, because ``os.replace`` is
    only atomic within one filesystem and a system temporary folder is often a
    different one. It is named with a leading dot and this file's own stem, so a
    crash leaves something recognisable rather than an anonymous `tmp` beside
    the music.

    The suffix is kept: ``mutagen`` decides what a file *is* partly by its name,
    and a copy called `.foo.tmp` would be opened as an unknown container.
    """
    handle, made = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    os.close(handle)
    temporary = Path(made)
    try:
        # `copy2` rather than `copy`: it carries the permission bits and the
        # times across. Writing to the copy moves its modification time forward
        # on its own, which is what an in-place save did too.
        shutil.copy2(path, temporary)
        write(temporary)
        # And the extended attributes by hand, because `copy2` does not carry
        # them on macOS — CPython exposes the `os.*xattr` calls on Linux only,
        # so on this platform it silently copies none. Without this, one tag
        # write strips the small cover the Finder draws on an audio file, which
        # lives in `com.apple.ResourceFork` with a flag in
        # `com.apple.FinderInfo`.
        # Read from the file being replaced rather than from the copy, since
        # `write` may itself have rewritten the copy from scratch.
        carry_across(path, temporary)
        _flush(temporary)
        os.replace(temporary, path)
        _flush_directory(path.parent)
    except BaseException:
        # Including KeyboardInterrupt and SystemExit: a run being stopped is
        # exactly when a stray half-written copy must not be left in the folder.
        temporary.unlink(missing_ok=True)
        raise


def _flush(path: Path) -> None:
    """Put this file's bytes on the disk itself, not in the page cache.

    ``os.replace`` makes the *rename* atomic, which is a promise about the
    directory entry and about nothing else. Without this, the entry can be on
    the disk while the bytes it points at are not: the file exists, at full
    length, holding zeros — and the original is gone, because the copy was the
    only other one and the rename consumed it. That is the loss this module
    exists to make impossible, and an external drive unplugged in the middle of
    a run is where it would happen.
    """
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def _flush_directory(folder: Path) -> None:
    """Put the *rename* on the disk, having already put the bytes there.

    A directory is a file too, and until it is flushed the entry pointing at
    the new content is only in memory. Failure is ignored on purpose: not every
    filesystem lets a directory be opened for this, and on one that does not,
    the bytes are still safe from `_flush` — this is the second belt, not the
    only one.
    """
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
