"""Planning, applying, and reverting cover art on real files."""

import hashlib
import logging
import shutil
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.artwork import (
    Artwork,
    ArtworkError,
    FilesystemArtworkStore,
    ImageKind,
    StagedImage,
    describe_bytes,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import ChangeExecutor, ExecutionError
from diglibrary.library.matching import align_tracks
from diglibrary.library.models import AlbumUnit
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import (
    ChangeOperation,
    ChangePlan,
    ChangePlanner,
    OperationKind,
)
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"
ALBUM_NAME = "Marina do Acordeão - Forró (1955) [FLAC]"


def test_the_cover_is_written_into_the_final_folder_and_into_every_track(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Image operations use post-rename paths, because they run last."""
    library = _library(tmp_path, "misnamed folder")
    unit = _rescan(library)
    artwork = _artwork(tmp_path, jpeg)

    plan = _plan(unit, artwork)

    written = [o for o in plan.operations if o.kind is OperationKind.WRITE_IMAGE]
    embedded = [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE]
    assert [o.target_path for o in written] == [library / ALBUM_NAME / "cover.jpg"]
    assert [o.target_path for o in embedded] == [
        library / ALBUM_NAME / "01. Primeira.flac",
        library / ALBUM_NAME / "02. Segunda.flac",
    ]
    assert plan.operations[-1].kind is OperationKind.EMBED_IMAGE


def test_the_back_cover_is_written_beside_the_front(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Front and back are both written to the folder; only the front is embedded."""
    unit = _rescan(_library(tmp_path, "misnamed folder"))
    artwork = _artwork(tmp_path, jpeg, back=(1200, 1200))

    plan = _plan(unit, artwork)

    names = sorted(
        operation.target_path.name
        for operation in plan.operations
        if operation.kind is OperationKind.WRITE_IMAGE
    )
    assert names == ["back.jpg", "cover.jpg"]


def test_a_cover_already_in_place_at_a_larger_size_is_left_alone(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A curated cover is not overwritten by a smaller one from the archive."""
    library = _library(tmp_path, ALBUM_NAME)
    (library / ALBUM_NAME / "cover.jpg").write_bytes(jpeg(1600, 1600))
    unit = _rescan(library)

    plan = _plan(unit, _artwork(tmp_path, jpeg))

    assert not any(o.kind is OperationKind.WRITE_IMAGE for o in plan.operations)
    assert any(o.kind is OperationKind.EMBED_IMAGE for o in plan.operations)


def test_an_album_that_already_has_this_exact_art_plans_nothing(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A second run over an organized album stays silent, art included."""
    library = _library(tmp_path, ALBUM_NAME, ("01. Primeira.flac", "02. Segunda.flac"))
    artwork = _artwork(tmp_path, jpeg)
    unit = _rescan(library)
    plan = _plan(unit, artwork)
    _executor(tmp_path).apply(plan)

    second = _plan(_rescan(library), artwork)

    assert second.is_empty


def test_a_multi_disc_album_gets_a_cover_in_every_disc_folder(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The cover belongs in each disc folder, under its canonical name."""
    library = tmp_path / "library"
    (library / "some anthology" / "CD 1").mkdir(parents=True)
    (library / "some anthology" / "disc2").mkdir()
    shutil.copy(FIXTURES / "tone.flac", library / "some anthology" / "CD 1" / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", library / "some anthology" / "disc2" / "bbb.flac")
    unit = _rescan(library)

    plan = _plan(unit, _artwork(tmp_path, jpeg), release=_multi_disc_release())

    organized = library / "Some Artist - Anthology (1998) [FLAC]"
    assert [
        operation.target_path
        for operation in plan.operations
        if operation.kind is OperationKind.WRITE_IMAGE
    ] == [organized / "CD1" / "cover.jpg", organized / "CD2" / "cover.jpg"]


def test_a_loose_track_receives_the_picture_but_no_file(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Dropping cover.jpg beside a loose track would put art in a folder that is not an album."""
    library = tmp_path / "library"
    library.mkdir()
    shutil.copy(FIXTURES / "tone.flac", library / "mystery.flac")
    # The folder shelves an album, which is what makes it a downloads folder
    # rather than an album of one track.
    shelved = library / "Some Other Album"
    shelved.mkdir()
    shutil.copy(FIXTURES / "tone-long.flac", shelved / "01.flac")
    unit = next(found for found in _scan(library) if found.is_loose_track)
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Single",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(TrackMetadata(title="A Song", position=1, duration_ms=400),),
    )

    plan = _plan(unit, _artwork(tmp_path, jpeg), release=release)

    assert unit.is_loose_track
    assert not any(o.kind is OperationKind.WRITE_IMAGE for o in plan.operations)
    assert sum(o.kind is OperationKind.EMBED_IMAGE for o in plan.operations) == 1


def test_applying_then_reverting_restores_the_album_exactly(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """An applied plan reverts exactly, once images are part of the plan."""
    library = _library(tmp_path, "misnamed folder")
    before = _snapshot(library)
    plan = _plan(_rescan(library), _artwork(tmp_path, jpeg, back=(1200, 1200)))
    executor = _executor(tmp_path)

    result = executor.apply(plan)
    assert result.is_complete
    assert (library / ALBUM_NAME / "cover.jpg").is_file()
    assert (
        FilesystemArtworkStore().describe_embedded(library / ALBUM_NAME / "01. Primeira.flac")
        is not None
    )

    executor.revert(result)

    assert _snapshot(library) == before


def test_a_replaced_cover_goes_to_the_backup_and_comes_back(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Nothing is deleted: a smaller cover is moved aside, and reversal restores it."""
    library = _library(tmp_path, ALBUM_NAME)
    existing = library / ALBUM_NAME / "cover.jpg"
    existing.write_bytes(jpeg(250, 250))
    original = existing.read_bytes()
    plan = _plan(_rescan(library), _artwork(tmp_path, jpeg))
    executor = _executor(tmp_path)

    result = executor.apply(plan)
    assert existing.read_bytes() != original
    assert any(path.is_file() for path in (tmp_path / "app" / "backup").iterdir())

    executor.revert(result)

    assert existing.read_bytes() == original


def test_a_replaced_picture_inside_a_track_comes_back(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The picture already in a file is backed up before a larger one replaces it."""
    library = _library(tmp_path, ALBUM_NAME, ("01. Primeira.flac", "02. Segunda.flac"))
    track = library / ALBUM_NAME / "01. Primeira.flac"
    store = FilesystemArtworkStore()
    small = tmp_path / "small.jpg"
    small.write_bytes(jpeg(100, 100))
    store.embed(track, small)
    plan = _plan(_rescan(library), _artwork(tmp_path, jpeg))
    executor = _executor(tmp_path)

    result = executor.apply(plan)
    replaced = store.describe_embedded(track)
    assert replaced is not None and replaced.width == 500

    executor.revert(result)

    restored = store.describe_embedded(track)
    assert restored is not None and restored.width == 100


def test_planning_art_without_an_artwork_store_is_refused(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A misassembled planner fails loudly rather than dropping the images."""
    unit = _rescan(_library(tmp_path, "misnamed folder"))
    planner = ChangePlanner(NamingPolicy(), MutagenTagStore())
    release = _release()

    with pytest.raises(ValueError):
        planner.plan(unit, release, _align(unit, release), _artwork(tmp_path, jpeg))


def test_an_image_plan_is_refused_before_any_write_when_the_executor_lacks_a_store(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The check happens up front, so no album is left half organized."""
    library = _library(tmp_path, "misnamed folder")
    plan = _plan(_rescan(library), _artwork(tmp_path, jpeg))
    before = _snapshot(library)

    with pytest.raises(ExecutionError):
        ChangeExecutor(MutagenTagStore(), logging.getLogger("test.cover")).apply(plan)

    assert _snapshot(library) == before


def _artwork(
    root: Path,
    jpeg: Callable[[int, int], bytes],
    front: tuple[int, int] = (1200, 1200),
    embed: tuple[int, int] = (500, 500),
    back: tuple[int, int] | None = None,
) -> Artwork:
    """Stage images the way the artwork service would, outside the library."""
    staging = root / "app" / "staging"
    staging.mkdir(parents=True, exist_ok=True)

    def stage(kind: ImageKind, size: tuple[int, int]) -> StagedImage:
        data = jpeg(*size)
        facts = describe_bytes(data)
        assert facts is not None
        path = staging / f"{facts.digest}.jpg"
        path.write_bytes(data)
        return StagedImage(kind=kind, path=path, facts=facts)

    files = [stage(ImageKind.FRONT, front)]
    if back is not None:
        files.append(stage(ImageKind.BACK, back))
    return Artwork(files=tuple(files), embedded=stage(ImageKind.FRONT, embed))


def _plan(
    unit: AlbumUnit, artwork: Artwork | None, release: ReleaseMetadata | None = None
) -> ChangePlan:
    release = release or _release()
    planner = ChangePlanner(NamingPolicy(), MutagenTagStore(), FilesystemArtworkStore())
    return planner.plan(unit, release, _align(unit, release), artwork)


def _align(unit: AlbumUnit, release: ReleaseMetadata):
    return align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100)


def _executor(root: Path) -> ChangeExecutor:
    backup = root / "app" / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    return ChangeExecutor(
        MutagenTagStore(),
        logging.getLogger("test.cover"),
        FilesystemArtworkStore(),
        backup,
    )


def _library(
    root: Path,
    folder_name: str,
    filenames: tuple[str, ...] = ("aaa.flac", "bbb.flac"),
) -> Path:
    library = root / "library"
    folder = library / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    for name, fixture in zip(filenames, ("tone.flac", "tone-long.flac"), strict=True):
        shutil.copy(FIXTURES / fixture, folder / name)
    return library


def _scan(library: Path) -> tuple[AlbumUnit, ...]:
    return LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.cover")).scan(library)


def _rescan(library: Path) -> AlbumUnit:
    return _scan(library)[0]


def _snapshot(root: Path) -> list[tuple[str, str]]:
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


def test_a_track_whose_picture_lies_about_its_size_is_re_embedded(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The same image, planned again, because the label on it is wrong.

    A picture is replaced only by one that has more pixels, and the comparison
    reads the bytes — so a file already carrying this exact image is left
    alone, correctly. What that misses is a block whose declared width, height
    and depth are zero over a perfectly good JPEG, which a player that trusts
    the declaration will not draw.

    Planning the embed again is the whole repair. The bytes make the round trip
    unchanged and come back through the writer, which fills the fields in from
    the image itself.
    """
    from mutagen.flac import FLAC

    library = _library(tmp_path, "misnamed folder")
    artwork = _artwork(tmp_path, jpeg)
    store = FilesystemArtworkStore()
    embedded_image = jpeg(500, 500)

    for track in sorted((library / "misnamed folder").glob("*.flac")):
        image = tmp_path / "already.jpg"
        image.write_bytes(embedded_image)
        store.embed(track, image)
        holder = FLAC(track)
        picture = holder.pictures[0]
        picture.width, picture.height, picture.depth = 0, 0, 0
        holder.clear_pictures()
        holder.add_picture(picture)
        holder.save()

    unit = _rescan(library)
    plan = _plan(unit, artwork)

    embedded = [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE]
    assert len(embedded) == 2, "both tracks are planned, though the image is already there"


def test_a_track_already_carrying_the_same_picture_is_left_alone(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The other half of the same rule, so that repairing does not make every run noisy.

    A second run of an organized album stays silent: the repair fires on a block
    that disagrees with its own image, never on one that agrees.
    """
    library = _library(tmp_path, "misnamed folder")
    artwork = _artwork(tmp_path, jpeg)
    store = FilesystemArtworkStore()
    image = tmp_path / "already.jpg"
    image.write_bytes(jpeg(500, 500))
    for track in sorted((library / "misnamed folder").glob("*.flac")):
        store.embed(track, image)

    unit = _rescan(library)
    plan = _plan(unit, artwork)

    assert [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE] == []


def _with_a_lying_block(library: Path, folder: str, data: bytes) -> None:
    """Embed ``data`` in every track and blank the block the way those writers do."""
    from mutagen.flac import FLAC

    store = FilesystemArtworkStore()
    image = library.parent / "arrived-with-the-album.jpg"
    image.write_bytes(data)
    for track in sorted((library / folder).glob("*.flac")):
        store.embed(track, image)
        holder = FLAC(track)
        picture = holder.pictures[0]
        picture.width, picture.height, picture.depth = 0, 0, 0
        holder.clear_pictures()
        holder.add_picture(picture)
        holder.save()


def test_a_lying_block_is_repaired_with_its_own_picture_never_with_the_candidate(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Repairing a label must not become an excuse to replace the image.

    The repair stands beside the refusal to replace a picture with a smaller
    one; it does not cancel it. A file carrying a 640-pixel cover under a `0x0`
    block keeps that cover when the archive offers a 500-pixel one, and only
    the declaration is corrected.
    """
    from mutagen.flac import FLAC

    library = _library(tmp_path, "misnamed folder")
    already_there = jpeg(640, 640)
    _with_a_lying_block(library, "misnamed folder", already_there)
    artwork = _artwork(tmp_path, jpeg, embed=(500, 500))

    plan = _plan(_rescan(library), artwork)
    embedded = [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE]
    assert len(embedded) == 2, "both tracks are planned: the label on both is wrong"
    assert all(o.after_state.get("repair") for o in embedded), "as repairs, not as replacements"

    _executor(tmp_path).apply(plan)

    for track in sorted((library / ALBUM_NAME).glob("*.flac")):
        picture = FLAC(track).pictures[0]
        assert bytes(picture.data) == already_there, "the image it already carried, byte for byte"
        assert (picture.width, picture.height, picture.depth) == (640, 640, 24)


def test_a_lying_block_is_repaired_even_when_nothing_was_fetched(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The repair cannot depend on there being an image to replace it with.

    This is a common case: an album identified on Discogs has no Cover Art
    Archive picture to embed, and a folder cover is refused for files that
    already carry one. A repair that waited for a fetched image would leave
    `0x0` on every track of such an album.
    """
    from mutagen.flac import FLAC

    library = _library(tmp_path, "misnamed folder")
    already_there = jpeg(640, 640)
    _with_a_lying_block(library, "misnamed folder", already_there)

    plan = _plan(_rescan(library), Artwork())
    embedded = [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE]
    assert len(embedded) == 2, "nothing was fetched, and the label is still wrong"

    _executor(tmp_path).apply(plan)

    for track in sorted((library / ALBUM_NAME).glob("*.flac")):
        picture = FLAC(track).pictures[0]
        assert bytes(picture.data) == already_there
        assert (picture.width, picture.height, picture.depth) == (640, 640, 24)


def test_no_picture_is_touched_when_cover_art_is_switched_off(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Off means off, including the repair that needs nothing fetched.

    The repair reaches an album with no artwork at all, so *nothing was found*
    and *artwork is turned off* cannot be the same empty answer: the second one
    arrives as no artwork rather than as an empty one.
    """
    library = _library(tmp_path, "misnamed folder")
    _with_a_lying_block(library, "misnamed folder", jpeg(640, 640))

    plan = _plan(_rescan(library), None)

    assert [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE] == []


def test_a_picture_already_640_wide_is_never_replaced(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """At 640 pixels the embedded picture is the one the album keeps.

    Many files arrive carrying a 640-pixel front. From that floor upward the
    embedded picture stays, whatever the archive offers — the folder's cover is
    where a bigger image belongs, and it still gets one.
    """
    library = _library(tmp_path, "misnamed folder")
    store = FilesystemArtworkStore()
    image = tmp_path / "already-there.jpg"
    image.write_bytes(jpeg(640, 640))
    for track in sorted((library / "misnamed folder").glob("*.flac")):
        store.embed(track, image)
    artwork = _artwork(tmp_path, jpeg, front=(1200, 1200), embed=(500, 500))

    plan = _plan(_rescan(library), artwork)

    assert [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE] == []
    written = [o for o in plan.operations if o.kind is OperationKind.WRITE_IMAGE]
    assert [o.target_path.name for o in written] == ["cover.jpg"], "the folder still gets its own"


def test_a_picture_below_the_floor_is_still_improved(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Below the floor more pixels win, and the size only ever goes upward."""
    library = _library(tmp_path, "misnamed folder")
    store = FilesystemArtworkStore()
    image = tmp_path / "small.jpg"
    image.write_bytes(jpeg(300, 300))
    for track in sorted((library / "misnamed folder").glob("*.flac")):
        store.embed(track, image)
    artwork = _artwork(tmp_path, jpeg, front=(1200, 1200), embed=(500, 500))

    plan = _plan(_rescan(library), artwork)

    embedded = [o for o in plan.operations if o.kind is OperationKind.EMBED_IMAGE]
    replacements = [o for o in embedded if not o.after_state.get("icon")]
    assert len(replacements) == 2
    assert all(o.after_state["width"] == 500 for o in replacements)


def _a_picture_the_system_can_draw() -> bytes:
    """Return a real JPEG, made by the system that will be asked to render it.

    The suite's own `jpeg` fixture writes a header and no image, which is enough
    to be measured and not enough to be drawn — exactly the case the planner
    must refuse rather than offer forever.
    """
    import AppKit

    image = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(64, 64))
    image.lockFocus()
    AppKit.NSColor.orangeColor().setFill()
    AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(0, 0, 64, 64))
    image.unlockFocus()
    sheet = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    return bytes(sheet.representationUsingType_properties_(AppKit.NSBitmapImageFileTypeJPEG, {}))


def test_a_file_with_no_finder_icon_is_given_one_from_its_own_picture(tmp_path: Path) -> None:
    """The third place a cover lives, and the only one the Finder shows for a FLAC.

    The icon is given only where there is none, drawn from the picture the file
    already carries, and reverting takes back exactly what was given. A file
    that arrives with an icon keeps it unchanged.
    """
    if sys.platform != "darwin":
        pytest.skip("The Finder icon is written through the macOS frameworks.")
    from diglibrary.library.xattrs import draws_its_own_icon

    library = _library(tmp_path, "misnamed folder")
    store = FilesystemArtworkStore()
    image = tmp_path / "real.jpg"
    image.write_bytes(_a_picture_the_system_can_draw())
    for track in sorted((library / "misnamed folder").glob("*.flac")):
        store.embed(track, image)
        assert not draws_its_own_icon(track), "these files start with no icon"

    plan = _plan(_rescan(library), Artwork())
    icons = [o for o in plan.operations if o.after_state.get("icon")]
    assert len(icons) == 2, "both files are offered one, with no artwork fetched at all"

    executor = _executor(tmp_path)
    result = executor.apply(plan)
    tracks = sorted((library / ALBUM_NAME).glob("*.flac"))
    assert [draws_its_own_icon(track) for track in tracks] == [True, True]

    again = _plan(_rescan(library), Artwork()).operations
    assert not [o for o in again if o.after_state.get("icon")], "and never offered twice"

    executor.revert(result)

    given_back = sorted((library / "misnamed folder").glob("*.flac"))
    assert [draws_its_own_icon(track) for track in given_back] == [False, False]


def test_an_icon_the_file_already_had_is_never_touched(tmp_path: Path) -> None:
    """An icon the file arrived with is never rewritten."""
    if sys.platform != "darwin":
        pytest.skip("The Finder icon is written through the macOS frameworks.")
    from diglibrary.library.xattrs import _read, draws_its_own_icon

    library = _library(tmp_path, "misnamed folder")
    store = FilesystemArtworkStore()
    image = tmp_path / "real.jpg"
    image.write_bytes(_a_picture_the_system_can_draw())
    already_there = tmp_path / "already-there.jpg"
    already_there.write_bytes(_a_picture_the_system_can_draw())
    for track in sorted((library / "misnamed folder").glob("*.flac")):
        store.embed(track, image)
        assert store.draw_in_the_finder(track), "as if the file had arrived with one"
    was = [
        _read(track, "com.apple.ResourceFork")
        for track in sorted((library / "misnamed folder").glob("*.flac"))
    ]

    plan = _plan(_rescan(library), Artwork())

    assert [o for o in plan.operations if o.after_state.get("icon")] == []
    _executor(tmp_path).apply(plan)
    kept = sorted((library / ALBUM_NAME).glob("*.flac"))
    assert [draws_its_own_icon(track) for track in kept] == [True, True]
    assert [
        _read(track, "com.apple.ResourceFork") for track in kept
    ] == was, "byte for byte as it was"


def test_a_file_with_no_picture_at_all_is_given_its_icon_in_the_same_run(tmp_path: Path) -> None:
    """Where there is none, write it — and in this run, not the next one.

    A file that arrives carrying nothing has no picture to draw an icon from at
    the moment the plan is made, and it is exactly the file that is about to be
    given one. Asking only what it holds at planning time would hand it its
    cover in this run and its icon only in a later one.
    """
    if sys.platform != "darwin":
        pytest.skip("The Finder icon is written through the macOS frameworks.")
    from diglibrary.library.xattrs import draws_its_own_icon

    library = _library(tmp_path, "misnamed folder")
    real = _a_picture_the_system_can_draw()
    staging = tmp_path / "app" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    facts = describe_bytes(real)
    assert facts is not None
    staged = staging / f"{facts.digest}.jpg"
    staged.write_bytes(real)
    artwork = Artwork(
        files=(StagedImage(kind=ImageKind.FRONT, path=staged, facts=facts),),
        embedded=StagedImage(kind=ImageKind.FRONT, path=staged, facts=facts),
    )
    tracks = sorted((library / "misnamed folder").glob("*.flac"))
    store = FilesystemArtworkStore()
    assert [store.describe_embedded(track) for track in tracks] == [None, None]

    plan = _plan(_rescan(library), artwork)
    icons = [o for o in plan.operations if o.after_state.get("icon")]
    assert len(icons) == 2, "the picture arriving in this very run is what they will draw"

    _executor(tmp_path).apply(plan)

    organized = sorted((library / ALBUM_NAME).glob("*.flac"))
    assert [draws_its_own_icon(track) for track in organized] == [True, True]


def test_a_cover_that_cannot_be_written_leaves_the_old_one_where_it_was(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The destructive half of this operation runs before the operation can be
    recorded, and an operation that raises never reaches `applied`.

    If the existing cover is moved into the backup folder and the copy then
    fails — a full disk, a permission, an I/O error on an external drive —
    nothing on record says where that file came from: the album has no cover
    and a revert restores everything except the picture. Every write is
    preceded by a *reversible* backup, and a backup nothing can find is not
    one, so a failed write puts the moved cover back.
    """
    album = tmp_path / "album"
    album.mkdir()
    original = album / "cover.jpg"
    original.write_bytes(jpeg(500, 500))
    staged = tmp_path / "staged.jpg"
    staged.write_bytes(jpeg(1200, 1200))
    executor = _executor(tmp_path)

    class RefusingStore(FilesystemArtworkStore):
        def copy(self, source: Path, destination: Path) -> tuple[Path, ...]:
            raise ArtworkError("the drive filled up")

    executor._artwork_store = RefusingStore()
    operation = ChangeOperation(
        sequence=1,
        kind=OperationKind.WRITE_IMAGE,
        target_path=original,
        after_state={"source": str(staged), "digest": "whatever", "kind": "front"},
    )

    with pytest.raises(ArtworkError):
        executor._write_image(operation)

    assert original.is_file(), "the cover it moved aside is put back when the write fails"
    assert original.read_bytes() == jpeg(500, 500)
    backups = list((tmp_path / "app" / "backup").glob("*"))
    assert backups == [], "and nothing is orphaned in the backup folder"


def test_two_discs_carrying_the_same_cover_both_get_it_back_on_a_revert(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A backup is named by its own content, so identical covers share one file.

    That is right for storage, and it means an undo must not *move* the file
    back: the first disc undone would consume it and the second would find
    nothing there, and `store.move` on a missing path raises, which aborts the
    whole revert. A two-disc album where both folders carry the same picture is
    the ordinary case, not a contrived one.
    """
    executor = _executor(tmp_path)
    album = tmp_path / "album"
    shared = jpeg(500, 500)
    covers = []
    for disc in ("CD1", "CD2"):
        (album / disc).mkdir(parents=True)
        cover = album / disc / "cover.jpg"
        cover.write_bytes(shared)
        covers.append(cover)
    staged = tmp_path / "staged.jpg"
    staged.write_bytes(jpeg(1200, 1200))

    applied = []
    for sequence, cover in enumerate(covers, start=1):
        written = executor._write_image(
            ChangeOperation(
                sequence=sequence,
                kind=OperationKind.WRITE_IMAGE,
                target_path=cover,
                after_state={
                    "source": str(staged),
                    "digest": describe_bytes(jpeg(1200, 1200)).digest,
                    "kind": "front",
                },
            )
        )
        applied.append(written)
    assert all(cover.read_bytes() != shared for cover in covers), "both were replaced"

    # Newest first, which is the order a revert walks.
    for entry in reversed(applied):
        executor._undo_write_image(entry)

    assert [cover.read_bytes() for cover in covers] == [
        shared,
        shared,
    ], "each disc gets its own cover back, and the digest proves the bytes are its own"
