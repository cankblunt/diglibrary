"""Unit tests for distinguishing a real change from a change of case or accent."""

import pytest

from diglibrary.library.spelling import (
    a_whole_word_apart,
    is_spelling_variant,
    one_word_apart,
    preserve_existing_spelling,
    resolve_spelling,
    richer_spelling,
)


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        # The slip is on the catalogue's side.
        ("Thirty Lanterns", "Thirty Lanterms"),
        ("Élan Quiet Harbour", "Elan Qiuet Harbour"),
        ("The Change Is Necessary", "The Change Is Neccessary"),
        ("Seven Zoétropes", "Seven Zoétrapes"),
        ("Bearded Harbour", "Beardod Harbour"),
        # The slip is on the disk's side, which is why the line is marked
        # instead of a side being picked.
        ("Wanted-A Lantern (Sulfix)", "Wanted-A Lantern (Sufix)"),
        ("That Old Mischef", "That Old Mischief"),
        ("Twíllight", "Twílight"),
        ("Ascention of a plowman", "Ascension Of A Plowman"),
        # Both real words, no dictionary would help, and it still gets asked.
        ("When the Singer Walks", "When the Singer Talks"),
    ],
)
def test_one_word_apart_marks_the_line_whichever_side_slipped(mine: str, theirs: str) -> None:
    """Either side may hold the slip, so the local spelling is returned and shown."""
    assert one_word_apart(mine, theirs) == mine


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        # Punctuation and case are the catalogue's to decide, and they are most
        # of the titles that differ by more than an accent.
        ("Roundel Roundelay (Harbour)", "Roundel, Roundelay (Harbour)"),
        ("Main avenue, part 2", "Main Avenue Part 2"),
        ("Where Is the Good Lantern", "Where Is the Good Lantern?"),
        # Accent alone is already settled by resolve_spelling.
        ("Zoéllê", "Zoelle"),
        ("Nêlla of Obarê", "Nella of Obare"),
        # A short joining word written two ways is below the floor.
        ("Marévo in the lanterns", "Marévo On The Lanterns"),
        # Short words: one edit is most of the word, and both are plausible titles.
        ("Tin Lantern", "Ten Lantern"),
        # A word the catalogue simply added or dropped.
        ("Marrowby", "Marrowby Dub"),
        # Nothing to compare against: an unreadable tag is not a disagreement.
        (None, "Quarried"),
    ],
)
def test_one_word_apart_stays_quiet_where_the_catalogue_is_simply_right(
    mine: str | None, theirs: str
) -> None:
    """A mark on every line is a mark on none, so the rule stays narrow."""
    assert one_word_apart(mine, theirs) == ""


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("LANTERNE", "Lanterne"),
        ("marrowby", "MARROWBY"),
        ("Mirén  Entire", "mirén entire"),
        ("Mirén", "Miren"),
        ("Zoé Marrowby", "Zoe Marrowby"),
    ],
)
def test_case_and_accent_are_spelling_variants(left: str, right: str) -> None:
    """One word reaches this project in two forms: miscased, or unaccented."""
    assert is_spelling_variant(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Vol. 2", "Vol 2"),
        ("01. Track", "02. Track"),
        ("Lanterne", "Lanterne"),
        ("Lanterne", "Lanternes"),
    ],
)
def test_punctuation_and_real_differences_are_not_variants(left: str, right: str) -> None:
    """Beyond case and accent, everything is content the source decided."""
    assert not is_spelling_variant(left, right)


@pytest.mark.parametrize(
    ("local", "catalogue", "expected"),
    [
        # The disk carries the accent and the catalogue is missing it.
        ("Zoé Marrowby", "Zoe Marrowby", "Zoé Marrowby"),
        # The folder was named without the accent; the catalogue has it.
        ("Miren Quarrel", "Mirén Quarrel", "Mirén Quarrel"),
        # Case comes from the catalogue, which arrives written by the one rule
        # for names.
        ("LANTERNE", "Lanterne", "Lanterne"),
        ("Orchestra Of The Hills", "Orchestra of the Hills", "Orchestra of the Hills"),
        # The disk's accent and the catalogue's case, each where it is right.
        ("Zoé Marrowby Of Quarrel", "Zoe Marrowby of Quarrel", "Zoé Marrowby of Quarrel"),
        # A value in capitals is one the rule leaves as written, and there the
        # disk keeps its own case.
        ("Say You Want Lanterns", "SAY YOU WANT LANTERNS", "Say You Want Lanterns"),
        # Anything beyond case and accent belongs to the catalogue.
        ("Miren Entire", "Lanterne Entire", "Lanterne Entire"),
    ],
)
def test_the_richer_spelling_wins(local: str, catalogue: str, expected: str) -> None:
    """More diacritics means someone who could type them wrote it."""
    assert resolve_spelling(local, catalogue) == expected


