"""Reading and writing audio tags, and deciding what an identified release implies."""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.id3 import ID3, TXXX, Frames

from diglibrary.application.contracts import MetadataSources, ReleaseMetadata
from diglibrary.library.atomicwrite import replacing
from diglibrary.library.genres import spell_genres
from diglibrary.library.matching import TrackAssignment
from diglibrary.library.naming import is_various

TagSet = Mapping[str, tuple[str, ...]]
"""Canonical, multi-valued tag names, independent of any container format."""

CORE_FIELDS = (
    "title",
    "artist",
    "albumartist",
    "album",
    "date",
    "tracknumber",
    "totaltracks",
    "discnumber",
    "totaldiscs",
    "originaldate",
    "releasedate",
)
CATALOGUE_FIELDS = (
    "genre",
    "organization",
    "catalognumber",
    "releasecountry",
    "isrc",
    "compilation",
    "musicbrainz_albumid",
    "musicbrainz_trackid",
    "discogs_release_id",
)
WRITTEN_FIELDS = CORE_FIELDS + CATALOGUE_FIELDS

_VARIOUS_ARTISTS_TAG = "Various Artists"

# mutagen's easy interfaces cover most of what this project writes, but not all
# of it. These registrations are the documented way to extend them, and each one
# exists because a round-trip test against a real file showed the field missing.
EasyID3.RegisterTXXXKey("discogs_release_id", "DISCOGS_RELEASE_ID")
EasyID3.RegisterTXXXKey("totaltracks", "TOTALTRACKS")
EasyID3.RegisterTXXXKey("totaldiscs", "TOTALDISCS")
EasyID3.RegisterTXXXKey("releasedate", "RELEASEDATE")
for _key, _name in (
    ("catalognumber", "CATALOGNUMBER"),
    ("isrc", "ISRC"),
    ("organization", "LABEL"),
    ("compilation", "COMPILATION"),
    ("totaltracks", "TOTALTRACKS"),
    ("totaldiscs", "TOTALDISCS"),
    ("originaldate", "ORIGINALDATE"),
    ("releasedate", "RELEASEDATE"),
    ("discogs_release_id", "DISCOGS_RELEASE_ID"),
):
    EasyMP4Tags.RegisterFreeformKey(_key, _name)

# WAV and AIFF carry ID3 inside the container, so mutagen's easy interface does
# not reach them. Verified against real files: raw frames round-trip correctly.
_ID3_TEXT_FRAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "albumartist": "TPE2",
    "album": "TALB",
    "date": "TDRC",
    "tracknumber": "TRCK",
    "discnumber": "TPOS",
    "genre": "TCON",
    "organization": "TPUB",
    "isrc": "TSRC",
    "compilation": "TCMP",
}
_ID3_USER_FRAMES = (
    "originaldate",
    "releasedate",
    "catalognumber",
    "releasecountry",
    "totaltracks",
    "totaldiscs",
    "musicbrainz_albumid",
    "musicbrainz_trackid",
    "discogs_release_id",
)
_CONTAINER_ID3 = frozenset({".wav", ".aiff", ".aif"})
_SLASH_TOTALS = (("tracknumber", "totaltracks"), ("discnumber", "totaldiscs"))


class TagError(RuntimeError):
    """Purpose: report that a file's tags could not be read or written.

    Responsibilities: give the caller one failure type regardless of container.
    Boundaries: it never leaves a file partly written — the writer saves once,
    after every value is prepared. Dependencies: built-in exception behavior.
    Collaborators: ``MutagenTagStore`` and the executor. Constraints: the
    message names the file, never the tag values, which may be long.
    """


class TagStore(Protocol):
    """Read and write one file's tags in canonical, format-independent names."""

    def read(self, path: Path) -> dict[str, tuple[str, ...]]:
        """Return every tag currently on the file, in canonical names."""

    def write(self, path: Path, tags: TagSet) -> None:
        """Replace the file's writable tags with the supplied values."""


