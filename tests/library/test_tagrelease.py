"""Unit tests for the release an album's own tags describe.

These are about the reading rules only. Whether the reading reaches the screen
is tested through the API, because a rule that is never wired is a rule that
does not exist.
"""

from datetime import UTC, datetime
from pathlib import Path

from diglibrary.application.contracts import MetadataSources
from diglibrary.library.audio import AudioProperties
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.tagrelease import release_from_tags

FOLDER = Path("/music/an album")


class Tags:
    """A tag store answering from a map, and raising for a file it was told to."""

    def __init__(self, values: dict[str, dict[str, tuple[str, ...]]], broken: str = "") -> None:
        self._values = values
        self._broken = broken

    def read(self, path: Path) -> dict[str, tuple[str, ...]]:
        """Return this file's tags, or raise the way an unreadable file does."""
        if path.name == self._broken:
            raise OSError("this file cannot be read")
        return dict(self._values.get(path.name, {}))

    def write(self, path: Path, tags: object) -> None:
        """Never called: this module reads."""
        raise AssertionError("the tag path writes nothing")


def _unit(*names: str, durations: tuple[int, ...] = ()) -> AlbumUnit:
    lengths = durations or tuple(1_000 * (index + 1) for index in range(len(names)))
    return AlbumUnit(
        folder_path=FOLDER,
        unit_signature="unit",
        audio_files=tuple(
            AudioFileFacts(
                path=FOLDER / name,
                content_signature=f"signature-{name}",
                file_size_bytes=1_000,
                modified_at=datetime(2001, 2, 3, tzinfo=UTC),
                properties=AudioProperties(
                    codec="flac", duration_ms=length, sample_rate=44_100, channels=2
                ),
            )
            for name, length in zip(names, lengths, strict=True)
        ),
    )


def _tags(album: str = "Zorvembro", **extra: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    values = {
        "albumartist": ("Quarteto Zunca",),
        "album": (album,),
        "date": ("1980",),
    }
    values.update(extra)
    return values


def test_a_complete_album_names_itself_and_says_the_tags_named_it() -> None:
    """Everything a name needs, and the source is the one that is not a catalogue."""
    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": _tags(title=("Zapa Plimbador",), tracknumber=("1",)),
            "b.flac": _tags(title=("Vorca Carnival",), tracknumber=("2",)),
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None
    assert release.source == MetadataSources.TAGS
    assert release.title == "Zorvembro"
    assert [artist.name for artist in release.artists] == ["Quarteto Zunca"]
    assert release.released_on is not None and release.released_on.year == 1980
    assert [track.title for track in release.tracks] == ["Zapa Plimbador", "Vorca Carnival"]
    # Keyed by the audio, never by the path: organizing renames the whole folder.
    assert release.source_release_id == "unit"
    # Every track carries the length of the file it came from, which is what
    # lets the ordinary aligner pair them at nothing apart.
    assert [track.duration_ms for track in release.tracks] == [1_000, 2_000]


def test_a_year_no_record_could_have_been_released_in_is_no_year_at_all() -> None:
    """A `0000` date is a value files do carry, and a folder named for it would be wrong."""
    unit = _unit("a.flac")
    store = Tags({"a.flac": _tags(date=("0000",), title=("Whatever",), tracknumber=("1",))})

    assert release_from_tags(unit, store) is None


def test_the_year_comes_from_date_before_originaldate() -> None:
    """`date` is the pressing in hand and `originaldate` is the first release.

    What a file's tags describe is most often the record in hand, so `date` is
    read first.
    """
    unit = _unit("a.flac")
    store = Tags(
        {
            "a.flac": _tags(
                date=("1980-05-01",),
                originaldate=("1975",),
                title=("Vorca Carnival",),
                tracknumber=("1",),
            )
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None and release.released_on is not None
    assert release.released_on.year == 1980


def test_one_track_without_a_title_stops_the_whole_proposal() -> None:
    """A name assembled around a hole is the one mistake this path could ship."""
    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": _tags(title=("Zapa Plimbador",), tracknumber=("1",)),
            "b.flac": _tags(tracknumber=("2",)),
        }
    )

    assert release_from_tags(unit, store) is None


def test_an_unreadable_file_stops_it_rather_than_raising() -> None:
    """A damaged file among the good ones is an answer of `None`, not an exception.

    This runs while a folder is being read, and an exception here is the
    difference between a card on screen and a folder that vanishes silently.
    """
    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {"a.flac": _tags(title=("Zapa Plimbador",), tracknumber=("1",))},
        broken="b.flac",
    )

    assert release_from_tags(unit, store) is None


