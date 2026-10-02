"""Tests for persisting scans, identifications, plans, and their outcomes."""

import logging
import shutil
import sqlite3
import unicodedata
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.database.connection import Database
from diglibrary.database.library_store import (
    LibraryStore,
    StoredQuality,
    word_for,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import AppliedOperation
from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.library.models import AlbumUnit
from diglibrary.library.planner import ChangeOperation, OperationKind
from diglibrary.library.scanner import LibraryScanner

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


def test_a_renamed_album_updates_its_row_instead_of_creating_a_second(tmp_path: Path) -> None:
    """Content, not path, is what identifies an album across runs."""
    store = _store(tmp_path)
    library = tmp_path / "library"
    unit = _album(library, "wrong name")

    first = store.record_unit(unit)
    (library / "wrong name").rename(library / "Correct Name (1970) [FLAC]")
    renamed = _rescan(library)
    second = store.record_unit(renamed)

    assert renamed.unit_signature == unit.unit_signature
    assert second == first
    stored = store.unit_by_signature(unit.unit_signature)
    assert stored is not None
    assert stored.folder_path.name == "Correct Name (1970) [FLAC]"


def test_an_album_dragged_back_in_is_not_recorded_a_second_time(tmp_path: Path) -> None:
    """One folder spelled two ways in its accents must stay one album.

    A drop hands the path decomposed (`c` + U+0327) and a directory listing
    hands it composed (U+00E7); SQLite compares bytes, so the lookup by path
    misses the row already recorded. The fallback by content signature then
    asks whether the recorded folder is still on disk — APFS answers yes for
    either spelling — and would conclude it is looking at a second copy.
    """
    import unicodedata

    store = _store(tmp_path)
    library = tmp_path / "library"
    unit = _album(library, "Fumaça Roxa (1972) [FLAC]")
    first = store.record_unit(unit)

    dropped = replace(unit, folder_path=Path(unicodedata.normalize("NFD", str(unit.folder_path))))
    assert str(dropped.folder_path) != str(unit.folder_path), "the two spellings really do differ"

    assert store.record_unit(dropped) == first, "the same folder is the same album"
    assert len(store.library_units()) == 1


def test_organized_albums_are_recognized_without_rescanning_their_names(tmp_path: Path) -> None:
    """This is what makes a second pass over a large library cheap."""
    store = _store(tmp_path)
    unit = _album(tmp_path / "library", "album")
    unit_id = store.record_unit(unit)

    store.set_unit_state(unit_id, "organized")

    assert store.ids_in_state("organized") == {unit_id}
    assert store.ids_in_state("needs_review") == set()


def test_a_release_survives_a_round_trip_through_storage(tmp_path: Path) -> None:
    """The full payload is kept so a better matcher can re-run without the network."""
    store = _store(tmp_path)
    release = _release()

    row_id = store.record_release(release)
    restored = store.release(row_id)

    assert restored == release


def test_recording_the_same_release_twice_updates_one_row(tmp_path: Path) -> None:
    """A re-fetch refreshes the cache rather than duplicating it."""
    store = _store(tmp_path)

    first = store.record_release(_release())
    second = store.record_release(_release())

    assert first == second


def test_a_superseded_identification_is_kept_not_deleted(tmp_path: Path) -> None:
    """A rejected answer is evidence, so it must not be proposed again unknowingly."""
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    release_id = store.record_release(_release())

    store.record_identification(unit_id, release_id, "folder_name", 0.62, "first guess")
    store.record_identification(unit_id, release_id, "existing_tags", 0.95, "better guess")

    with store._database.connect() as connection:
        rows = connection.execute(
            "SELECT state, confidence FROM identifications WHERE album_unit_id = ? " "ORDER BY id",
            (unit_id,),
        ).fetchall()

    assert [row[0] for row in rows] == ["superseded", "proposed"]
    assert [row[1] for row in rows] == [0.62, 0.95]


def test_a_plan_and_its_outcome_are_recorded_operation_by_operation(tmp_path: Path) -> None:
    """The trail in the database must match the trail a reversal would follow."""
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    identification_id = store.record_identification(
        unit_id, store.record_release(_release()), "folder_name", 0.99, "durations agree"
    )
    operations = (
        ChangeOperation(
            sequence=1,
            kind=OperationKind.WRITE_TAGS,
            target_path=Path("/music/album/01.flac"),
            after_state={"tags": {"title": ["A"]}},
            before_state={"tags": {}},
        ),
        ChangeOperation(
            sequence=2,
            kind=OperationKind.RENAME_FILE,
            target_path=Path("/music/album/01.flac"),
            after_state={"path": "/music/album/01. A.flac"},
            before_state={"path": "/music/album/01.flac"},
        ),
    )

    plan_id = store.record_plan(unit_id, identification_id, operations)
    store.record_plan_outcome(plan_id, "applied", applied_sequences=(1, 2))

    with store._database.connect() as connection:
        state, applied_at = connection.execute(
            "SELECT state, applied_at FROM change_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        recorded = connection.execute(
            "SELECT sequence, kind, before_state, applied_at FROM change_operations "
            "WHERE change_plan_id = ? ORDER BY sequence",
            (plan_id,),
        ).fetchall()

    assert state == "applied"
    assert applied_at is not None
    assert [row[1] for row in recorded] == ["write_tags", "rename_file"]
    assert all(row[2] for row in recorded), "every operation must record what it replaced"
    assert all(row[3] for row in recorded)


def test_a_run_that_stopped_partway_is_recorded_as_partway(tmp_path: Path) -> None:
    """A plan that wrote only some of its operations must not read as applied.

    It is the row that most needs finding again, so it must say what it wrote,
    and it must stay revertible: a half-applied album is the one a reversal is
    for.
    """
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    operations = (
        ChangeOperation(
            sequence=1,
            kind=OperationKind.WRITE_TAGS,
            target_path=Path("/music/album/01.flac"),
            after_state={"tags": {"title": ["A"]}},
        ),
        ChangeOperation(
            sequence=2,
            kind=OperationKind.RENAME_FOLDER,
            target_path=Path("/music/album"),
            after_state={"path": "/music/Album (1970) [FLAC]"},
        ),
    )
    plan_id = store.record_plan(unit_id, None, operations)
    store.assign_run([plan_id], "run-1")

    store.record_execution(
        plan_id,
        (AppliedOperation(operation=operations[0], before_state={"tags": {}}),),
        complete=False,
    )

    entry = next(row for row in store.plan_history() if row["plan_id"] == plan_id)
    assert entry["state"] == "failed"
    assert (entry["applied_operations"], entry["operations"]) == (1, 2)
    assert store.run_plans("run-1") == (plan_id,), "a partial run must stay revertible"


def test_an_apply_killed_partway_still_leaves_a_trail_to_revert(tmp_path: Path) -> None:
    """The operations are written down as they happen, not at the end.

    Every failure the executor can see was already covered, because it returns
    the trail with the failure. This is the one it cannot see: the process
    disappears between the first write and the last, `record_execution` is
    never reached, and what is on disk has to be revertible anyway.
    """
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    operations = (
        ChangeOperation(
            sequence=1,
            kind=OperationKind.WRITE_TAGS,
            target_path=Path("/music/album/01.flac"),
            after_state={"tags": {"title": ["A"]}},
        ),
        ChangeOperation(
            sequence=2,
            kind=OperationKind.RENAME_FOLDER,
            target_path=Path("/music/album"),
            after_state={"path": "/music/Album (1970) [FLAC]"},
        ),
    )
    plan_id = store.record_plan(unit_id, None, operations)

    # The first operation lands, and nothing else ever runs.
    store.record_operation_applied(
        plan_id, AppliedOperation(operation=operations[0], before_state={"tags": {"title": ["B"]}})
    )

    trail = store.applied_trail(plan_id)
    assert [entry.operation.sequence for entry in trail] == [1]
    assert trail[0].before_state == {"tags": {"title": ["B"]}}, "what a reversal has to put back"
    entry = next(row for row in store.plan_history() if row["plan_id"] == plan_id)
    assert entry["state"] == "failed", "an interrupted run is a partial run"
    assert (entry["applied_operations"], entry["operations"]) == (1, 2)


def test_a_finished_run_is_not_pulled_back_to_partial_by_a_late_record(tmp_path: Path) -> None:
    """The running record never overwrites the verdict the finished run wrote."""
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    operations = (
        ChangeOperation(
            sequence=1,
            kind=OperationKind.WRITE_TAGS,
            target_path=Path("/music/album/01.flac"),
            after_state={"tags": {"title": ["A"]}},
        ),
    )
    plan_id = store.record_plan(unit_id, None, operations)
    applied = AppliedOperation(operation=operations[0], before_state={"tags": {}})

    store.record_operation_applied(plan_id, applied)
    store.record_execution(plan_id, (applied,), complete=True)
    store.record_operation_applied(plan_id, applied)

    entry = next(row for row in store.plan_history() if row["plan_id"] == plan_id)
    assert entry["state"] == "applied"


def test_a_measurement_taken_before_this_question_still_answers(tmp_path: Path) -> None:
    """A row without an audio key keeps answering for its signature.

    Nothing sweeps the stored measurements: a row written before the key
    existed answers exactly as it did, and a fresh measurement happens only
    where one is asked for.
    """
    store = _store(tmp_path)
    store.record_quality({"shared-shape": _measurement("transcoded", cutoff=19_000)})

    found = store.quality_for_files([("shared-shape", "flac:whatever")])[
        "shared-shape", "flac:whatever"
    ]

    assert found.encoding == "transcoded"
    assert found.audio_key is None
    # And it says so, because a number read from it may have been measured from
    # another recording and must never reach a filename.
    assert not found.is_this_file


def test_the_row_measured_from_this_audio_wins(tmp_path: Path) -> None:
    """Two recordings, one shape, two answers.

    A file that measures lossless must not show the verdict of another
    recording of the same shape whose wall at 19 kHz is real. Both files answer
    here, and each answers for itself.
    """
    store = _store(tmp_path)
    store.record_quality({"shared-shape": _measurement("transcoded", cutoff=19_000)})

    store.record_quality(
        {"shared-shape": _measurement("lossless", cutoff=None, audio_key="flac:first-recording")}
    )

    named = store.quality_for_files([("shared-shape", "flac:first-recording")])[
        "shared-shape", "flac:first-recording"
    ]
    assert named.encoding == "lossless"
    assert named.is_this_file
    # The other file of the same shape is untouched, and its verdict was right.
    unnamed = store.quality_for_files([("shared-shape", "flac:second-recording")])[
        "shared-shape", "flac:second-recording"
    ]
    assert unnamed.encoding == "transcoded"
    assert not unnamed.is_this_file


def test_two_files_of_one_shape_are_answered_in_one_pass_without_colliding(
    tmp_path: Path,
) -> None:
    """Two files of one shape asked about in a single call keep separate answers.

    Resolving each file correctly and then filing every answer under the
    *signature* makes two files of one shape write into one slot, where the
    last pair processed wins and both files read it. Asking one file at a time,
    as the test above does, cannot see this; the bench asks for the whole bench
    in a single call.
    """
    store = _store(tmp_path)
    store.record_quality({"shared-shape": _measurement("transcoded", cutoff=19_000)})
    store.record_quality(
        {"shared-shape": _measurement("lossless", cutoff=None, audio_key="flac:first-recording")}
    )

    found = store.quality_for_files(
        [("shared-shape", "flac:first-recording"), ("shared-shape", "flac:second-recording")]
    )

    assert found["shared-shape", "flac:first-recording"].encoding == "lossless"
    assert found["shared-shape", "flac:second-recording"].encoding == "transcoded"
    # And the file reading a row nobody named still says so, which is the
    # warning that was being suppressed on exactly the files that needed it.
    assert found["shared-shape", "flac:first-recording"].is_this_file
    assert not found["shared-shape", "flac:second-recording"].is_this_file


def test_measuring_the_same_audio_twice_replaces_rather_than_joins(tmp_path: Path) -> None:
    """One row per audio. Two would make the newest answer a matter of luck."""
    store = _store(tmp_path)

    store.record_quality({"shape": _measurement("lossless", cutoff=None, audio_key="flac:one")})
    store.record_quality({"shape": _measurement("transcoded", cutoff=16_000, audio_key="flac:one")})

    found = store.quality_for_files([("shape", "flac:one")])["shape", "flac:one"]
    assert found.encoding == "transcoded"
    assert found.cutoff_hertz == 16_000


def test_only_a_signature_with_a_named_measurement_is_worth_opening_a_file_for(
    tmp_path: Path,
) -> None:
    """The bench asks this before reading any bytes, so the ordinary path costs nothing.

    Computing an audio key means a seek and a read per file. It can only change
    the answer where a keyed row exists, which is only what has been measured in
    depth — so that is the only place it is paid for.
    """
    store = _store(tmp_path)
    store.record_quality({"untouched": _measurement("lossy", cutoff=16_000)})
    store.record_quality({"looked-at": _measurement("lossless", cutoff=None, audio_key="flac:x")})

    assert store.keyed_signatures(["untouched", "looked-at"]) == {"looked-at"}


def test_a_signature_only_ever_measured_in_depth_still_has_a_verdict(tmp_path: Path) -> None:
    """A file measured for the first time by the deep pass is not left unmeasured.

    Asked without a key there is no row that speaks for the whole signature, and
    answering nothing would make an album just analyzed look unmeasured on
    every other screen.
    """
    store = _store(tmp_path)
    store.record_quality({"fresh": _measurement("lossless", cutoff=None, audio_key="flac:only")})

    assert store.quality_for(["fresh"])["fresh"].encoding == "lossless"


def test_the_bench_reopens_without_walking_a_disk(tmp_path: Path) -> None:
    """What the bench keeps is the album's membership, never its verdict.

    A verdict is re-judged from the numbers on every read, so storing one would
    be the one thing this table must not do. Storing the membership is what
    lets the screen open at once: reading the albums back is far cheaper than
    walking the disk.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    first = _album(library, "first")
    second = _album(library, "second")
    root_id = store.add_bench_root(library)

    store.record_bench_albums(root_id, [first, second])

    albums = store.bench_albums()
    assert [album.folder_path.name for album in albums] == ["first", "second"]
    assert all(album.files for album in albums), "membership is what a median needs"
    assert {file.content_signature for file in albums[0].files} == {
        file.content_signature for file in first.audio_files
    }
    assert store.bench_roots()[0].albums == 2
    assert store.bench_roots()[0].walked_at is not None


def test_a_second_walk_says_what_is_there_now(tmp_path: Path) -> None:
    """The walk answers *what is under this folder now*, so it replaces rather than adds.

    An album that was moved or deleted has to leave the bench — the same rule
    the Library applies — and one that was added has to arrive. A walk that
    merged would leave a folder on screen forever after it was gone.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    stays = _album(library, "stays")
    leaves = _album(library, "leaves")
    root_id = store.add_bench_root(library)
    store.record_bench_albums(root_id, [stays, leaves])

    arrives = _album(library, "arrives")
    store.record_bench_albums(root_id, [stays, arrives])

    assert [album.folder_path.name for album in store.bench_albums()] == ["arrives", "stays"]


def test_taking_a_folder_off_the_bench_keeps_every_measurement(tmp_path: Path) -> None:
    """Removing a root takes rows off a screen. It touches no file and no measurement.

    What was measured is keyed by the audio and not by any of this, so putting
    the folder back costs a walk and no decoding at all.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    album = _album(library, "album")
    root_id = store.add_bench_root(library)
    store.record_bench_albums(root_id, [album])
    store.record_quality(
        {
            file.content_signature: _measurement("lossless", cutoff=None)
            for file in album.audio_files
        }
    )

    store.remove_bench_root(root_id)

    assert store.bench_roots() == ()
    assert store.bench_albums() == ()
    kept = store.quality_for(file.content_signature for file in album.audio_files)
    assert len(kept) == len({file.content_signature for file in album.audio_files})


def test_the_same_folder_is_only_ever_on_the_bench_once(tmp_path: Path) -> None:
    """Adding it twice is pointing at it twice, not a second bench row."""
    store = _store(tmp_path)

    first = store.add_bench_root(tmp_path / "library")
    second = store.add_bench_root(tmp_path / "library")

    assert first == second
    assert len(store.bench_roots()) == 1


def test_the_walk_names_the_audio_and_a_remembered_key_only_fills_a_gap(tmp_path: Path) -> None:
    """The walk names the audio of every file it reads (migration 20).

    Reading a key is cheap against a file the walk already has open — so the
    walk is where it belongs, and it answers about the file as it is right now.
    A key remembered from before fills the gap where a walk could not read one,
    and a file replaced in place drops the key it had, because that slot is
    holding different audio.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    album = _album(library, "album")
    root_id = store.add_bench_root(library)
    store.record_bench_albums(root_id, [album])
    walked = store.bench_albums()[0].files[0]
    album_id = store.bench_albums()[0].album_id

    assert walked.audio_key, "the walk itself names the audio it just read"

    # A file the walk could not name keeps whatever is remembered about it.
    store.remember_audio_keys({(album_id, walked.name): "flac:remembered"})
    store.record_bench_albums(root_id, [replace(album, audio_files=())])
    store.record_bench_albums(root_id, [album])
    assert store.bench_albums()[0].files[0].audio_key == walked.audio_key
    # Different audio under the same name is named afresh, never left behind.
    shutil.copy(FIXTURES / "tone-long.flac", library / "album" / walked.name)
    store.record_bench_albums(root_id, [_rescan(library, "album")])
    replaced = store.bench_albums()[0].files[0].audio_key
    assert replaced and replaced != walked.audio_key


def _measurement(encoding: str, cutoff: int | None, audio_key: str | None = None) -> StoredQuality:
    return StoredQuality(
        encoding=encoding,
        effective_bitrate_kbps=256 if encoding == "transcoded" else None,
        cutoff_hertz=cutoff,
        steepest_drop_db=22.1,
        findings=("transcoded",) if encoding == "transcoded" else (),
        reason=f"measured {encoding}",
        decay_db=22.1 if encoding == "transcoded" else 6.1,
        ceiling_db=-104.3 if encoding == "transcoded" else -80.1,
        audio_key=audio_key,
    )


def _store(tmp_path: Path) -> LibraryStore:
    database = Database(tmp_path / "library.sqlite3", logging.getLogger("test.store"))
    database.initialize()
    return LibraryStore(database, logging.getLogger("test.store"))


def _album(library: Path, folder_name: str, tracks: int = 2) -> AlbumUnit:
    """Create an album whose content differs from the others unless asked otherwise."""
    folder = library / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    sources = ("tone.flac", "tone-long.flac")
    for index in range(tracks):
        shutil.copy(FIXTURES / sources[index % len(sources)], folder / f"track{index}.flac")
    return _rescan(library, folder_name)


def _rescan(library: Path, folder_name: str | None = None) -> AlbumUnit:
    units = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.store")).scan(library)
    if folder_name is None:
        return units[0]
    return next(unit for unit in units if unit.folder_path.name == folder_name)


def _release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1000001",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão", sort_name="Acordeão, Marina do"),),
        tracks=(
            TrackMetadata(
                title="Primeira",
                position=1,
                medium_number=1,
                position_on_medium=1,
                duration_ms=400,
                isrcs=("ZZABC1234567",),
                external_ids={MetadataSources.MUSICBRAINZ: "track-mbid"},
            ),
        ),
        released_on=date(1955, 3, 1),
        labels=("Selo",),
        catalog_numbers=("CAT-1",),
        country="XX",
        genres=("Polka",),
        external_ids={MetadataSources.DISCOGS: "r1000001"},
    )


