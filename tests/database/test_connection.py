"""Tests for SQLite initialization and versioned migration."""

import logging
import sqlite3

import pytest

from diglibrary.database.connection import Database, MigrationError
from diglibrary.database.migrations import MIGRATIONS


def test_initialize_applies_every_migration(tmp_path) -> None:
    """A new database ends at the latest schema version with the library tables present."""
    database = Database(tmp_path / "library.sqlite3", _logger())

    version = database.initialize()

    assert version == MIGRATIONS[-1].version
    assert _tables(database) >= {
        "album_units",
        "audio_files",
        "acoustic_fingerprints",
        "metadata_releases",
        "identifications",
        "change_plans",
        "change_operations",
        "settings",
        "schema_migrations",
    }


def test_initialize_is_idempotent(tmp_path) -> None:
    """Running initialization twice applies each migration exactly once."""
    database = Database(tmp_path / "library.sqlite3", _logger())

    database.initialize()
    database.initialize()

    with database.connect() as connection:
        applied = connection.execute("SELECT version FROM schema_migrations").fetchall()

    assert [row[0] for row in applied] == [migration.version for migration in MIGRATIONS]
    assert database.schema_version() == MIGRATIONS[-1].version


def test_initialize_replaces_the_stub_schema_without_losing_settings(tmp_path) -> None:
    """A database created by the pre-migration build upgrades in place."""
    path = tmp_path / "library.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO settings VALUES ('library_root', '/music')")
    database = Database(path, _logger())

    database.initialize()

    tables = _tables(database)
    assert "albums" not in tables
    assert "tracks" not in tables
    with database.connect() as connection:
        stored = connection.execute("SELECT value FROM settings WHERE key = ?", ("library_root",))
        assert stored.fetchone()[0] == "/music"


def test_a_database_from_a_newer_build_is_refused(tmp_path) -> None:
    """An unknown future schema is never silently used or downgraded."""
    database = Database(tmp_path / "library.sqlite3", _logger())
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (999, "from_the_future")
        )

    with pytest.raises(MigrationError, match="newer than this build"):
        database.initialize()


def test_a_migration_that_fails_partway_leaves_no_table_behind(tmp_path, monkeypatch) -> None:
    """`sqlite3` opens a transaction for `INSERT` and not for `CREATE TABLE`.

    A migration that creates a table and then fills it — 13, 19 and 22 all do
    — commits the table the instant it is created unless the whole upgrade runs
    in one explicit transaction. When the fill fails, the rollback takes the
    rows and leaves the table, `schema_migrations` records nothing, the next
    launch runs the migration again, and `CREATE TABLE` answers *table already
    exists*: the application cannot start until the file is repaired by hand.
    """
    from diglibrary.database.migrations import Migration

    doomed = Migration(
        version=MIGRATIONS[-1].version + 1,
        name="creates_then_fails",
        statements=(
            "CREATE TABLE halfway (id INTEGER PRIMARY KEY)",
            "INSERT INTO halfway (id) VALUES (1)",
            "INSERT INTO halfway (id) VALUES (1)",
        ),
    )
    path = tmp_path / "library.sqlite3"
    database = Database(path, _logger())
    database.initialize()
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", (*MIGRATIONS, doomed))

    with pytest.raises(MigrationError, match="creates_then_fails"):
        database.initialize()

    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "halfway" not in tables, "a rolled-back migration leaves nothing on the disk"

    # And the proof that matters to whoever launches it next: it starts.
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", MIGRATIONS)
    assert database.initialize() == MIGRATIONS[-1].version


def test_two_instances_upgrading_at_once_do_not_both_apply_a_migration(tmp_path) -> None:
    """A double click launches two instances. If each reads the version before
    taking the write lock, both read the same one, both apply the next
    migration, and the loser raises on the duplicate row. The lock is taken
    *before* the version is read, so the second waits and finds nothing to do."""
    import threading

    path = tmp_path / "library.sqlite3"
    failures: list[BaseException] = []

    def upgrade() -> None:
        try:
            Database(path, _logger()).initialize()
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=upgrade) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"an instance failed to start: {failures}"
    with sqlite3.connect(path) as connection:
        applied = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert applied == len(MIGRATIONS), "each migration is recorded exactly once"


