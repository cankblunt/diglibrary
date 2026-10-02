"""Round-trip tests for applying and reverting a change plan on real files."""

import errno
import hashlib
import logging
import os
import plistlib
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import (
    ChangeExecutor,
    ExecutionError,
    ExecutionState,
    StalePlanError,
    _rename,
    rerooted_trail,
)
from diglibrary.library.matching import align_tracks
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import (
    ChangeOperation,
    ChangePlan,
    ChangePlanner,
    OperationKind,
)
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.library.xattrs import _read, _write

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


def test_applying_then_reverting_restores_the_album_exactly(tmp_path: Path) -> None:
    """Every change this project makes can be undone."""
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    before = _snapshot(tmp_path)
    plan = _plan(album, _release())
    executor = _executor()

    result = executor.apply(plan)
    assert result.is_complete
    assert _snapshot(tmp_path) != before

    executor.revert(result)

    assert _snapshot(tmp_path) == before


def test_applying_produces_the_named_album(tmp_path: Path) -> None:
    """After applying, the folder and files carry the release's own names."""
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))

    _executor().apply(_plan(album, _release()))

    folder = tmp_path / "Marina do Acordeão - Forró (1955) [FLAC]"
    assert folder.is_dir()
    assert sorted(path.name for path in folder.iterdir()) == [
        "01. Primeira.flac",
        "02. Segunda.flac",
    ]
    tags = MutagenTagStore().read(folder / "01. Primeira.flac")
    assert tags["title"] == ("Primeira",)
    assert tags["album"] == ("Forró",)


def test_each_operation_is_reported_the_moment_it_takes_effect(tmp_path: Path) -> None:
    """The witness hears every operation, in order, as it happens.

    Reported *after* the write, never before, because a trail must describe
    what the disk really holds — a row for an operation that had not happened
    would make a reversal undo something nobody did.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    plan = _plan(album, _release())
    heard: list[tuple[int, bool]] = []

    result = _executor().apply(
        plan, witness=lambda entry: heard.append((entry.operation.sequence, _took_effect(entry)))
    )

    assert [sequence for sequence, _ in heard] == [
        operation.sequence for operation in plan.operations
    ]
    assert all(done for _, done in heard), "each one is reported after its own write"
    assert len(heard) == len(result.applied)


def test_a_witness_that_fails_does_not_abandon_a_half_written_album(tmp_path: Path) -> None:
    """Losing the running record is bad; stopping mid-album because of it is worse."""
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))

    def broken(entry):
        raise RuntimeError("the database is locked")

    result = _executor().apply(_plan(album, _release()), witness=broken)

    assert result.state is ExecutionState.APPLIED
    assert (tmp_path / "Marina do Acordeão - Forró (1955) [FLAC]").is_dir()


def _took_effect(entry) -> bool:
    """Report whether the operation's own target already moved when it was announced."""
    operation = entry.operation
    if operation.kind is OperationKind.WRITE_TAGS:
        return True
    return Path(str(operation.after_state["path"])).exists()


def test_applying_never_changes_the_audio_itself(tmp_path: Path) -> None:
    """Organizing a library must not touch a single byte of audio."""
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    probe = MutagenAudioProbe()
    signatures_before = sorted(file.content_signature for file in album.audio_files)

    _executor().apply(_plan(album, _release()))
    rescanned = _rescan(tmp_path)

    assert sorted(file.content_signature for file in rescanned.audio_files) == signatures_before
    assert all(probe.read(file.path) is not None for file in rescanned.audio_files)


def test_a_blocked_plan_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """A plan with a blocker must never reach the disk."""
    album = _album(tmp_path, "album", ("aaa.flac",))
    blocked = ChangePlan(unit=album, release=_release(), blockers=("Ambiguous.",))
    before = _snapshot(tmp_path)

    with pytest.raises(ExecutionError, match="blocked"):
        _executor().apply(blocked)

    assert _snapshot(tmp_path) == before