def test_two_copies_of_the_same_album_are_two_units(tmp_path: Path) -> None:
    """Identical audio in two folders is a duplicate download, not one album moved."""
    store = _store(tmp_path)
    library = tmp_path / "library"

    first = store.record_unit(_album(library, "album"))
    second = store.record_unit(_album(library, "album (1)"))

    assert first != second


def test_a_fingerprint_is_remembered_against_the_audio_not_the_file(tmp_path) -> None:
    """Organizing renames every file of an album; the fingerprint must outlive it.

    Decoding audio is the most expensive operation this project performs, so
    the store exists to pay it once — and keyed by path it would have been paid
    again for every album the app succeeded on.
    """
    store = _store(tmp_path)
    printed = AudioFingerprint(fingerprint="AQADtIqoKXGU", duration_seconds=186)

    assert store.fingerprint_for("audio-sig", "key-1") is None

    store.remember_fingerprint("audio-sig", "key-1", printed)

    assert store.fingerprint_for("audio-sig", "key-1") == printed
    # Recomputed after a re-encode, the same audio keeps one row rather than
    # accumulating them.
    store.remember_fingerprint("audio-sig", "key-1", AudioFingerprint("AQAB-new", 187))
    again = store.fingerprint_for("audio-sig", "key-1")
    assert again is not None and again.fingerprint == "AQAB-new"


