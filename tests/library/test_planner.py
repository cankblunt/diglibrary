"""Unit tests for change planning."""

import logging
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.artwork import FilesystemArtworkStore, ImageKind, StagedImage
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.matching import align_tracks
from diglibrary.library.models import AlbumUnit
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangePlanner, OperationKind, _named_briefly, _tracks_that
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


def test_a_plan_renames_files_and_folder_and_writes_tags(tmp_path: Path) -> None:
    """The full set of changes a wrongly named album needs."""
    unit = _album(tmp_path, "unknown folder", ("aaa.flac", "bbb.flac"))
    release = _release()

    plan = _planner().plan(unit, release, _align(unit, release))

    assert plan.is_applicable
    kinds = [operation.kind for operation in plan.operations]
    assert kinds.count(OperationKind.WRITE_TAGS) == 2
    assert kinds.count(OperationKind.RENAME_FILE) == 2
    assert kinds.count(OperationKind.RENAME_FOLDER) == 1
    assert kinds[-1] is OperationKind.RENAME_FOLDER, "the folder must move last"
    renames = [
        Path(operation.after_state["path"]).name
        for operation in plan.operations
        if operation.kind is OperationKind.RENAME_FILE
    ]
    assert sorted(renames) == ["01. Dawnlight.flac", "02. Duskfall.flac"]
    folder = next(o for o in plan.operations if o.kind is OperationKind.RENAME_FOLDER)
    assert Path(folder.after_state["path"]).name == "Zoé of Lanterns - Quarrél (1955) [FLAC]"


def test_an_album_already_correct_produces_an_empty_plan(tmp_path: Path) -> None:
    """The least-change principle: nothing to do is a successful outcome."""
    release = _release()
    unit = _album(
        tmp_path,
        "Zoé of Lanterns - Quarrél (1955) [FLAC]",
        ("01. Dawnlight.flac", "02. Duskfall.flac"),
    )
    planner = _planner()
    plan = planner.plan(unit, release, _align(unit, release))

    # Apply the tags the plan asks for, then re-plan from the same disk state.
    store = MutagenTagStore()
    for operation in plan.operations:
        if operation.kind is OperationKind.WRITE_TAGS:
            store.write(
                operation.target_path,
                {k: tuple(v) for k, v in operation.after_state["tags"].items()},
            )
    unit = _rescan(tmp_path)
    second = planner.plan(unit, release, _align(unit, release))

    assert second.is_empty
    assert second.is_applicable


def test_an_ambiguous_alignment_blocks_the_plan(tmp_path: Path) -> None:
    """Files that two tracks could equally claim are never renamed on a guess."""
    unit = _album(tmp_path, "album", ("a.flac", "b.flac"))
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(
            TrackMetadata(title="One", position=1, duration_ms=400),
            TrackMetadata(title="Two", position=2, duration_ms=400),
        ),
    )

    plan = _planner().plan(unit, release, _align(unit, release))

    assert plan.is_applicable is False
    assert plan.operations == ()
    # The blocker names the file: on a long album, a reason that does not say
    # which file leaves the reader to find it.
    assert any("does not say which" in blocker and "a.flac" in blocker for blocker in plan.blockers)


def test_two_tracks_with_the_same_title_block_the_plan(tmp_path: Path) -> None:
    """A collision would silently destroy a file, so it stops the plan instead."""
    unit = _album(tmp_path, "album", ("a.flac", "b.flac"))
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(
            TrackMetadata(title="Refrain Zed", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Refrain Zed", position=2, position_on_medium=1, duration_ms=900),
        ),
    )

    plan = _planner().plan(unit, release, _align(unit, release))

    assert plan.is_applicable is False
    assert any("named identically" in blocker for blocker in plan.blockers)


