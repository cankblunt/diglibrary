"""Copies of the database, taken as the application opens and aged out by size.

Deliberately not called a *backup*: `backup/` in this application already means
the images a write replaced, which is what a revert puts back. A name this
project already uses never gets a second meaning, so these are copies of the
database and they live in their own folder.

**Never `shutil.copy`.** This database runs in WAL mode, and the window is
usually open while the copy is taken: the file on disk at any instant is
missing whatever is still in `-wal`, so a plain copy of it is a copy of a
database mid-sentence. `VACUUM INTO` reads through one connection's snapshot
and writes a complete, compacted database.

Then gzip, because it is nearly free and it changes what a ceiling buys: a
database of this shape compresses to a fraction of its size, so the same
ceiling holds several times as many versions.

**And every copy is read back before it is accepted.** Consistent by construction
is not the same as verified: a full disk or a truncated write leaves a file that
exists, is named what a restore looks for, and is not a database. The vacuumed
file is opened read-only for `PRAGMA integrity_check`, and the archive is
decompressed to nowhere so its trailing CRC is checked — a failure at this moment
costs a second, and the same failure found during a restore costs the restore.
"""

import gzip
import logging
import shutil
import sqlite3
import time
from pathlib import Path

COPY_SUFFIX = ".sqlite3.gz"
"""What a finished copy is called. A partial one is written under a different
name and renamed into place, so this suffix only ever names a complete file —
which is what lets the ceiling and the restore both trust what they find."""

DEFAULT_COPY_LIMIT_BYTES = 500 * 1024 * 1024
"""Enough to hold many versions of a database describing a large library."""


def take_copy(database: Path, folder: Path, logger: logging.Logger) -> Path | None:
    """Write one compressed copy of the database, or ``None`` when none was due.

    Returns ``None`` without writing anything when the database has not been
    written since the newest copy was taken. Opening the window twice to look
    at something is not a new version of anything, and a folder of identical
    copies is a folder whose ceiling discards real history to hold repetitions.
    """
    if not database.is_file():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    if not _changed_since_newest(database, folder):
        return None

    _sweep_leftovers(folder)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Written beside the destination and renamed, because a copy interrupted
    # half-written must not be findable by the name a restore looks for. The
    # rename is atomic within one filesystem, which these two share by
    # construction — the partial is made in the folder it will land in.
    partial = folder / f".diglibrary-{stamp}{COPY_SUFFIX}.partial"
    plain = folder / f".diglibrary-{stamp}.sqlite3.partial"
    destination = folder / f"diglibrary-{stamp}{COPY_SUFFIX}"
    try:
        # `mode=ro` so this can never be the thing that writes to the database,
        # and `VACUUM INTO` so what lands is consistent rather than whatever the
        # file happened to hold while the window was working.
        # `as_uri` rather than an f-string, because SQLite parses this itself:
        # it cuts the path at the first `?` or `#` and decodes `%XX`. A library
        # under `~/Music #2` — an ordinary folder name — would open `~/Music`
        # instead, **and lose `mode=ro` with the rest of the query**, so the
        # one guard keeping this path from writing to the live database would
        # be gone and what got copied would be a database SQLite had just
        # created empty.
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("VACUUM INTO ?", (str(plain),))
        finally:
            connection.close()
        # **Opened before it is trusted.** `VACUUM INTO` is consistent by
        # construction, which is not the same as verified: a full disk, a bad
        # sector or a truncated write produce a file that exists and is named the
        # thing a restore looks for. This is the only moment the answer is cheap:
        # the check runs against a copy nobody is writing to.
        _refuse_a_copy_that_cannot_be_read(plain)
        with plain.open("rb") as source, gzip.open(partial, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target)
        # And the compressed file is read back through gzip, because the CRC is at
        # the end of the stream: a truncated `.gz` is only discovered by
        # decompressing it, and discovering it here costs a second while
        # discovering it during a restore costs the restore.
        _refuse_an_archive_that_cannot_be_unpacked(partial, plain.stat().st_size)
        partial.rename(destination)
    except Exception:
        logger.exception(
            "The database could not be copied.",
            extra={"operation": "database.copy.failure"},
        )
        return None
    finally:
        # Both temporaries, whichever one the failure left behind.
        for leftover in (plain, partial):
            leftover.unlink(missing_ok=True)

    logger.info(
        "A copy of the database was taken.",
        extra={
            "operation": "database.copy.taken",
            "bytes": destination.stat().st_size,
            "path": str(destination),
        },
    )
    return destination


class CopyUnreadableError(RuntimeError):
    """Raised when a copy that was just written cannot be read back.

    Its own type so `take_copy` logs it as what it is — a copy that failed
    verification, not a database that failed to be read — and so a restore can
    name the same condition when it meets it later.
    """


def _refuse_a_copy_that_cannot_be_read(copy: Path) -> None:
    """Open the copy read-only and let SQLite say whether it is a database.

    Both of SQLite's answers arrive as one type here. A file that is a database
    with damage inside it *returns* a description of the damage, and a file that
    is not a database at all *raises* `DatabaseError` — and a caller deciding
    whether to move somebody's live library aside should not have to know which
    kind of not-a-copy it is holding.
    """
    try:
        connection = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
        try:
            answer = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise CopyUnreadableError(f"{copy.name} is not a database: {error}") from error
    if not answer or answer[0] != "ok":
        raise CopyUnreadableError(
            f"The copy just written to {copy.name} is not a sound database: "
            f"{answer[0] if answer else 'integrity_check said nothing'}."
        )