def test_how_an_album_was_identified_survives_the_restart(tmp_path) -> None:
    """`acoustic` is an allowed method, so it has to be written and read back.

    The Library rebuilds itself from these rows, so without reading the method
    back an identification the fingerprint made returns from a restart
    indistinguishable from a text search, and the window has nothing to say
    about it.
    """
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    release_row = store.record_release(_release())

    store.record_identification(
        unit_id, release_row, method="acoustic", confidence=0.97, explanation="the audio"
    )

    recorded = store.identification_for(unit_id)
    assert recorded is not None
    assert recorded["method"] == "acoustic"


def test_the_bench_follows_the_album_to_the_folder_it_now_lives_in(tmp_path: Path) -> None:
    """Organising renames the folder, and the bench has to follow it.

    `quality_bench_albums` holds a folder path, so an album put on the bench
    and then organised — which is the ordinary order of work here — would leave
    that screen pointing at a name that no longer exists, and opening it would
    answer `Not a folder`. Filing the album away afterwards moves it again.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/was', 'sig', 2)"
        )
        connection.execute("INSERT INTO quality_bench_roots (path) VALUES ('/music/was')")
        root = connection.execute("SELECT id FROM quality_bench_roots").fetchone()[0]
        connection.execute(
            "INSERT INTO quality_bench_albums (root_id, folder_path, unit_signature, track_count) "
            "VALUES (?, '/music/was', 'sig', 2)",
            (root,),
        )

    store.relocate_unit("/music/was", "/music/now (1979) [FLAC]")

    with store._database.connect() as connection:
        bench = connection.execute("SELECT folder_path FROM quality_bench_albums").fetchone()[0]
        root = connection.execute("SELECT path FROM quality_bench_roots").fetchone()[0]
    assert (
        bench == "/music/now (1979) [FLAC]"
    ), "the bench has to move with the album, or its screen answers `Not a folder`"
    assert root == "/music/now (1979) [FLAC]", (
        "and so does the root: pointing the bench at one album's own folder is an "
        "ordinary gesture, and a root left behind only trades `Not a folder` for a "
        "re-walk that finds nothing"
    )


def test_the_registered_files_move_with_the_album_that_holds_them(tmp_path: Path) -> None:
    """`audio_files` keeps a whole path, so it has to be carried when the album moves.

    A row left behind is not merely stale — the same file registered under two
    names is two names over one content signature, which is what
    `ambiguous_signatures` looks for.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count) "
            "VALUES ('/music/was', 'sig', 1)"
        )
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/was/01 Song.flac', 'aaa', 1, 'now')"
        )
        # A folder whose name starts with the same letters, which is why the
        # prefix has to carry its separator.
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/was not this one/01 Song.flac', 'bbb', 1, 'now')"
        )

    store.relocate_unit("/music/was", "/music/now (1979) [FLAC]")

    with store._database.connect() as connection:
        paths = [row[0] for row in connection.execute("SELECT path FROM audio_files ORDER BY id")]
    assert paths[0] == "/music/now (1979) [FLAC]/01 Song.flac", (
        "the files inside a moved album have to move with it, or every one of "
        "them names a file that is no longer there"
    )
    assert (
        paths[1] == "/music/was not this one/01 Song.flac"
    ), "and a folder that merely starts with the same letters is a different folder"


