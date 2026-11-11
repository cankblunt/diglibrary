"""A title's capitals are written one way, whoever typed it."""

from dataclasses import replace
from datetime import date

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.casing import (
    is_shouted,
    normalize_artist,
    normalize_release,
    normalize_title,
)


@pytest.mark.parametrize(
    ("catalogue", "written"),
    [
        # Invented examples, from what a catalogue might type.
        ("Orquestra Da Serra", "Orquestra da Serra"),
        ("Volta cedo a lonjura", "Volta Cedo a Lonjura"),
        ("A Plimba E A Zorca De Dino Rocha", "A Plimba e a Zorca de Dino Rocha"),
        # The start of every part takes a capital, whatever the word.
        ("O Zecão: lonjura No Leblon", "O Zecão: Lonjura no Leblon"),
        ("Eu, Juiz, Me Absolvo / Pão Com Queijo", "Eu, Juiz, Me Absolvo / Pão com Queijo"),
        ("Brisa (Lanterna & Tom Ferraz remix)", "Brisa (Lanterna & Tom Ferraz Remix)"),
        # Each part of a compound takes its own capital.
        ("Bem-vindo Ao Salão", "Bem-Vindo ao Salão"),
        # Numerals, pronouns and verbs keep their capital.
        ("Conto Até Um Na Rua", "Conto Até Um na Rua"),
        ("Zunto Que Te Plimo", "Zunto Que Te Plimo"),
        # English, where `Do` and `No` are words of their own.
        ("Home Is Where The Heart Is", "Home Is Where the Heart Is"),
        ("Songs of No Return", "Songs of No Return"),
        ("What Do You Need", "What Do You Need"),
        (
            "Não Me Atrase O Dia",
            "Não Me Atrase o Dia",
        ),
        # Spanish.
        ("Quedar bien con el vecino", "Quedar Bien con el Vecino"),
        # Left exactly as written: initialisms, abbreviations, numbers, inner capitals.
        ("A.E.I. (A Dança)", "A.E.I. (A Dança)"),
        ("Night Bird Call (Galpão No. 2)", "Night Bird Call (Galpão No. 2)"),
        ("Return to the 12th Floor", "Return to the 12th Floor"),
        ("eMail Blues", "eMail Blues"),
        # An apostrophe typed as a grave mark, and a bullet separating parts.
        ("Menina De Gelo (Tito`s Extended Remix)", "Menina de Gelo (Tito`s Extended Remix)"),
        (
            "Zorvelo (Entrada • A Plimba • A Zorca Dos Peixes)",
            "Zorvelo (Entrada • A Plimba • A Zorca dos Peixes)",
        ),
        ("O rumo da pá.lavra", "O Rumo da Pá.lavra"),
        # A title in capitals from end to end is kept.
        ("ZUNTO QUE TE PLIMO", "ZUNTO QUE TE PLIMO"),
        ("A TOADA LIVRE DE ZÉ BARRENTO", "A TOADA LIVRE DE ZÉ BARRENTO"),
    ],
)
def test_the_one_rule_for_names(catalogue: str, written: str) -> None:
    assert normalize_title(catalogue) == written


def test_only_case_ever_moves() -> None:
    """Nothing is added or removed, so the rule is undone by comparing without case."""
    for title in (
        "Tia D'Argila",
        "Bring It To 'Em A.C.",
        "Pé-De-Bruma  Das Onze",
    ):
        assert normalize_title(title).casefold() == title.casefold()


def test_a_word_that_is_a_word_in_another_language_follows_the_album() -> None:
    """Where the part says nothing, the release's language is asked; failing that, Portuguese."""
    assert normalize_title("Orquestra Da Serra") == "Orquestra da Serra"
    assert normalize_title("No Lantern No Road", fallback="en") == "No Lantern No Road"
    assert normalize_title("Sons Da Mata", fallback="en") == "Sons Da Mata"
    assert normalize_title("O Que For Melhor") == "O Que For Melhor"


def test_the_last_word_of_every_part_takes_a_capital() -> None:
    assert normalize_title("Who's Moving on") == "Who's Moving On"
    assert normalize_title("Movin' on / Something About Time") == (
        "Movin' On / Something About Time"
    )
    assert normalize_title("Traga o Que for") == "Traga o Que For"
    assert normalize_title("Zorvel (Let the Moonlight in)") == "Zorvel (Let the Moonlight In)"


