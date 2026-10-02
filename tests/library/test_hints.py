"""Unit tests for deriving search hints from what is on disk."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from diglibrary.library.containers import numbers_deny_one_album
from diglibrary.library.hints import (
    derive_hints,
    files_in_track_order,
    folders_carry_an_index,
    hints_from_folder_name,
    local_track_titles,
    names_carry_an_index,
    numbered_tracks,
    query_ladder,
    tag_titles_distinguish,
    track_title_hint,
)
from diglibrary.library.models import AlbumUnit, AudioFileFacts


@pytest.mark.parametrize(
    ("folder", "artist", "album", "year"),
    [
        ("Lumãs - Janela Meridiano (1986) [FLAC]", "Lumãs", "Janela Meridiano", 1986),
        ("Mickey Marlow - Dusk (Remastered) (1987) [FLAC]", "Mickey Marlow", "Dusk", 1987),
        ("Tavola - Tavola (1974) (1974) [FLAC]", "Tavola", "Tavola", 1974),
        (
            "Banda Zinco Pardo - Lenha Zorca (Remasterizado) (1977) [FLAC]",
            "Banda Zinco Pardo",
            "Lenha Zorca",
            1977,
        ),
        (
            "Lia Monte - Lia A Todo Pano (Live) (1971) [FLAC]",
            "Lia Monte",
            "Lia A Todo Pano",
            1971,
        ),
        ("Some Album Without Anything", None, "Some Album Without Anything", None),
    ],
)
def test_edition_wording_and_repeated_years_are_stripped(
    folder: str, artist: str | None, album: str, year: int | None
) -> None:
    """A catalogue stores the release title, not the edition the folder describes."""
    hints = hints_from_folder_name(folder)

    assert hints.artist == artist
    assert hints.album == album
    assert hints.year == year


@pytest.mark.parametrize("label", ["VA", "V.A.", "Various", "various artists"])
def test_a_compilation_is_credited_the_way_a_catalogue_credits_it(label: str) -> None:
    """A folder says VA; Discogs and MusicBrainz both say Various."""
    assert hints_from_folder_name(f"{label} - Zunido Pardo (2015) [FLAC]").artist == "Various"


def test_a_year_that_is_part_of_the_title_is_not_mistaken_for_an_edition() -> None:
    """Stripping must not eat a title, only the decoration around it."""
    hints = hints_from_folder_name("VA - Zinco Pardo (Coastal Reed Power 1971-1980) (2002) [FLAC]")

    assert hints.year == 2002
    assert hints.album == "Zinco Pardo (Coastal Reed Power 1971-1980)"


@pytest.mark.parametrize(
    ("file_name", "title"),
    [
        ("01. Zúnida.flac", "Zúnida"),
        ("1-01. Zúnida.flac", "Zúnida"),
        ("07 - Zúnida.mp3", "Zúnida"),
        ("Zúnida.flac", "Zúnida"),
    ],
)
def test_a_track_title_is_read_without_its_numbering(file_name: str, title: str) -> None:
    """Numbering is the project's own convention, not part of the title."""
    assert track_title_hint(file_name) == title


def test_the_ladder_tries_the_conjunction_spelled_every_common_way() -> None:
    """A sleeve may say "Zumbra e Dance" where a catalogue says "Zumbra & Dance".

    Neither side is wrong, and a ladder that only asks with the sleeve's
    spelling returns nothing on every rung.
    """
    hints = hints_from_folder_name("Olívio Marfins - Zumbra e Dance (1999) [FLAC]")

    queries = query_ladder(hints)
    albums = {query.album for query in queries if query.album}

    assert "Zumbra & Dance" in albums
    assert "Zumbra and Dance" in albums


def test_the_ladder_tries_the_query_without_accents() -> None:
    """Catalogues index what was typed, and what was typed is often unaccented."""
    hints = hints_from_folder_name("Olívio Marfins - Zumbra e Dance (1999) [FLAC]")

    queries = query_ladder(hints)

    assert any(query.artist == "Olivio Marfins" for query in queries)


def test_the_ladder_ends_on_the_most_distinctive_track_title() -> None:
    """The last resort is how a person searches: the artist and a track they know."""
    hints = hints_from_folder_name("Olívio Marfins - Zumbra e Dance (1999) [FLAC]")
    titles = ("Alô Turma", "Vou Remar Pra Outra Margem", "Lá Vem Ela")

    queries = query_ladder(hints, titles)
    free_texts = [query.text for query in queries if query.text]

    assert any("Vou Remar Pra Outra Margem" in text for text in free_texts)