def test_the_tags_numbers_decide_the_order_when_the_disk_disagrees() -> None:
    """The file order on disk is alphabetical; the album order is the tag's."""
    unit = _unit("zzz.flac", "aaa.flac", durations=(1_000, 2_000))
    store = Tags(
        {
            "zzz.flac": _tags(title=("First",), tracknumber=("1",)),
            "aaa.flac": _tags(title=("Second",), tracknumber=("2",)),
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None
    assert [track.title for track in release.tracks] == ["First", "Second"]
    assert [track.position_on_medium for track in release.tracks] == [1, 2]
    # And each keeps the length of its own file, wherever the sorting put it.
    assert [track.duration_ms for track in release.tracks] == [1_000, 2_000]


def test_two_files_claiming_one_track_number_fall_back_to_the_order_on_disk() -> None:
    """All or nothing for the album: half an index would leave the rest invented."""
    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": _tags(title=("First",), tracknumber=("3",)),
            "b.flac": _tags(title=("Second",), tracknumber=("3",)),
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None
    assert [track.position_on_medium for track in release.tracks] == [1, 2]


def test_a_disc_number_reaches_the_track_that_carries_it() -> None:
    """`7/12` is seven, and the disc is what tells two track sevens apart."""
    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": _tags(title=("One",), tracknumber=("1/6",), discnumber=("2",)),
            "b.flac": _tags(title=("Two",), tracknumber=("1/6",), discnumber=("1",)),
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None
    assert [track.title for track in release.tracks] == ["Two", "One"]
    assert [track.medium_number for track in release.tracks] == [1, 2]


def test_the_album_is_what_most_of_its_files_agree_on() -> None:
    """A retag that reached eleven files of twelve leaves the album the eleven."""
    unit = _unit("a.flac", "b.flac", "c.flac")
    store = Tags(
        {
            "a.flac": _tags(title=("One",), tracknumber=("1",)),
            "b.flac": _tags(title=("Two",), tracknumber=("2",)),
            "c.flac": _tags(
                album="Zorvembro (Remasterizado)", title=("Three",), tracknumber=("3",)
            ),
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None and release.title == "Zorvembro"


def test_a_compilation_is_credited_the_way_its_album_artist_writes_it() -> None:
    """Reading `artist` first would name the record after whoever sings track one."""
    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": {
                "albumartist": ("Various Artists",),
                "artist": ("Tom Zaia",),
                "album": ("Zambass",),
                "date": ("1998",),
                "title": ("One",),
                "tracknumber": ("1",),
            },
            "b.flac": {
                "albumartist": ("Various Artists",),
                "artist": ("Plassiano",),
                "album": ("Zambass",),
                "date": ("1998",),
                "title": ("Two",),
                "tracknumber": ("2",),
            },
        }
    )

    release = release_from_tags(unit, store)

    assert release is not None
    assert [artist.name for artist in release.artists] == ["Various Artists"]
    # And each track keeps its own credit, which is what puts an artist into a
    # compilation's file names.
    assert [track.artists[0].name for track in release.tracks] == ["Tom Zaia", "Plassiano"]


def test_the_reading_names_the_files_that_dissent_from_the_majority() -> None:
    """An arrangement is a majority's answer, and the minority is named.

    A retag that reached eleven files of twelve leaves one dissenting value, and
    what the majority chose against is what should be read before a rename
    built on it is approved.
    """
    from diglibrary.library.tagrelease import read_tags

    unit = _unit("a.flac", "b.flac", "c.flac")
    store = Tags(
        {
            "a.flac": _tags(title=("One",), tracknumber=("1",)),
            "b.flac": _tags(title=("Two",), tracknumber=("2",)),
            "c.flac": _tags(album="Zorvembro (Remaster)", title=("Three",), tracknumber=("3",)),
        }
    )

    reading = read_tags(unit, store)

    assert reading.release is not None
    assert reading.files == 3
    assert reading.album == "Zorvembro" and reading.album_agree == 2
    assert reading.dissent == ("c.flac: album “Zorvembro (Remaster)”",)
    assert reading.numbered is True


def test_the_reading_says_what_is_missing_when_it_cannot_name() -> None:
    """`None` with reasons, because "cannot" without "why" is a dead end on screen."""
    from diglibrary.library.tagrelease import read_tags

    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": {
                "albumartist": ("Quarteto Zunca",),
                "album": ("Zorvembro",),
                "title": ("One",),
            },
            "b.flac": {"albumartist": ("Quarteto Zunca",), "album": ("Zorvembro",)},
        }
    )

    reading = read_tags(unit, store)

    assert reading.release is None
    assert "no plausible year in any file" in reading.missing
    assert "1 of 2 files have no title" in reading.missing