def desired_tags(
    release: ReleaseMetadata,
    assignment: TrackAssignment,
    total_tracks: int,
    total_discs: int,
    genre_spellings: Iterable[str] = (),
) -> dict[str, tuple[str, ...]]:
    """Return the tags an identified release implies for one of its tracks.

    Every value comes from the release, verbatim: metadata is never invented,
    so nothing here is inferred from the file's current name or tags.
    A field the source did not publish is simply absent, never filled with a
    placeholder.
    """
    track = assignment.track
    various = is_various(release)
    # Several values, not one string: a player groups by artist, so a credit
    # written as "First Artist, Second Artist" reaches the tag already split,
    # while the file name keeps the credit whole.
    track_artists = tuple(artist.name for artist in track.artists) or tuple(
        artist.name for artist in release.artists
    )
    tags: dict[str, tuple[str, ...]] = {
        "title": (track.title,),
        "artist": track_artists,
        "albumartist": (
            (_VARIOUS_ARTISTS_TAG,) if various else tuple(artist.name for artist in release.artists)
        ),
        "album": (release.title,),
        "tracknumber": (str(assignment.track_number),),
        "totaltracks": (str(total_tracks),),
    }
    # The album's own release date is what gets written, so a player shows 1974
    # for a 1974 album even when the matched pressing is a 2006 reissue.
    # The pressing's own date is kept alongside it.
    original = release.original_released_on or release.released_on
    if original is not None:
        tags["date"] = (original.isoformat(),)
        tags["originaldate"] = (original.isoformat(),)
    if release.released_on is not None and (original is None or release.released_on != original):
        tags["releasedate"] = (release.released_on.isoformat(),)
    if total_discs > 1:
        tags["discnumber"] = (str(assignment.disc_number),)
        tags["totaldiscs"] = (str(total_discs),)
    if various:
        tags["compilation"] = ("1",)
    # Styles first: Discogs' genres are a shelf as broad as `Rock`, and a
    # collection is filed by the narrower styles. The genres follow so nothing
    # the catalogue said is lost, and a release with no styles falls back to
    # its genres alone — each in one spelling whichever source said it.
    _add_if_present(
        tags, "genre", spell_genres(release, release.styles + release.genres, genre_spellings)
    )
    _add_if_present(tags, "organization", release.labels)
    _add_if_present(tags, "catalognumber", release.catalog_numbers)
    _add_if_present(tags, "releasecountry", (release.country,) if release.country else ())
    _add_if_present(tags, "isrc", track.isrcs)
    _add_source_ids(tags, release, track)
    return {name: values for name, values in tags.items() if values}