def test_an_article_that_opens_a_credited_name_keeps_its_capital() -> None:
    names = ["The Amazing Conga Band"]
    assert normalize_title("The Revenge Of The Amazing Conga Band", names) == (
        "The Revenge of The Amazing Conga Band"
    )


def test_an_artist_is_a_proper_noun_from_end_to_end() -> None:
    assert normalize_artist("Moreira Da Costa") == "Moreira da Costa"
    assert normalize_artist("Praiano & Os Novos Mineiros") == "Praiano & Os Novos Mineiros"
    assert normalize_artist("Paulo Roberto E Joca") == "Paulo Roberto e Joca"
    # Every article inside a name keeps its capital, and a preposition does not;
    # `a` is both, and is the article only after the words that join two names.
    assert normalize_artist("Rodas Contra The Zunto") == "Rodas Contra The Zunto"
    assert normalize_artist("Reis of The Plimba") == "Reis of The Plimba"
    assert normalize_artist("Tenda Unidos A Zorvelim") == "Tenda Unidos a Zorvelim"
    assert normalize_artist("Zunto e A Plimba") == "Zunto e A Plimba"
    # Read by the name's own words: an English word keeps `a` an article, and a
    # name with none is read as Portuguese, the presumed language.
    assert normalize_artist("Just A Zorvo Band") == "Just A Zorvo Band"
    assert normalize_artist("Only A Zorvo Band") == "Only a Zorvo Band"
    # A file's tags list several artists in one value, separated by `;`.
    assert normalize_artist("Moreira Dias; Da Serra Trio") == "Moreira Dias; Da Serra Trio"


def test_a_name_is_not_written_in_the_language_of_the_album_it_is_on() -> None:
    """`da` is a word of its own in English, and an album's English titles once made it one.

    The name's own words still decide: `on` says the second one is English.
    """
    english = replace(
        _release(),
        title="What You Zunto",
        tracks=(
            TrackMetadata(title="Who Plimbs It", position=1),
            TrackMetadata(title="When the Vorca Is Over", position=2),
        ),
    )
    for credited, written in (
        ("Moreira da Costa", "Moreira da Costa"),
        ("Moreira Da Costa", "Moreira da Costa"),
        ("Zorvo on Da Bus", "Zorvo on Da Bus"),
    ):
        release = replace(english, artists=(ArtistMetadata(name=credited),))
        assert normalize_release(release).artists[0].name == written


def test_an_article_that_ends_the_words_between_two_names_opens_the_second() -> None:
    from diglibrary.application.contracts import written_credit

    for joined, credit in (
        ("& The", "Lia & The Plimba Band"),
        ("And The", "Lia and The Plimba Band"),
        ("E Os", "Lia e Os Plimba Band"),
        ("Feat.", "Lia Feat. Plimba Band"),
    ):
        release = replace(
            _release(),
            artists=(
                ArtistMetadata(name="Lia", joined_by=joined),
                ArtistMetadata(name="Plimba Band"),
            ),
        )
        assert written_credit(normalize_release(release).artists) == credit


def test_a_credited_name_inside_a_title_is_written_as_the_name_is() -> None:
    """A folder's name holds the credit and the title, and one name must not read two ways."""
    names = ["Lia & The Plimba Band", "Praiano e Os Novos Mineiros"]
    assert normalize_title("Lia & the Plimba Band", names) == "Lia & The Plimba Band"
    assert normalize_title("Praiano E Os Novos Mineiros - Volume 2", names) == (
        "Praiano e Os Novos Mineiros - Volume 2"
    )
    # Only where the whole name stands: elsewhere an article is an ordinary word.
    assert normalize_title("Live at the Club With Lia", names) == "Live at the Club with Lia"
    release = replace(
        _release(),
        title="Lia & the Plimba Band",
        artists=(ArtistMetadata(name="Lia & The Plimba Band"),),
    )
    named = normalize_release(release)
    assert named.title == named.artists[0].name


def test_shouting_is_capitals_from_end_to_end() -> None:
    assert is_shouted("ZUNTO QUE TE PLIMO")
    assert not is_shouted("MPB Na Praça")
    assert not is_shouted("A")