def test_the_library_stops_calling_one_file_two_files_after_it_moves(tmp_path: Path) -> None:
    """The cost of the dead rows, crossing to the screen that pays it.

    A file registered under the name it had and the name it has is two
    differently-named rows over one content signature, so `ambiguous_signatures`
    calls it ambiguous and the measured bitrate is withheld from its own name.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/was/Artist - Song.flac', 'aaa', 1, 'now')"
        )

    # What an apply does, in the order it does it: the file first, while the
    # folder still has the name it was planned under, and the folder last.
    store.relocate_file("/music/was/Artist - Song.flac", "/music/was/01. Song.flac")
    store.relocate_unit("/music/was", "/music/now (1979) [FLAC]")
    # And the scan that reads the album where it lives now registers the file it
    # finds there, which is the second row that used to appear.
    with store._database.connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO audio_files "
            "(path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/now (1979) [FLAC]/01. Song.flac', 'aaa', 1, 'now')"
        )

    assert store.ambiguous_signatures(["aaa"]) == set(), (
        "one file that moved is one file: with the old row left behind, the "
        "library reads two names over one signature and withholds the number it "
        "measured from the file it measured it on"
    )


def test_a_moved_album_finds_its_files_under_either_spelling_of_an_accent(
    tmp_path: Path,
) -> None:
    """The same trap as `_both_spellings`, one table over."""
    store = _store(tmp_path)
    composed = unicodedata.normalize("NFC", "/music/Quixotação")
    decomposed = unicodedata.normalize("NFD", "/music/Quixotação")
    assert composed != decomposed
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES (?, 'aaa', 1, 'now')",
            (f"{decomposed}/01 Song.flac",),
        )

    store.relocate_unit(composed, "/music/now")

    with store._database.connect() as connection:
        path = connection.execute("SELECT path FROM audio_files").fetchone()[0]
    assert path == "/music/now/01 Song.flac", (
        "the file system opens either spelling and SQLite compares bytes, so a "
        "path looked up in one spelling has to be offered the other"
    )


def test_a_file_already_registered_where_the_album_is_going_is_left_alone(
    tmp_path: Path,
) -> None:
    """`path` is unique, so the destination may already be taken.

    A scan can reach the album at its new name before anything follows it there.
    This guards the move itself: written as a plain `UPDATE` it raises
    `IntegrityError` in the middle of an apply, and written as `OR REPLACE` it
    deletes the row that describes the file which is actually there.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/was/01 Song.flac', 'aaa', 1, 'then')"
        )
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/now/01 Song.flac', 'aaa', 1, 'now')"
        )

    store.relocate_unit("/music/was", "/music/now")

    with store._database.connect() as connection:
        rows = connection.execute(
            "SELECT path, modified_at FROM audio_files ORDER BY id"
        ).fetchall()
    assert rows[1] == ("/music/now/01 Song.flac", "now"), (
        "the row describing the file that is there stays exactly as the scan " "wrote it"
    )


