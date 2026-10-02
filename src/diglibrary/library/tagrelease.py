"""Reading what an album's own tags say, so it can be arranged — not identified.

Arranging and identifying are different verbs, and this module serves the
first. A scan asks the world *which record this is* and answers with proof;
this reads what the files already say and arranges it into the naming
pattern — ``Artist - Album (Year) [FORMAT]``. It
claims nothing about the world, so it owes no proof. What it owes instead is
**provenance**: which values were read, how many files agree on them, and which
files dissent.

An album that carries its artist, its title, its year and a title per track
already holds everything a name needs. Measured on a real library of folders
already named by that pattern, most albums arrive at their final name this way.

What comes out of here is a ``ReleaseMetadata`` like any other, so it goes
through the same aligner and the same ``NamingPolicy``. No naming code exists
for this path and none should: a second renderer is a second answer, and the two
would drift.

**It invents no designator.** ``(Ed. 2012)``, ``(Remasterizado - 1977)`` are what
a catalogue is for, and a name colliding with another album's is the honest
signal that this album wants a scan.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.hints import tag_titles_distinguish
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.tags import TagStore
from diglibrary.metadata.normalization import normalize_text

PLAUSIBLE_YEARS = range(1900, 2100)
"""The years a record could have been released in.

A tag can hold `0000`: a year nobody could have released in is no year at
all, which is the refusal ``hints`` already makes.
"""

_YEAR_FIELDS = ("date", "originaldate", "year")
"""Where the year is read from, in the order that measured best.