def test_migration_versions_are_unique_and_ordered() -> None:
    """Migrations are applied by version, so the declared order must match it."""
    versions = [migration.version for migration in MIGRATIONS]

    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == 1


def test_foreign_keys_are_enforced(tmp_path) -> None:
    """A change operation cannot reference a plan that does not exist."""
    database = Database(tmp_path / "library.sqlite3", _logger())
    database.initialize()

    with pytest.raises(sqlite3.IntegrityError), database.connect() as connection:
        connection.execute(
            "INSERT INTO change_operations "
            "(change_plan_id, sequence, kind, target_path, after_state) "
            "VALUES (?, ?, ?, ?, ?)",
            (404, 1, "rename_file", "/music/track.flac", "{}"),
        )


def test_a_connection_is_closed_when_its_block_ends(tmp_path) -> None:
    """sqlite3's own context manager ends the transaction and leaves the handle open.

    A scan of a large library opens tens of thousands of these, so the close
    has to be owned here rather than left to the garbage collector.
    """
    database = Database(tmp_path / "library.sqlite3", _logger())
    database.initialize()

    with database.connect() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_work_in_a_block_is_committed_and_a_failure_rolls_back(tmp_path) -> None:
    """The block is one transaction, which is what every store method relies on."""
    database = Database(tmp_path / "library.sqlite3", _logger())
    database.initialize()

    with database.connect() as connection:
        connection.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
    with pytest.raises(sqlite3.IntegrityError), database.connect() as connection:
        connection.execute("INSERT INTO settings (key, value) VALUES ('b', '2')")
        connection.execute("INSERT INTO settings (key, value) VALUES ('b', '3')")

    with database.connect() as connection:
        stored = {row[0] for row in connection.execute("SELECT key FROM settings")}
    assert stored == {"a"}


def _tables(database: Database) -> set[str]:
    with database.connect() as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _logger() -> logging.Logger:
    return logging.getLogger("test.database")