def test_a_collaboration_keeps_every_name_the_tag_credits() -> None:
    """A credit is several values, and reading the first alone loses the rest.

    This project writes a collaboration as one value per artist, so a player and
    Rekordbox can group by either name. Reading `values[0]` alone would read an
    album this application tagged itself back as its first artist only, and
    arranging that folder would take the co-artist off its name.
    """
    from diglibrary.library.tagrelease import read_tags

    unit = _unit("a.flac", "b.flac")
    both = ("Zamil Plem", "Vara De Zândido")
    store = Tags(
        {
            "a.flac": _tags(albumartist=both, title=("One",), tracknumber=("1",)),
            "b.flac": _tags(albumartist=both, title=("Two",), tracknumber=("2",)),
        }
    )

    reading = read_tags(unit, store)

    assert reading.release is not None
    assert [artist.name for artist in reading.release.artists] == list(both)
    # And the name the policy renders from it is the whole credit, which is the
    # only reason any of this matters.
    assert reading.artist == "Zamil Plem & Vara De Zândido"
    assert reading.artist_agree == 2 and reading.dissent == ()


def test_a_compilation_track_keeps_every_name_its_own_credit_holds() -> None:
    """The same field, one row down: a track credited to two writes two values."""
    from diglibrary.library.tagrelease import read_tags

    unit = _unit("a.flac", "b.flac")
    store = Tags(
        {
            "a.flac": {
                "albumartist": ("Various Artists",),
                "artist": ("Tom Zaia", "Plassiano"),
                "album": ("Zambass",),
                "date": ("1998",),
                "title": ("One",),
                "tracknumber": ("1",),
            },
            "b.flac": {
                "albumartist": ("Various Artists",),
                "artist": ("Zeraldo Plampos",),
                "album": ("Zambass",),
                "date": ("1998",),
                "title": ("Two",),
                "tracknumber": ("2",),
            },
        }
    )

    reading = read_tags(unit, store)

    assert reading.release is not None
    first, second = reading.release.tracks
    assert [artist.name for artist in first.artists] == ["Tom Zaia", "Plassiano"]
    assert [artist.name for artist in second.artists] == ["Zeraldo Plampos"]


def test_one_title_repeated_on_every_file_does_not_name_the_tracks() -> None:
    """The path that renders a file name out of a tag must not render this one.

    Files whose every `TIT2` reads one site address would otherwise give a
    complete release with nothing in `missing`, and the arrangement offered
    would write `01. <site address>.mp3` once per file. *A title on every
    single track* is this module's rule, and a value that names none of them
    does not satisfy it.
    """
    from diglibrary.library.tagrelease import read_tags

    unit = _unit("01- zunto livre.mp3", "02- plimba 64.mp3")
    said = {
        "albumartist": ("Artista",),
        "album": ("Compacto",),
        "date": ("1980",),
        "title": ("www.example-downloads.com",),
    }
    store = Tags({"01- zunto livre.mp3": said, "02- plimba 64.mp3": said})

    reading = read_tags(unit, store)

    assert reading.release is None
    assert reading.titled == 0
    assert "all 2 files carry one same title, which names none of them" in reading.missing


def test_titles_that_differ_still_name_the_tracks() -> None:
    """The refusal is about one value standing for every track, and nothing else."""
    from diglibrary.library.tagrelease import read_tags

    unit = _unit("a.flac", "b.flac")
    common = {"albumartist": ("Quarteto Zunca",), "album": ("Zorvembro",), "date": ("1974",)}
    store = Tags(
        {
            "a.flac": {**common, "title": ("Primeiro",)},
            "b.flac": {**common, "title": ("Segundo",)},
        }
    )

    reading = read_tags(unit, store)

    assert reading.release is not None
    assert [track.title for track in reading.release.tracks] == ["Primeiro", "Segundo"]
    assert reading.missing == ()
