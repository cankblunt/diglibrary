"""Cover art as the library sees it: what an image is, and how it reaches a file."""

import hashlib
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import APIC, ID3, PictureType
from mutagen.mp4 import MP4Cover

from diglibrary.library.atomicwrite import replacing
from diglibrary.library.xattrs import claims_an_icon, draws_its_own_icon, forget_own_icon

JPEG_MEDIA_TYPE = "image/jpeg"
PNG_MEDIA_TYPE = "image/png"

_EXTENSIONS = {JPEG_MEDIA_TYPE: ".jpg", PNG_MEDIA_TYPE: ".png"}
CACHE_SUFFIXES = tuple(sorted(set(_EXTENSIONS.values())))
"""What a staged picture can be named, and therefore all that may be discarded.

Derived from the same table that names them rather than written out again:
a media type this project learns to write would otherwise become a file the
cache ceiling cannot see and can never take back.
"""

DEFAULT_BACKUP_LIMIT_BYTES = 500 * 1024 * 1024
"""How much the replaced-image backups may hold.

Deliberately twice the cache ceiling and never given a `Clear` button, because
what is discarded here is not disk that can be earned back — it is the ability
to revert an old run. A replaced image is moved here rather than deleted, and
the ceiling is far above what ordinary use accumulates. It exists so a folder
that could grow without limit has a limit, not because it is expected to be
reached.

The oldest go first, which is also the least valuable order here: a run is
reverted soon after it was applied, not thousands of albums later.
"""

DEFAULT_CACHE_LIMIT_BYTES = 250 * 1024 * 1024
"""How much disk the staged pictures and drawn sleeves may hold together.

Without a ceiling both folders grow for as long as the application is used. A
picture discarded here is fetched or extracted again; a cover already written
into an album is a file in that album and is not part of this at all.
"""
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8"
# Markers 0xC0 to 0xCF are start-of-frame markers, which carry the dimensions,
# except these three: a Huffman table, a reserved marker, and an arithmetic
# coding table. They are skipped like any other segment.
_JPEG_SKIPPED = frozenset({0xC4, 0xC8, 0xCC})
# Restart markers and the start and end of the image carry no length field.
_JPEG_STANDALONE = frozenset(range(0xD0, 0xDA))


class ImageKind(StrEnum):
    """The cover images this project fetches and writes."""

    FRONT = "front"
    BACK = "back"


class ArtworkError(RuntimeError):
    """Purpose: report that an image could not be read, written, or embedded.

    Responsibilities: give the caller one failure type regardless of container
    and image format. Boundaries: it repairs nothing and removes nothing.
    Dependencies: built-in exception behavior. Collaborators:
    ``FilesystemArtworkStore`` and the executor. Constraints: the message names
    the file, never the image bytes.
    """


@dataclass(frozen=True, slots=True)
class ImageFacts:
    """Purpose: describe one image well enough to compare it with another.

    Responsibilities: carry the media type, the pixel dimensions, and the digest
    of the exact bytes. Boundaries: it holds no image data and no path.
    Dependencies: none. Collaborators: the change planner, which compares an
    existing image with a fetched one, and the executor, which will not remove a
    file whose digest no longer matches. Constraints: dimensions come from the
    image's own header, so a picture that lies about its size in a tag is
    measured by what it actually is.
    """

    media_type: str
    width: int
    height: int
    digest: str

    @property
    def pixels(self) -> int:
        """Return the pixel count, which is what decides a replacement."""
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class StagedImage:
    """Purpose: hold one downloaded image that is waiting to be written.

    Responsibilities: pair the staging file with what kind of cover it is and
    what it contains. Boundaries: it is not yet anywhere near the user's library.
    Dependencies: ``ImageFacts``. Collaborators: the artwork service that stages
    it, the planner that decides whether it is worth writing, and the executor
    that puts it in place. Constraints: the image lives on disk rather than in
    memory, so a change operation can carry a path and a digest instead of
    bytes.
    """

    kind: ImageKind
    path: Path
    facts: ImageFacts

    @property
    def extension(self) -> str:
        """Return the file extension this image should carry."""
        return extension_for(self.facts.media_type)