def test_widening_the_correction_field_keeps_the_corrections(tmp_path, monkeypatch) -> None:
    """Migration 10 rebuilds a table, so what it must prove is that nothing is lost.

    The `field` column refused `track_artist` — a CHECK from migration 2, which
    is published and therefore never edited. SQLite cannot widen a CHECK in
    place, so the table is rebuilt, and a rebuild is the one operation in this
    schema that can silently drop what the user typed. Every column is copied
    by name, including the id and the date, and this asserts it against a
    database that really was created before the widening.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 10)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/zorvo', 'sig', 2)"
        )
        connection.execute(
            "INSERT INTO manual_corrections (id, album_unit_id, field, track_position, "
            "value, replaced, created_at) VALUES "
            "(41, 1, 'folder_name', NULL, 'Talvia - Zorvó [kept]', 'Talvia (1955)', "
            "'2001-07-01 10:00:00'), "
            "(42, 1, 'track_title', 1, 'Brisa de Marfol', 'Track 1', '2001-07-02 11:00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
                "VALUES (1, 'track_artist', 1, 'Ondrel Vask, Milka Vask')"
            )
    monkeypatch.undo()

    version = Database(path, _logger()).initialize()

    assert version == MIGRATIONS[-1].version
    with Database(path, _logger()).connect() as connection:
        kept = connection.execute(
            "SELECT id, field, track_position, value, replaced, created_at "
            "FROM manual_corrections ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in kept] == [
            (
                41,
                "folder_name",
                None,
                "Talvia - Zorvó [kept]",
                "Talvia (1955)",
                "2001-07-01 10:00:00",
            ),
            (42, "track_title", 1, "Brisa de Marfol", "Track 1", "2001-07-02 11:00:00"),
        ]
        # The credit is now a correction like the other two. An unlisted word
        # is not refused here, because migration 26 drops the enumeration; the
        # rule the column still holds is asserted in
        # `test_opening_the_field_keeps_every_correction_and_its_release`.
        connection.execute(
            "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
            "VALUES (1, 'track_artist', 1, 'Ondrel Vask, Milka Vask')"
        )
        # And the UNIQUE constraint came back with the table, which is what keeps
        # a resubmitted correction from accumulating instead of replacing.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
                "VALUES (1, 'track_title', 1, 'again')"
            )


def test_widening_the_field_a_third_time_keeps_every_correction(tmp_path, monkeypatch) -> None:
    """Migration 12 rebuilds the same table again, and the same thing is at stake.

    The CHECK from migration 2 enumerates the fields, so a new field needs the
    table rebuilt, and each rebuild is a chance to lose what the user typed.
    This drives it from a database created before migration 12, with
    corrections of all three earlier fields standing, and asserts they cross
    intact — id and date included — while the new field becomes writable and the
    column that names the release comes across empty for every one of them.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 12)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/quelvo', 'sig', 9)"
        )
        connection.execute(
            "INSERT INTO manual_corrections (id, album_unit_id, field, track_position, "
            "value, replaced, created_at) VALUES "
            "(41, 1, 'folder_name', NULL, 'Quelvo [kept]', 'Quelvo (1989)', "
            "'2001-07-01 10:00:00'), "
            "(42, 1, 'track_title', 1, 'Lumen', 'Track 1', '2001-07-02 11:00:00'), "
            "(43, 1, 'track_artist', 1, 'Quelvo', 'Tarn Records', '2001-07-03 12:00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
                "VALUES (1, 'track_file', 1, 'signature-of-lumen')"
            )
    monkeypatch.undo()

    version = Database(path, _logger()).initialize()

    assert version == MIGRATIONS[-1].version
    with Database(path, _logger()).connect() as connection:
        kept = connection.execute(
            "SELECT id, field, track_position, value, replaced, release_key, created_at "
            "FROM manual_corrections ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in kept] == [
            (
                41,
                "folder_name",
                None,
                "Quelvo [kept]",
                "Quelvo (1989)",
                None,
                "2001-07-01 10:00:00",
            ),
            (42, "track_title", 1, "Lumen", "Track 1", None, "2001-07-02 11:00:00"),
            (43, "track_artist", 1, "Quelvo", "Tarn Records", None, "2001-07-03 12:00:00"),
        ]
        # A pairing is now a correction like the other three, and it carries the
        # release it was made against, because a position belongs to a tracklist.
        connection.execute(
            "INSERT INTO manual_corrections (album_unit_id, field, track_position, value, "
            "release_key) VALUES (1, 'track_file', 1, 'signature-of-lumen', 'discogs:1234567')"
        )
        # The UNIQUE constraint came back with the table, a third time: one track
        # holds one pairing, and a second answer replaces it rather than joining.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
                "VALUES (1, 'track_file', 1, 'another-signature')"
            )


