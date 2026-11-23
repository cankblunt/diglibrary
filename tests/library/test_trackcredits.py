"""A file's own artist credit, where the catalogue published none for its track."""

import logging
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

from mutagen.flac import FLAC

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.matching import align_tracks
from diglibrary.library.models import AlbumUnit
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangePlanner, OperationKind
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.library.trackcredits import drops_file_credits, read_file_credits

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


def test_a_silent_release_leaves_each_file_the_credit_it_holds(tmp_path: Path) -> None:
    """The release credits itself and publishes nobody per track.

    A tribute filed under the artist it honours has this shape. Written as the
    release stands, both tracks would take the release's credit, and the two
    performers the files name would be gone from the tags.
    """
    unit = _album(tmp_path, ("Olga Hirt", "Elba Soriano"))
    release = _release()

    plan = _plan(unit, release)

    assert _written(plan, "artist") == [("Olga Hirt",), ("Elba Soriano",)]
    assert _written(plan, "albumartist") == [("Zoé of Lanterns",)] * 2


def test_files_that_all_carry_one_credit_take_the_releases(tmp_path: Path) -> None:
    """A credit every file repeats says nothing about any one track.

    It is a statement about the album, which is the release's to make — so an
    album tagged with a misspelt artist throughout is still corrected.
    """
    unit = _album(tmp_path, ("Zoe Lanterns", "Zoe Lanterns"))

    plan = _plan(unit, _release())

    assert _written(plan, "artist") == [("Zoé of Lanterns",)] * 2


def test_a_file_crediting_the_release_in_its_own_spelling_takes_the_catalogues(
    tmp_path: Path,
) -> None:
    """Only the file that names somebody else keeps its credit.

    The album's files disagree, so each is asked on its own — and the one that
    writes the release's artist without the accent is agreeing with it.
    """
    unit = _album(tmp_path, ("zoe of lanterns", "Elba Soriano"))

    plan = _plan(unit, _release())

    assert _written(plan, "artist") == [("Zoé of Lanterns",), ("Elba Soriano",)]


def test_a_credit_the_source_published_is_never_set_aside_for_a_tag(tmp_path: Path) -> None:
    unit = _album(tmp_path, ("Olga Hirt", "Elba Soriano"))
    release = _release()
    release = replace(
        release,
        tracks=(
            replace(release.tracks[0], artists=(ArtistMetadata(name="Ruth Amsel"),)),
            release.tracks[1],
        ),
    )

    plan = _plan(unit, release)

    assert _written(plan, "artist") == [("Ruth Amsel",), ("Elba Soriano",)]


def test_a_release_is_said_to_drop_credits_only_where_it_would(tmp_path: Path) -> None:
    """The question the identification asks before it stops at a source."""
    release = _release()
    differing = _album(tmp_path / "a", ("Olga Hirt", "Elba Soriano"))
    agreeing = _album(tmp_path / "b", ("Olga Hirt", "Olga Hirt"))
    credited = replace(
        release,
        tracks=tuple(
            replace(track, artists=(ArtistMetadata(name="Ruth Amsel"),)) for track in release.tracks
        ),
    )
    store = MutagenTagStore()

    assert drops_file_credits(
        release, _align(differing, release), read_file_credits(differing, store)
    )
    assert not drops_file_credits(
        release, _align(agreeing, release), read_file_credits(agreeing, store)
    )
    assert not drops_file_credits(
        credited, _align(differing, credited), read_file_credits(differing, store)
    )


def test_an_album_the_user_calls_a_compilation_is_named_as_one(tmp_path: Path) -> None:
    """The source credits one artist; the user says it is a compilation.

    Everything a compilation's name and tags do follows, and each track keeps
    the artist it had — here the one its own file holds, since the source
    published none.
    """
    unit = _album(tmp_path, ("Olga Hirt", "Elba Soriano"))
    release = replace(_release(), various_by_user=True)

    plan = _plan(unit, release)

    renames = sorted(
        Path(operation.after_state["path"]).name
        for operation in plan.operations
        if operation.kind is OperationKind.RENAME_FILE
    )
    assert renames == ["01. Olga Hirt - Dawnlight.flac", "02. Elba Soriano - Duskfall.flac"]
    folder = next(o for o in plan.operations if o.kind is OperationKind.RENAME_FOLDER)
    assert Path(folder.after_state["path"]).name == "VA - Quarrél (1955) [FLAC]"
    assert _written(plan, "albumartist") == [("Various Artists",)] * 2
    assert _written(plan, "compilation") == [("1",)] * 2
    assert _written(plan, "artist") == [("Olga Hirt",), ("Elba Soriano",)]


def _written(plan, name: str) -> list[tuple[str, ...]]:
    """What the plan writes into one tag, in the order of the track numbers."""
    tags = [
        operation.after_state["tags"]
        for operation in plan.operations
        if operation.kind is OperationKind.WRITE_TAGS
    ]
    tags.sort(key=lambda one: int(one["tracknumber"][0]))
    return [tuple(one.get(name, ())) for one in tags]


def _plan(unit: AlbumUnit, release: ReleaseMetadata):
    plan = ChangePlanner(NamingPolicy(), MutagenTagStore()).plan(
        unit, release, _align(unit, release)
    )
    assert plan.is_applicable, plan.blockers
    return plan


def _align(unit: AlbumUnit, release: ReleaseMetadata):
    return align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100)


def _album(root: Path, artists: tuple[str, str]) -> AlbumUnit:
    """Two files of different lengths, each carrying its own artist tag."""
    folder = root / "unknown folder"
    folder.mkdir(parents=True)
    for source, name, artist in zip(
        ("tone.flac", "tone-long.flac"), ("aaa.flac", "bbb.flac"), artists, strict=True
    ):
        shutil.copy(FIXTURES / source, folder / name)
        audio = FLAC(folder / name)
        audio["artist"] = [artist]
        audio.save()
    return LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.trackcredits")).scan(root)[0]


def _release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Quarrél",
        artists=(ArtistMetadata(name="Zoé of Lanterns"),),
        tracks=(
            TrackMetadata(title="Dawnlight", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Duskfall", position=2, position_on_medium=2, duration_ms=900),
        ),
        released_on=date(1955, 3, 1),
    )