def test_a_renamed_file_is_followed_where_it_stands(tmp_path: Path) -> None:
    """An organized album renames its files first and its folder last.

    Following the folder alone leaves every file row naming a name that no
    longer exists, and the file renames are where most such rows come from.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/was/Artist - Song.flac', 'aaa', 1, 'now')"
        )

    moved = store.relocate_file("/music/was/Artist - Song.flac", "/music/was/01. Song.flac")

    with store._database.connect() as connection:
        path = connection.execute("SELECT path FROM audio_files").fetchone()[0]
    assert moved == 1
    assert path == "/music/was/01. Song.flac"


def test_two_recordings_of_one_shape_keep_two_prints_and_two_answers(tmp_path: Path) -> None:
    """Two different recordings under one content signature each keep their own print.

    Two recordings of the same length in the same format share a signature.
    Read by shape alone, the second file would be handed the first one's
    identity — and no request would ever be spent to find out.
    """
    store = _store(tmp_path)
    first_take = AudioFingerprint(fingerprint="AQAB-first-take", duration_seconds=240)
    second_take = AudioFingerprint(fingerprint="AQAB-second-take", duration_seconds=240)

    store.remember_fingerprint("shared-shape", "win:aaa111", first_take)
    store.remember_fingerprint("shared-shape", "flac:bbb222", second_take)
    store.remember_lookup("shared-shape", "win:aaa111", "rec-0001", 0.94)

    assert store.fingerprint_for("shared-shape", "win:aaa111") == first_take
    assert (
        store.fingerprint_for("shared-shape", "flac:bbb222") == second_take
    ), "one recording's print must not overwrite another's just for sharing a shape"
    answers = store.acoustic_answers(
        [("shared-shape", "win:aaa111"), ("shared-shape", "flac:bbb222")]
    )
    # Answered by the pair, because both files answer under one signature and
    # the question is which of them the identity belongs to.
    named = answers[("shared-shape", "win:aaa111")]
    other = answers[("shared-shape", "flac:bbb222")]
    assert named.recording_id == "rec-0001" and named.is_this_audio
    assert other.recording_id is None and not other.asked, (
        "the file that was never put to the service has to say so, instead of "
        "wearing the answer its neighbour paid for"
    )


def test_a_print_from_before_the_audio_had_a_name_is_recomputed_not_trusted(
    tmp_path: Path,
) -> None:
    """A legacy row may have come from either recording, so it settles nothing.

    Recomputing costs a fraction of a second and no request; being wrong costs
    a record wearing another record's name. Asked without a key — by something
    that cannot name its audio — the legacy row answers exactly as it always
    has.
    """
    store = _store(tmp_path)
    legacy = AudioFingerprint(fingerprint="AQAB-legacy", duration_seconds=240)
    store.remember_fingerprint("shared-shape", None, legacy)

    assert store.fingerprint_for("shared-shape", "flac:bbb222") is None
    assert store.fingerprint_for("shared-shape") == legacy


def test_an_identical_print_claims_the_row_it_was_always_about(tmp_path: Path) -> None:
    """The judge: the print settles what the shape could not, and the answer comes with it.

    A row written before the audio had a name is neither thrown away nor left
    orphaned. When the fingerprint computed from a named audio comes back byte
    for byte the same, that row was always about this audio — and the lookup it
    spent a rate-limited request on is adopted along with it. Nothing sweeps the
    library; the repair happens where the work is.
    """
    store = _store(tmp_path)
    printed = AudioFingerprint(fingerprint="AQAB-first-take", duration_seconds=240)
    store.remember_fingerprint("shared-shape", None, printed)
    store.remember_lookup("shared-shape", None, "rec-0001", 0.94)

    store.remember_fingerprint("shared-shape", "win:aaa111", printed)

    answer = store.acoustic_answers([("shared-shape", "win:aaa111")])[
        ("shared-shape", "win:aaa111")
    ]
    assert answer.recording_id == "rec-0001", "the request that bought this is not spent again"
    assert (
        answer.is_this_audio
    ), "and it is now proved to be about this file, not merely filed near it"
    with store._database.connect() as connection:
        rows = connection.execute("SELECT audio_key FROM acoustic_prints").fetchall()
    assert [row[0] for row in rows] == ["win:aaa111"], "one row, claimed rather than duplicated"


def test_a_different_print_leaves_the_legacy_row_where_it_is(tmp_path: Path) -> None:
    """The other half of the judge, and the half that must not adopt.

    A fingerprint that differs is the proof that this is the *other* recording,
    so the legacy row stays exactly where it is — still answering for whatever
    it came from — and this audio gets a row of its own.
    """
    store = _store(tmp_path)
    store.remember_fingerprint(
        "shared-shape", None, AudioFingerprint(fingerprint="AQAB-first-take", duration_seconds=240)
    )
    store.remember_lookup("shared-shape", None, "rec-0001", 0.94)

    store.remember_fingerprint(
        "shared-shape",
        "flac:bbb222",
        AudioFingerprint(fingerprint="AQAB-second-take", duration_seconds=240),
    )

    with store._database.connect() as connection:
        rows = connection.execute(
            "SELECT audio_key, fingerprint, external_recording_id FROM acoustic_prints ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (None, "AQAB-first-take", "rec-0001"),
        ("flac:bbb222", "AQAB-second-take", None),
    ]


def test_a_scan_writes_down_which_audio_each_file_holds(tmp_path: Path) -> None:
    """A scan names the audio of every file it registers.

    Without it the key exists only where a file has been measured or where the
    bench has been asked for it, and there is nothing a duplicate map could be
    made from.
    """
    store = _store(tmp_path)
    unit = _album(tmp_path / "library", "album")

    store.record_unit(unit)

    with store._database.connect() as connection:
        keys = [row[0] for row in connection.execute("SELECT audio_key FROM audio_files")]
    assert all(keys), "the scan already had the file open; naming its audio is the cheap half"


def test_the_same_rip_filed_twice_is_one_recording_held_in_two_places(tmp_path: Path) -> None:
    """The duplicate map, from what the walks already wrote and no decoding at all.

    The same audio key is the same stream byte for byte — one rip in two
    folders. Nothing is proposed and nothing is moved: which copy to keep is
    the user's decision, and this application says what it measured and leaves
    the files alone.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    _album(library, "Downloads copy")
    store.record_unit(_rescan(library, "Downloads copy"))
    shutil.copytree(library / "Downloads copy", library / "Filed away")
    store.record_unit(_rescan(library, "Filed away"))

    held = store.duplicate_audio()

    assert held, "one rip in two folders is one recording held twice"
    assert {copy.album for copy in held[0].copies} == {"Downloads copy", "Filed away"}
    assert held[0].proof == "audio"


def test_one_album_on_two_screens_is_not_a_duplicate_of_itself(tmp_path: Path) -> None:
    """The Library and the bench can both hold one album, and that is one file."""
    store = _store(tmp_path)
    library = tmp_path / "library"
    album = _album(library, "album")
    store.record_unit(album)
    root_id = store.add_bench_root(library)
    store.record_bench_albums(root_id, [album])

    assert store.duplicate_audio() == (), "two registers naming one path is one file"


def test_one_performance_encoded_twice_is_found_by_the_print_alone(tmp_path: Path) -> None:
    """What the stream digest cannot see and the fingerprint can.

    Two rips of one performance are different bytes and therefore different
    audio keys — the identical-stream test says nothing about them. The acoustic
    print does, and it costs a fingerprint per file, so it answers only about
    files something has already asked about.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    for folder, source in (("The FLAC", "tone.flac"), ("Another rip", "tone-long.flac")):
        (library / folder).mkdir(parents=True)
        shutil.copy(FIXTURES / source, library / folder / "01. One Song.flac")
        store.record_unit(_rescan(library, folder))
    with store._database.connect() as connection:
        rows = connection.execute(
            "SELECT content_signature, audio_key FROM audio_files ORDER BY path"
        ).fetchall()
    assert len({key for _, key in rows}) == 2, "different bytes, so the stream digest differs"
    # What a fingerprint of each would have come back saying: one performance.
    one = AudioFingerprint(fingerprint="AQAB-same-performance", duration_seconds=186)
    for signature, key in rows:
        store.remember_fingerprint(signature, key, one)

    held = [found for found in store.duplicate_audio() if found.proof == "print"]

    assert held, "the same performance encoded twice is still one recording"
    assert len({copy.audio_key for copy in held[0].copies}) > 1, (
        "and it is precisely the case the stream digest cannot see, so the "
        "copies must have different audio keys"
    )


def test_what_the_source_printed_as_a_position_survives_the_cache(tmp_path: Path) -> None:
    """`published_position` is part of the cached payload.

    It is what the source printed, and it is the only thing the medley fold
    reads. `metadata_releases` promises a payload "complete enough to rebuild it
    exactly"; without this field every release read back has every position as
    None, the fold cannot fire, and an album with a medley comes back with one
    track saying *no file here*.

    Restoring the Library, adopting a release by hand and pasting a link all plan
    through `adopt_release`, and all three read this row.
    """
    store = _store(tmp_path)
    printed = _release().tracks[0]
    release = replace(
        _release(),
        tracks=(
            replace(printed, published_position="A5.1"),
            replace(printed, title="Segunda", position=2, published_position="A5.2"),
        ),
    )

    restored = store.release(store.record_release(release))

    assert restored is not None
    assert [track.published_position for track in restored.tracks] == ["A5.1", "A5.2"]
    assert restored == release, "the payload is complete enough to rebuild it exactly"


def test_the_shelf_reads_the_album_s_own_year_not_the_pressing_s(tmp_path: Path) -> None:
    """One card must not show two years depending on how far a restore had got.

    `metadata_releases` keeps `released_on` in a column and the album's own year
    inside the payload, which is harmless while every reader deserializes the
    whole release. The Library draws itself from those columns, so a shelf
    reading `released_on` would show the pressing's year, and the album's own
    once the album was read back, with nothing having happened in between.

    Fails against the schema without migration 23, and against a `shelf()` that
    reads `released_on`.
    """
    store = _store(tmp_path)
    unit = _album(tmp_path / "library", "album")
    unit_id = store.record_unit(unit)
    release = replace(
        _release(),
        released_on=date(2012, 1, 1),
        original_released_on=date(1979, 1, 1),
    )
    store.record_identification(unit_id, store.record_release(release), "folder_name", 0.9, "")

    row = next(row for row in store.shelf() if row["unit_id"] == unit_id)

    assert row["year"] == 1979, "the year the album is from, which is the year the card shows"


def test_the_separator_a_source_publishes_survives_the_cache(tmp_path: Path) -> None:
    """The same trap as `published_position`, on the separator between artists.

    A separator kept only in memory is a separator the restore loses, and every
    restored release would go back to joining its artists with `&` — which is
    the whole defect. The row also carries `primary_artist`, so the credit the
    cache shows has to be the published one too.
    """
    store = _store(tmp_path)
    release = replace(
        _release(),
        artists=(
            ArtistMetadata(name="Tiago Bragança", joined_by="Presents", credited_as="Tiago"),
            ArtistMetadata(name="A Fabulosa Fanfarra Do Bragança"),
        ),
    )

    row_id = store.record_release(release)
    restored = store.release(row_id)

    assert restored is not None
    assert restored.artists[0].joined_by == "Presents"
    assert restored.artists[0].credited_as == "Tiago"
    assert restored == release, "the payload is complete enough to rebuild it exactly"
    with store._database.connect() as connection:
        credit = connection.execute(
            "SELECT primary_artist FROM metadata_releases WHERE id = ?", (row_id,)
        ).fetchone()[0]
    assert credit == "Tiago Bragança Presents A Fabulosa Fanfarra Do Bragança"


def test_one_folder_spelled_two_ways_moves_as_one_row(tmp_path: Path) -> None:
    """Two rows claiming one folder must not collide when the folder moves.

    Two rows can claim one folder — a composed one and a decomposed one, from
    the drop `_both_spellings` was written for — one still on the shelf and one
    cleared. Offering both spellings matches both rows and moves them onto one
    path: `UNIQUE constraint failed: album_units.folder_path`, and the exception
    takes down the whole of `library_present`, so no album is followed to where
    it now lives and none is let go. One row moves, the one still on the shelf,
    and nothing is deleted.
    """
    store = _store(tmp_path)
    composed = unicodedata.normalize("NFC", "/music/Quixotação (1971) [FLAC]")
    decomposed = unicodedata.normalize("NFD", "/music/Quixotação (1971) [FLAC]")
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (1, ?, 'sig', 12)",
            (composed,),
        )
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count, cleared_at) "
            "VALUES (2, ?, 'sig', 12, CURRENT_TIMESTAMP)",
            (decomposed,),
        )

    moved = store.relocate_unit(composed, "/music/filed away")

    assert moved == 1, "one folder is one row, however many rows claim it"
    with store._database.connect() as connection:
        rows = dict(connection.execute("SELECT id, folder_path FROM album_units").fetchall())
    assert rows[1] == "/music/filed away", "and the row still on the shelf is the one that follows"
    assert rows[2] == decomposed, "the cleared one stays where it is; nothing is deleted"


def test_an_album_recorded_where_this_one_is_going_is_not_overwritten(tmp_path: Path) -> None:
    """A second row may already describe the destination, and it is the truthful one.

    Left to raise, one collision ends a sweep that was following every album in
    the library. The row that stays put is worse than the row that moves, and
    both are better than every other album going unexamined.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (1, '/music/was', 'sig-a', 12)"
        )
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (2, '/music/now', 'sig-b', 9)"
        )

    assert store.relocate_unit("/music/was", "/music/now") == 0

    with store._database.connect() as connection:
        rows = dict(connection.execute("SELECT id, folder_path FROM album_units").fetchall())
    assert rows == {1: "/music/was", 2: "/music/now"}