def test_a_loose_track_is_tagged_and_renamed_but_no_folder_is_touched(tmp_path: Path) -> None:
    """A single file in a downloads folder must not rename that folder.

    The folder shelves an album, which is what makes it a downloads folder and
    not an album of one track.
    """
    shutil.copy(FIXTURES / "tone.flac", tmp_path / "mystery.flac")
    shelved = tmp_path / "Some Other Album"
    shelved.mkdir()
    shutil.copy(FIXTURES / "tone-long.flac", shelved / "01.flac")
    unit = next(found for found in _scan(tmp_path) if found.is_loose_track)
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Single",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(TrackMetadata(title="A Song", position=1, duration_ms=400),),
        released_on=date(2010, 1, 1),
    )

    plan = _planner().plan(unit, release, _align(unit, release))

    assert unit.is_loose_track
    assert not any(o.kind is OperationKind.RENAME_FOLDER for o in plan.operations)
    rename = next(o for o in plan.operations if o.kind is OperationKind.RENAME_FILE)
    assert Path(rename.after_state["path"]) == tmp_path.resolve() / "01. A Song.flac"


def test_every_operation_records_what_it_would_replace(tmp_path: Path) -> None:
    """Reversal is only possible if the previous state is captured."""
    unit = _album(tmp_path, "wrong", ("a.flac", "b.flac"))
    release = _release()

    plan = _planner().plan(unit, release, _align(unit, release))

    for operation in plan.operations:
        assert operation.before_state, f"{operation.kind} recorded no previous state"
        assert operation.after_state


def test_the_file_name_keeps_the_accent_the_local_tag_carries(tmp_path: Path) -> None:
    """A title the catalogue holds without its accent keeps the accent in the file name.

    The richer spelling wins for the tag, where `preserve_existing_spelling`
    restores it; the file name has no such net, so the planner has to settle the
    spelling before it renders the name.

    Asserted on the rename operation rather than on `resolve_spelling`: the rule
    can pass as a piece while the planner does not apply it.
    """
    unit = _album(tmp_path, "Artist - Album (1999) [FLAC]", ("a.flac", "b.flac"))
    store = MutagenTagStore()
    for file, title in zip(unit.audio_files, ("Zoéllê", "Mirén"), strict=True):
        store.write(file.path, {"title": (title,)})
    unit = _rescan(tmp_path)
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(
            TrackMetadata(title="Zoelle", position=1, duration_ms=400),
            TrackMetadata(title="Miren", position=2, duration_ms=900),
        ),
    )

    plan = _planner().plan(unit, release, _align(unit, release))
    renamed = [
        Path(str(operation.after_state["path"])).name
        for operation in plan.operations
        if operation.kind is OperationKind.RENAME_FILE
    ]

    assert renamed == ["01. Zoéllê.flac", "02. Mirén.flac"]


def _align(unit: AlbumUnit, release: ReleaseMetadata):
    """Align with a tolerance suited to the short fixtures.

    The production tolerance is three seconds, which is wider than the entire
    length of these test files; a tighter one keeps the tests meaningful.
    """
    return align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100)


def _planner(include_edition: bool = False) -> ChangePlanner:
    return ChangePlanner(NamingPolicy(include_edition=include_edition), MutagenTagStore())


def _unit_in(folder: Path, filenames: tuple[str, ...]) -> AlbumUnit:
    """Build one album unit in a folder named exactly as the test needs."""
    return _album(folder.parent, folder.name, filenames)


def _aligned(unit: AlbumUnit, release: ReleaseMetadata):
    return _align(unit, release)


def _reissue(title: str, original: int, pressing: int) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title=title,
        artists=(ArtistMetadata(name="The Zoélles"),),
        tracks=(
            TrackMetadata(title="Dawnlight", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Duskfall", position=2, position_on_medium=2, duration_ms=900),
        ),
        released_on=date(pressing, 1, 1),
        original_released_on=date(original, 1, 1),
    )


def _renamed_to(plan) -> str:
    for operation in plan.operations:
        if operation.kind is OperationKind.RENAME_FOLDER:
            return str(operation.after_state["path"])
    return ""