def test_a_plan_that_cannot_finish_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """A destination that is taken refuses the whole plan.

    A plan that stops at a late operation, such as the folder rename, has
    already written the tags and moved the files. Nothing undoes that on its
    own, and the album is left in a state no later scan can read back. A name
    that cannot be taken has to be found while finding it still costs nothing.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    plan = _plan(album, _release())
    # An unrelated file already occupies the name the second track wants.
    (album.folder_path / "02. Segunda.flac").write_bytes(b"not ours")
    before = _snapshot(tmp_path)

    with pytest.raises(ExecutionError, match="already exists"):
        _executor().apply(plan)

    assert _snapshot(tmp_path) == before, "not even the tags may be written"


def test_a_refusal_names_every_problem_the_plan_has(tmp_path: Path) -> None:
    """One refusal says everything that is wrong, so it is fixed in one pass."""
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    plan = _plan(album, _release())
    (album.folder_path / "02. Segunda.flac").write_bytes(b"not ours")
    folder_rename = next(
        operation for operation in plan.operations if operation.kind is OperationKind.RENAME_FOLDER
    )
    Path(str(folder_rename.after_state["path"])).mkdir()

    with pytest.raises(ExecutionError) as raised:
        _executor().apply(plan)

    assert "02. Segunda.flac already exists" in str(raised.value)
    assert str(folder_rename.after_state["path"]) + " already exists" in str(raised.value)


def test_a_failure_partway_leaves_a_trail_that_reverts_cleanly(tmp_path: Path) -> None:
    """A run that dies in the middle must still be fully undoable.

    The rehearsal cannot foresee everything — a permission, a disk that fills, a
    file that goes away between the check and the write — so the trail remains
    the guarantee. Here the tag store is broken on purpose, because nothing the
    disk shows beforehand could have predicted it.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    before = _snapshot(tmp_path)
    plan = _plan(album, _release())
    tag_store = _BreakableTagStore()
    tag_store.fail_after = 1
    executor = ChangeExecutor(tag_store, logging.getLogger("test.executor"))

    result = executor.apply(plan)

    assert result.state is ExecutionState.FAILED
    assert result.failure is not None and "the disk went away" in result.failure
    assert result.applied, "operations before the failure must be recorded"

    tag_store.fail_after = None
    executor.revert(result)

    assert _snapshot(tmp_path) == before


def test_an_empty_plan_applies_without_touching_anything(tmp_path: Path) -> None:
    """Nothing to do must be a no-op, not an error."""
    album = _album(tmp_path, "album", ("aaa.flac",))
    empty = ChangePlan(unit=album, release=_release())
    before = _snapshot(tmp_path)

    result = _executor().apply(empty)

    assert result.is_complete
    assert result.applied == ()
    assert _snapshot(tmp_path) == before


def _executor() -> ChangeExecutor:
    return ChangeExecutor(MutagenTagStore(), logging.getLogger("test.executor"))


class _BreakableTagStore:
    """A real tag store that can be told to start failing, and to stop failing."""

    def __init__(self) -> None:
        self._inner = MutagenTagStore()
        self._writes = 0
        self.fail_after: int | None = None

    def read(self, path: Path) -> dict[str, tuple[str, ...]]:
        return self._inner.read(path)

    def write(self, path: Path, tags: Mapping[str, tuple[str, ...]]) -> None:
        self._writes += 1
        if self.fail_after is not None and self._writes > self.fail_after:
            raise OSError("the disk went away")
        self._inner.write(path, tags)