def test_rekeying_the_fingerprint_carries_it_onto_the_audio(tmp_path, monkeypatch) -> None:
    """Migration 11 rebuilds a table, and the point is what it survives: a rename.

    Bound to `audio_files(id)`, a fingerprint was lost the moment the album was
    organized, because files are written `ON CONFLICT (path)` and a renamed file
    is a new row. Keyed by the audio itself it outlives the rename — which is
    the whole reason the expensive work is worth caching.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 11)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/zorvo', 'sig', 1)"
        )
        connection.execute(
            "INSERT INTO audio_files (id, path, content_signature, file_size_bytes, "
            "modified_at, album_unit_id) VALUES "
            "(7, '/music/zorvo/01 brisa.flac', 'audio-sig', 100, '2001-07-01', 1)"
        )
        connection.execute(
            "INSERT INTO acoustic_fingerprints (id, audio_file_id, algorithm, fingerprint, "
            "duration_seconds, external_recording_id, computed_at) VALUES "
            "(3, 7, 'chromaprint', 'AQABz0', 214, 'rec-mbid', '2001-07-30 09:00:00')"
        )
    monkeypatch.undo()

    version = Database(path, _logger()).initialize()

    assert version == MIGRATIONS[-1].version
    with Database(path, _logger()).connect() as connection:
        carried = connection.execute(
            "SELECT id, content_signature, algorithm, fingerprint, duration_seconds, "
            "external_recording_id, computed_at FROM acoustic_fingerprints"
        ).fetchall()
        assert [tuple(row) for row in carried] == [
            (3, "audio-sig", "chromaprint", "AQABz0", 214, "rec-mbid", "2001-07-30 09:00:00")
        ]
        # Organizing renames the file, which is a new row and a new id. The
        # fingerprint is reachable from the audio regardless, which is what it
        # was bound to the path instead of.
        connection.execute(
            "INSERT INTO audio_files (id, path, content_signature, file_size_bytes, "
            "modified_at, album_unit_id) VALUES "
            "(9, '/music/Talvia - Zorvó/01. Brisa de Marfol.flac', 'audio-sig', 100, "
            "'2001-07-01', 1)"
        )
        found = connection.execute(
            "SELECT print.fingerprint FROM audio_files AS file "
            "JOIN acoustic_fingerprints AS print "
            "ON print.content_signature = file.content_signature "
            "WHERE file.id = 9"
        ).fetchone()
        assert found[0] == "AQABz0"


def test_the_same_recording_in_two_folders_carries_one_fingerprint(tmp_path, monkeypatch) -> None:
    """Two `audio_files` rows can hold the same audio, and the new key is unique.

    A copy that collides is the same bytes and therefore the same fingerprint,
    so the migration keeps one rather than failing the upgrade.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 11)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/one', 'sig', 1)"
        )
        for file_id, file_path in ((1, "/music/one/a.flac"), (2, "/music/two/a.flac")):
            connection.execute(
                "INSERT INTO audio_files (id, path, content_signature, file_size_bytes, "
                "modified_at, album_unit_id) VALUES (?, ?, 'shared-sig', 100, '2001-07-01', 1)",
                (file_id, file_path),
            )
            connection.execute(
                "INSERT INTO acoustic_fingerprints (audio_file_id, algorithm, fingerprint, "
                "duration_seconds) VALUES (?, 'chromaprint', 'AQABz0', 214)",
                (file_id,),
            )
    monkeypatch.undo()

    Database(path, _logger()).initialize()

    with Database(path, _logger()).connect() as connection:
        rows = connection.execute("SELECT content_signature FROM acoustic_fingerprints").fetchall()
        assert [row[0] for row in rows] == ["shared-sig"]


