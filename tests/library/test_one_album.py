"""What a folder's own track numbers prove about it.

Arithmetic rather than a heuristic, so these tests are arithmetic too: they hand
the rule the numbers a folder's tags carry and read back the sentence it says.
The rule speaks about very few folders and is right about those, which is the
property worth keeping.
"""

from diglibrary.library.containers import (
    SHORTEST_RUN,
    NumberedTrack,
    numbers_deny_one_album,
)


def _run(disc: str, titles: tuple[str, ...], bitrate: int | None = None) -> list[NumberedTrack]:
    """One complete run of numbered tracks on one disc."""
    return [
        NumberedTrack(disc, number, title, bitrate) for number, title in enumerate(titles, start=1)
    ]


TWELVE_1974 = (
    "Zunto Plimba",
    "Vorca (Zorca)",
    "Quelmo Zarvo",
    "Plinta Vurca",
    "Zolho Plasa",
    "Vreto Zanca",
    "Zilha Pliqui",
    "Zuleca Vorco",
    "Plohau Zere",
    "Zoidera Vurco",
    "Zorvações",
    "Plesma Vurteza",
)
TWELVE_1977 = (
    "Zarpo Quilma",
    "365 Zorvas",
    "Plumo Zenca",
    "Vilto Zarca",
    "Zem Plovê",
    "Zadorva",
    "Plidaço Zurmo",
    "Vunho Zelca",
    "Zida Plorva",
    "Zoração Vilmo",
    "Plinquedo Zebrou",
    "Zoltando Plurca",
)


def test_two_different_albums_in_one_folder_are_named_as_that() -> None:
    """Two self-titled LPs by one artist, in one folder.

    Every number from 1 to 12 is present twice on different songs, so the
    folder cannot be one album. Without this rule twelve of the files pair
    against the adopted release, the other twelve are orphaned, and the whole
    is refused as ambiguous with no reason given.
    """
    numbered = _run("1", TWELVE_1974) + _run("1", TWELVE_1977)

    said = numbers_deny_one_album(numbered, files=24)

    assert "2 different albums of 12 tracks" in said
    assert "on different songs" in said
    assert "own folder" in said, "it says what to do about it"


def test_one_album_ripped_twice_is_named_as_that_instead() -> None:
    """The other shape, and the count alone cannot tell them apart.

    The same album ripped twice under two file naming conventions. What
    separates it from two albums is that the repeated number carries *the same
    song*, and `Plim Tom Tom` against `Plim, Tom, Tom` has to count as the same
    song or every duplicate rip reads as a second album.
    """
    once = _run("1", ("Zup Que Lada!", "Plim Tom Tom", "Zorve Zuva", "Vem Zorena Vem"))
    again = _run("1", ("Zup, Que Lada!", "Plim, Tom, Tom", "Zorve, Zuva", "Vem, Zorena, Vem"))

    said = numbers_deny_one_album(once + again, files=8)

    assert "2 copies of the same 4-track album" in said
    assert "Keep one copy" in said


def test_a_two_disc_album_is_one_album_however_its_tags_read() -> None:
    """A two-disc album whose files carry no disc tag is still one album.

    Read from the tags alone, such an album looks like two different albums,
    and the advice would be to split a record the scanner has just joined from
    `CD1` and `CD2`. The disc a file is on is the scanner's answer, not the
    tag's, and with it the arithmetic does not close.
    """
    two_discs = _run("1", TWELVE_1974) + _run("2", TWELVE_1977)

    assert numbers_deny_one_album(two_discs, files=24) == ""


def test_an_ordinary_album_is_not_spoken_about() -> None:
    """Silence is the answer for nearly every album."""
    assert numbers_deny_one_album(_run("1", TWELVE_1974), files=12) == ""


def test_a_folder_where_every_file_is_track_one_says_nothing() -> None:
    """Sloppy tagging is not a second album, and there is plenty of it.

    A run of one is not a run: without this floor the rule fires on folders whose
    numbers were simply never filled in, which is the opposite of a measurement.
    """
    all_first = [NumberedTrack("1", 1, f"Song {index}") for index in range(1, 13)]

    assert numbers_deny_one_album(all_first, files=12) == ""
    assert SHORTEST_RUN >= 2, "the floor exists for exactly that reason"


def test_one_unnumbered_file_closes_the_rule_s_mouth() -> None:
    """The arithmetic has to close completely or it proves nothing.

    A file with no number contributes no entry, so the count no longer matches
    the folder — and a folder this cannot speak about is a folder it says nothing
    about, rather than one it guesses at.
    """
    numbered = _run("1", TWELVE_1974) + _run("1", TWELVE_1977)

    assert numbers_deny_one_album(numbered, files=25) == ""


def test_an_uneven_run_says_nothing() -> None:
    """Eleven numbers twice and one number three times is not m runs of k."""
    numbered = (
        _run("1", TWELVE_1974) + _run("1", TWELVE_1977) + [NumberedTrack("1", 5, "A Third Song")]
    )

    assert numbers_deny_one_album(numbered, files=25) == ""


def test_when_the_titles_split_evenly_it_refuses_to_say_which() -> None:
    """Half the repeats the same song, half not: it says what it measured and stops.

    The rule refuses, and says what was measured, rather than guess a shape or
    split on the file order.
    """
    once = _run("1", ("One", "Two", "Three", "Four"))
    again = _run("1", ("One", "Two", "Different", "Another"))

    said = numbers_deny_one_album(once + again, files=8)

    assert "2 runs of 4 tracks" in said
    assert "do not say whether" in said


def test_the_better_copy_is_named_when_the_streams_disagree() -> None:
    """The sentence says which copy is better; discarding is left to the user.

    Counted number by number rather than by grouping the files, because nothing
    here knows which file belongs to which rip — one is named `01 - Song.mp3` and
    the other `01. Song.mp3`, and reading a convention out of that is a guess.
    """
    good = _run("1", ("One", "Two", "Three", "Four"), bitrate=320_000)
    poor = _run("1", ("One", "Two", "Three", "Four"), bitrate=192_000)

    said = numbers_deny_one_album(good + poor, files=8)

    assert "2 copies of the same 4-track album" in said
    assert "320 against 192 kbps" in said
    assert "keep the higher one" in said


def test_two_copies_of_one_rip_say_nothing_about_which_is_better() -> None:
    """Silence where there is nothing to measure: identical bitrates decide nothing."""
    once = _run("1", ("One", "Two", "Three", "Four"), bitrate=320_000)
    again = _run("1", ("One", "Two", "Three", "Four"), bitrate=320_000)

    said = numbers_deny_one_album(once + again, files=8)

    assert "2 copies of the same 4-track album" in said
    assert "kbps" not in said, "it does not invent a winner"
