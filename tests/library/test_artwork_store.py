"""Unit tests for reading, comparing, embedding, and undoing cover images."""

import hashlib
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from diglibrary.library.artwork import (
    ArtworkError,
    FilesystemArtworkStore,
    ImageFacts,
    describe_bytes,
    extension_for,
    is_worth_writing,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"
CONTAINERS = ("tone.flac", "tone.mp3", "tone.m4a", "tone.wav", "tone.aiff")


def test_a_jpeg_is_measured_from_its_frame_header(jpeg: Callable[[int, int], bytes]) -> None:
    """Dimensions come from the image itself, never from a tag that claims them."""
    facts = describe_bytes(jpeg(1200, 1198))

    assert facts is not None
    assert (facts.width, facts.height) == (1200, 1198)
    assert facts.media_type == "image/jpeg"
    assert extension_for(facts.media_type) == ".jpg"


def test_a_png_is_measured_from_its_header_chunk(png: Callable[[int, int], bytes]) -> None:
    """The archive serves PNG as well as JPEG, and both are written as served."""
    facts = describe_bytes(png(500, 500))

    assert facts is not None
    assert (facts.width, facts.height) == (500, 500)
    assert facts.media_type == "image/png"
    assert extension_for(facts.media_type) == ".png"


def test_bytes_that_are_not_an_image_are_reported_as_unknown() -> None:
    """An unreadable image is never guessed at, so it never wins a comparison."""
    assert describe_bytes(b"this is not a picture") is None
    assert describe_bytes(b"") is None


def test_only_a_larger_image_replaces_what_is_already_there(
    jpeg: Callable[[int, int], bytes],
) -> None:
    """An existing cover is replaced by more pixels and by nothing else."""
    small = describe_bytes(jpeg(500, 500))
    large = describe_bytes(jpeg(1200, 1200))
    assert small is not None and large is not None

    assert is_worth_writing(None, small) is True
    assert is_worth_writing(small, large) is True
    assert is_worth_writing(large, small) is False
    assert is_worth_writing(large, large) is False, "the same image is never rewritten"


@pytest.mark.parametrize("container", CONTAINERS)
def test_a_picture_round_trips_through_every_container(
    tmp_path: Path, jpeg: Callable[[int, int], bytes], container: str
) -> None:
    """Each format stores a picture differently; all five must read back the same."""
    audio = tmp_path / container
    shutil.copy(FIXTURES / container, audio)
    image = tmp_path / "cover.jpg"
    image.write_bytes(jpeg(500, 400))
    store = FilesystemArtworkStore()

    store.embed(audio, image)

    facts = store.describe_embedded(audio)
    assert facts is not None
    assert (facts.width, facts.height) == (500, 400)
    assert facts.digest == hashlib.sha256(image.read_bytes()).hexdigest()


@pytest.mark.parametrize("container", CONTAINERS)
def test_an_embedded_picture_can_be_extracted_and_then_cleared(
    tmp_path: Path, jpeg: Callable[[int, int], bytes], container: str
) -> None:
    """Reversal depends on both halves: taking the old picture out, and removing the new."""
    audio = tmp_path / container
    shutil.copy(FIXTURES / container, audio)
    image = tmp_path / "cover.jpg"
    image.write_bytes(jpeg(300, 300))
    store = FilesystemArtworkStore()
    store.embed(audio, image)

    extracted = store.extract(audio, tmp_path / "backup" / "old.jpg")
    store.clear(audio)

    assert extracted is not None and (extracted.width, extracted.height) == (300, 300)
    assert (tmp_path / "backup" / "old.jpg").read_bytes() == image.read_bytes()
    assert store.describe_embedded(audio) is None


def test_a_file_without_a_picture_describes_as_nothing(tmp_path: Path) -> None:
    """An album with no art is an ordinary state, not a failure."""
    audio = tmp_path / "tone.flac"
    shutil.copy(FIXTURES / "tone.flac", audio)

    assert FilesystemArtworkStore().describe_embedded(audio) is None


def test_an_image_is_removed_only_while_it_is_still_what_was_written(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The one deletion in the pipeline is bounded by the digest."""
    store = FilesystemArtworkStore()
    written = tmp_path / "cover.jpg"
    written.write_bytes(jpeg(500, 500))
    facts = store.describe(written)
    assert facts is not None

    written.write_bytes(jpeg(600, 600))
    store.remove(written, facts.digest)
    assert written.is_file(), "a file the user replaced is theirs now"

    replaced = store.describe(written)
    assert replaced is not None
    store.remove(written, replaced.digest)
    assert not written.exists()


def test_embedding_something_that_is_not_an_image_is_refused(tmp_path: Path) -> None:
    """Nothing unreadable is ever written into a user's file."""
    audio = tmp_path / "tone.flac"
    shutil.copy(FIXTURES / "tone.flac", audio)
    junk = tmp_path / "junk.jpg"
    junk.write_bytes(b"not a picture")

    with pytest.raises(ArtworkError):
        FilesystemArtworkStore().embed(audio, junk)


def test_copying_reports_the_directories_it_had_to_create(
    tmp_path: Path, png: Callable[[int, int], bytes]
) -> None:
    """A reversal removes exactly the folders the write created, and no others."""
    source = tmp_path / "staged.png"
    source.write_bytes(png(250, 250))

    created = FilesystemArtworkStore().copy(source, tmp_path / "album" / "CD1" / "cover.png")

    assert (tmp_path / "album" / "CD1" / "cover.png").is_file()
    assert created == (tmp_path / "album" / "CD1", tmp_path / "album")


@pytest.mark.parametrize("dangling", [True, False])
def test_a_write_is_never_followed_through_a_symbolic_link(
    tmp_path: Path, png: Callable[[int, int], bytes], dangling: bool
) -> None:
    """This is the only write in the pipeline that opens its destination.

    Every other one renames, which acts on the link and not on what it points
    at. A folder extracted from an archive a stranger built can carry a
    `cover.jpg` pointing anywhere on the disk — the Rekordbox database among
    them — and a *dangling* link answers `False` to both `describe` and
    `exists`, so nothing upstream stops it. Then `shutil.copyfile` opens the
    destination `'wb'` and the write lands outside the album folder entirely.
    """
    source = tmp_path / "staged.png"
    source.write_bytes(png(250, 250))
    outside = tmp_path / "elsewhere" / "precious.xml"
    outside.parent.mkdir()
    if not dangling:
        outside.write_bytes(b"the file this must never reach")
    album = tmp_path / "album"
    album.mkdir()
    (album / "cover.png").symlink_to(outside)

    with pytest.raises(ArtworkError, match="symbolic link"):
        FilesystemArtworkStore().copy(source, album / "cover.png")

    assert (
        not outside.exists()
        if dangling
        else outside.read_bytes() == (b"the file this must never reach")
    )


def test_facts_compare_by_pixels(jpeg: Callable[[int, int], bytes]) -> None:
    """A wide image and a tall one of the same area are equally good."""
    wide = describe_bytes(jpeg(1000, 500))
    tall = describe_bytes(jpeg(500, 1000))
    assert isinstance(wide, ImageFacts) and isinstance(tall, ImageFacts)

    assert wide.pixels == tall.pixels
    assert is_worth_writing(wide, tall) is False


def test_a_picture_big_enough_already_is_never_swapped_inside_a_track(
    jpeg: Callable[[int, int], bytes],
) -> None:
    """The track's picture and the folder's cover stop at different sizes.

    At 640x640 the embedded picture is enough, so nothing the archive publishes
    competes for the inside of a file — and a replacement is never only a
    replacement, since it leaves the icon the Finder draws showing the picture
    that was there. Below the floor more pixels win, and the folder's own cover
    still takes the big one.
    """
    from diglibrary.library.artwork import is_worth_embedding

    small = describe_bytes(jpeg(400, 400))
    enough = describe_bytes(jpeg(640, 640))
    large = describe_bytes(jpeg(1200, 1200))
    assert small is not None and enough is not None and large is not None

    assert is_worth_embedding(enough, large) is False, "the embedded picture is the one that stays"
    assert is_worth_writing(enough, large) is True, "and the folder's cover still takes it"
    assert is_worth_embedding(small, enough) is True, "below the floor, more pixels win"
    assert is_worth_embedding(small, describe_bytes(jpeg(300, 300))) is False, "fewer never do"
    assert is_worth_embedding(None, small) is True, "an empty file still gets a picture"


def test_a_picture_block_that_lies_about_its_size_is_seen(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A good image no player will draw, because the label says it is nothing.

    A FLAC picture block states width, height and depth, and a player is
    entitled to believe it. Several writers leave those at zero, so a block can
    carry `0x0` with depth zero over a perfectly good JPEG, and a player that
    trusts the label shows no cover.

    `describe_embedded` reads the *bytes*, so a planner comparing images sees
    two correct ones and rightly finds nothing to write — an image is replaced
    only by one with more pixels, and these have exactly as many. The
    declaration has to be checked on its own.
    """
    from mutagen.flac import FLAC

    audio = tmp_path / "track.flac"
    shutil.copy(FIXTURES / "tone.flac", audio)
    image = tmp_path / "cover.jpg"
    image.write_bytes(jpeg(640, 640))
    store = FilesystemArtworkStore()
    store.embed(audio, image)
    assert not store.misdeclares_embedded(audio), "what this store writes is declared right"

    # Blanked the way those writers leave it, with the bytes untouched.
    holder = FLAC(audio)
    picture = holder.pictures[0]
    picture.width, picture.height, picture.depth = 0, 0, 0
    holder.clear_pictures()
    holder.add_picture(picture)
    holder.save()

    assert store.misdeclares_embedded(audio), "the block disagrees with its own image"
    described = store.describe_embedded(audio)
    assert described is not None and described.width == 640, "and the bytes are fine"

    store.embed(audio, image)

    assert not store.misdeclares_embedded(audio)
    repaired = FLAC(audio).pictures[0]
    assert (repaired.width, repaired.height, repaired.depth) == (640, 640, 24)
    assert bytes(repaired.data) == image.read_bytes(), "the same image, relabelled"


def test_a_container_that_declares_no_size_is_not_reported_as_lying(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """ID3 and MP4 store no dimensions, so there is nothing there to disagree with."""
    store = FilesystemArtworkStore()
    image = tmp_path / "cover.jpg"
    image.write_bytes(jpeg(500, 500))
    for name in ("tone.mp3", "tone.m4a"):
        audio = tmp_path / name
        shutil.copy(FIXTURES / name, audio)
        store.embed(audio, image)
        assert not store.misdeclares_embedded(audio), name


def test_a_file_with_no_picture_at_all_is_not_reported_as_lying(tmp_path: Path) -> None:
    """Nothing to disagree with, and no reason to open it twice."""
    audio = tmp_path / "track.flac"
    shutil.copy(FIXTURES / "tone.flac", audio)

    assert not FilesystemArtworkStore().misdeclares_embedded(audio)


def _drawable_jpeg() -> bytes:
    """A small JPEG the platform will render, made by the platform itself."""
    import AppKit

    image = AppKit.NSImage.alloc().initWithSize_((64, 64))
    image.lockFocus()
    AppKit.NSColor.orangeColor().setFill()
    AppKit.NSRectFill(((0, 0), (64, 64)))
    image.unlockFocus()
    bitmap = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    data = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypeJPEG, {})
    return bytes(data)


def test_a_file_that_claims_an_icon_and_has_none_is_given_one(tmp_path: Path) -> None:
    """An empty icon claim is taken away before the icon is written.

    The system will not write an icon onto a file that already *claims* one:
    with `kHasCustomIcon` set over a resource fork holding nothing, `NSWorkspace`
    answers `False`. Not believing the claim is therefore not enough, because
    the system believes it; the claim has to be removed first, and then the same
    call succeeds.
    """
    if sys.platform != "darwin":
        pytest.skip("the Finder's icon is written through the platform's own call")
    from diglibrary.library.xattrs import _write, claims_an_icon, draws_its_own_icon

    audio = tmp_path / "01. Glazier's Waltz.flac"
    shutil.copy(FIXTURES / "tone.flac", audio)
    image = tmp_path / "cover.jpg"
    # A picture the system can actually draw. The suite's own `jpeg` builds a
    # 41-byte header, which is everything the planner's comparisons need and
    # nothing an icon can be made of — and an icon is drawn by the platform,
    # not measured by this project.
    image.write_bytes(_drawable_jpeg())
    store = FilesystemArtworkStore()
    store.embed(audio, image)
    # The flag set, over a fork holding nothing.
    _write(audio, "com.apple.FinderInfo", b"\x00" * 8 + b"\x04\x00" + b"\x00" * 22)
    _write(audio, "com.apple.ResourceFork", b"\x00" * 128)
    assert claims_an_icon(audio) and not draws_its_own_icon(audio), "an empty claim"

    assert store.draw_in_the_finder(audio), "the empty claim is what the system refuses"

    assert draws_its_own_icon(audio), "and now the file really does draw one"