def test_naming_the_audio_a_measurement_came_from_keeps_every_old_one(
    tmp_path, monkeypatch
) -> None:
    """Migration 13 rebuilds `track_quality`, which holds every measurement made.

    The signature does not identify a recording, so one row serves every file
    of the same shape, and different songs of one length and format share it.
    The rebuild gives a row somewhere to say which audio it came from, and
    nothing sweeps what is already there: every existing measurement crosses
    with `audio_key` NULL and keeps answering for its signature as before.

    This drives it from a database created before migration 13, with a
    measurement standing, and asserts it crosses intact — id and date included
    — while a second row for the *same signature* becomes writable because it
    names a different audio, which the old UNIQUE could never have allowed.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 13)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO track_quality (id, content_signature, encoding, "
            "effective_bitrate_kbps, cutoff_hertz, steepest_drop_db, findings, reason, "
            "decay_db, ceiling_db, measured_at) VALUES "
            "(5, 'shared-shape', 'transcoded', 256, 19000, 22.1, 'transcoded', "
            "'wall at 19 kHz', 22.1, -104.3, '2001-07-28 03:00:00')"
        )
        # One row per signature, whichever file was measured first: the defect.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO track_quality (content_signature, encoding) "
                "VALUES ('shared-shape', 'lossless')"
            )
    monkeypatch.undo()

    version = Database(path, _logger()).initialize()

    assert version == MIGRATIONS[-1].version
    with Database(path, _logger()).connect() as connection:
        kept = connection.execute(
            "SELECT id, content_signature, audio_key, encoding, effective_bitrate_kbps, "
            "cutoff_hertz, steepest_drop_db, findings, reason, decay_db, ceiling_db, "
            "measured_at FROM track_quality"
        ).fetchall()
        assert [tuple(row) for row in kept] == [
            (
                5,
                "shared-shape",
                None,
                "transcoded",
                256,
                19000,
                22.1,
                "transcoded",
                "wall at 19 kHz",
                22.1,
                -104.3,
                "2001-07-28 03:00:00",
            )
        ]
        # The other recording of the same shape can now be measured on its own,
        # beside the row it used to be sentenced by.
        connection.execute(
            "INSERT INTO track_quality (content_signature, audio_key, encoding) "
            "VALUES ('shared-shape', 'flac:e6a3', 'lossless')"
        )
        # And it is still one row per audio: a second measurement of the same
        # recording replaces it rather than joining it.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO track_quality (content_signature, audio_key, encoding) "
                "VALUES ('shared-shape', 'flac:e6a3', 'transcoded')"
            )
        # NULL is never equal to NULL in SQLite, so the index is written over
        # `IFNULL` precisely so the legacy row keeps the guarantee it had.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO track_quality (content_signature, encoding) "
                "VALUES ('shared-shape', 'lossy')"
            )


def test_the_print_and_its_answer_cross_onto_the_audio_they_came_from(
    tmp_path, monkeypatch
) -> None:
    """Migration 19: one fingerprint per shape was one recording wearing another's name.

    `acoustic_fingerprints.content_signature` is UNIQUE, and a signature is a
    declared shape rather than a recording. Two different recordings of the
    same length in the same format share one signature, so the fingerprint
    stored for the first, and the recording id it was answered with, were
    returned for both.

    Nothing is rebuilt and nothing is swept: every row crosses with `audio_key`
    NULL, exactly as migration 13 did, and the second recording becomes
    writable beside it.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 19)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO acoustic_fingerprints (content_signature, algorithm, fingerprint, "
            "duration_seconds, external_recording_id, recording_score, looked_up_at, "
            "computed_at) VALUES ('shared-shape', 'chromaprint', 'AQABz0', 245, "
            "'0a1b2c3d', 0.9, '2001-08-02 09:00:01', '2001-08-02 09:00:00')"
        )
    monkeypatch.undo()

    assert Database(path, _logger()).initialize() == MIGRATIONS[-1].version

    with Database(path, _logger()).connect() as connection:
        carried = connection.execute(
            "SELECT content_signature, audio_key, algorithm, fingerprint, duration_seconds, "
            "external_recording_id, recording_score, looked_up_at, computed_at "
            "FROM acoustic_prints"
        ).fetchall()
        assert [tuple(row) for row in carried] == [
            (
                "shared-shape",
                None,
                "chromaprint",
                "AQABz0",
                245,
                "0a1b2c3d",
                0.9,
                "2001-08-02 09:00:01",
                "2001-08-02 09:00:00",
            )
        ], "the request that bought this answer is not paid for twice"
        # The other recording of the same shape can now carry its own print,
        # beside the row that used to answer for both.
        connection.execute(
            "INSERT INTO acoustic_prints (content_signature, audio_key, algorithm, "
            "fingerprint, duration_seconds) "
            "VALUES ('shared-shape', 'flac:eda2b', 'chromaprint', 'AQABzz-other', 245)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO acoustic_prints (content_signature, audio_key, algorithm, "
                "fingerprint, duration_seconds) "
                "VALUES ('shared-shape', 'flac:eda2b', 'chromaprint', 'AQABzz-again', 245)"
            )
        # NULL is never equal to NULL in SQLite, so the index is written over
        # `IFNULL` precisely so the legacy row keeps the guarantee it had.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO acoustic_prints (content_signature, algorithm, fingerprint, "
                "duration_seconds) VALUES ('shared-shape', 'chromaprint', 'AQABzz', 245)"
            )