@dataclass(frozen=True, slots=True)
class Artwork:
    """Purpose: carry everything fetched for one release, already staged.

    Responsibilities: hold the images destined for the folder and the one
    destined for the tracks. Boundaries: it writes nothing and knows nothing
    about the album's folders. Dependencies: ``StagedImage``. Collaborators: the
    artwork service and the change planner. Constraints: the embedded image is
    the front cover at a smaller size, which is a separate download from the
    file-sized front.
    """

    files: tuple[StagedImage, ...] = ()
    embedded: StagedImage | None = None

    @property
    def is_empty(self) -> bool:
        """Return whether nothing was fetched, which is a normal outcome."""
        return not self.files and self.embedded is None


class ArtworkStore(Protocol):
    """Read, place, embed, and undo cover images, whatever the container."""

    def describe(self, path: Path) -> ImageFacts | None:
        """Return the facts of an image file, or ``None`` when there is none to read."""

    def describe_embedded(self, path: Path) -> ImageFacts | None:
        """Return the facts of the picture embedded in an audio file, if any."""

    def misdeclares_embedded(self, path: Path) -> bool:
        """Report whether a picture block disagrees with the image it carries."""

    def copy(self, source: Path, destination: Path) -> tuple[Path, ...]:
        """Copy an image into place, returning the directories that had to be created."""

    def move(self, source: Path, destination: Path) -> None:
        """Move an image file, used to send a replaced image to the backup."""

    def remove(self, path: Path, digest: str) -> None:
        """Remove an image only while it is still byte-for-byte what was written."""

    def embed(self, audio_path: Path, image_path: Path) -> None:
        """Replace the front-cover picture of one audio file."""

    def extract(self, audio_path: Path, destination: Path) -> ImageFacts | None:
        """Write the embedded picture out to a file, returning its facts, if there is one."""

    def clear(self, audio_path: Path) -> None:
        """Remove every embedded picture from one audio file."""

    def shows_its_cover_in_the_finder(self, audio_path: Path) -> bool:
        """Report whether the file already carries the small icon the Finder draws."""

    def can_show_its_cover_in_the_finder(self, audio_path: Path) -> bool:
        """Report whether this file's own picture could be drawn as its icon."""

    def image_can_be_drawn(self, image_path: Path) -> bool:
        """Report whether an image file could be drawn as a file's icon."""

    def draw_in_the_finder(self, audio_path: Path) -> bool:
        """Give the file a custom icon made from the picture it already carries."""

    def stop_drawing_in_the_finder(self, audio_path: Path) -> None:
        """Take away a custom icon this application gave the file."""


def describe_bytes(data: bytes) -> ImageFacts | None:
    """Return what an image is, read from its own header.

    Only JPEG and PNG are recognized, which is what the Cover Art Archive
    serves. Anything else is reported as unknown rather than guessed at, so an
    unreadable image never wins a comparison against a real one.
    """
    dimensions = _png_dimensions(data) or _jpeg_dimensions(data)
    if dimensions is None:
        return None
    width, height = dimensions
    media_type = PNG_MEDIA_TYPE if data.startswith(_PNG_SIGNATURE) else JPEG_MEDIA_TYPE
    return ImageFacts(
        media_type=media_type,
        width=width,
        height=height,
        digest=hashlib.sha256(data).hexdigest(),
    )


def extension_for(media_type: str) -> str:
    """Return the file extension for a media type the archive serves."""
    return _EXTENSIONS.get(media_type, ".jpg")


def is_worth_writing(existing: ImageFacts | None, candidate: ImageFacts) -> bool:
    """Report whether a fetched image should replace what is already there.

    An existing image is replaced only by one with more pixels. An image
    identical to the one already in place is never rewritten, which keeps a
    second run of the same album silent.
    """
    if existing is None:
        return True
    if existing.digest == candidate.digest:
        return False
    return candidate.pixels > existing.pixels


SATISFIED_EMBED_PIXELS = 640 * 640
"""A picture already this big is the one the album keeps.

Most files arrive carrying a front cover of at least this size, so the floor
avoids replacing the greater part of the pictures that come with the music. A
higher floor replaces far more of them, and a replacement is never only a
replacement — it also leaves the small cover the Finder draws pointing at the
picture that *was* there (`library.xattrs`).

Below the floor the general rule decides: more pixels, never fewer.
"""