def test_a_rename_is_refused_when_the_folder_grew_into_a_container(tmp_path: Path) -> None:
    """The container guard reads the disk, so a stale plan cannot rename a shelf.

    The scan saw a two-track album and planned to rename its folder. Albums
    landed in that folder afterwards. Nothing may be written: the folder now
    mostly holds other people's music, and the plan does not know it.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    plan = _plan(album, _release())
    shelf = tmp_path / "misnamed folder"
    for index in range(2):
        other = shelf / f"Another Album {index}"
        other.mkdir()
        for track in range(5):
            (other / f"{track}.flac").write_bytes(f"{index}-{track}".encode())
    before = _snapshot(tmp_path)

    with pytest.raises(ExecutionError, match="not its own"):
        _executor().apply(plan)

    assert _snapshot(tmp_path) == before


def test_a_multi_disc_rename_is_not_mistaken_for_a_container(tmp_path: Path) -> None:
    """CD1 and CD2 belong to the unit, so the album still names its own folder."""
    album = tmp_path / "some anthology"
    (album / "CD 1").mkdir(parents=True)
    (album / "disc2").mkdir()
    shutil.copy(FIXTURES / "tone.flac", album / "CD 1" / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "disc2" / "bbb.flac")
    unit = _rescan(tmp_path)

    result = _executor().apply(_plan(unit, _multi_disc_release()))

    assert result.is_complete
    assert not album.exists()
    assert (tmp_path / "Some Artist - Anthology (1998) [FLAC]").is_dir()


def _plan(unit: AlbumUnit, release: ReleaseMetadata) -> ChangePlan:
    planner = ChangePlanner(NamingPolicy(), MutagenTagStore())
    return planner.plan(
        unit, release, align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100)
    )


def _album(root: Path, folder_name: str, filenames: tuple[str, ...]) -> AlbumUnit:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    sources = ("tone.flac", "tone-long.flac")
    for index, name in enumerate(filenames):
        shutil.copy(FIXTURES / sources[index % len(sources)], folder / name)
    return _rescan(root)


def _rescan(root: Path) -> AlbumUnit:
    units = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.executor")).scan(root)
    return units[0]


def _snapshot(root: Path) -> list[tuple[str, str]]:
    """Return every path and file digest, so a reversal is compared exactly.

    Directories are included deliberately: a revert that restored every file but
    left an empty folder behind would otherwise pass unnoticed.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_file():
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        elif path.is_dir():
            entries.append((relative, "<directory>"))
    return entries


def _release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(
            TrackMetadata(title="Primeira", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Segunda", position=2, position_on_medium=2, duration_ms=900),
        ),
        released_on=date(1955, 3, 1),
    )


def test_a_multi_disc_album_keeps_one_folder_per_disc(tmp_path: Path) -> None:
    """One folder per disc, applied end to end from a rip with non-canonical names."""
    album = tmp_path / "some anthology"
    (album / "CD 1").mkdir(parents=True)
    (album / "disc2").mkdir()
    shutil.copy(FIXTURES / "tone.flac", album / "CD 1" / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "disc2" / "bbb.flac")
    (album / "notes.txt").write_bytes(b"somebody put this here")
    unit = _rescan(tmp_path)

    result = _executor().apply(_plan(unit, _multi_disc_release()))

    assert result.is_complete
    organized = tmp_path / "Some Artist - Anthology (1998) [FLAC]"
    assert sorted(path.name for path in organized.iterdir()) == ["CD1", "CD2", "notes.txt"]
    assert [p.name for p in (organized / "CD1").iterdir()] == ["1-01. Primeira.flac"]
    assert [p.name for p in (organized / "CD2").iterdir()] == ["2-01. Segunda.flac"]
    tags = MutagenTagStore().read(organized / "CD2" / "2-01. Segunda.flac")
    assert tags["discnumber"][0].startswith("2")