def test_the_ladder_still_costs_nothing_when_the_first_answer_exists() -> None:
    """Later rungs only run on silence, so the first query must stay first."""
    hints = hints_from_folder_name("Lumãs - Janela Meridiano (1986) [FLAC]")

    queries = query_ladder(hints)

    assert queries[0].artist == "Lumãs"
    assert queries[0].album == "Janela Meridiano"


def test_a_title_without_a_conjunction_grows_no_conjunction_rungs() -> None:
    """The variant rung must not exist for the albums that do not need it."""
    hints = hints_from_folder_name("Tavola - Tavola (1974) [FLAC]")

    queries = query_ladder(hints)
    albums = [query.album for query in queries if query.album]

    assert all("&" not in album for album in albums)


@pytest.mark.parametrize(
    ("folder", "artist", "album", "year"),
    [
        # A collection that numbers its folders: the number is not the artist.
        ("003 - Tico Barque - Armação [1971]", "Tico Barque", "Armação", None),
        ("015 - Jonas Bem - Toada Esquema Torto", "Jonas Bem", "Toada Esquema Torto", None),
        # A leading year is kept as the year rather than read as a credit.
        ("1972 - Zorva", None, "Zorva", 1972),
        # And what must not be touched: a band whose name is a number, and an
        # album whose credit merely opens with one.
        ("404 - Undergrowth", "404", "Undergrowth", None),
        ("1200 Amps - Live in Zorvia (2005)", "1200 Amps", "Live in Zorvia", 2005),
        (
            "110 Discos de Toada Costeira - Por DJ 440",
            "110 Discos de Toada Costeira",
            "Por DJ 440",
            None,
        ),
    ],
)
def test_a_number_leading_a_folder_is_read_as_its_index(
    folder: str, artist: str | None, album: str, year: int | None
) -> None:
    """A numbered collection credits its albums to the number without this."""
    hints = hints_from_folder_name(folder)
    assert (hints.artist, hints.album, hints.year) == (artist, album, year)


def test_an_album_whose_names_are_numbered_is_read_without_the_numbers() -> None:
    """A file name may carry a number that no separator follows."""
    names = [f"{index:02d} Faixa {index}.flac" for index in range(1, 13)]
    assert names_carry_an_index(names) is True
    assert track_title_hint(names[0], drop_leading_number=True) == "Faixa 1"


def test_a_title_that_opens_with_a_number_keeps_it() -> None:
    """Alone among neighbours that are not numbered, the digits are the title."""
    names = ["99 Lanterns.flac", "Somehow Somewhere Sometime.flac", "Here I Stay.flac"]
    assert names_carry_an_index(names) is False
    assert track_title_hint(names[0]) == "99 Lanterns"


def test_a_repeated_number_is_not_an_index() -> None:
    """A numbering never says two, twice; a title that opens with a digit may."""
    names = ["7 Kites.flac", "7 Harbour Lamps.flac", "7 Minutes.flac", "50 Doors.flac"]
    assert names_carry_an_index(names) is False


def test_an_unnumbered_bonus_track_does_not_unnumber_the_album() -> None:
    """Eleven numbered files and a hidden one is still a numbered folder."""
    names = [f"{index:02d} Faixa {index}.flac" for index in range(1, 12)] + ["Bonus.flac"]
    assert names_carry_an_index(names) is True


@pytest.mark.parametrize(
    ("folder", "artist", "album", "year"),
    [
        ("(1974) Tavola", None, "Tavola", 1974),
        ("(1976) Órbita Zunca", None, "Órbita Zunca", 1976),
        ("(1973) Zup-la, Mandolo!", None, "Zup-la, Mandolo!", 1973),
        # A parenthesis that is not a year, and a year that is not a parenthesis,
        # both stay exactly where they are.
        ("(Lia a Todo Pano)", None, "(Lia a Todo Pano)", None),
        # A name that is only a year is read by the trailing pattern, as it
        # always was: the year is the year, and nothing is left to be a title.
        ("(1974)", None, "(1974)", 1974),
    ],
)
def test_a_year_written_in_front_of_the_name_is_the_year(
    folder: str, artist: str | None, album: str, year: int | None
) -> None:
    """Some collections name albums this way, so the year is read at the front too."""
    hints = hints_from_folder_name(folder)
    assert (hints.artist, hints.album, hints.year) == (artist, album, year)


def test_neighbours_are_what_say_a_tight_number_is_an_index() -> None:
    """`020-Jô Tonato-Vila Zunca` cannot be read from its own name alone."""
    collection = [f"{index:03d}-Album {index}" for index in range(1, 12)]
    assert folders_carry_an_index(collection) is True
    assert folders_carry_an_index(["404-Undergrowth", "Tavola", "Zorva"]) is False


