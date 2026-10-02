"""Keeping a folder of discardable files under a ceiling, oldest out.

Three folders hold things the application can always make again: rendered
spectrograms, catalogue responses, and staged pictures. Without a ceiling they
only grow: the metadata cache expires entries by time, but only *as they are
read*, so a release nobody asks for again is a file that is never removed.

The rule is a ceiling in megabytes, and what goes is what has not been *looked
at* for longest rather than what was written first. That distinction is the
reason a read touches the file.

This module does not decide what is discardable. It is handed a folder and a
set of suffixes, and it takes the oldest of those until the rest fits. It is
never pointed at a folder of the music library, and the suffixes are the second
lock on that.
"""

import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path

MEGABYTE = 1024 * 1024


def _entries(directories: Iterable[Path], suffixes: Iterable[str]) -> list[Path]:
    """Every cached file directly inside these folders, and nothing below them.

    Several folders rather than one because a budget is about disk, not about
    layout: the staged pictures and the sleeve thumbnails the window draws are
    two folders of the same discardable thing, and giving each its own ceiling
    would mean the number in Settings is not the number on disk.

    Never recursive. What is one level down from a folder this is pointed at is
    not this module's business, and `iterdir` is what keeps that true.
    """
    wanted = {suffix.casefold() for suffix in suffixes}
    found: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        found.extend(
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix.casefold() in wanted
        )
    return found


def folder_bytes(directories: Iterable[Path], suffixes: Iterable[str]) -> int:
    """Return what this folder currently holds, so the window can say it."""
    total = 0
    for entry in _entries(directories, suffixes):
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def touch(path: Path) -> None:
    """Mark a file as looked at, without reading or rewriting it.

    A cache hit has to say so, or the ceiling discards by age of *writing* and
    throws away exactly what is being used most.
    """
    try:
        now = time.time()
        os.utime(path, (now, now))
    except OSError:
        # A cache that cannot be touched is still a cache. Losing the hint costs
        # accuracy in what gets discarded; failing here would cost the answer.
        pass


def enforce_folder_limit(
    directories: Iterable[Path],
    limit_bytes: int,
    suffixes: Iterable[str],
    logger: logging.Logger,
    operation: str,
) -> int:
    """Discard the least recently used files until the folder fits, and say what is left.

    Ordered by modification time, which ``touch`` keeps meaning *last used*. A
    file that vanishes underneath this — two windows, or a job writing while
    this runs — is simply skipped: it is already not taking up room.
    """
    entries = []
    for entry in _entries(directories, suffixes):
        try:
            status = entry.stat()
        except OSError:
            continue
        entries.append((status.st_mtime, status.st_size, entry))
    entries.sort(key=lambda item: item[0])

    held = sum(size for _, size, _ in entries)
    discarded = 0
    for _, size, entry in entries:
        if held <= limit_bytes:
            break
        try:
            entry.unlink()
        except OSError:
            continue
        held -= size
        discarded += 1
    if discarded:
        logger.info(
            "Cached files were discarded to stay under the folder's limit.",
            extra={
                "operation": operation,
                "discarded": discarded,
                "held_bytes": held,
                "limit_bytes": limit_bytes,
            },
        )
    return held


def clear_folder(directories: Iterable[Path], suffixes: Iterable[str]) -> int:
    """Delete every cached file of these kinds here, and return the bytes freed.

    Only this folder, never below it, and only the suffixes asked for. The rule
    that makes `Clear downloaded` safe is the rule that makes this safe: pointed
    anywhere else, it would delete music files.
    """
    freed = 0
    for entry in _entries(directories, suffixes):
        try:
            size = entry.stat().st_size
            entry.unlink()
        except OSError:
            continue
        freed += size
    return freed