def test_a_multi_disc_album_in_one_folder_is_split_into_disc_folders(tmp_path: Path) -> None:
    """A rip using the older single-folder convention converges on the new layout."""
    album = tmp_path / "anthology"
    album.mkdir()
    shutil.copy(FIXTURES / "tone.flac", album / "1-01 old.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "2-01 old.flac")
    unit = _rescan(tmp_path)
    before = _snapshot(tmp_path)
    executor = _executor()

    result = executor.apply(_plan(unit, _multi_disc_release()))
    organized = tmp_path / "Some Artist - Anthology (1998) [FLAC]"

    assert (organized / "CD1" / "1-01. Primeira.flac").is_file()
    assert (organized / "CD2" / "2-01. Segunda.flac").is_file()

    executor.revert(result)

    assert _snapshot(tmp_path) == before, "reverting must remove the folders it created"


def test_a_multi_disc_album_is_refused_whole_when_its_name_is_taken(tmp_path: Path) -> None:
    """Files would move into disc folders, and then the folder's name would fail.

    The folder rename is the last operation, so every tag and every move has
    already happened by the time it fails. Refusing the plan up front costs the
    album nothing; failing at the end costs the disk its consistency.
    """
    album = tmp_path / "anthology"
    album.mkdir()
    shutil.copy(FIXTURES / "tone.flac", album / "1-01 old.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "2-01 old.flac")
    unit = _rescan(tmp_path)
    plan = _plan(unit, _multi_disc_release())
    # The name was free when the album was scanned and planned, and is not now.
    (tmp_path / "Some Artist - Anthology (1998) [FLAC]").mkdir()
    before = _snapshot(tmp_path)

    with pytest.raises(ExecutionError, match="already exists"):
        _executor().apply(plan)

    assert _snapshot(tmp_path) == before, "no tag written, no file moved, no folder created"


def _multi_disc_release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="anthology",
        title="Anthology",
        artists=(ArtistMetadata(name="Some Artist"),),
        tracks=(
            TrackMetadata(
                title="Primeira",
                position=1,
                medium_number=1,
                position_on_medium=1,
                duration_ms=400,
            ),
            TrackMetadata(
                title="Segunda",
                position=2,
                medium_number=2,
                position_on_medium=1,
                duration_ms=900,
            ),
        ),
        released_on=date(1998, 1, 1),
    )


def test_a_plan_whose_folder_has_moved_is_refused_as_stale(tmp_path: Path) -> None:
    """A folder that is gone is an out-of-date plan, not a container.

    The container guard measures a vanished folder as `own=0, foreign=0`, which
    reads as a folder that mostly holds other albums. That names a shelving
    problem that does not exist, when the disk has simply moved out from under
    the plan, so the missing folder is checked first.
    """
    album = tmp_path / "unit-folder"
    album.mkdir()
    (album / "01.flac").write_bytes(b"x")
    unit = AlbumUnit(
        folder_path=album,
        unit_signature="sig",
        audio_files=(
            AudioFileFacts(
                path=album / "01.flac",
                content_signature="c1",
                file_size_bytes=1,
                modified_at=datetime.now(UTC),
            ),
        ),
    )
    plan = ChangePlan(
        unit=unit,
        release=None,
        operations=(
            ChangeOperation(
                sequence=1,
                kind=OperationKind.RENAME_FOLDER,
                target_path=tmp_path / "Vanished",
                after_state={"path": str(tmp_path / "Renamed")},
                before_state={"path": str(tmp_path / "Vanished")},
            ),
        ),
    )

    with pytest.raises(StalePlanError) as raised:
        _executor().apply(plan)

    assert "no longer on disk" in str(raised.value)
    assert "Scan the folder again" in str(raised.value)
    assert "other albums" not in str(raised.value)


def test_a_plan_applied_twice_is_refused_in_one_sentence_about_the_folder(tmp_path: Path) -> None:
    """Each missing path is named once, by the name the disk had to hold.

    A plan applied a second time could be refused in one sentence per operation
    that reaches each file — the tags under the old name, the rename, the
    picture under the new name — naming files as gone under names they never
    had. The folder the plan renames is gone, and that is the whole of what is
    true.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    plan = _plan(album, _release())
    _executor().apply(plan)

    with pytest.raises(StalePlanError) as raised:
        _executor().apply(plan)

    assert str(raised.value) == (
        "misnamed folder is no longer on disk, so this plan is out of date. "
        "Scan the folder again to plan it from what is there now."
    )


def test_files_that_went_missing_are_each_named_once(tmp_path: Path) -> None:
    """Two files gone from a folder that is still there: two names, each once."""
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    plan = _plan(album, _release())
    (tmp_path / "misnamed folder" / "aaa.flac").unlink()
    (tmp_path / "misnamed folder" / "bbb.flac").unlink()

    with pytest.raises(StalePlanError) as raised:
        _executor().apply(plan)

    assert str(raised.value) == (
        "2 of the paths this plan names are no longer on disk: aaa.flac and bbb.flac. "
        "This plan is out of date. Scan the folder again to plan it from what is there now."
    )


def test_a_reversal_follows_the_album_to_where_it_was_filed(tmp_path: Path) -> None:
    """Filing an album away must not take its safety net off.

    A finished folder is commonly moved out of a downloads folder and into a
    music library. The trail names absolute paths, so without rerooting a
    reversal raises on the first file it tries to open.

    What comes back is the album's *name*, where it was put. The filing is
    left alone — nothing is dragged back to the folder it arrived in.
    """
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    album = _album(downloads, "misnamed folder", ("aaa.flac", "bbb.flac"))
    before = _snapshot(downloads)
    result = _executor().apply(_plan(album, _release()))
    assert result.is_complete
    organized = downloads / "Marina do Acordeão - Forró (1955) [FLAC]"
    assert organized.is_dir()

    music = tmp_path / "music"
    music.mkdir()
    filed = music / organized.name
    organized.rename(filed)

    _executor().revert_trail(rerooted_trail(result.applied, organized, filed))

    assert list(downloads.iterdir()) == [], "the reversal never reaches back into downloads"
    assert _snapshot(music) == before, "the album came back, byte for byte, where it was filed"


def test_a_reversal_that_cannot_finish_refuses_before_undoing_anything(tmp_path: Path) -> None:
    """A reversal is all or nothing, exactly as an apply is.

    The reversal walks the trail backwards, so the operation that raises is
    rarely the first one: everything after it in the trail has already been
    written back. And the outcome is recorded only once the walk *returns*, so
    a reversal that stopped halfway would leave the plan reading ``applied``
    with every ``reverted_at`` empty — the disk half-way back and the database
    silent.

    One file is taken from the middle of the album, which is the shape of a
    partially filed folder. Nothing may move.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    result = _executor().apply(_plan(album, _release()))
    organized = tmp_path / "Marina do Acordeão - Forró (1955) [FLAC]"
    (organized / "01. Primeira.flac").unlink()
    after_the_loss = _snapshot(tmp_path)

    with pytest.raises(StalePlanError) as raised:
        _executor().revert_trail(result.applied)

    assert "01. Primeira.flac is no longer on disk" in str(raised.value)
    assert "Nothing was undone" in str(raised.value)
    assert _snapshot(tmp_path) == after_the_loss, "the refusal touched nothing at all"


def test_a_reversal_reports_every_operation_as_it_comes_back(tmp_path: Path) -> None:
    """The mirror of the apply's witness, so a reversal cut short is not a lie.

    The rehearsal makes stopping partway rare; this is what makes the database
    true when a disk fills or a volume goes away between two operations.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    result = _executor().apply(_plan(album, _release()))
    heard: list[int] = []

    undone, stopped = _executor().revert_trail(
        result.applied, undone_witness=lambda entry: heard.append(entry.operation.sequence)
    )

    assert stopped is None, "nothing was in the way of this one"
    assert heard == [entry.operation.sequence for entry in undone]
    assert heard == list(reversed([entry.operation.sequence for entry in result.applied]))


def test_a_reversal_stopped_by_the_disk_reports_what_came_back(tmp_path: Path) -> None:
    """`ExecutionError` is raised *only for a refusal before any write*.

    A failure partway is reported as a result, because the trail matters more
    than the exception. That holds for the reversal as it does for the apply: a
    reversal that raised in the middle would hand what was already written back
    to nobody, and no recorded path could follow it.

    The rehearsal is what makes this rare, and it cannot make it impossible: it
    walks a model of the disk, and a permission, a lock or a full volume is not
    in that model.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    executor = _executor()
    result = executor.apply(_plan(album, _release()))
    undo = executor._undo

    def refuse(entry: object) -> object:
        if entry.operation.kind is OperationKind.RENAME_FILE:  # type: ignore[attr-defined]
            raise OSError("the disk refused this rename_file")
        return undo(entry)

    executor._undo = refuse  # type: ignore[method-assign]

    undone, stopped = executor.revert_trail(result.applied)

    assert stopped == "the disk refused this rename_file"
    assert undone, "what came back is reported, not thrown away"
    assert all(entry.operation.kind is not OperationKind.RENAME_FILE for entry in undone)
    assert (tmp_path / "misnamed folder").is_dir(), "the folder came back before it stopped"


def test_a_reversal_stopped_by_a_wordless_error_still_says_it_stopped(tmp_path: Path) -> None:
    """*Was there a stop* is the question, not *is the reason worth reading*.

    An exception whose text is empty — `PermissionError()` raised bare, and
    every `raise SomeError()` written without a message — is falsy, so carrying
    the stop as `stopped or result.failure` falls through to the apply's own
    failure, which on a plan that applied cleanly is `None`. The caller would
    be handed a `REVERTED` result with nothing wrong with it, for a walk that
    stopped in the middle of the album.
    """
    album = _album(tmp_path, "misnamed folder", ("aaa.flac", "bbb.flac"))
    executor = _executor()
    result = executor.apply(_plan(album, _release()))
    assert result.failure is None, "the apply itself went through, so it has nothing to say"

    def refuse(entry: object) -> object:
        raise PermissionError()

    executor._undo = refuse  # type: ignore[method-assign]

    reverted = executor.revert(result)

    assert reverted.failure is not None, "a reversal that stopped must not read as a clean one"
    assert reverted.reverted == (), "nothing came back before it stopped"


@pytest.mark.skipif(sys.platform != "darwin", reason="Finder tags are macOS extended attributes")
def test_a_move_onto_another_volume_keeps_the_finder_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ratings and genres can live on the album folder, and `copy2` carries none on macOS."""
    album = tmp_path / "Album"
    (album / "CD1").mkdir(parents=True)
    (album / "CD1" / "01.flac").write_bytes(b"audio")
    tags = plistlib.dumps(["Favourite\n6", "Soul\n0"], fmt=plistlib.FMT_BINARY)
    name = "com.apple.metadata:_kMDItemUserTags"
    for path in (album, album / "CD1" / "01.flac"):
        assert _write(path, name, tags)

    def across_volumes(*_: object, **__: object) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    # `Path.rename` and `shutil.move` both reach the disk through `os.rename`.
    monkeypatch.setattr(os, "rename", across_volumes)
    destination = tmp_path / "Filed" / "Album (1975) [FLAC]"

    _rename(album, destination)

    assert not album.exists()
    assert _read(destination, name) == tags
    assert _read(destination / "CD1" / "01.flac", name) == tags
    assert (destination / "CD1" / "01.flac").read_bytes() == b"audio"


@pytest.mark.skipif(sys.platform != "darwin", reason="Finder tags are macOS extended attributes")
def test_a_copy_that_lost_the_finder_tags_leaves_the_original_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is removed only once the copy is proved to carry what it carried."""
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"audio")
    assert _write(album, "com.apple.metadata:_kMDItemUserTags", b"rating")

    def across_volumes(*_: object, **__: object) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(os, "rename", across_volumes)
    monkeypatch.setattr("diglibrary.library.executor.carry_across", lambda *_: ())

    with pytest.raises(ExecutionError, match="lost the Finder attributes"):
        _rename(album, tmp_path / "Filed" / "Album")

    assert (album / "01.flac").read_bytes() == b"audio"
    assert _read(album, "com.apple.metadata:_kMDItemUserTags") == b"rating"