def _release(source=MetadataSources.DISCOGS, **track) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=source,
        source_release_id="1",
        title="Orquestra Da Serra",
        artists=(ArtistMetadata(name="Trio Solitário"),),
        tracks=(
            TrackMetadata(title="Três Gatos Na Rua", position=1),
            TrackMetadata(title="Zunto Plimba Com Vorca", position=2, **track),
        ),
        released_on=date(1970, 1, 1),
    )


def test_the_release_is_written_one_way_and_the_users_words_are_not() -> None:
    named = normalize_release(_release(), user_titles=frozenset({2}))
    assert named.title == "Orquestra da Serra"
    assert [track.title for track in named.tracks] == [
        "Três Gatos na Rua",
        "Zunto Plimba Com Vorca",
    ]
    added = normalize_release(_release(added_by_user=True))
    assert added.tracks[1].title == "Zunto Plimba Com Vorca"


def test_a_release_read_from_the_albums_own_tags_takes_the_same_case() -> None:
    """Arranging by the tags normalizes to the pattern, case included.

    A title the user corrected stays as typed: that is a word written for this
    album, not one a file happened to carry.
    """
    mine = _release(source=MetadataSources.TAGS)
    named = normalize_release(mine)
    assert named.title == normalize_release(_release()).title
    assert named.title != mine.title, "the fixture must have something to normalize"
    assert normalize_release(mine, user_titles=frozenset({1})).tracks[0].title == (
        mine.tracks[0].title
    )


def test_no_word_being_decided_is_counted_as_evidence_of_a_language() -> None:
    """A marker that is also a word in dispute decides its own case, which is circular."""
    from diglibrary.library.casing import _CONTENT_ELSEWHERE, _MARKERS

    for language, markers in _MARKERS.items():
        assert not markers & set(_CONTENT_ELSEWHERE), language


def test_the_words_between_two_credited_names_answer_to_the_rule() -> None:
    from diglibrary.application.contracts import written_credit

    release = replace(
        _release(),
        artists=(
            ArtistMetadata(name="Lia", joined_by="E"),
            ArtistMetadata(name="Lara", joined_by="Feat."),
            ArtistMetadata(name="Tina"),
        ),
    )
    assert written_credit(normalize_release(release).artists) == "Lia e Lara Feat. Tina"


@pytest.mark.parametrize(
    ("typed", "written"),
    [
        # A second capital held down at the start is a slip of the shift key.
        ("ZOrvelim Na Rua", "Zorvelim na Rua"),
        # A capital beside an accented letter mid-word is a broken earlier casing.
        ("PlimbÃO Da Serra", "Plimbão da Serra"),
        ("InfluêNcia Do Zorvo", "Influência do Zorvo"),
        ("ZunÍCia", "Zunícia"),
        # A capital inside a name is deliberate, and so is a plural initialism.
        ("McZorvo Na Rua", "McZorvo na Rua"),
        ("ZunZun", "ZunZun"),
        ("iZorvo", "iZorvo"),
        ("Três LPs", "Três LPs"),
        # Ordinary accented words are untouched.
        ("Ação Na Praça", "Ação na Praça"),
    ],
)
def test_a_capital_no_name_is_spelled_with_is_lowered(typed, written) -> None:
    assert normalize_title(typed) == written


@pytest.mark.parametrize(
    "name",
    [
        # The shapes the title repair lowers are an artist's real spelling here.
        "ZNorvão",
        "Os Zorvos MC's",
        "Os Zorvos MC\u2019s",
        "Plimbo ZiléZilé",
    ],
)
def test_an_artist_s_name_keeps_the_capitals_a_title_would_lose(name) -> None:
    assert normalize_artist(name) == name


def test_a_credited_name_inside_a_title_keeps_its_capitals_too() -> None:
    assert normalize_title("Canto Com ZNorvão", ["ZNorvão"]) == "Canto com ZNorvão"
    assert (
        normalize_title("Canto Com ZNorvão") == "Canto com Znorvão"
    ), "uncredited, it is a title's own word and the repair still reaches it"


def test_a_release_s_artists_keep_their_capitals_on_the_way_to_a_name() -> None:
    release = replace(
        _release(),
        artists=(ArtistMetadata(name="ZNorvão"),),
    )
    assert normalize_release(release).artists[0].name == "ZNorvão"