@pytest.mark.parametrize(
    ("folder", "artist", "album"),
    [
        # The index runs into a credit, and into an album title, and neither
        # shape can be told from the other by looking at one name.
        ("020-Jô Tonato-Vila Zunca", None, "Jô Tonato-Vila Zunca"),
        ("011-Lia & Tim", None, "Lia & Tim"),
        ("023-Juvenal Prantos-Lousas", None, "Juvenal Prantos-Lousas"),
        ("012-SAUL FREIXAS_ ZUP-LA MANDOLO!", None, "SAUL FREIXAS_ ZUP-LA MANDOLO!"),
    ],
)
def test_a_numbered_collection_loses_the_index_and_keeps_everything_else(
    folder: str, artist: str | None, album: str
) -> None:
    """Nothing is split on a dash with no spaces — `ZUP-LA` is one word.

    What is left with no credit in it reaches the source as text, which is the
    ladder's last rung.
    """
    hints = hints_from_folder_name(folder, numbered_collection=True)
    assert (hints.artist, hints.album) == (artist, album)
    assert hints.is_usable


def test_the_same_name_outside_a_numbered_collection_is_left_alone() -> None:
    """Without the neighbours saying so, the number may be part of a name."""
    hints = hints_from_folder_name("404-Undergrowth")
    assert hints.album == "404-Undergrowth"


def test_the_collection_is_looked_at_again_after_a_folder_is_renamed(tmp_path: Path) -> None:
    """Organizing renames the folder, and a remembered answer would outlive the fact.

    The listing is remembered per parent, because asking once an album is slow
    over a large library, so what invalidates it has to be the thing organizing
    changes: the parent's own modification time.
    """
    collection = tmp_path / "collection"
    collection.mkdir()
    for index in range(1, 6):
        (collection / f"{index:03d}-Album {index}").mkdir()
    album = collection / "003-Album 3"
    unit = AlbumUnit(folder_path=album, unit_signature="unit", audio_files=())

    assert derive_hints(unit, None).album == "Album 3"

    for index in range(1, 6):
        (collection / f"{index:03d}-Album {index}").rename(collection / f"Album {index}")
    renamed = AlbumUnit(folder_path=collection / "Album 3", unit_signature="unit", audio_files=())

    assert derive_hints(renamed, None).album == "Album 3"
    assert derive_hints(unit, None).album == "003-Album 3"


def _numbered(name: str, disc_hint: int | None = None) -> AudioFileFacts:
    return AudioFileFacts(
        path=Path("/music/Album") / name,
        content_signature=f"signature-{name}",
        file_size_bytes=1_000,
        modified_at=datetime(2001, 2, 3, tzinfo=UTC),
        disc_hint=disc_hint,
    )


class _Tags:
    """A tag store answering from a map of file name to what the file says."""

    def __init__(self, said: dict[str, dict[str, tuple[str, ...]]]) -> None:
        self._said = said

    def read(self, path: Path) -> dict[str, tuple[str, ...]]:
        if path.name not in self._said:
            raise KeyError(path.name)
        return self._said[path.name]


def test_the_queue_follows_the_tags_and_not_the_folder_listing() -> None:
    """An album whose files are named without numbers sorts by name, not by track.

    The track number is usually in the tag of every file even when the name
    carries none, so the order is asked of the file, not of the listing.
    """
    files = (_numbered("Amora Avante.flac"), _numbered("Balanceio.flac"), _numbered("Zum.flac"))
    unit = AlbumUnit(folder_path=Path("/music/Album"), unit_signature="unit", audio_files=files)
    tags = _Tags(
        {
            "Amora Avante.flac": {"tracknumber": ("3",)},
            "Balanceio.flac": {"tracknumber": ("2/12",)},
            "Zum.flac": {"tracknumber": ("1",)},
        }
    )

    assert [file.path.name for file in files_in_track_order(unit, tags)] == [
        "Zum.flac",
        "Balanceio.flac",
        "Amora Avante.flac",
    ]


def test_a_file_that_states_no_place_keeps_the_folder_order_after_the_ones_that_do() -> None:
    """Nothing is invented, and an album nothing tags comes back unchanged."""
    files = (_numbered("A.flac"), _numbered("B.flac"), _numbered("C.flac"))
    unit = AlbumUnit(folder_path=Path("/music/Album"), unit_signature="unit", audio_files=files)
    partly = _Tags({"C.flac": {"tracknumber": ("1",)}, "A.flac": {"title": ("no number",)}})

    assert [file.path.name for file in files_in_track_order(unit, partly)] == [
        "C.flac",
        "A.flac",
        "B.flac",
    ]
    assert files_in_track_order(unit, _Tags({})) == files
    assert files_in_track_order(unit, None) == files