def _album(root: Path, folder_name: str, filenames: tuple[str, ...]) -> AlbumUnit:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    # Two fixtures of different lengths, so alignment has real evidence to use.
    sources = ("tone.flac", "tone-long.flac")
    for index, name in enumerate(filenames):
        shutil.copy(FIXTURES / sources[index % len(sources)], folder / name)
    return _rescan(root)


def _scan(root: Path) -> tuple[AlbumUnit, ...]:
    import logging

    return LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.planner")).scan(root)


def _rescan(root: Path) -> AlbumUnit:
    return _scan(root)[0]


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


def test_a_pressing_year_already_in_the_folder_is_not_written_again(tmp_path: Path) -> None:
    """The application's own inscription is not a marker somebody wrote by hand.

    The folder below is what an earlier run produced. Read back as a marker to
    preserve, it would survive every replan and could never come off.
    """
    folder = tmp_path / "The Zoélles - Quarrel at the Lanterns (Ed. 1995) (1986) [FLAC]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert _renamed_to(plan).endswith("Quarrel at the Lanterns (1986) [FLAC]")


def test_a_written_designator_with_its_year_still_rides_along(tmp_path: Path) -> None:
    """A designator written by hand, in a shape the template never produces, is kept."""
    folder = tmp_path / "The Zoélles - Quarrel at the Lanterns (Remastered - 1995) [FLAC]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert _renamed_to(plan).endswith("Quarrel at the Lanterns (Remastered - 1995) (1986) [FLAC]")


def test_no_edition_is_invented_for_a_folder_that_never_had_one(tmp_path: Path) -> None:
    """By default no edition is added to a folder that carries none."""
    folder = tmp_path / "The Zoélles - Quarrel at the Lanterns (1986) [FLAC]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert _renamed_to(plan) == "", "the folder name was already right; only its files change"


def test_a_collision_is_broken_by_naming_the_pressing(tmp_path: Path) -> None:
    """The one case that adds text nobody wrote: two pressings, one name."""
    folder = tmp_path / "raw folder"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)
    claimed = frozenset({"The Zoélles - Quarrel at the Lanterns (1986) [FLAC]"})

    plan = _planner().plan(unit, release, _aligned(unit, release), taken_names=claimed)

    assert _renamed_to(plan).endswith("Quarrel at the Lanterns (Ed. 1995) (1986) [FLAC]")


def test_the_switch_still_names_every_pressing(tmp_path: Path) -> None:
    """include_edition keeps its old meaning for anyone who wants it."""
    folder = tmp_path / "raw folder"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)

    plan = _planner(include_edition=True).plan(unit, release, _aligned(unit, release))

    assert _renamed_to(plan).endswith("Quarrel at the Lanterns (Ed. 1995) (1986) [FLAC]")


def test_a_distinct_edition_earns_its_name_unprompted(tmp_path: Path) -> None:
    """Bonus content is a different thing on the shelf, and the name says so."""
    folder = tmp_path / "raw folder"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)
    release = replace(
        release,
        tracks=(
            *release.tracks[:-1],
            replace(release.tracks[-1], title="Duskfall (Bonus Track)"),
        ),
    )

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert "(Ed. 1995) (1986)" in _renamed_to(plan)


def test_a_plain_remaster_still_earns_no_edition(tmp_path: Path) -> None:
    """Same tracklist, new mastering: an edition in the name would only be noise."""
    folder = tmp_path / "raw folder"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = _reissue("Quarrel at the Lanterns", original=1986, pressing=1995)
    release = replace(release, formats=("CD", "Reissue", "Remastered"))

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert "Ed." not in _renamed_to(plan)


def _ep_release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="r",
        title="Live4Lanterns",
        artists=(ArtistMetadata(name="Zoé Marrowby"),),
        tracks=(
            TrackMetadata(title="Dawnlight", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Duskfall", position=2, position_on_medium=2, duration_ms=900),
        ),
        released_on=date(2015, 1, 1),
    )