class MutagenTagStore:
    """Purpose: read and write tags across every format this project supports.

    Responsibilities: translate canonical names into each container's own
    vocabulary and back. Boundaries: it decides no value — what to write is
    ``desired_tags``' answer — and it never renames or moves a file.
    Dependencies: mutagen. Collaborators: the change planner and the executor.
    Constraints: two paths exist because mutagen's easy interface does not reach
    ID3 stored inside a WAV or AIFF container, which round-trip tests against
    real files confirmed. Both paths are exercised by those tests.
    """

    def read(self, path: Path) -> dict[str, tuple[str, ...]]:
        """Return the file's current tags, in canonical names."""
        if path.suffix.lower() in _CONTAINER_ID3:
            return self._read_container_id3(path)
        audio = self._open(path, easy=True)
        if audio.tags is None:
            return {}
        return {
            str(name): tuple(str(value) for value in values)
            for name, values in audio.tags.items()
            if values
        }

    def write(self, path: Path, tags: TagSet) -> None:
        """Replace the file's writable tags, saving once at the end."""
        if path.suffix.lower() in _CONTAINER_ID3:
            self._write_container_id3(path, tags)
            return
        audio = self._open(path, easy=True)
        if audio.tags is None:
            audio.add_tags()
        prepared = as_the_container_writes(path, tags)
        for name in WRITTEN_FIELDS:
            try:
                if name in prepared:
                    audio[name] = list(prepared[name])
                elif name in audio:
                    del audio[name]
            except (KeyError, ValueError):
                # A container that cannot express this field is a limitation of
                # the format, not a failure of the operation.
                continue
        self._save(audio, path)

    def _read_container_id3(self, path: Path) -> dict[str, tuple[str, ...]]:
        audio = self._open(path, easy=False)
        frames = audio.tags
        if frames is None:
            return {}
        inverted = {frame: name for name, frame in _ID3_TEXT_FRAMES.items()}
        found: dict[str, tuple[str, ...]] = {}
        for key, frame in frames.items():
            identifier = key.split(":")[0]
            if identifier == "TXXX" and getattr(frame, "desc", "").lower() in _ID3_USER_FRAMES:
                found[frame.desc.lower()] = tuple(str(value) for value in frame.text)
            elif identifier in inverted:
                found[inverted[identifier]] = tuple(str(value) for value in frame.text)
        return found

    def _write_container_id3(self, path: Path, tags: TagSet) -> None:
        audio = self._open(path, easy=False)
        if audio.tags is None:
            audio.add_tags()
        frames: ID3 = audio.tags
        prepared = as_the_container_writes(path, tags)
        for name, frame_id in _ID3_TEXT_FRAMES.items():
            frames.delall(frame_id)
            if name in prepared:
                frames.add(Frames[frame_id](encoding=3, text=list(prepared[name])))
        for name in _ID3_USER_FRAMES:
            frames.delall(f"TXXX:{name.upper()}")
            if name in prepared:
                frames.add(TXXX(encoding=3, desc=name.upper(), text=list(prepared[name])))
        self._save(audio, path)

    @staticmethod
    def _open(path: Path, easy: bool) -> mutagen.FileType:
        try:
            audio = mutagen.File(path, easy=easy)
        except Exception as error:
            raise TagError(f"Tags could not be read from {path.name}.") from error
        if audio is None:
            raise TagError(f"Unsupported audio container: {path.name}.")
        return audio

    @staticmethod
    def _save(audio: mutagen.FileType, path: Path) -> None:
        """Write the tags, on a copy, and put the copy in the file's place.

        ``audio.save()`` rewrites the container in place, so a tag that no longer
        fits the padding moves the audio stream and the file is briefly neither
        version. See ``library.atomicwrite`` for why that is not acceptable here.
        """
        try:
            replacing(path, audio.save)
        except Exception as error:
            raise TagError(f"Tags could not be written to {path.name}.") from error


def as_the_container_writes(path: Path, tags: TagSet) -> dict[str, tuple[str, ...]]:
    """Return these tags in the exact form writing them to this file produces.

    One rule, shared by the writer and by anyone comparing against a file the
    writer touched. A verifier that compares the plan's `tracknumber` (`"7"`)
    against what the file reads back (`"7/11"`, the fold ID3 and MP4 expect
    natively) reports a difference on every MP3 whose write was correct. Two
    copies of the folding rule are how the writer and the verifier drift apart.
    """
    if path.suffix.lower() == ".flac":
        return dict(tags)
    return _with_slash_totals(tags)


def _with_slash_totals(tags: TagSet) -> dict[str, tuple[str, ...]]:
    """Fold totals into the ``number/total`` form that ID3 and MP4 expect natively."""
    prepared = dict(tags)
    for number_field, total_field in _SLASH_TOTALS:
        number = prepared.get(number_field)
        total = prepared.get(total_field)
        if number and total:
            prepared[number_field] = (f"{number[0]}/{total[0]}",)
    return prepared


def _add_if_present(tags: dict[str, tuple[str, ...]], name: str, values: tuple[str, ...]) -> None:
    if values:
        tags[name] = values


def _add_source_ids(
    tags: dict[str, tuple[str, ...]], release: ReleaseMetadata, track: object
) -> None:
    """Record where each written value came from."""
    album_ids = dict(release.external_ids)
    if release.source is MetadataSources.MUSICBRAINZ or MetadataSources.MUSICBRAINZ in album_ids:
        identifier = album_ids.get(MetadataSources.MUSICBRAINZ, release.source_release_id)
        tags["musicbrainz_albumid"] = (identifier,)
    if release.source is MetadataSources.DISCOGS or MetadataSources.DISCOGS in album_ids:
        identifier = album_ids.get(MetadataSources.DISCOGS, release.source_release_id)
        tags["discogs_release_id"] = (identifier,)
    track_ids = dict(getattr(track, "external_ids", {}) or {})
    if MetadataSources.MUSICBRAINZ in track_ids:
        tags["musicbrainz_trackid"] = (track_ids[MetadataSources.MUSICBRAINZ],)
