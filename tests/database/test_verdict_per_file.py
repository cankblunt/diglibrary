"""Migration 15: a verdict belongs to a file, and the ones already said survive."""

import logging
from pathlib import Path

from diglibrary.database.connection import Database
from diglibrary.database.migrations import MIGRATIONS


def _database_at(path: Path, version: int) -> Database:
    """Bring a database up to one version and stop, so the next one can be watched."""
    database = Database(path, logging.getLogger("test.migrations"))
    original = MIGRATIONS[:]
    try:
        import diglibrary.database.connection as connection_module

        connection_module.MIGRATIONS = tuple(m for m in original if m.version <= version)
        database.initialize()
    finally:
        connection_module.MIGRATIONS = original
    return database


def test_a_verdict_said_about_a_whole_album_becomes_one_about_each_of_its_files(
    tmp_path: Path,
) -> None:
    """A verdict must survive the control that recorded it being taken away.

    The flag belongs to a file and no longer to the folder row or the album
    line: only a file can have been lossy before it arrived, so only a file is
    asked. A database may still carry an album-wide verdict.

    Left as it was it would have become unreachable. With an album marked honest
    no file reads as marked, so a file's own flag computes `clear`, deletes a row
    that is not there, and changes nothing — a control that answers a click with
    silence.
    """
    database = _database_at(tmp_path / "library.sqlite3", 14)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/Album', 'album-sig', 2)"
        )
        unit_id = connection.execute("SELECT id FROM album_units").fetchone()[0]
        for name, signature in (("a.flac", "sig-a"), ("b.flac", "sig-b")):
            connection.execute(
                "INSERT INTO audio_files (album_unit_id, path, content_signature, "
                " file_size_bytes, modified_at) VALUES (?, ?, ?, 1, '2001-02-03')",
                (unit_id, f"/music/Album/{name}", signature),
            )
        connection.executemany(
            "INSERT INTO quality_overrides (unit_signature, content_signature, verdict) "
            "VALUES (?, ?, ?)",
            [
                ("album-sig", "", "honest"),
                # One file with a verdict of its own, recorded before the
                # album's. The more specific word is the one that stands.
                ("album-sig", "sig-a", "transcoded"),
                # An album this app has never registered the files of: there is
                # nothing to expand onto, and dropping it would discard a verdict.
                ("stranger-sig", "", "honest"),
            ],
        )

    Database(tmp_path / "library.sqlite3", logging.getLogger("test.migrations")).initialize()

    with database.connect() as connection:
        rows = dict(
            connection.execute(
                "SELECT content_signature, verdict FROM quality_overrides "
                "WHERE unit_signature = 'album-sig'"
            ).fetchall()
        )
        orphan = connection.execute(
            "SELECT verdict FROM quality_overrides WHERE unit_signature = 'stranger-sig'"
        ).fetchall()

    assert "" not in rows, "the album-wide row has to be gone once it has been expanded"
    assert rows["sig-b"] == "honest", "the album's word has to reach the file that had none"
    assert (
        rows["sig-a"] == "transcoded"
    ), "and it must not overwrite what was already said about a file"
    assert orphan == [("honest",)], (
        "an album whose files are unknown has nothing to expand onto, "
        "so its row stays rather than being discarded"
    )


def test_a_measurement_filed_under_a_file_name_is_discarded(tmp_path: Path) -> None:
    """Migration 16: rubble from a write path that could not guarantee its key.

    A row can carry a file name where a content signature belongs. No such row
    is readable, because no lookup this application performs can produce such a
    key.

    The predicate is the extension. The obvious rule — a signature is a
    64-character digest, so anything else is rubbish — is not an invariant this
    schema enforces, and it is wrong about real data: a file name can be exactly
    64 characters long.
    """
    database = _database_at(tmp_path / "library.sqlite3", 15)
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO track_quality (content_signature, encoding, findings, reason) "
            "VALUES (?, 'lossless', '[]', '')",
            [
                ("a" * 64,),
                # A short but perfectly good key, of the kind a fixture uses.
                ("shared-shape",),
                ("01 - Some Song.mp3",),
                ("Another Song.flac",),
                # A file name that happens to be exactly 64 characters long.
                (f"{'x' * 59}.mp3",),
            ],
        )

    Database(tmp_path / "library.sqlite3", logging.getLogger("test.migrations")).initialize()

    with database.connect() as connection:
        kept = {row[0] for row in connection.execute("SELECT content_signature FROM track_quality")}
    assert kept == {
        "a" * 64,
        "shared-shape",
    }, "every name goes, whatever its length, and every key that is not a name stays"