def is_worth_embedding(existing: ImageFacts | None, candidate: ImageFacts) -> bool:
    """Report whether a fetched image should replace the picture inside a track.

    The folder's cover and the track's picture are two different questions, and
    only this one has a size that is *enough*: what goes into every track is
    deliberately small, so that a twenty-track album stays light. Above the
    floor the picture already in the file wins, whatever the candidate is;
    below it, more pixels win.
    """
    if existing is not None and existing.pixels >= SATISFIED_EMBED_PIXELS:
        return False
    return is_worth_writing(existing, candidate)


class FilesystemArtworkStore:
    """Purpose: perform every image read and write, across formats and containers.

    Responsibilities: measure images, copy and move image files, and embed,
    extract, or clear the front-cover picture of an audio file. Boundaries: it
    decides nothing — whether an image is worth writing is the planner's
    answer — and it removes a file only when handed the digest that proves the
    file is still the one this project wrote. Dependencies: mutagen.
    Collaborators: the change planner and the executor. Constraints: FLAC, MP4,
    and the ID3 containers each store a picture differently, so the three paths
    exist for the same reason the tag store's two do.
    """

    def describe(self, path: Path) -> ImageFacts | None:
        """Return the facts of an image file, or ``None`` when it is absent or unreadable."""
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return describe_bytes(data)

    def describe_embedded(self, path: Path) -> ImageFacts | None:
        """Return the facts of an audio file's front-cover picture, if it has one."""
        data = self._embedded_bytes(path)
        return describe_bytes(data) if data is not None else None

    def misdeclares_embedded(self, path: Path) -> bool:
        """Report whether a picture block disagrees with the image it carries.

        A FLAC picture block states the width, height and depth of what it
        holds, and a player is entitled to believe it. Several writers leave
        those fields at zero over a perfectly good image, and a player that
        believes the block then draws nothing.

        Invisible to everything else here, because `describe_embedded` reads the
        *bytes* — so the planner compares two correct images and rightly decides
        there is nothing to write, while the block stays wrong. This is the one
        question that has to be asked of the block itself.

        Only FLAC and Ogg declare it. ID3 and MP4 store no dimensions, so there
        is nothing there to disagree with.
        """
        try:
            audio = self._open(path)
        except ArtworkError:
            return False
        pictures = getattr(audio, "pictures", None)
        if not pictures:
            return False
        front = next(
            (picture for picture in pictures if picture.type == PictureType.COVER_FRONT),
            pictures[0],
        )
        facts = describe_bytes(bytes(front.data))
        if facts is None:
            return False
        return (front.width, front.height) != (facts.width, facts.height)

    def copy(self, source: Path, destination: Path) -> tuple[Path, ...]:
        """Copy an image into place, creating and reporting any missing directories.

        A destination that is a symlink is refused rather than followed. This
        is the one write in the pipeline that opens its destination — every
        other one renames, which acts on the link itself — and a link is how a
        write aimed at an album folder lands somewhere else entirely. A folder
        extracted from an archive a stranger built can carry `cover.jpg`
        pointing at anything, and a *dangling* one answers `False` to both
        `describe` and `exists`, so the checks upstream let it through.
        """
        if destination.is_symlink():
            raise ArtworkError(f"Refusing to write through a symbolic link: {destination}.")
        missing: list[Path] = []
        candidate = destination.parent
        while not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, destination)
        except OSError as error:
            raise ArtworkError(f"An image could not be written to {destination}.") from error
        return tuple(missing)

    def move(self, source: Path, destination: Path) -> None:
        """Move an image file, creating the destination directory when needed."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.replace(destination)
        except OSError as error:
            raise ArtworkError(f"An image could not be moved to {destination}.") from error

    def remove(self, path: Path, digest: str) -> None:
        """Remove an image, but only while its content is still what was written.

        This is the second and last deletion in the write pipeline. A file the
        user has replaced since it was written no longer matches the digest,
        and is left where it is.
        """
        facts = self.describe(path)
        if facts is None or facts.digest != digest:
            return
        try:
            path.unlink()
        except OSError:
            # A file that cannot be removed is left alone: an incomplete
            # reversal is reported by its trail, never by damage on disk.
            return

    def embed(self, audio_path: Path, image_path: Path) -> None:
        """Replace the front-cover picture of one audio file."""
        data = image_path.read_bytes()
        facts = describe_bytes(data)
        if facts is None:
            raise ArtworkError(f"Not a usable cover image: {image_path.name}.")
        audio = self._open(audio_path)
        self._clear(audio)
        if hasattr(audio, "add_picture"):
            audio.add_picture(_flac_picture(data, facts))
        elif isinstance(audio.tags, ID3):
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime=facts.media_type,
                    type=PictureType.COVER_FRONT,
                    desc="Cover",
                    data=data,
                )
            )
        elif audio.tags is not None and _is_mp4(audio):
            audio.tags["covr"] = [_mp4_cover(data, facts)]
        else:
            raise ArtworkError(f"This container cannot hold a picture: {audio_path.name}.")
        self._save(audio, audio_path)

    def extract(self, audio_path: Path, destination: Path) -> ImageFacts | None:
        """Write an audio file's embedded picture out to a file of its own."""
        data = self._embedded_bytes(audio_path)
        if data is None:
            return None
        facts = describe_bytes(data)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_bytes(data)
        except OSError as error:
            raise ArtworkError(f"An image could not be written to {destination}.") from error
        return facts

    def shows_its_cover_in_the_finder(self, audio_path: Path) -> bool:
        """Report whether the file already carries the small icon the Finder draws.

        A third place a cover can live, beside the folder's `cover.jpg` and the
        picture inside the file — and the only one the Finder shows for a FLAC
        (see `library.xattrs`). An icon already there belongs to the user: this
        application never replaces one, it only ever gives one to a file that
        has none.
        """
        return draws_its_own_icon(audio_path)

    def can_show_its_cover_in_the_finder(self, audio_path: Path) -> bool:
        """Report whether this file's own picture could be drawn as its icon.

        Asked before offering the icon, because an operation that cannot succeed
        must not be offered: without this, a file whose picture the system
        refuses to render would be planned an icon, fail to get one, and be
        planned the same icon again on every run after — a plan that never goes
        quiet, about a file nothing can be done for.
        """
        data = self._embedded_bytes(audio_path)
        return data is not None and _renders(data)

    def image_can_be_drawn(self, image_path: Path) -> bool:
        """Report whether an image file could be drawn as a file's icon.

        Asked about a picture that is about to be put *into* a track, so that a
        file arriving with no picture at all is given its icon in the same run
        that gives it its cover — rather than in whichever run came next.
        """
        try:
            return _renders(image_path.read_bytes())
        except OSError:
            return False

    def draw_in_the_finder(self, audio_path: Path) -> bool:
        """Give the file a custom icon made from the picture it already carries.

        Nothing new enters the file and nothing is fetched: the icon is drawn
        from the very bytes the file holds, so the icon is the picture the
        file already carries. Written through the system's own `NSWorkspace`,
        so the result is the same pair of attributes any other tool's icon
        consists of.

        Answers `False` rather than raising when there is no picture to draw or
        the platform has no such idea — an icon is an adornment, and adornments
        never stop an album from being organized.
        """
        data = self._embedded_bytes(audio_path)
        if data is None:
            return False
        # A file that *claims* an icon and holds none is a state the system will
        # not write over: `NSWorkspace` answers `False` for as long as the flag
        # sits over an empty resource fork, and `True` once the empty claim is
        # taken away.
        #
        # Only the empty claim is cleared, never a real icon: this application
        # only ever gives one to a file that has none.
        if claims_an_icon(audio_path) and not draws_its_own_icon(audio_path):
            forget_own_icon(audio_path)
        return _set_finder_icon(audio_path, data)

    def stop_drawing_in_the_finder(self, audio_path: Path) -> None:
        """Take away a custom icon this application gave the file."""
        forget_own_icon(audio_path)

    def clear(self, audio_path: Path) -> None:
        """Remove every embedded picture from one audio file."""
        audio = self._open(audio_path)
        self._clear(audio)
        self._save(audio, audio_path)

    def _embedded_bytes(self, path: Path) -> bytes | None:
        audio = self._open(path)
        pictures = getattr(audio, "pictures", None)
        if pictures:
            front = next(
                (picture for picture in pictures if picture.type == PictureType.COVER_FRONT),
                pictures[0],
            )
            return bytes(front.data)
        tags = audio.tags
        if isinstance(tags, ID3):
            frames = tags.getall("APIC")
            if not frames:
                return None
            front = next(
                (frame for frame in frames if frame.type == PictureType.COVER_FRONT), frames[0]
            )
            return bytes(front.data)
        if tags is not None and _is_mp4(audio):
            covers = tags.get("covr") or []
            return bytes(covers[0]) if covers else None
        return None

    @staticmethod
    def _clear(audio: mutagen.FileType) -> None:
        if hasattr(audio, "clear_pictures"):
            audio.clear_pictures()
            return
        if isinstance(audio.tags, ID3):
            audio.tags.delall("APIC")
            return
        if audio.tags is not None and _is_mp4(audio) and "covr" in audio.tags:
            del audio.tags["covr"]

    @staticmethod
    def _open(path: Path) -> mutagen.FileType:
        try:
            audio = mutagen.File(path)
        except Exception as error:
            raise ArtworkError(f"Cover art could not be read from {path.name}.") from error
        if audio is None:
            raise ArtworkError(f"Unsupported audio container: {path.name}.")
        if audio.tags is None and not hasattr(audio, "add_picture"):
            try:
                audio.add_tags()
            except Exception as error:
                raise ArtworkError(f"Tags could not be created on {path.name}.") from error
        return audio

    @staticmethod
    def _save(audio: mutagen.FileType, path: Path) -> None:
        """Write the picture, on a copy, and put the copy in the file's place.

        An embedded cover *always* moves the audio stream — it is megabytes
        going in ahead of it — so this write above all must not be done in
        place. See ``library.atomicwrite``.
        """
        try:
            replacing(path, audio.save)
        except Exception as error:
            raise ArtworkError(f"Cover art could not be written to {path.name}.") from error