def test_an_album_that_could_not_follow_its_folder_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Declining the move is right; declining it silently is not.

    The row already there describes the folder that is there, so the move is
    declined. It has to be logged: the album keeps a path that no longer exists,
    the Library then takes it off the shelf, and an album that was just
    organized disappears with nothing anywhere saying why.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (1, '/music/was', 'sig-a', 12)"
        )
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (2, '/music/now', 'sig-b', 9)"
        )

    with caplog.at_level(logging.WARNING, logger="test.store"):
        store.relocate_unit("/music/was", "/music/now")

    declined = [
        record
        for record in caplog.records
        if getattr(record, "operation", "") == "database.relocate.declined"
        and getattr(record, "table", "") == "album_units"
    ]
    assert len(declined) == 1, "a move nobody could make is a thing to say once"
    assert declined[0].occupied_by == 2, "and the sentence names who is already there"


def test_files_that_could_not_follow_their_album_are_counted_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same silence one table over, which is where dead file rows come from.

    A registered file whose destination is already registered stays under its old
    name — so following the album is itself what makes that row dead. The file is
    never lost, because the row holding the destination is the one describing it,
    but it has to be counted and said.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (1, '/music/was', 'sig-a', 12)"
        )
        for path in ("/music/was/01 Song.flac", "/music/was/02 Other.flac"):
            connection.execute(
                "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
                "VALUES (?, 'aaa', 1, 'then')",
                (path,),
            )
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/now/01 Song.flac', 'aaa', 1, 'now')"
        )

    with caplog.at_level(logging.WARNING, logger="test.store"):
        moved = store.relocate_unit("/music/was", "/music/now")

    assert moved == 1, "the album itself moves; this is about the files under it"
    declined = [
        record
        for record in caplog.records
        if getattr(record, "operation", "") == "database.relocate.declined"
        and getattr(record, "table", "") == "audio_files"
    ]
    assert len(declined) == 1
    assert declined[0].rows == 1, "one of the two files had its name taken, and only one"
    assert declined[0].first == "/music/was/01 Song.flac"


def test_an_album_whose_files_all_follow_it_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning every album raises is a warning nobody reads.

    The ordinary apply — every file free to take its new name — must stay silent,
    which is the half of this that a log line cannot be trusted to get right on
    its own.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO album_units (id, folder_path, unit_signature, track_count) "
            "VALUES (1, '/music/was', 'sig-a', 12)"
        )
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES ('/music/was/01 Song.flac', 'aaa', 1, 'then')"
        )

    with caplog.at_level(logging.WARNING, logger="test.store"):
        store.relocate_file("/music/was/01 Song.flac", "/music/was/01. Song.flac")
        store.relocate_unit("/music/was", "/music/now")

    assert not [
        record
        for record in caplog.records
        if getattr(record, "operation", "") == "database.relocate.declined"
    ]


def test_a_row_naming_a_file_that_is_gone_is_not_a_second_file(tmp_path: Path) -> None:
    """A row whose file no longer exists does not make a signature ambiguous.

    A file registered under the name it had and the name it has is two rows over
    one signature, which is what this guard is looking for — so the library
    would call one file two and withhold from a track the bitrate measured on
    that very track.

    The rows are kept rather than deleted, and the answer corrects itself the
    moment a file comes back.
    """
    store = _store(tmp_path)
    here = tmp_path / "library"
    here.mkdir()
    shutil.copy(FIXTURES / "tone.flac", here / "01. Song.flac")
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES (?, 'aaa', 1, 'now')",
            (str(here / "01. Song.flac"),),
        )
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES (?, 'aaa', 1, 'then')",
            (str(here / "Artist - Song.flac"),),  # the name it used to have
        )

    def on_disk(path: str) -> bool:
        return Path(path).exists()

    assert store.ambiguous_signatures(["aaa"]) == {"aaa"}, "asked without the check, as before"
    assert (
        store.ambiguous_signatures(["aaa"], on_disk) == set()
    ), "the name nothing answers to is history, not a rival"
    # And a real rival still is one: two files, both there, different names.
    shutil.copy(FIXTURES / "tone-long.flac", here / "Artist - Song.flac")
    assert store.ambiguous_signatures(["aaa"], on_disk) == {"aaa"}


def test_one_word_answers_for_two_files_that_are_the_same_audio(tmp_path: Path) -> None:
    """One word given to one file answers for another file holding the same audio.

    Two files in one folder can be the same recording under two names, with the
    same digest of decoded samples. One word answering for both is not a leak,
    it is the only answer that is not a contradiction.
    """
    store = _store(tmp_path)

    store.record_quality_override("album", "honest", "shared-shape", "flac:same-audio")

    words = store.quality_words("album")
    assert word_for(words, "shared-shape", "flac:same-audio") == "honest"
    assert word_for(words, "shared-shape", "flac:same-audio") == "honest", "both copies, one audio"