_WRITTEN_DESIGNATORS = "EP Single Remix Compilation Live"


@pytest.mark.parametrize("written", _WRITTEN_DESIGNATORS.split())
def test_a_designator_already_written_rides_along(tmp_path: Path, written: str) -> None:
    """The catalogue says what the release is; the designator in the folder is kept."""
    folder = tmp_path / f"Zoé Marrowby - Live4Lanterns ({written} - 2015) [FLAC]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))

    plan = _planner().plan(unit, _ep_release(), _aligned(unit, _ep_release()))

    assert _renamed_to(plan) == "", "the folder already carries the designator"


def test_a_designator_survives_a_folder_that_needs_renaming(tmp_path: Path) -> None:
    """A messy name still keeps the designator written in it."""
    folder = tmp_path / "zoe marrowby - live4lanterns (ep - 2015) [flac]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))

    plan = _planner().plan(unit, _ep_release(), _aligned(unit, _ep_release()))

    assert _renamed_to(plan).endswith("Live4Lanterns (EP - 2015) [FLAC]")


def test_no_designator_is_invented(tmp_path: Path) -> None:
    """None is imposed where none was written."""
    folder = tmp_path / "raw folder"
    unit = _unit_in(folder, ("a.flac", "b.flac"))

    plan = _planner().plan(unit, _ep_release(), _aligned(unit, _ep_release()))

    assert _renamed_to(plan).endswith("Live4Lanterns (2015) [FLAC]")


@pytest.mark.parametrize(
    "written",
    ["Album EP", "Album - EP", "Album (EP)", "Album [EP]", "Album ep"],
)
def test_a_designator_is_recognized_however_it_was_written(tmp_path: Path, written: str) -> None:
    """A library assembled over years writes the same fact every way."""
    folder = tmp_path / f"Zoé Marrowby - {written} (2015) [FLAC]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = replace(_ep_release(), title="Album")

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert _renamed_to(plan).endswith("Album (EP - 2015) [FLAC]")


def test_a_word_the_catalogue_itself_uses_is_a_title(tmp_path: Path) -> None:
    """A catalogue may hold a title with and without `(Remixes)` as separate
    release groups, so the word is part of the name."""
    folder = tmp_path / "Orchestra Marrowby - 80 Years (Remixes) (2015) [FLAC]"
    unit = _unit_in(folder, ("a.flac", "b.flac"))
    release = replace(
        _ep_release(),
        title="80 Years (Remixes)",
        artists=(ArtistMetadata(name="Orchestra Marrowby"),),
    )

    plan = _planner().plan(unit, release, _aligned(unit, release))

    assert _renamed_to(plan) == "", "the album's actual name must survive untouched"


def test_a_measured_transcode_is_named_for_what_it_is(tmp_path: Path) -> None:
    """A FLAC the audio proved was an MP3 is named with the configured label.

    The signatures come from the Quality Engine, never from the extension,
    which is the whole point: nothing in the container admits to this.
    """
    unit = _album(tmp_path, "unknown folder", ("aaa.flac", "bbb.flac"))
    release = _release()
    planner = ChangePlanner(NamingPolicy(transcoded_label="Fake"), MutagenTagStore())

    plan = planner.plan(
        unit,
        release,
        _align(unit, release),
        transcoded=frozenset(file.content_signature for file in unit.audio_files),
    )

    assert "[Fake]" in _renamed_to(plan)


def test_an_unmeasured_album_is_named_from_what_it_declares(tmp_path: Path) -> None:
    """Without ffmpeg there is no measurement, and the label degrades honestly."""
    unit = _album(tmp_path, "unknown folder", ("aaa.flac", "bbb.flac"))
    release = _release()

    plan = _planner().plan(unit, release, _align(unit, release))

    assert "[FLAC]" in _renamed_to(plan)