def _rendered(image: bytes) -> object | None:
    """Return the system's own reading of these bytes, or ``None``.

    Imported here rather than at the top so that a machine without AppKit still
    reads and writes covers normally; it simply never draws an icon. A picture
    that decodes to nothing — zero by zero — counts as unreadable, because the
    icon made from it would be blank.
    """
    try:
        import AppKit
    except ImportError:
        return None
    picture = AppKit.NSImage.alloc().initWithData_(
        AppKit.NSData.dataWithBytes_length_(image, len(image))
    )
    if picture is None or picture.size().width <= 0 or picture.size().height <= 0:
        return None
    return picture


def _renders(image: bytes) -> bool:
    """Report whether the system can make a picture out of these bytes."""
    return _rendered(image) is not None


def _set_finder_icon(path: Path, image: bytes) -> bool:
    """Ask the system to draw *image* as this file's icon, and say whether it did.

    Through `NSWorkspace`, which is what writes the icon resource and sets the
    flag — the same pair of attributes an icon given by any other tool consists
    of.
    """
    picture = _rendered(image)
    if picture is None:
        return False
    import AppKit

    return bool(
        AppKit.NSWorkspace.sharedWorkspace().setIcon_forFile_options_(picture, str(path), 0)
    )


def _flac_picture(data: bytes, facts: ImageFacts) -> Picture:
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = facts.media_type
    picture.desc = "Cover"
    picture.width = facts.width
    picture.height = facts.height
    picture.depth = 24
    picture.data = data
    return picture


def _mp4_cover(data: bytes, facts: ImageFacts) -> MP4Cover:
    image_format = (
        MP4Cover.FORMAT_PNG if facts.media_type == PNG_MEDIA_TYPE else MP4Cover.FORMAT_JPEG
    )
    return MP4Cover(data, imageformat=image_format)


def _is_mp4(audio: mutagen.FileType) -> bool:
    return type(audio.tags).__name__ == "MP4Tags"


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(_PNG_SIGNATURE) or len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG segment chain to the frame header that carries the size."""
    if not data.startswith(_JPEG_SIGNATURE):
        return None
    index = 2
    length = len(data)
    while index + 3 < length:
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:
            # Segments may be padded with fill bytes before the marker itself.
            index += 1
            continue
        if marker in _JPEG_STANDALONE:
            index += 2
            continue
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        is_frame_header = 0xC0 <= marker <= 0xCF and marker not in _JPEG_SKIPPED
        if is_frame_header:
            if index + 9 > length:
                return None
            return (
                int.from_bytes(data[index + 7 : index + 9], "big"),
                int.from_bytes(data[index + 5 : index + 7], "big"),
            )
        if segment_length < 2:
            return None
        index += 2 + segment_length
    return None
