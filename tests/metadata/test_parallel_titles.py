"""A name a catalogue prints in two scripts is written in Latin letters alone.

Discogs writes a title in two languages as `original = translation`, and
enters the artists of such a sleeve twice, once per script, joined by an equals
sign. What is written to a library is the spelling a person types: the Latin
side where there is one, and each artist once.
"""

from __future__ import annotations

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
    written_credit,
)
from diglibrary.metadata.discogs import (
    _discogs_artists,
    _map_discogs_release,
    _map_discogs_search_result,
)
from diglibrary.metadata.normalization import (
    latin_spelling,
    parallel_forms,
    release_in_latin_spelling,
)


def test_the_latin_spelling_is_kept_and_the_other_script_dropped() -> None:
    assert latin_spelling("北の風 = Brumel Tarvo") == "Brumel Tarvo"
    assert latin_spelling("Brumel Tarvo = 北の風") == "Brumel Tarvo"
    assert latin_spelling("ريح الشمال = Rih Al Shamal") == "Rih Al Shamal"


def test_two_latin_script_languages_are_both_kept_in_the_catalogue_s_order() -> None:
    assert latin_spelling("Brumel Tarvo = Tarvo Mist") == "Brumel Tarvo = Tarvo Mist"
    assert latin_spelling("Um = Dois = Three") == "Um = Dois = Three"


def test_two_latin_sides_survive_a_third_in_another_script() -> None:
    assert latin_spelling("北の風 = Brumel Tarvo = Tarvo Mist") == "Brumel Tarvo = Tarvo Mist"


def test_a_name_with_no_latin_side_is_left_as_the_catalogue_wrote_it() -> None:
    assert latin_spelling("北の風") == "北の風"
    assert latin_spelling("北の風 = ريح الشمال") == "北の風 = ريح الشمال"


def test_an_equals_sign_that_joins_no_translation_drops_nothing() -> None:
    assert latin_spelling("Dois + Dois = 4") == "Dois + Dois = 4"
    assert latin_spelling("Brumel Tarvo") == "Brumel Tarvo"


def test_a_side_that_repeats_the_other_is_written_once() -> None:
    assert latin_spelling("Brumel Tarvo = Brumel Tarvo") == "Brumel Tarvo"
    assert latin_spelling("Brúmel Tarvo = brumel tarvo") == "Brúmel Tarvo"


def test_an_accented_latin_letter_is_still_latin() -> None:
    assert latin_spelling("北の風 = Ālibe Ñandú") == "Ālibe Ñandú"


def test_an_ordinal_sign_does_not_make_a_side_another_script() -> None:
    assert latin_spelling("北の風 = Brumel Nº 1") == "Brumel Nº 1"


def test_each_side_is_a_whole_spelling() -> None:
    assert parallel_forms("北の風 = Brumel Tarvo") == ("北の風", "Brumel Tarvo")
    assert parallel_forms("Brumel Tarvo") == ("Brumel Tarvo",)


def test_a_discogs_release_is_mapped_in_latin_letters() -> None:
    """The release's title, every track's and the credit: everything a plan names."""
    release = _map_discogs_release(
        {
            "id": 42,
            "title": "北の風 = Brumel Tarvo",
            "artists": [
                {"id": 7, "name": "北の人", "join": "="},
                {"id": 7, "name": "北の人", "anv": "Zed Marrowby"},
            ],
            "tracklist": [
                {"position": "1", "title": "朝 = Ulmó"},
                {"position": "2", "title": "Veska"},
            ],
        }
    )

    assert release.title == "Brumel Tarvo"
    assert [track.title for track in release.tracks] == ["Ulmó", "Veska"]
    assert [artist.name for artist in release.artists] == ["Zed Marrowby"]


def test_an_artist_entered_once_per_script_is_named_once() -> None:
    """The catalogue's own name stands when it is already in Latin letters."""
    artists = _discogs_artists(
        [
            {"id": 7, "name": "Zed Marrowby", "anv": "北の人", "join": "="},
            {"id": 7, "name": "Zed Marrowby"},
        ]
    )

    assert written_credit(artists) == "Zed Marrowby"
    assert len(artists) == 1


def test_two_artists_credited_in_two_scripts_keep_the_latin_join() -> None:
    artists = _discogs_artists(
        [
            {"id": 7, "name": "Zed Marrowby", "anv": "Z. Marrowby", "join": "And"},
            {"id": 8, "name": "The Glass Herons", "join": "="},
            {"id": 7, "name": "Zed Marrowby", "anv": "北の人", "join": "及び"},
            {"id": 8, "name": "The Glass Herons", "anv": "ガラス"},
        ]
    )

    assert written_credit(artists) == "Zed Marrowby And The Glass Herons"