def test_only_the_odd_track_carries_its_quality_in_its_name(tmp_path: Path) -> None:
    """The folder already says what the album holds; every file repeating it is noise.

    The mark goes on the tracks that differ from the majority, so an album
    that agrees with itself leaves every file name clean.
    """
    unit = _album(tmp_path, "unknown folder", ("aaa.flac", "bbb.flac"))
    release = _release()
    odd = unit.audio_files[1]
    planner = ChangePlanner(NamingPolicy(transcoded_label="Fake"), MutagenTagStore())

    plan = planner.plan(
        unit, release, _align(unit, release), transcoded=frozenset({odd.content_signature})
    )

    renamed = {
        Path(op.target_path).name: Path(str(op.after_state["path"])).name
        for op in plan.operations
        if op.kind is OperationKind.RENAME_FILE
    }
    assert "[Fake]" in renamed["bbb.flac"], "the odd track says what it is"
    assert "[" not in renamed["aaa.flac"].removesuffix(".flac"), "the rest stay clean"


def test_an_album_that_agrees_with_itself_marks_no_track(tmp_path: Path) -> None:
    """Every track being a transcode is the folder's news, not each file's."""
    unit = _album(tmp_path, "unknown folder", ("aaa.flac", "bbb.flac"))
    release = _release()
    planner = ChangePlanner(NamingPolicy(transcoded_label="Fake"), MutagenTagStore())

    plan = planner.plan(
        unit,
        release,
        _align(unit, release),
        transcoded=frozenset(file.content_signature for file in unit.audio_files),
    )

    renamed = [
        Path(str(op.after_state["path"])).name
        for op in plan.operations
        if op.kind is OperationKind.RENAME_FILE
    ]
    assert renamed, "the album is still organized"
    assert not any("Fake" in name for name in renamed)
    assert "[Fake]" in _renamed_to(plan), "the folder carries it instead"


def test_a_refusal_names_a_few_and_counts_the_rest() -> None:
    """A folder of many files matched against a short release names all of them.

    Printing every file name overflows the dialog. A refusal is a sentence, so
    the count carries the scale and a few names give the eye somewhere to start.
    """
    names = [f"SOMEBODY MARROWBY - Track {index}.flac" for index in range(1, 29)]

    said = _named_briefly(names)

    assert said.count(",") == 3, "three named, then the count"
    assert said.endswith("and 25 more")
    # Measured against the full enumeration it replaces rather than a magic
    # number.
    assert len(said) * 5 < len(", ".join(sorted(names)))


def test_a_feature_credit_the_file_carries_survives_the_rename(tmp_path: Path) -> None:
    """An aside in the file's own name is kept when the catalogue publishes the bare title.

    A feature credit, a remix or a version named in a parenthesis would be lost
    by renaming to the catalogue's title, so an aside the file has and the
    release does not is carried over.

    Nothing is invented and nothing is repeated: what the catalogue already says
    is not appended a second time.
    """
    folder = tmp_path / "album"
    folder.mkdir()
    shutil.copy(FIXTURES / "tone.flac", folder / "01 - Dawnlight (feat. Zed Marrowby).flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "02 - Duskfall (Live).flac")
    unit = _scan(tmp_path)[0]
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Quarrél",
        artists=(ArtistMetadata(name="Zoé of Lanterns"),),
        tracks=(
            TrackMetadata(title="Dawnlight", position=1, position_on_medium=1, duration_ms=400),
            # The catalogue already says it, so it must not be said twice.
            TrackMetadata(
                title="Duskfall (Live)", position=2, position_on_medium=2, duration_ms=900
            ),
        ),
        released_on=date(1955, 3, 1),
    )

    plan = _planner().plan(unit, release, _align(unit, release))

    renamed = {
        Path(str(op.target_path)).name: Path(str(op.after_state["path"])).name
        for op in plan.operations
        if op.kind is OperationKind.RENAME_FILE
    }
    assert (
        renamed["01 - Dawnlight (feat. Zed Marrowby).flac"]
        == "01. Dawnlight (feat. Zed Marrowby).flac"
    )
    assert renamed.get("02 - Duskfall (Live).flac", "02. Duskfall (Live).flac").count("Live") == 1