def test_the_disc_the_scanner_measured_outranks_the_tag() -> None:
    """A file out of a `CD2` folder is on disc two whatever its tags say."""
    files = (
        _numbered("second disc first track.flac", disc_hint=2),
        _numbered("first disc last track.flac", disc_hint=1),
    )
    unit = AlbumUnit(folder_path=Path("/music/Album"), unit_signature="unit", audio_files=files)
    tags = _Tags(
        {
            "second disc first track.flac": {"tracknumber": ("1",), "discnumber": ("1",)},
            "first disc last track.flac": {"tracknumber": ("9",), "discnumber": ("1",)},
        }
    )

    assert [file.path.name for file in files_in_track_order(unit, tags)] == [
        "first disc last track.flac",
        "second disc first track.flac",
    ]


def test_two_files_claiming_one_number_keep_the_order_the_folder_gave_them() -> None:
    """A sort that is not stable swaps them between two reads of one shelf."""
    files = (_numbered("first.flac"), _numbered("second.flac"))
    unit = AlbumUnit(folder_path=Path("/music/Album"), unit_signature="unit", audio_files=files)
    tags = _Tags({"first.flac": {"tracknumber": ("4",)}, "second.flac": {"tracknumber": ("4",)}})

    assert files_in_track_order(unit, tags) == files


def test_a_title_every_file_repeats_is_read_from_the_names_instead() -> None:
    """Four files, one site address in every `TIT2`.

    A tag that wins because it exists hides the four titles the *names* carry,
    and the correct release then pairs none of the files. A title tag that does
    not tell the tracks apart is not read as a title.
    """
    names = (
        "01- zunto livre.mp3",
        "02- plimba 64.mp3",
        "03- zorcas.mp3",
        "04- no virar das vorcas.mp3",
    )
    unit = AlbumUnit(
        folder_path=Path("/music/Artista - 1980"),
        unit_signature="unit",
        audio_files=tuple(_numbered(name) for name in names),
    )
    spam = {name: {"title": ("www.example-downloads.com",)} for name in names}

    assert local_track_titles(unit, _Tags(spam)) == (
        "zunto livre",
        "plimba 64",
        "zorcas",
        "no virar das vorcas",
    )


def test_titles_that_differ_are_still_the_tags_word() -> None:
    """The rule is about a value that names nothing, and must not reach a real tag."""
    names = ("01 first.mp3", "02 second.mp3")
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=tuple(_numbered(name) for name in names),
    )
    tags = _Tags(
        {"01 first.mp3": {"title": ("Nosso Rumo",)}, "02 second.mp3": {"title": ("Outro Rumo",)}}
    )

    assert local_track_titles(unit, tags) == ("Nosso Rumo", "Outro Rumo")


@pytest.mark.parametrize(
    ("titles", "distinguish"),
    [
        (("One", "Two"), True),
        (("Same", "Same"), False),
        (("Same", "same", "SAME"), False),
        (("Same", "Same", "Other"), True),
        # One title agrees with itself, which says nothing about anything.
        (("Same",), True),
        # A hole is not this question: `titled < len(read)` is what answers it.
        (("Same", None), True),
        ((None, None), True),
        ((), True),
    ],
)
def test_tag_titles_distinguish_asks_only_whether_one_value_stands_for_every_track(
    titles: tuple[str | None, ...], distinguish: bool
) -> None:
    """Written by the complement: nothing here knows what a bad title looks like."""
    assert tag_titles_distinguish(titles) is distinguish


def test_a_repeated_title_does_not_decide_whether_a_folder_holds_two_albums() -> None:
    """`_one_song` reads these titles, so one value in all of them reverses the advice.

    Two four-track albums in one folder: every number from 1 to 4 twice, on
    eight different songs. A site address in every tag answers *the same song*
    about every pair — and the sentence would tell the user to keep one copy of
    an album they own once. `SHORTEST_RUN` is why the run is four and not two.
    """
    names = tuple(f"{side}{number}.mp3" for side in "ab" for number in range(1, 5))
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=tuple(_numbered(name) for name in names),
    )
    spam = {name: {"tracknumber": (name[1],), "title": ("www.example.com",)} for name in names}

    numbered = numbered_tracks(unit, _Tags(spam))

    assert [track.title for track in numbered] == [
        name[:2] for name in names
    ], "the names, which are what tell them apart"
    assert numbers_deny_one_album(numbered, len(names)).startswith(
        "The track numbers here describe 2 different albums"
    )