def test_a_second_credit_naming_one_artist_more_keeps_them_all() -> None:
    artists = _discogs_artists(
        [
            {"id": 7, "name": "Zed Marrowby", "anv": "北の人", "join": "="},
            {"id": 7, "name": "Zed Marrowby", "join": "&"},
            {"id": 8, "name": "The Glass Herons", "anv": "ガラス"},
        ]
    )

    assert written_credit(artists) == "Zed Marrowby & The Glass Herons"


def test_an_artist_entered_once_takes_the_latin_spelling_the_sleeve_gives() -> None:
    artists = _discogs_artists([{"id": 7, "name": "北の人", "anv": "Zed Marrowby"}])

    assert [artist.name for artist in artists] == ["Zed Marrowby"]


def test_the_catalogue_s_spelling_is_kept_beside_the_one_written() -> None:
    """Files tagged in the original script are still compared as this artist's."""
    artists = _discogs_artists([{"id": 7, "name": "北の人", "anv": "Zed Marrowby"}])

    assert [artist.credited_as for artist in artists] == ["北の人"]


def test_an_artist_with_no_latin_spelling_keeps_the_catalogue_s_name() -> None:
    artists = _discogs_artists([{"id": 7, "name": "北の人"}])

    assert [artist.name for artist in artists] == ["北の人"]


def test_an_ordinary_credit_is_mapped_as_it_was() -> None:
    """No equals sign, Latin names: the names, the sleeve spelling and the joins stand."""
    artists = _discogs_artists(
        [
            {"id": 7, "name": "Zéd Marrowby", "anv": "Zed Marrowby", "join": "E"},
            {"id": 8, "name": "The Glass Herons (2)"},
        ]
    )

    assert [(artist.name, artist.credited_as, artist.joined_by) for artist in artists] == [
        ("Zéd Marrowby", "Zed Marrowby", "E"),
        ("The Glass Herons", None, None),
    ]


def test_a_search_answer_is_named_the_way_the_release_will_be() -> None:
    """One string holds the credit and the title, each printed in two scripts."""
    answer = _map_discogs_search_result(
        {"id": 42, "title": "北の人 = Zed Marrowby - 北の風 = Brumel Tarvo - Live"}
    )

    assert answer.title == "Zed Marrowby - Brumel Tarvo - Live"


def test_an_ordinary_search_answer_is_left_as_it_is() -> None:
    answer = _map_discogs_search_result({"id": 42, "title": "Zed Marrowby - Um = One"})

    assert answer.title == "Zed Marrowby - Um = One"


def _stored_before_the_rule() -> ReleaseMetadata:
    """A release as it was mapped when both scripts were kept."""
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="42",
        title="北の風 = Brumel Tarvo",
        artists=(
            ArtistMetadata(
                name="北の人", external_ids={MetadataSources.DISCOGS: "7"}, joined_by="="
            ),
            ArtistMetadata(
                name="北の人",
                external_ids={MetadataSources.DISCOGS: "7"},
                credited_as="Zed Marrowby",
            ),
        ),
        tracks=(TrackMetadata(title="朝 = Olnic Reva", position=1),),
    )


def test_a_release_stored_before_the_rule_is_read_by_it() -> None:
    """A plan made around the release on record asks no catalogue to map it again."""
    release = release_in_latin_spelling(_stored_before_the_rule())

    assert release.title == "Brumel Tarvo"
    assert [track.title for track in release.tracks] == ["Olnic Reva"]
    assert written_credit(release.artists) == "Zed Marrowby"
    assert release.artists[0].credited_as == "北の人"


def test_reading_a_release_already_in_that_form_changes_nothing() -> None:
    once = release_in_latin_spelling(_stored_before_the_rule())

    assert release_in_latin_spelling(once) == once


def test_what_the_catalogue_maps_today_is_read_back_as_it_was_mapped() -> None:
    mapped = _map_discogs_release(
        {
            "id": 42,
            "title": "北の風 = Brumel Tarvo",
            "artists": [
                {"id": 7, "name": "北の人", "join": "="},
                {"id": 7, "name": "北の人", "anv": "Zed Marrowby", "join": "&"},
                {"id": 8, "name": "The Glass Herons"},
            ],
            "tracklist": [{"position": "1", "type_": "track", "title": "朝 = Olnic Reva"}],
        }
    )

    assert release_in_latin_spelling(mapped) == mapped


def test_another_catalogue_s_release_is_left_as_it_was_stored() -> None:
    """Its equals sign joins no translation, and its names are chosen elsewhere."""
    release = ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="x",
        title="北の風 = Brumel Tarvo",
        artists=(ArtistMetadata(name="北の人"),),
    )

    assert release_in_latin_spelling(release) == release