def test_the_user_words_cross_onto_the_audio_they_were_said_about(tmp_path, monkeypatch) -> None:
    """Migration 22: the last table keyed by the shape alone held the user's word.

    `quality_overrides` is UNIQUE (unit_signature, content_signature), so one
    album and one signature is one word — right where two files are the same
    audio and wrong where they are two recordings of one shape. A UNIQUE cannot
    be dropped in place and a table is not rebuilt to do it, so the words move.

    Every word crosses with `audio_key` NULL and answers for its signature as
    before, which is what keeps the words already given in force.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 22)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO quality_overrides (unit_signature, content_signature, verdict, "
            "created_at) VALUES ('album', 'one-shape', 'honest', '2001-08-01 22:10:00')"
        )
        connection.execute(
            "INSERT INTO quality_overrides (unit_signature, content_signature, verdict) "
            "VALUES ('album', '', 'transcoded')"
        )
    monkeypatch.undo()

    assert Database(path, _logger()).initialize() == MIGRATIONS[-1].version

    with Database(path, _logger()).connect() as connection:
        carried = connection.execute(
            "SELECT unit_signature, content_signature, audio_key, verdict, created_at "
            "FROM quality_words ORDER BY content_signature"
        ).fetchall()
        assert [tuple(row) for row in carried] == [
            ("album", "", None, "transcoded", carried[0][4]),
            ("album", "one-shape", None, "honest", "2001-08-01 22:10:00"),
        ], "a word keeps the day it was given"
        # The other recording of that shape can now be spoken about on its own.
        connection.execute(
            "INSERT INTO quality_words (unit_signature, content_signature, audio_key, verdict) "
            "VALUES ('album', 'one-shape', 'flac:the-other', 'transcoded')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO quality_words (unit_signature, content_signature, audio_key, "
                "verdict) VALUES ('album', 'one-shape', 'flac:the-other', 'honest')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO quality_words (unit_signature, content_signature, verdict) "
                "VALUES ('album', 'one-shape', 'honest')"
            )


def test_threads_upserting_the_same_row_all_land_and_none_loses_its_batch(tmp_path) -> None:
    """A `SELECT` runs in autocommit, so read-to-decide and write are two
    transactions — and every upsert in this project has that shape.

    Left that way, two threads both read *not there*, both insert, and the
    second is refused by the unique index; its whole block rolls back. In
    `record_scan` that is a batch of units and files gone: albums that never
    appear, with nothing logged. With a deferred transaction one of two threads
    takes an `IntegrityError`; with `BEGIN IMMEDIATE` both succeed.

    The sleep is the window, kept small on purpose: what is asserted is that no
    thread is ever refused, not that a particular interleaving happened.
    """
    import threading
    import time

    path = tmp_path / "library.sqlite3"
    Database(path, _logger()).initialize()
    outcomes: dict[str, str] = {}

    def upsert(name: str) -> None:
        try:
            with Database(path, _logger()).connect(write=True) as connection:
                row = connection.execute(
                    "SELECT value FROM settings WHERE key = 'library_root'"
                ).fetchone()
                time.sleep(0.05)
                if row is None:
                    connection.execute(
                        "INSERT INTO settings (key, value) VALUES ('library_root', ?)", (name,)
                    )
                else:
                    connection.execute(
                        "UPDATE settings SET value = ? WHERE key = 'library_root'", (name,)
                    )
            outcomes[name] = "ok"
        except Exception as error:
            outcomes[name] = type(error).__name__

    threads = [threading.Thread(target=upsert, args=(str(n),)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)

    assert not any(thread.is_alive() for thread in threads), "an upsert never finished"
    assert set(outcomes.values()) == {"ok"}, f"a batch was lost: {outcomes}"
    with Database(path, _logger()).connect() as connection:
        held = connection.execute(
            "SELECT COUNT(*) FROM settings WHERE key = 'library_root'"
        ).fetchone()[0]
    assert held == 1, "read-then-write stayed one decision, so there is one row"


def test_opening_a_new_database_from_several_processes_at_once_never_refuses(tmp_path) -> None:
    """`PRAGMA journal_mode = WAL` needs a brief exclusive lock, and it is the
    one statement the busy timeout does not cover — it is refused immediately
    rather than waited on.

    Several threads opening one new database can therefore fail with `database
    is locked`. In use that is a double click, or the application relaunching
    itself, and an instance that refuses to start. The mode belongs to the file
    rather than to a connection, so a refusal means another instance is setting
    it, which is not a reason to stop.
    """
    import threading

    path = tmp_path / "library.sqlite3"
    failures: list[str] = []

    def open_it() -> None:
        try:
            Database(path, _logger()).initialize()
        except BaseException as error:
            failures.append(f"{type(error).__name__}: {error}")

    for _ in range(6):
        threads = [threading.Thread(target=open_it) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=40)
        assert not any(thread.is_alive() for thread in threads)

    assert failures == [], f"an instance refused to start: {failures}"


def test_the_backfill_anchors_only_what_one_album_answers_for(tmp_path, monkeypatch) -> None:
    """Migration 25 writes a column, so what it must prove is where it stays quiet.

    A download can be organised where it landed and then moved into the library
    by hand. This application renames and never moves between directories, so
    nothing records that last step, `landed_path` goes on naming a folder that
    is not there, and the download stops showing the album it brought in.

    The leaf name is the only tie those old rows still have, and it is worth
    exactly as much as its being unambiguous. So: anchor it when one album
    carries that name, and write nothing at all when two do or when none does.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 25)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect(write=True) as connection:
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) VALUES "
            # Moved into the library after being organised: the leaf is the
            # same, the parent is not.
            "(1, '/Music/Ravo Tellis - Quimária (1996) [FLAC]', 'quimaria', 12), "
            # Two copies of one record, which is what a shared signature *is*.
            "(2, '/Music/Copy A', 'shared', 3), "
            "(3, '/Downloads/Copy A', 'shared', 3), "
            # Renamed by hand after the move, so nothing ties it to its download.
            "(4, '/Music/Lo Verrin - Um Quelme Com Varzos (1990) [FLAC]', 'verrin', 10)"
        )
        connection.execute(
            "INSERT INTO downloads (id, source, directory, folder, files, size_bytes, "
            "whole_folder, collected_at, landed_path) VALUES "
            "(1, 'peer', '@@p\\Quimária', 'Quimária', 12, 500, 1, '2001-08-05 10:00:00', "
            "'/Incoming/Ravo Tellis - Quimária (1996) [FLAC]'), "
            "(2, 'peer', '@@p\\CopyA', 'Copy A', 3, 100, 1, '2001-08-06 00:00:00', "
            "'/Incoming/Copy A'), "
            "(3, 'peer', '@@p\\Verrin', 'Verrin', 10, 400, 1, '2001-08-07 12:00:00', "
            "'/Incoming/Um Quelme Com Varzos (1990)'), "
            # Never arrived: there is nothing to anchor, and the backfill must
            # not reach it.
            "(4, 'peer', '@@p\\Absent', 'Absent', 8, 635, 1, NULL, '')"
        )
    monkeypatch.undo()

    assert Database(path, _logger()).initialize() == MIGRATIONS[-1].version

    with Database(path, _logger()).connect() as connection:
        anchored = dict(
            connection.execute("SELECT id, unit_signature FROM downloads ORDER BY id").fetchall()
        )
    assert anchored[1] == "quimaria", "one album carries that name, so the tie is certain"
    assert anchored[2] == "", "two folders carry that name; naming one of them would be a guess"
    assert anchored[3] == "", "renamed after the move, so no name ties it to anything"
    assert anchored[4] == "", "nothing arrived, so there is nothing to be anchored to"