def test_a_chosen_cover_replaces_the_finder_icon_that_is_already_there(tmp_path, jpeg) -> None:
    """A chosen cover redraws the icon of every file, including those that have one.

    The icon rule fills an absence, which is right for an ordinary run and wrong
    for a picture chosen to replace everything: a file that already had an icon
    would keep the old one, and the Finder would go on showing it.
    """
    folder = tmp_path / "Marrowby - Lanterns of the Quarry"
    folder.mkdir(parents=True)
    picture = Picture()
    picture.type, picture.mime, picture.depth = 3, "image/jpeg", 24
    picture.width = picture.height = 500
    picture.data = jpeg(500, 500)
    for name in ("01. One.flac", "02. Other.flac"):
        shutil.copy(FIXTURES / "tone.flac", folder / name)
        audio = FLAC(folder / name)
        audio.add_picture(picture)
        audio.save()
    unit = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test")).scan(tmp_path)[0]

    class IconsEverywhere(FilesystemArtworkStore):
        """A disk where every file already draws its cover in the Finder."""

        def shows_its_cover_in_the_finder(self, path):
            return True

        def can_show_its_cover_in_the_finder(self, path):
            return True

    store = IconsEverywhere()
    chosen = tmp_path / "sleeve.jpg"
    chosen.write_bytes(jpeg(1900, 1900))
    staged = StagedImage(
        kind=ImageKind.FRONT,
        path=chosen,
        facts=store.describe(chosen),
    )
    planner = ChangePlanner(NamingPolicy(), MutagenTagStore(), store)

    plan = planner.chosen_cover_plan(unit, staged)

    icons = [
        operation
        for operation in plan.operations
        if operation.kind is OperationKind.EMBED_IMAGE and operation.after_state.get("icon")
    ]
    assert len(icons) == 2, "every file's icon is redrawn, not only the ones without"
    assert any(
        operation.kind is OperationKind.WRITE_IMAGE for operation in plan.operations
    ), "and the folder still gets the picture"


def test_one_release_track_has_no_file_rather_than_have() -> None:
    """A release with one track more than the folder has files reads in the singular.

    The wording lives in one place, so no caller writes the plural by hand.
    """
    assert _tracks_that(1) == "1 release track has"
    assert _tracks_that(2) == "2 release tracks have"
    assert _tracks_that(0) == "0 release tracks have"


def test_the_genre_on_disk_is_respelled_while_every_other_field_keeps_the_disks_spelling(
    tmp_path: Path,
) -> None:
    """`jazz-funk` on the disk was written by this application, not by the user.

    The disk's spelling is kept when only case differs, and that holds for every
    field outside the names. It is asserted beside the genre, on the plan's own
    write. The album is a name, so its case is the rule's.
    """
    unit = _album(tmp_path, "Artist - Album (1999) [FLAC]", ("a.flac", "b.flac"))
    store = MutagenTagStore()
    for file in unit.audio_files:
        store.write(
            file.path,
            {"album": ("ALBUM",), "organization": ("LABEL",), "genre": ("jazz-funk", "mpb")},
        )
    unit = _rescan(tmp_path)
    release = replace(
        _release(),
        title="Album",
        labels=("Label",),
        genres=("Jazz",),
        styles=("Jazz-Funk", "MPB"),
    )

    plan = _planner().plan(unit, release, _align(unit, release))
    written = [
        operation.after_state["tags"]
        for operation in plan.operations
        if operation.kind is OperationKind.WRITE_TAGS
    ]

    assert written, "the genre differs, so every track is rewritten"
    assert all(tags["genre"] == ["Jazz-Funk", "MPB", "Jazz"] for tags in written)
    assert all(tags["organization"] == ["LABEL"] for tags in written)
    assert all(tags["album"] == ["Album"] for tags in written)