def test_a_missing_local_value_leaves_the_catalogue_alone() -> None:
    """Nothing on disk is not evidence about spelling."""
    assert resolve_spelling(None, "Zoé Marrowby") == "Zoé Marrowby"
    assert richer_spelling("Zoe", "Zoé") == "Zoé"


def test_existing_spelling_survives_a_write() -> None:
    """An album kept in capitals is not rewritten because a contributor typed it mixed."""
    current = {"album": ("LANTERNE",), "artist": ("QRX", "VELLUMO")}
    desired = {"album": ("Lanterne",), "artist": ("QRX", "Vellumo"), "date": ("2001-01-01",)}

    preserved = preserve_existing_spelling(current, desired)

    assert preserved["album"] == ("LANTERNE",)
    assert preserved["artist"] == ("QRX", "VELLUMO")
    assert preserved["date"] == ("2001-01-01",)


def test_a_genuinely_different_value_is_still_corrected() -> None:
    """Preserving case must not preserve a wrong value."""
    preserved = preserve_existing_spelling({"album": ("Wrong Album",)}, {"album": ("Lanterne",)})

    assert preserved["album"] == ("Lanterne",)


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        # Several edits apart and both real words, which is why
        # `one_word_apart` refuses it and this does not.
        ("Little Harbour", "Lonely Harbour"),
        # Short joining words and short spellings the letters alone refuse.
        ("Mirén in Marrowby", "Miren On Marrowby"),
        ("Theme From Zoélle", "Theme For Zoélle"),
        ("Oh! How We Miss an Amélie", "Oh! How We Miss the Amélie"),
        ("Hei Zoélle", "Hey Zoélle"),
        ("Marévo in the lanterns", "Marévo On The Lanterns"),
    ],
)
def test_a_whole_word_apart_marks_what_the_letters_alone_could_not(mine: str, theirs: str) -> None:
    """The witness supplies the evidence, so the rule can stop guessing."""
    assert a_whole_word_apart(mine, theirs) == mine


def test_the_wider_rule_still_answers_where_the_narrow_one_does() -> None:
    """A typo is a differing word too, so the two never disagree about a line.

    They differ in what they may be *asked*, not in what they see: a line the
    narrow rule marks comes back the same from this one, which is what lets the
    payload prefer the witness reason without ever changing the offer itself.
    """
    assert a_whole_word_apart("Thirty Lanterns", "Thirty Lanterms") == "Thirty Lanterns"
    assert one_word_apart("Thirty Lanterns", "Thirty Lanterms") == "Thirty Lanterns"


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        # Case, accent and punctuation are settled before this is asked and
        # are not a differing word.
        ("Zoéllê", "zoelle"),
        ("Main avenue, part 2", "Main Avenue Part 2"),
        # Two words apart is a different title, not a spelling of this one.
        ("One Thing Like This", "Other Thing Like That"),
        # A word more is an edition's parenthesis, and this rule says nothing
        # about those: a title against itself with `(Bonus Track)` is not a slip.
        ("Little Harbour", "Little Harbour (Bonus Track)"),
        # Nothing to compare: no words at all on the local side.
        ("1987", "Nineteen Eighty Seven"),
    ],
)
def test_a_whole_word_apart_stays_quiet_where_nothing_is_in_dispute(mine: str, theirs: str) -> None:
    """The mark costs a look, so it is spent only on a word that differs."""
    assert a_whole_word_apart(mine, theirs) == ""


def test_a_whole_word_apart_has_nothing_to_say_about_a_file_it_cannot_read() -> None:
    """No local title is the arrangement case and every unidentified album."""
    assert a_whole_word_apart(None, "Lonely Harbour") == ""
    assert a_whole_word_apart("", "Lonely Harbour") == ""


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        # A title of one word has no rest of itself to hold still, so every pair
        # of them differs in exactly one word. These are whole titles replaced,
        # not words disputed.
        ("aaa", "Track 1"),
        ("Brittlé", "Oddmentia"),
        ("Far-off", "Quarried"),
    ],
)
def test_a_one_word_title_is_a_whole_title_and_never_a_disputed_word(
    mine: str, theirs: str
) -> None:
    """Something has to agree for the disagreement to be about a word."""
    assert a_whole_word_apart(mine, theirs) == ""


def test_a_name_the_disk_holds_decomposed_is_not_renamed_to_itself() -> None:
    """A file system may return `É` decomposed, and composing it is not a change."""
    import unicodedata

    on_disk = unicodedata.normalize("NFD", "Élan Yard")
    assert resolve_spelling(on_disk, "Élan Yard") == on_disk