Counter-intuitive, and measured on a real library: reading ``date`` first
agrees with more folder names than reading ``originaldate`` first. ``date`` is
the pressing in hand and ``originaldate`` is the first release, and what a tag
describes is usually the record in hand.
"""


@dataclass(frozen=True, slots=True)
class TagReading:
    """Purpose: report what one album's tags say, and how unanimously they say it.

    Responsibilities: carry the release the tags describe — or ``None`` with the
    reason they cannot — beside the provenance a person needs to judge an
    arrangement: the values chosen, how many files agree on each, and the
    dissenting files by name. Boundaries: it reads nothing itself, decides
    nothing, and holds no proof — provenance is a statement about *the files*,
    never about the world. Dependencies: ``ReleaseMetadata``.
    Collaborators: ``read_tags`` builds it; the identification workflow and the
    window read it. Constraints: ``missing`` is written for the screen, so it
    names what is absent rather than what failed.
    """

    release: ReleaseMetadata | None
    files: int = 0
    artist: str = ""
    artist_agree: int = 0
    album: str = ""
    album_agree: int = 0
    year: int | None = None
    titled: int = 0
    numbered: bool = False
    # One sentence per dissenting file: which file, which field, what it says
    # instead. Bounded by the caller's screen, not here — an album is at most a
    # few dozen files, and every dissent is worth a line.
    dissent: tuple[str, ...] = ()
    # What stops these tags from naming the album, empty when nothing does.
    missing: tuple[str, ...] = ()


def read_tags(unit: AlbumUnit, tag_store: TagStore) -> TagReading:
    """Read what this album's tags say, with the provenance to judge it by.

    A ``release`` of ``None`` is an answer, not a failure: it means these files
    do not say enough to name themselves, and ``missing`` says what is absent.
    Everything is required — an album artist, a title, a plausible year, and a
    title on every single track — because a name assembled from a hole is the
    one mistake this path could make that reaches the disk.

    The album's fields are what most of its files agree on, alphabetically first
    where they tie, so the answer is the same every time it is asked — and the
    files that said something else are named, because a majority is a choice and
    an arrangement to be approved should show what it chose against.
    """
    read = _read(unit.audio_files, tag_store)
    if not read:
        return TagReading(release=None, missing=("no audio files",))
    credits = [(file, _credit(values)) for file, values in read]
    albums = [(file, _first(values, "album")) for file, values in read]
    years = [(file, _year_of(_first(values, *_YEAR_FIELDS))) for file, values in read]
    # Every value of the field, not the first — an `albumartist` holds one value
    # per artist, which is how this project writes a collaboration. Reading
    # only the first would name a folder after the first artist and take the
    # co-artist off it, and an album this application tagged would read back
    # differently from the release that tagged it.
    written = _agreed_credit(value for _, value in credits)
    artist = " & ".join(written)
    title = _agreed(value for _, value in albums)
    year = min({value for _, value in years if value is not None}, default=None)
    titles = [_first(values, "title") for _, values in read]
    # A title every file repeats is not a title per track: it is one value
    # standing where the names should be, and this path renders a file name out
    # of it. Counted as untitled, which is what stops the arrangement: the
    # rule is a title on every single track, and a value that names nothing
    # does not satisfy it.
    distinguishing = tag_titles_distinguish(titles)
    titled = sum(1 for value in titles if value) if distinguishing else 0

    dissent: list[str] = []
    for file, value in credits:
        if value and value != written:
            dissent.append(f"{file.path.name}: artist “{' & '.join(value)}”")
    for file, value in albums:
        if value and value != title:
            dissent.append(f"{file.path.name}: album “{value}”")
    for file, value in years:
        if value is not None and year is not None and value != year:
            dissent.append(f"{file.path.name}: year {value}")

    missing: list[str] = []
    if not artist:
        missing.append("no artist in any file")
    if not title:
        missing.append("no album title in any file")
    if year is None:
        missing.append("no plausible year in any file")
    if not distinguishing:
        missing.append(f"all {len(read)} files carry one same title, which names none of them")
    elif titled < len(read):
        missing.append(f"{len(read) - titled} of {len(read)} files have no title")

    numbers = _numbers(read)
    provenance = {
        "files": len(read),
        "artist": artist,
        "artist_agree": sum(1 for _, value in credits if value == written) if written else 0,
        "album": title,
        "album_agree": sum(1 for _, value in albums if value == title) if title else 0,
        "year": year,
        "titled": titled,
        "numbered": numbers is not None,
        "dissent": tuple(dissent),
    }
    if missing:
        return TagReading(release=None, missing=tuple(missing), **provenance)
    return TagReading(
        release=ReleaseMetadata(
            source=MetadataSources.TAGS,
            # Keyed by the audio, never by the path: organizing renames the whole
            # folder, and a release cached under the old name would be a second
            # row for the same album the next time it is read.
            source_release_id=unit.unit_signature,
            title=title,
            artists=tuple(ArtistMetadata(name=name) for name in written),
            # A year is a year: the day is not something a tag claims to know.
            released_on=date(year, 1, 1),
            tracks=_tracks(read),
        ),
        **provenance,
    )


def release_from_tags(unit: AlbumUnit, tag_store: TagStore) -> ReleaseMetadata | None:
    """Return the release this album's own tags describe, or ``None``."""
    return read_tags(unit, tag_store).release


def _read(
    files: Iterable[AudioFileFacts], tag_store: TagStore
) -> list[tuple[AudioFileFacts, dict[str, tuple[str, ...]]]]:
    """Read every file's tags, treating an unreadable one as an untagged one.

    An exception here must not cost the album: a folder with one damaged file
    among twelve is answered by the twelve, and the missing title is what stops
    the proposal — which is the check above, stated once.
    """
    read = []
    for file in files:
        try:
            read.append((file, tag_store.read(file.path)))
        except Exception:
            read.append((file, {}))
    return read