def _refuse_an_archive_that_cannot_be_unpacked(archive: Path, expected_bytes: int) -> None:
    """Decompress the archive to nowhere, and check it holds what it should.

    Read in blocks rather than into memory: this is a database, and one that no
    longer fits in RAM is exactly the one worth having a copy of.
    """
    unpacked = 0
    with gzip.open(archive, "rb") as source:
        while chunk := source.read(1024 * 1024):
            unpacked += len(chunk)
    if unpacked != expected_bytes:
        raise CopyUnreadableError(
            f"{archive.name} unpacks to {unpacked} bytes, not the {expected_bytes} "
            f"that were compressed into it."
        )


def _sweep_leftovers(folder: Path) -> None:
    """Remove partials a killed process left behind.

    The `finally` below clears both temporaries on every ordinary failure, and
    does not run when the process is killed or the machine loses power. What is
    left is the *uncompressed* intermediate, several times the size of a
    finished copy, under a name `_copies()` excludes on both of its tests, so it
    counts toward neither `held_bytes()` nor the ceiling `enforce_copy_limit`
    enforces. Unswept, they accumulate one per crash while the folder reports
    itself smaller than it is.
    """
    for leftover in folder.glob(".diglibrary-*.partial"):
        leftover.unlink(missing_ok=True)


def enforce_copy_limit(folder: Path, limit_bytes: int, logger: logging.Logger) -> int:
    """Discard the oldest copies until the folder fits, and return what is held.

    **The newest copy is never discarded, whatever the ceiling says.** The
    shared `enforce_folder_limit` is right for a cache, where discarding
    everything costs time and nothing else; here the last file standing is the
    only record of the library that is not the live database, and a ceiling
    typed one digit short must not be what removes it.

    Written as *everything except the newest* rather than as a list of what may
    go: a copy this function has never met still falls on the right side of
    that sentence.
    """
    copies = _copies(folder)
    if not copies:
        return 0
    held = sum(size for _, size, _ in copies)
    # Oldest first, and the last one is off the table by never being reached.
    discarded = 0
    for _, size, path in copies[:-1]:
        if held <= limit_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        held -= size
        discarded += 1
    if discarded:
        logger.info(
            "Older copies of the database were discarded to stay under the limit.",
            extra={
                "operation": "database.copy.discarded",
                "discarded": discarded,
                "held_bytes": held,
                "limit_bytes": limit_bytes,
            },
        )
    return held


def held_bytes(folder: Path) -> int:
    """Return what the copies take up, for the line in Settings that says so."""
    return sum(size for _, size, _ in _copies(folder))


def newest_copy(folder: Path) -> Path | None:
    """Return the most recent copy, which is the one a restore would start from."""
    copies = _copies(folder)
    return copies[-1][2] if copies else None


def available_copies(folder: Path) -> list[tuple[float, int, Path]]:
    """Every complete copy here, newest first, for something that offers a choice.

    Public because `_copies` serves only the ceiling: without this, the copies
    could be counted and discarded by this module and offered by nothing.
    """
    return list(reversed(_copies(folder)))


def restore_copy(archive: Path, database: Path) -> Path:
    """Put one copy back, and never over the top of what is there.

    **The existing database is moved aside, not replaced.** A restore is done in
    the belief that the live file is the damaged one, and that belief is sometimes
    wrong — so the file it displaces is kept under a name this application will
    not open, and the path to it is returned. Undoing a restore is then one
    rename, which is the difference between a recoverable mistake and a second
    loss.

    The archive is verified before anything is moved: unpacked, opened read-only,
    and asked `PRAGMA integrity_check`. A copy that cannot answer is refused with
    the live database still exactly where it was.
    """
    if not archive.is_file():
        raise CopyUnreadableError(f"There is no copy at {archive}.")
    database.parent.mkdir(parents=True, exist_ok=True)
    # Unpacked beside the destination, so the move into place is a rename on one
    # filesystem rather than a copy that can fail halfway.
    unpacked = database.with_name(f".{database.name}.restoring")
    unpacked.unlink(missing_ok=True)
    try:
        with gzip.open(archive, "rb") as source, unpacked.open("wb") as target:
            shutil.copyfileobj(source, target)
        _refuse_a_copy_that_cannot_be_read(unpacked)
    except Exception:
        unpacked.unlink(missing_ok=True)
        raise
    displaced = database.with_name(f"{database.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
    if database.is_file():
        database.rename(displaced)
    # And the write-ahead files of the database being displaced, which belong to
    # it and not to the one arriving: left behind, SQLite would read them as this
    # database's own uncommitted tail — a file that means one thing beside one
    # database and another beside the next.
    for tail in ("-wal", "-shm"):
        beside = database.with_name(database.name + tail)
        if beside.is_file():
            beside.rename(displaced.with_name(displaced.name + tail))
    unpacked.rename(database)
    return displaced


def _copies(folder: Path) -> list[tuple[float, int, Path]]:
    """Every complete copy here, oldest first. Partials are not copies."""
    found = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(COPY_SUFFIX) or entry.name.startswith("."):
            continue
        try:
            status = entry.stat()
        except OSError:
            continue
        found.append((status.st_mtime, status.st_size, entry))
    found.sort(key=lambda item: item[0])
    return found


def _changed_since_newest(database: Path, folder: Path) -> bool:
    """Whether the database has been written since the newest copy was taken.

    Compared by modification time, which is what a write to the database moves
    and what a copy of it stamps. A database with no copy yet has changed by
    definition, and a clock that cannot be read answers *yes* — the cost of
    being wrong here is one redundant copy, and the cost of the other answer is
    a version that was never taken.
    """
    newest = newest_copy(folder)
    if newest is None:
        return True
    try:
        return database.stat().st_mtime > newest.stat().st_mtime
    except OSError:
        return True
