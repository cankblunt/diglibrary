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
    assert normalize_title("No Money No Love", fallback="en") == "No Money No Love"
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
    assert normalize_artist("Moreira Da Costa", "pt") == "Moreira da Costa"
    assert normalize_artist("Praiano & Os Novos Mineiros") == "Praiano & Os Novos Mineiros"
    assert normalize_artist("Paulo Roberto E Joca") == "Paulo Roberto e Joca"


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


def test_a_release_read_from_the_users_own_tags_is_their_words() -> None:
    mine = _release(source=MetadataSources.TAGS)
    assert normalize_release(mine) is mine


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
