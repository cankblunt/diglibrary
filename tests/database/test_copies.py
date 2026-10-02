"""The copies of the database: taken when due, and aged out without ever
discarding the last one standing."""

import gzip
import logging
import shutil
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from diglibrary.database.copies import (
    COPY_SUFFIX,
    CopyUnreadableError,
    _refuse_a_copy_that_cannot_be_read,
    enforce_copy_limit,
    held_bytes,
    newest_copy,
    restore_copy,
    take_copy,
)

LOGGER = logging.getLogger("test")


def _database(path: Path, rows: int = 1) -> Path:
    """A real SQLite file in WAL mode, which is what the application runs."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS albums (id INTEGER PRIMARY KEY, name TEXT)")
    connection.executemany(
        "INSERT INTO albums (name) VALUES (?)", [(f"album {n}",) for n in range(rows)]
    )
    connection.commit()
    connection.close()
    return path


def _reopen(copy: Path, tmp_path: Path) -> sqlite3.Connection:
    """Decompress a copy and open it, which is what a restore would do."""
    plain = tmp_path / "restored.sqlite3"
    with gzip.open(copy, "rb") as source:
        plain.write_bytes(source.read())
    return sqlite3.connect(plain)


def test_a_copy_is_a_database_a_restore_can_open(tmp_path: Path) -> None:
    """The point of the whole thing, asserted by reading the rows back out.

    A copy that gunzips into something SQLite refuses is indistinguishable from
    a copy that was never taken, and both look exactly like a folder with a
    file in it.
    """
    database = _database(tmp_path / "diglibrary.sqlite3", rows=25)
    folder = tmp_path / "database-copies"

    copy = take_copy(database, folder, LOGGER)

    assert copy is not None and copy.name.endswith(COPY_SUFFIX)
    restored = _reopen(copy, tmp_path)
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 25
    restored.close()


def test_a_copy_holds_what_was_still_in_the_write_ahead_log(tmp_path: Path) -> None:
    """The reason this is `VACUUM INTO` and never `shutil.copy`.

    The application holds its connection while the window is open, so the
    `.sqlite3` file on disk is routinely missing rows that are committed and
    sitting in `-wal`. A plain copy of the
    file is a copy of a database mid-sentence, and it fails exactly when it
    matters: on the session whose work has not been checkpointed yet.
    """
    database = tmp_path / "diglibrary.sqlite3"
    live = sqlite3.connect(database)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT)")
    live.commit()
    live.execute("INSERT INTO albums (name) VALUES ('committed, still in the wal')")
    live.commit()
    # Deliberately still open, holding the WAL — the state the app is in.
    try:
        copy = take_copy(database, tmp_path / "database-copies", LOGGER)
        assert copy is not None
        restored = _reopen(copy, tmp_path)
        assert restored.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 1
        restored.close()
    finally:
        live.close()


def test_a_database_nobody_wrote_to_is_not_copied_again(tmp_path: Path) -> None:
    """Opening the window twice to look at something is not a new version.

    A folder of identical copies is a folder whose ceiling discards real
    history to hold repetitions.
    """
    database = _database(tmp_path / "diglibrary.sqlite3")
    folder = tmp_path / "database-copies"

    first = take_copy(database, folder, LOGGER)
    again = take_copy(database, folder, LOGGER)

    assert first is not None
    assert again is None, "an unchanged database was copied a second time"
    assert len(list(folder.glob(f"*{COPY_SUFFIX}"))) == 1


def test_a_database_that_changed_is_copied_again(tmp_path: Path) -> None:
    """The other half of the same rule, which is the half that must not fail."""
    database = _database(tmp_path / "diglibrary.sqlite3")
    folder = tmp_path / "database-copies"
    first = take_copy(database, folder, LOGGER)
    assert first is not None

    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO albums (name) VALUES ('new')")
    connection.commit()
    connection.close()
    # The copy and the write can land in the same clock tick on a fast disk.
    import os

    stat = database.stat()
    os.utime(database, (stat.st_atime, stat.st_mtime + 10))

    assert take_copy(database, folder, LOGGER) is not None


def test_the_newest_copy_is_never_discarded_by_the_ceiling(tmp_path: Path) -> None:
    """A ceiling typed one digit short must not empty the folder.

    This is the whole reason these do not use the shared `enforce_folder_limit`:
    for a cache, discarding everything costs time and nothing else, and that
    helper will take the last file. Here the last file standing is the only
    record of the library outside the live database.
    """
    folder = tmp_path / "database-copies"
    folder.mkdir()
    for index in range(4):
        copy = folder / f"diglibrary-2001020{index}-000000{COPY_SUFFIX}"
        copy.write_bytes(b"x" * 1000)
        import os

        os.utime(copy, (index * 100, index * 100))

    held = enforce_copy_limit(folder, 0, LOGGER)

    survivors = sorted(p.name for p in folder.glob(f"*{COPY_SUFFIX}"))
    assert survivors == [
        "diglibrary-20010203-000000.sqlite3.gz"
    ], "a ceiling of zero took every copy, including the newest"
    assert held == 1000


def test_the_oldest_go_first(tmp_path: Path) -> None:
    """Oldest-first, so what survives is the most recent history, not a random
    slice of it."""
    folder = tmp_path / "database-copies"
    folder.mkdir()
    import os

    for index in range(5):
        copy = folder / f"diglibrary-2001020{index}-000000{COPY_SUFFIX}"
        copy.write_bytes(b"x" * 1000)
        os.utime(copy, (index * 100, index * 100))

    # Five at a thousand bytes against a ceiling of 2,500: the three oldest go,
    # and it stops at the first state that fits rather than at a count.
    held = enforce_copy_limit(folder, 2500, LOGGER)

    survivors = sorted(p.name for p in folder.glob(f"*{COPY_SUFFIX}"))
    assert survivors == [
        "diglibrary-20010203-000000.sqlite3.gz",
        "diglibrary-20010204-000000.sqlite3.gz",
    ]
    assert held == 2000


def test_a_half_written_copy_is_not_something_a_restore_can_find(tmp_path: Path) -> None:
    """A copy interrupted mid-write must not wear the name a restore looks for.

    Written as a dotted partial and renamed into place, so the suffix only ever
    names a complete file — and neither the ceiling nor `newest_copy` counts a
    partial as history.
    """
    folder = tmp_path / "database-copies"
    folder.mkdir()
    (folder / f".diglibrary-20010206-000000{COPY_SUFFIX}.partial").write_bytes(b"x" * 5000)
    (folder / ".diglibrary-20010206-000000.sqlite3.partial").write_bytes(b"x" * 5000)
    real = folder / f"diglibrary-20010206-000001{COPY_SUFFIX}"
    real.write_bytes(b"x" * 100)

    assert newest_copy(folder) == real
    assert held_bytes(folder) == 100


def test_a_database_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    """A first run has no database yet, and housekeeping never stops a window
    from opening."""
    assert take_copy(tmp_path / "absent.sqlite3", tmp_path / "copies", LOGGER) is None


def test_a_library_in_a_folder_whose_name_carries_a_hash_is_still_copied(
    tmp_path: Path,
) -> None:
    """SQLite parses the `file:` URI itself: it cuts the path at the first `?`
    or `#`. A path handed over unescaped, for a library under `~/Music #2` — an
    ordinary folder name — opens `~/Music` instead and loses `mode=ro` with the
    rest of the query, which is the one guard keeping this path from writing to
    the live database. What is copied then is a database SQLite has just created
    empty, installed under the name a restore looks for.
    """
    awkward = tmp_path / "Music #2"
    awkward.mkdir()
    database = awkward / "diglibrary.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO album (name) VALUES ('Tin Lantern')")

    copy = take_copy(database, tmp_path / "copies", LOGGER)

    assert copy is not None
    with gzip.open(copy, "rb") as packed:
        body = packed.read()
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(body)
    with sqlite3.connect(restored) as connection:
        rows = connection.execute("SELECT name FROM album").fetchall()
    assert rows == [("Tin Lantern",)], "the copy holds the rows, not an empty database"


def test_a_partial_left_by_a_killed_process_is_swept_before_the_next_copy(
    tmp_path: Path,
) -> None:
    """The `finally` clears both temporaries on an ordinary failure and does not
    run when the process is killed. What is left is the *uncompressed*
    intermediate, under a name `_copies()` excludes on both of its tests — so it
    counts toward neither the reported size nor the ceiling, and unswept it
    accumulates one per crash."""
    database = tmp_path / "diglibrary.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE album (id INTEGER PRIMARY KEY)")
    folder = tmp_path / "copies"
    folder.mkdir()
    orphan = folder / ".diglibrary-20010201-000000.sqlite3.partial"
    orphan.write_bytes(b"x" * 4096)

    assert take_copy(database, folder, LOGGER) is not None

    assert not orphan.exists(), "a crash's leftover is collected, not kept forever"


def test_a_copy_goes_all_the_way_back_to_a_working_database(tmp_path: Path) -> None:
    """The round trip: copy, damage, restore, read.

    A backup whose restore has never run is not known to be a backup, so this
    takes one, damages the live file, puts the copy back, and reads the rows
    out of it.
    """
    database = _database(tmp_path / "library.sqlite3", rows=3)
    folder = tmp_path / "copies"

    copy = take_copy(database, folder, LOGGER)
    assert copy is not None
    # What a damaged database looks like from here: the file is still there and
    # still named the thing the application opens.
    database.write_bytes(b"this is not a database any more")

    displaced = restore_copy(copy, database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT count(*) FROM albums").fetchone()[0] == 3
    finally:
        connection.close()
    # And the damaged one is kept, under a name this application does not open.
    assert displaced.is_file() and displaced.read_bytes().startswith(b"this is not")
    assert displaced.name != database.name


def test_a_restore_refuses_a_copy_that_is_not_a_database_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """The live file is the one thing a failed restore must not have touched."""
    database = _database(tmp_path / "library.sqlite3", rows=2)
    folder = tmp_path / "copies"
    folder.mkdir()
    ruined = folder / f"diglibrary-20010207-000000{COPY_SUFFIX}"
    with gzip.open(ruined, "wb") as target:
        target.write(b"not a database")

    with pytest.raises(CopyUnreadableError):
        restore_copy(ruined, database)

    connection = sqlite3.connect(database)
    try:
        assert (
            connection.execute("SELECT count(*) FROM albums").fetchone()[0] == 2
        ), "the live database was disturbed"
    finally:
        connection.close()
    assert not list(tmp_path.glob("*.replaced-*")), "something was moved aside for nothing"


def test_a_file_that_is_not_a_database_is_refused_by_one_type(tmp_path: Path) -> None:
    """SQLite answers two ways and a caller should have to know only one.

    Damage *inside* a database comes back as a description of the damage; a file
    that is not a database at all *raises*. Whoever is about to move a live
    library aside gets `CopyUnreadableError` either way, and never the raw
    `sqlite3.DatabaseError`.
    """
    nonsense = tmp_path / "not-a-database.sqlite3"
    nonsense.write_bytes(b"just some bytes")

    with pytest.raises(CopyUnreadableError, match="not a database"):
        _refuse_a_copy_that_cannot_be_read(nonsense)


def test_a_truncated_archive_is_never_named_as_a_copy(tmp_path: Path) -> None:
    """The gzip CRC is at the end of the stream, so only unpacking finds this.

    A copy interrupted by a full disk leaves an archive that exists and is named
    what a restore looks for. Refused here, where it costs a second, instead of
    during the restore, where it costs the restore.

    The truncation is done by standing in for the compression step, because a
    disk that fills mid-write is not something a test can arrange — and what is
    being checked is the guard after it, not gzip.
    """
    database = _database(tmp_path / "library.sqlite3", rows=200)
    folder = tmp_path / "copies"
    real_copyfileobj = shutil.copyfileobj

    def stop_early(source, target, *args, **kwargs):
        target.write(source.read(64))

    with mock.patch("diglibrary.database.copies.shutil.copyfileobj", stop_early):
        assert take_copy(database, folder, LOGGER) is None

    assert list(folder.glob(f"*{COPY_SUFFIX}")) == [], "a copy nobody can unpack was published"
    assert list(folder.glob("*.partial")) == [], "a temporary was left behind"
    assert shutil.copyfileobj is real_copyfileobj