def _tracks(
    read: list[tuple[AudioFileFacts, dict[str, tuple[str, ...]]]],
) -> tuple[TrackMetadata, ...]:
    """Turn each file into the track it says it is, in the order the tags give.

    Every track carries the length of the file it came from, so the aligner
    pairs them at zero milliseconds apart and the album is read by the same
    positional rule as any other. That is deliberate: pinning the pairing here
    would be this module deciding what the aligner exists to decide, and the one
    place the durations cannot settle — two files of one length carrying one
    title — is precisely the folder whose tags are wrong.

    The numbers come from the tags when they are a complete index and from the
    order on disk when they are not, decided for the album at once, because that
    is the only scale at which the question can be answered.
    """
    numbered = _numbers(read)
    ordered = sorted(
        range(len(read)),
        key=lambda index: (numbered[index] if numbered else (1, index + 1), index),
    )
    tracks = []
    for position, index in enumerate(ordered, start=1):
        file, values = read[index]
        disc, number = numbered[index] if numbered else (1, position)
        # Every value again, for the same reason the album's credit takes them
        # all: a compilation track credited to two people writes two values, and
        # its file name is built from what is read here.
        credit = _all(values, "artist")
        tracks.append(
            TrackMetadata(
                title=_first(values, "title"),
                position=position,
                medium_number=disc,
                position_on_medium=number,
                artists=tuple(ArtistMetadata(name=name) for name in credit),
                duration_ms=file.properties.duration_ms if file.properties else None,
            )
        )
    return tuple(tracks)


def _numbers(
    read: list[tuple[AudioFileFacts, dict[str, tuple[str, ...]]]],
) -> list[tuple[int, int]] | None:
    """Return each file's disc and track number, or ``None`` if they are not an index.

    All or nothing, for the album: a folder where two files claim track 3, or
    where one file claims nothing, is not numbered — and taking the numbers of
    the files that do have them would leave the rest to be invented.
    """
    numbers = []
    for _, values in read:
        number = _number(_first(values, "tracknumber"))
        if number is None:
            return None
        numbers.append((_number(_first(values, "discnumber")) or 1, number))
    return numbers if len(set(numbers)) == len(numbers) else None


def _number(text: str) -> int | None:
    """Read `7`, `07` or `7/12` as seven, and anything else as no number at all."""
    digits = text.split("/")[0].strip()
    return int(digits) if digits.isdigit() and int(digits) > 0 else None


def _credit(values: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Every name the album credits, in the order the file writes them.

    ``albumartist`` is what a compilation fills in with `Various Artists`, and
    reading `artist` first would call such an album by whoever happens to sing
    its first track.

    **All the values, never the first.** A tag field holds one value per artist
    — that is how this project writes a collaboration, so that a player can
    group by either name — and a folder named from the first alone loses
    everyone after it.
    """
    return _all(values, "albumartist") or _all(values, "artist")


def _agreed_credit(credits: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """What most of the files credit, alphabetically first where they tie.

    The same rule as ``_agreed`` over a whole credit rather than one string,
    because a credit is several values and comparing them one at a time would
    let two files agree on the first artist and disagree on the second without
    anything noticing.
    """
    counted = Counter(credit for credit in credits if credit)
    if not counted:
        return ()
    return min(counted, key=lambda credit: (-counted[credit], credit))


def _agreed(values: Iterable[str]) -> str:
    """What most of the files say, alphabetically first where they tie.

    A retag that reached eleven of twelve files leaves one dissenting value, and
    the album is the eleven. Ties are broken by the text itself rather than by
    the order the folder happened to be read in, so the same folder answers the
    same way twice.
    """
    counted = Counter(value for value in values if value)
    if not counted:
        return ""
    return min(counted, key=lambda value: (-counted[value], value))


def _year_of(text: str) -> int | None:
    """Read the year out of `1974`, `1974-05-01` or `1974/05`, refusing the rest."""
    digits = text[:4]
    if not digits.isdigit():
        return None
    year = int(digits)
    return year if year in PLAUSIBLE_YEARS else None


def _first(values: dict[str, tuple[str, ...]], *names: str) -> str:
    """The first non-empty value among these tag names, tidied of stray space."""
    for name in names:
        for value in values.get(name, ()):
            tidied = normalize_text(value)
            if tidied:
                return tidied
    return ""


def _all(values: dict[str, tuple[str, ...]], name: str) -> tuple[str, ...]:
    """Every non-empty value this field holds, tidied and in the file's own order."""
    return tuple(tidied for value in values.get(name, ()) if (tidied := normalize_text(value)))