def test_a_word_about_one_recording_never_reaches_another_of_the_same_shape(
    tmp_path: Path,
) -> None:
    """A word about one recording must not land on another of the same shape.

    A signature is a declared shape rather than a recording, so one album can
    hold two tracks with different audio under one signature. Keyed by the
    signature alone, a word about one would land on the other.
    """
    store = _store(tmp_path)

    store.record_quality_override("album", "honest", "one-shape", "flac:seventh-track")

    words = store.quality_words("album")
    assert word_for(words, "one-shape", "flac:seventh-track") == "honest"
    assert (
        word_for(words, "one-shape", "flac:ninth-track") is None
    ), "the other recording was never spoken about, and must not wear that word"
    # And it can be given its own, opposite word, which the old table could not hold.
    store.record_quality_override("album", "transcoded", "one-shape", "flac:ninth-track")
    words = store.quality_words("album")
    assert word_for(words, "one-shape", "flac:seventh-track") == "honest"
    assert word_for(words, "one-shape", "flac:ninth-track") == "transcoded"


def test_a_word_said_before_the_audio_had_a_name_still_answers(tmp_path: Path) -> None:
    """Every word already given keeps working, and nothing sweeps them.

    The same shape as migrations 13, 19 and 20: a NULL key means *said before
    this existed*, and it answers for the signature exactly as it did.
    """
    store = _store(tmp_path)

    store.record_quality_override("album", "honest", "old-shape")

    words = store.quality_words("album")
    assert word_for(words, "old-shape", None) == "honest"
    assert word_for(words, "old-shape", "flac:whatever") == "honest"
    # And a word about one audio wins over it, for that audio only.
    store.record_quality_override("album", "transcoded", "old-shape", "flac:this-one")
    words = store.quality_words("album")
    assert word_for(words, "old-shape", "flac:this-one") == "transcoded"
    assert word_for(words, "old-shape", "flac:another") == "honest"


def test_taking_a_word_back_takes_back_the_one_given_before(tmp_path: Path) -> None:
    """A withdrawal that leaves a legacy row answering is a control that does nothing.

    A track that was marked has to be unmarkable.
    """
    store = _store(tmp_path)
    store.record_quality_override("album", "honest", "shape")
    store.record_quality_override("album", "honest", "shape", "flac:this-one")

    store.clear_quality_override("album", "shape", "flac:this-one")

    assert word_for(store.quality_words("album"), "shape", "flac:this-one") is None


