"""A file holding one side of `original = translation` is that track.

The catalogue prints both spellings and a file's tag holds one. Compared
against the two at once, the side that agrees letter for letter scored under
every bar, the candidate was vetoed for having no title in common, and a
release pasted by hand paired none of the files.
"""

from __future__ import annotations

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.matching import (
    TITLE_MATCH_THRESHOLD,
    _artist_similarity,
    _similarity,
    title_similarity,
)
from diglibrary.library.naming import is_various


def _release(*credits: tuple[str, ...], artists: tuple[ArtistMetadata, ...]) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="42",
        title="Brumel Tarvo",
        artists=artists,
        tracks=tuple(
            TrackMetadata(
                title=f"Track {position}",
                position=position,
                artists=tuple(ArtistMetadata(name=name) for name in names),
            )
            for position, names in enumerate(credits, start=1)
        ),
    )


def test_a_title_is_compared_with_each_side_of_the_equals_sign() -> None:
    assert _similarity("Brumel Tarvo", "北の風 = Brumel Tarvo") == 1.0
    assert _similarity("北の風", "Brumel Tarvo = 北の風") == 1.0
    assert title_similarity("brumel tarvo", "北の風 = Brumel Tarvo") >= TITLE_MATCH_THRESHOLD


def test_both_sides_written_in_the_other_order_are_one_name() -> None:
    assert _similarity("Brumel Tarvo = 北の風", "北の風 = Brumel Tarvo") == 1.0


def test_a_different_title_is_not_lifted_by_the_split() -> None:
    whole = _similarity("Olnic Reva", "北の風 = Brumel Tarvo")
    assert whole < TITLE_MATCH_THRESHOLD
    assert _similarity("Olnic Reva", "Brumel Tarvo") < TITLE_MATCH_THRESHOLD


def test_a_name_with_no_equals_sign_scores_as_it_did() -> None:
    assert _similarity("Brumel Tarvo", "Brumel Tarva") < 1.0
    assert _similarity("Brumel Tarvo", "brumel tarvo") == 1.0


def test_the_spelling_the_sleeve_printed_answers_for_the_artist() -> None:
    """The catalogue names the artist in one script and the sleeve in another."""
    release = _release(
        artists=(
            ArtistMetadata(name="ريح الشمال", joined_by="="),
            ArtistMetadata(name="ريح الشمال", credited_as="Zed Marrowby"),
        )
    )

    assert _artist_similarity("Zed Marrowby", release) == 1.0
    assert _artist_similarity("Zed Marowby", release) > 0.9


def test_files_tagged_in_the_original_script_are_still_the_artist_s() -> None:
    """The name written is Latin; the catalogue's own spelling is still compared."""
    release = _release(artists=(ArtistMetadata(name="Zed Marrowby", credited_as="ريح الشمال"),))

    assert _artist_similarity("ريح الشمال", release) == 1.0
    assert _artist_similarity("Zed Marrowby", release) == 1.0


def test_the_catalogue_s_own_name_still_answers_for_the_artist() -> None:
    release = _release(artists=(ArtistMetadata(name="Zed Marrowby", credited_as="Z. M."),))

    assert _artist_similarity("Zed Marrowby", release) == 1.0


def test_a_credit_the_album_s_artist_leads_in_one_value_is_not_a_compilation() -> None:
    """Read from tags, `Lead, Guest` is one name and shares none with the album."""
    release = _release(
        ("Zed Marrowby, Ojel Varn",),
        ("Zed Marrowby, Pim Tolle",),
        ("Zed Marrowby & Ojel Varn",),
        artists=(ArtistMetadata(name="Zed Marrowby"),),
    )

    assert not is_various(release)


def test_one_track_led_by_the_album_s_artist_keeps_the_album_out() -> None:
    release = _release(
        ("Zed Marrowby, Ojel Varn",),
        ("Pim Tolle",),
        ("Ojel Varn",),
        artists=(ArtistMetadata(name="Zed Marrowby"),),
    )

    assert not is_various(release)


def test_tracks_all_by_other_people_are_still_a_compilation() -> None:
    release = _release(
        ("Ojel Varn",),
        ("Pim Tolle",),
        ("Zed Marrowbyson, Ojel Varn",),
        artists=(ArtistMetadata(name="Zed Marrowby"),),
    )

    assert is_various(release)
