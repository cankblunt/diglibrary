"""On the disk, the catalogue's names reach folders, files and tags in one case.

Asserted through the planner and the executor over real files, and read back
with ``os.listdir``: on a Mac the disk is case-insensitive, so ``exists()``
answers yes for `Orquestra da Serra` while the folder is still `Orquestra Da Serra`,
and only the listing says which one is there.
"""

import logging
import os
import shutil
from datetime import date
from pathlib import Path

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import ChangeExecutor
from diglibrary.library.matching import align_tracks
from diglibrary.library.models import AlbumUnit
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangePlanner, OperationKind, folder_name_is_free
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


def _release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Orquestra Da Serra",
        artists=(ArtistMetadata(name="Trio Solitário"),),
        tracks=(
            TrackMetadata(title="Três Gatos Na Rua", position=1, duration_ms=400),
            TrackMetadata(title="Zunto Plimba Com Vorca", position=2, duration_ms=900),
        ),
        # A reissue, because a name that is not free is sent to the pressing
        # year: without one the folder rename lands whatever the check says.
        released_on=date(2006, 1, 1),
        original_released_on=date(1970, 1, 1),
    )


def _unit(root: Path) -> AlbumUnit:
    return LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.casing")).scan(root)[0]


def _album(root: Path, folder: str, names: tuple[str, str]) -> AlbumUnit:
    (root / folder).mkdir(parents=True)
    for source, name in zip(("tone.flac", "tone-long.flac"), names, strict=True):
        shutil.copy(FIXTURES / source, root / folder / name)
    return _unit(root)


def _plan(unit: AlbumUnit, release: ReleaseMetadata, **kwargs):
    planner = ChangePlanner(NamingPolicy(), MutagenTagStore())
    alignment = align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100)
    return planner.plan(unit, release, alignment, **kwargs)


def _apply(plan) -> None:
    ChangeExecutor(MutagenTagStore(), logging.getLogger("test.casing")).apply(plan)


def _listed(root: Path) -> tuple[str, list[str]]:
    (folder,) = os.listdir(root)
    return folder, sorted(os.listdir(root / folder))


def test_the_catalogue_s_names_are_written_in_one_case(tmp_path: Path) -> None:
    _apply(_plan(_album(tmp_path, "incoming", ("a.flac", "b.flac")), _release()))

    folder, files = _listed(tmp_path)
    assert "Orquestra da Serra" in folder
    assert any("Três Gatos na Rua" in name for name in files)
    assert any("Zunto Plimba com Vorca" in name for name in files)
    titles = {MutagenTagStore().read(tmp_path / folder / name)["title"][0] for name in files}
    assert titles == {"Três Gatos na Rua", "Zunto Plimba com Vorca"}
    album = MutagenTagStore().read(tmp_path / folder / files[0])["album"]
    assert album == ("Orquestra da Serra",)


def _put_the_old_case_back(root: Path, folder: str, files: list[str]) -> tuple[str, list[str]]:
    """Rename the album and its tracks to the case this project no longer writes.

    Two tests need an album sitting on the disk under the old case, and a rename
    that changes nothing but case is the one kind a filesystem may quietly
    decline — the name it is given and the name it has are the same name to
    anything that compares them without regard to case. A decline here leaves
    the album already correctly named, so the plan that follows is empty and the
    test fails saying only that no rename was planned, which is true and says
    nothing about why.

    So it is checked. What comes back is what the disk reports, not what was
    asked for.
    """
    old_folder = folder.replace("Orquestra da Serra", "Orquestra Da Serra")
    os.rename(root / folder, root / old_folder)
    old_files = []
    for name in files:
        old = name.replace(" na ", " Na ")
        os.rename(root / old_folder / name, root / old_folder / old)
        old_files.append(old)

    landed = _listed(root)
    assert landed == (old_folder, sorted(old_files)), (
        "the filesystem did not take a rename that changes only case, so this "
        f"album is still named the way it already should be: {landed!r}"
    )
    return landed


def test_an_album_named_in_the_old_case_is_renamed_when_it_is_planned(tmp_path: Path) -> None:
    """A rename that changes only case: refused by nothing, and landed on the disk."""
    _apply(_plan(_album(tmp_path, "incoming", ("a.flac", "b.flac")), _release()))
    folder, files = _listed(tmp_path)
    _put_the_old_case_back(tmp_path, folder, files)

    plan = _plan(_unit(tmp_path), _release())
    renamed = {operation.kind for operation in plan.operations}
    assert OperationKind.RENAME_FOLDER in renamed
    assert OperationKind.RENAME_FILE in renamed
    _apply(plan)

    assert _listed(tmp_path) == (folder, files)
    assert not _plan(_unit(tmp_path), _release()).operations


def test_a_folder_name_differing_only_in_case_is_its_own(tmp_path: Path) -> None:
    source = tmp_path / "Trio Solitário - Orquestra Da Serra"
    source.mkdir()
    assert folder_name_is_free(
        tmp_path / "Trio Solitário - Orquestra da Serra", source, frozenset()
    )


def test_a_title_the_user_corrected_keeps_their_case(tmp_path: Path) -> None:
    release = _release()
    plan = _plan(
        _album(tmp_path, "incoming", ("a.flac", "b.flac")), release, user_titles=frozenset({2})
    )
    _apply(plan)

    _, files = _listed(tmp_path)
    assert any("Zunto Plimba Com Vorca" in name for name in files)
    assert any("Três Gatos na Rua" in name for name in files)


def test_a_rename_that_changed_only_case_is_undone_to_the_old_case(tmp_path: Path) -> None:
    """The reversal restores the old case too, not only the forward rename."""
    _apply(_plan(_album(tmp_path, "incoming", ("a.flac", "b.flac")), _release()))
    folder, files = _listed(tmp_path)
    before = _put_the_old_case_back(tmp_path, folder, files)

    executor = ChangeExecutor(MutagenTagStore(), logging.getLogger("test.casing"))
    result = executor.apply(_plan(_unit(tmp_path), _release()))
    assert _listed(tmp_path) == (folder, files)
    reverted = executor.revert(result)

    assert reverted.failure is None
    assert _listed(tmp_path) == before