def test_opening_the_field_keeps_every_correction_and_its_release(tmp_path, monkeypatch) -> None:
    """Migration 26 drops the enumeration, and nothing the user typed may be lost.

    A CHECK that lists the known fields refuses every field added after it, and
    each addition costs a rebuild of the table. The set is written as its
    complement instead — any field that is not blank — so a field that does not
    exist yet is already allowed.

    Driven from a database created before 26, with corrections of all four
    earlier fields standing, one of them carrying a release. They must cross
    intact — id, date and `release_key` — the new field must become writable,
    and a field that is only whitespace must still be refused.
    """
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 26)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/quelvo', 'sig', 11)"
        )
        connection.execute(
            "INSERT INTO manual_corrections (id, album_unit_id, field, track_position, "
            "value, replaced, release_key, created_at) VALUES "
            "(51, 1, 'folder_name', NULL, 'Vimes [kept]', 'Vimes (1986)', NULL, "
            "'2001-08-01 10:00:00'), "
            "(52, 1, 'track_file', 10, 'signature-of-ten', NULL, 'discogs:1234567', "
            "'2001-08-02 11:00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
                "VALUES (1, 'extra_track', 11, 'signature-of-the-bonus')"
            )
    monkeypatch.undo()

    version = Database(path, _logger()).initialize()

    assert version == MIGRATIONS[-1].version
    with Database(path, _logger()).connect() as connection:
        kept = connection.execute(
            "SELECT id, field, track_position, value, replaced, release_key, created_at "
            "FROM manual_corrections ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in kept] == [
            (51, "folder_name", None, "Vimes [kept]", "Vimes (1986)", None, "2001-08-01 10:00:00"),
            (
                52,
                "track_file",
                10,
                "signature-of-ten",
                None,
                "discogs:1234567",
                "2001-08-02 11:00:00",
            ),
        ]
        # The member the list had never met, now writable — and so is the one
        # after it, which is the whole point of writing the set by complement.
        connection.execute(
            "INSERT INTO manual_corrections (album_unit_id, field, track_position, value, "
            "release_key) VALUES (1, 'extra_track', 11, 'sig-bonus', 'discogs:1234567')"
        )
        connection.execute(
            "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
            "VALUES (1, 'a_field_nobody_has_invented_yet', 1, 'x')"
        )
        # What is kept: a field that says nothing is still refused, so a typo
        # producing an empty word cannot become a row.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO manual_corrections (album_unit_id, field, track_position, value) "
                "VALUES (1, '   ', 2, 'x')"
            )


def test_the_playlists_go_and_nothing_beside_them_does(tmp_path, monkeypatch) -> None:
    """Migration 35 drops the two tables the player's saved lists lived in, and
    must take nothing else with it."""
    path = tmp_path / "library.sqlite3"
    older = tuple(migration for migration in MIGRATIONS if migration.version < 35)
    monkeypatch.setattr("diglibrary.database.connection.MIGRATIONS", older)
    Database(path, _logger()).initialize()
    with Database(path, _logger()).connect() as connection:
        connection.execute("INSERT INTO playlists (id, name) VALUES (1, 'a list')")
        connection.execute(
            "INSERT INTO playlist_tracks (playlist_id, place, audio_key, remembered_track) "
            "VALUES (1, 0, 'key', 'a track')"
        )
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/album', 'sig', 2)"
        )
        before = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    monkeypatch.undo()

    assert Database(path, _logger()).initialize() == MIGRATIONS[-1].version

    with Database(path, _logger()).connect() as connection:
        after = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert before - after == {"playlists", "playlist_tracks"}
        assert connection.execute("SELECT COUNT(*) FROM album_units").fetchone()[0] == 1