def test_a_row_naming_a_file_that_is_gone_is_not_a_second_copy(tmp_path: Path) -> None:
    """A row whose file no longer exists is not a second copy.

    Organising an album renames its files, and the registry keeps the row it
    walked under the old name. A duplicate map that matches by audio key and
    never asks the disk reads one file registered twice as one recording held
    in two places, and an album is told it holds twice the files it has.

    Nothing is deleted: a volume that comes back brings its answer back with it.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    _album(library, "Downloads copy")
    store.record_unit(_rescan(library, "Downloads copy"))
    shutil.copytree(library / "Downloads copy", library / "Filed away")
    store.record_unit(_rescan(library, "Filed away"))
    assert store.duplicate_audio(), "two folders that both exist really are two places"

    # The second folder is organised away, exactly as an apply renames it. The
    # rows it left behind name files that are no longer anywhere.
    shutil.rmtree(library / "Filed away")

    def on_disk(path: str) -> bool:
        return Path(path).exists()

    assert store.duplicate_audio(on_disk) == (), "a name nothing answers to is not a place"
    assert store.duplicate_audio() != (), "and nothing was deleted to make that true"


def test_one_unreadable_cached_release_does_not_bring_down_the_library(tmp_path: Path) -> None:
    """What is stored came from somebody else's API, through a serializer that
    may not be this one — an older build, a provider whose shape has moved, a
    hand editing the file. Every field was indexed on the assumption it was
    there, so a payload that is a list raised `TypeError` out of the store and
    took down the restore or the identification pass that asked for it. One
    unreadable row is one lookup to ask again, not a library that fails to open.
    """
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO metadata_releases "
            "(source, source_release_id, title, track_count, payload) VALUES (?, ?, ?, ?, ?)",
            ("discogs", "1", "Whatever", 1, '["not", "an", "object"]'),
        )
        row_id = connection.execute("SELECT id FROM metadata_releases").fetchone()[0]

    assert store.release(row_id) is None


def test_a_release_whose_artists_are_not_a_list_is_passed_over_too(tmp_path: Path) -> None:
    """The shape is wrong one level down, which is where `_artist(item)` used to
    raise `TypeError: string indices must be integers`."""
    store = _store(tmp_path)
    with store._database.connect() as connection:
        connection.execute(
            "INSERT INTO metadata_releases "
            "(source, source_release_id, title, track_count, payload) VALUES (?, ?, ?, ?, ?)",
            (
                "discogs",
                "2",
                "Whatever",
                1,
                '{"source": "discogs", "source_release_id": "2", '
                '"title": "Whatever", "artists": "Just A Name"}',
            ),
        )
        row_id = connection.execute("SELECT id FROM metadata_releases").fetchone()[0]

    assert store.release(row_id) is None


def test_one_download_can_be_taken_off_the_waiting_list_and_only_that_one(tmp_path: Path) -> None:
    """`clear_collected_downloads` only ever reaches the ones that *arrived*.

    So a request whose files never came, asked of a peer that went away, could
    not be dismissed by anything and would stay pending for the life of the
    database.

    One row, named by the user — nothing here sweeps — and the row stays, so
    History still answers for what was asked for.
    """
    store = _store(tmp_path)
    dead = store.record_download("peer", "@@peer\\Unanswered", "Unanswered", 5, 2000, True)
    alive = store.record_download("peer", "@@peer\\Coming", "Coming", 3, 1000, True)

    assert store.clear_download(dead) is True
    assert store.clear_download(dead) is False, "already set aside, so there is nothing to do"

    history = {row["download_id"]: row for row in store.download_history()}
    assert history[dead]["cleared_at"]
    assert not history[alive]["cleared_at"], "the neighbour is untouched"
    assert len(history) == 2, "nothing was deleted"


def test_history_still_knows_the_album_after_it_is_moved_into_the_library(tmp_path: Path) -> None:
    """The tie is what the album *is*, because where it is stops being true.

    This application renames a folder and follows it; it never moves one between
    directories. So a folder dragged into another directory afterwards is
    invisible here, and `landed_path` goes on naming a folder that is not there.
    Tied by path alone, the History row loses its album with nothing on screen
    looking broken.
    """
    store = _store(tmp_path)
    library = tmp_path / "downloads"
    unit = _album(library, "Someone - Planície (1996) [FLAC]")
    store.record_unit(unit)
    download = store.record_download("peer", "@@peer\\Planície", "Planície", 2, 500, True)
    store.mark_download_collected(download, str(unit.folder_path), unit.unit_signature)

    row = next(r for r in store.download_history() if r["download_id"] == download)
    assert row["unit_id"], "the path is exact while the folder is where it landed"

    # What happens next, and what no table here records: the folder moves.
    moved = tmp_path / "Music" / "Someone - Planície (1996) [FLAC]"
    moved.parent.mkdir(parents=True, exist_ok=True)
    store.relocate_unit(str(unit.folder_path), str(moved))

    row = next(r for r in store.download_history() if r["download_id"] == download)
    assert row["unit_id"], "the signature survives the move, so the row keeps its album"
    assert row["landed_path"] == str(unit.folder_path), "and the landing is still what it was"


def test_a_signature_two_copies_share_anchors_neither_of_them(tmp_path: Path) -> None:
    """`unit_signature` is a shape, not an identity, and copies share one.

    A fallback that picks among the albums sharing a shape would put the wrong
    copy on a History row and look exactly as confident as a right one.
    """
    store = _store(tmp_path)
    library = tmp_path / "downloads"
    original = _album(library, "First")
    shutil.copytree(original.folder_path, library / "Second")
    both = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.store")).scan(library)
    for unit in both:
        store.record_unit(unit)
    assert len({unit.unit_signature for unit in both}) == 1, "the copies share a shape"

    download = store.record_download("peer", "@@peer\\First", "First", 2, 100, True)
    store.mark_download_collected(download, str(library / "Nowhere"), both[0].unit_signature)

    row = next(r for r in store.download_history() if r["download_id"] == download)
    assert row["unit_id"] is None, "two albums answer to that shape, so the row names neither"


def test_a_rating_is_kept_and_withdrawn(tmp_path: Path) -> None:
    """Five values and a way back out, which is the whole vocabulary."""
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "Artist - Album"))

    assert store.unit_by_id(unit_id).rating is None
    store.rate_unit(unit_id, 4)
    assert store.unit_by_id(unit_id).rating == 4
    store.rate_unit(unit_id, None)
    assert store.unit_by_id(unit_id).rating is None


def test_a_rating_outside_the_five_is_refused_by_the_database(tmp_path: Path) -> None:
    """The column is the last guard, so a caller that skips the check cannot write 9."""
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "Artist - Album"))

    for impossible in (0, 6, -1):
        with pytest.raises(sqlite3.IntegrityError):
            store.rate_unit(unit_id, impossible)
    assert store.unit_by_id(unit_id).rating is None


def test_a_rating_survives_the_album_being_renamed_and_re_scanned(tmp_path: Path) -> None:
    """The claim the column is placed on: the row outlives what happens to the folder.

    Both halves of `_upsert_unit` are exercised here — the rename, which the old
    folder no longer existing makes findable by audio, and the re-scan of a
    folder whose files changed, which is found by its path.
    """
    library = tmp_path / "library"
    unit = _album(library, "raw folder", tracks=2)
    store = _store(tmp_path)
    unit_id = store.record_unit(unit)
    store.rate_unit(unit_id, 5)

    # Organized: the folder is renamed and the old one is gone.
    (library / "raw folder").rename(library / "Artist - Album (1999) [FLAC]")
    renamed = _rescan(library, "Artist - Album (1999) [FLAC]")
    assert store.record_unit(renamed) == unit_id
    assert store.unit_by_id(unit_id).rating == 5

    # And a file leaves, which changes the unit's signature entirely.
    next(iter((library / "Artist - Album (1999) [FLAC]").glob("*.flac"))).unlink()
    thinner = _rescan(library, "Artist - Album (1999) [FLAC]")
    assert thinner.unit_signature != renamed.unit_signature
    assert store.record_unit(thinner) == unit_id
    assert store.unit_by_id(unit_id).rating == 5


def test_the_shelf_query_carries_the_rating(tmp_path: Path) -> None:
    """The first sight of the window is drawn from these columns alone."""
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "Artist - Album"))
    store.rate_unit(unit_id, 3)

    row = next(row for row in store.shelf() if row["unit_id"] == unit_id)

    assert row["rating"] == 3
    assert row["title"] is None, "an unidentified album still answers the rest"


def test_one_walk_is_one_sighting_and_two_walks_are_two(tmp_path: Path) -> None:
    """What `audio_sizes` weighs is a group, and this is what makes it one.

    *What did this album weigh when it was last seen* is answered by keeping the
    rows that carry the newest `last_seen_at`. That is only an answer if a walk
    stamps all of its rows alike and no two walks stamp alike — and
    `CURRENT_TIMESTAMP` gives neither: it is fixed within a statement and not
    across them, so a walk straddling a second is two sightings and half an
    album, while two walks inside one second are one sighting and every name a
    file has ever carried.
    """
    store = _store(tmp_path)
    library = tmp_path / "library"
    unit = _album(library, "an album", tracks=2)

    store.record_unit(unit)
    first = _stamps(tmp_path)
    (library / "an album" / "track0.flac").rename(library / "an album" / "renamed.flac")
    store.record_unit(_rescan(library, "an album"))
    second = _stamps(tmp_path)

    assert len(first) == 1, f"one walk stamped its two files differently: {first}"
    walked_again = second - first
    assert len(walked_again) == 1, f"the second walk did not stamp as one: {second}"
    assert walked_again != first, (
        "two walks a moment apart carry one stamp, so the name a file used to "
        "have weighs as much as the album it left"
    )


def _stamps(tmp_path: Path) -> set[str]:
    """Every distinct `last_seen_at` in the database, as text."""
    connection = sqlite3.connect(tmp_path / "library.sqlite3")
    try:
        return {row[0] for row in connection.execute("SELECT last_seen_at FROM audio_files")}
    finally:
        connection.close()


def test_a_catalogue_answering_is_remembered_after_its_answer_is_set_aside(
    tmp_path: Path,
) -> None:
    """Whether a catalogue has answered is the album's history, not its standing row.

    Reading `identification_for` answers *there is an identification on record*,
    which is a different sentence in both directions: an arrangement from the
    album's own tags writes one for itself the moment it is applied, and a
    look-up that was superseded leaves none. An album identified with low
    confidence, left in review and then arranged by its own tags would be
    reported as never scanned.

    Fails against `identification_for(...) is not None`: the album below has no
    standing identification at all, and a catalogue has answered about it.
    """
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))

    assert store.catalogue_answered_for(unit_id) is False, "nobody has been asked yet"

    catalogue = store.record_identification(
        unit_id, store.record_release(_release()), "text_search", 0.15, ""
    )
    store.record_identification_state(catalogue, "superseded", None)

    assert (
        store.catalogue_answered_for(unit_id) is True
    ), "a look-up that was superseded is still a look-up that happened"


def test_arranging_an_album_from_its_own_tags_is_not_a_catalogue_answering(
    tmp_path: Path,
) -> None:
    """Arranging from the album's own tags is an identification, not a catalogue answer.

    The album's own files are a source everywhere downstream, on purpose, and
    they are the one source that is not a catalogue. An album dropped on the
    window arrives arranged from them, and applying it records an
    identification — which must not be what makes the window say a catalogue
    answered.
    """
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    from_own_tags = replace(_release(), source=MetadataSources.TAGS)

    store.record_identification(
        unit_id, store.record_release(from_own_tags), "existing_tags", 1.0, "", "accepted", "user"
    )

    assert store.catalogue_answered_for(unit_id) is False
    row = next(row for row in store.shelf() if row["unit_id"] == unit_id)
    assert row["catalogue_asked"] is False


def test_the_shelf_and_the_album_read_back_agree_about_who_was_asked(
    tmp_path: Path,
) -> None:
    """The card must not change its badge on being read back.

    `_shelf_summary` promises in so many words that every field it draws is the
    same one the album carries once it is in memory. `catalogue_asked` is read by
    the badge that says `not scanned`, so a shelf row that did not carry it would
    answer `undefined` — falsy, and therefore the very sentence the field exists
    to stop, on every album until it is opened.
    """
    store = _store(tmp_path)
    unit_id = store.record_unit(_album(tmp_path / "library", "album"))
    store.record_identification(
        unit_id, store.record_release(_release()), "text_search", 0.9, "", "accepted", "user"
    )

    row = next(row for row in store.shelf() if row["unit_id"] == unit_id)

    assert row["catalogue_asked"] is store.catalogue_answered_for(unit_id) is True
