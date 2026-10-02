"""The tracks a user adds to a release because the copy on disk holds them."""

from dataclasses import replace

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.extras import (
    ExtraTrack,
    next_free_position,
    renumbers_the_catalogue,
    with_extra_tracks,
    without_extra_tracks,
)


def _release(count: int, published: bool = False) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Luzes Dos Becos",
        artists=(ArtistMetadata(name="Tonzinho"),),
        tracks=tuple(
            TrackMetadata(
                title=f"Faixa {index}",
                position=index,
                position_on_medium=index,
                published_position=f"A{index}" if published else None,
                duration_ms=1000 * index,
            )
            for index in range(1, count + 1)
        ),
    )


def test_the_offered_position_is_the_one_after_the_release_s_last() -> None:
    """Ten listed, an eleventh on the disc, and no typing needed."""
    assert next_free_position(_release(10)) == 11
    assert next_free_position(_release(0)) == 1


def test_appending_leaves_every_catalogue_number_exactly_as_it_was() -> None:
    """Appending is the case that costs nothing."""
    release = _release(10)
    extras = (ExtraTrack(11, "Zunto Do Vorca-Zorca (Sobra De Plimba)"),)

    grown = with_extra_tracks(release, extras)

    assert [track.position for track in grown.tracks] == list(range(1, 12))
    assert [track.title for track in grown.tracks[:10]] == [f"Faixa {n}" for n in range(1, 11)]
    assert grown.tracks[10].title == "Zunto Do Vorca-Zorca (Sobra De Plimba)"
    assert renumbers_the_catalogue(release, extras) is False


def test_inserting_in_the_middle_moves_everything_after_it_down_one() -> None:
    """*Insert at N* and *renumber the rest* are one operation, not two."""
    release = _release(10)
    extras = (ExtraTrack(5, "Only On This Copy"),)

    grown = with_extra_tracks(release, extras)

    assert [track.position for track in grown.tracks] == list(range(1, 12))
    assert grown.tracks[4].title == "Only On This Copy"
    # The four before it are untouched; the six after carry their old titles at
    # a number one higher, which is the cost of inserting.
    assert [track.title for track in grown.tracks[:4]] == [f"Faixa {n}" for n in range(1, 5)]
    assert [track.title for track in grown.tracks[5:]] == [f"Faixa {n}" for n in range(5, 11)]
    assert renumbers_the_catalogue(release, extras) is True


def test_what_the_source_printed_is_never_shifted() -> None:
    """`published_position` is a fact about the source, and stays true."""
    release = _release(4, published=True)

    grown = with_extra_tracks(release, (ExtraTrack(2, "Zorvel"),))

    printed = {track.title: track.published_position for track in grown.tracks}
    assert printed["Faixa 2"] == "A2", "the source still printed A2 for it"
    assert printed["Zorvel"] is None, "no source printed anything for an added track"
    assert [track.position for track in grown.tracks] == [1, 2, 3, 4, 5]


def test_each_added_track_holds_the_number_it_was_given_in_the_finished_list() -> None:
    """The rule when there is more than one: the given number is the *final* one.

    Two tracks added to a three-track release, at 1 and at 4. Read against the
    catalogue's own numbering, `4` would mean *after all three*; read against the
    finished list, it means *fourth*, and the third catalogue track is pushed
    past it. The second reading is the one the screen can keep its promise
    about, because the screen shows the finished list.
    """
    release = _release(3)

    grown = with_extra_tracks(
        release, (ExtraTrack(4, "Plimba Longe"), ExtraTrack(1, "Plimba Cedo"))
    )

    assert [track.position for track in grown.tracks] == [1, 2, 3, 4, 5]
    numbered = {track.position: track.title for track in grown.tracks}
    assert numbered[1] == "Plimba Cedo"
    assert numbered[4] == "Plimba Longe"


def test_a_release_with_no_added_track_comes_back_untouched() -> None:
    """The ordinary album pays nothing for this feature existing."""
    release = _release(10)
    assert with_extra_tracks(release, ()) is release


def test_growing_a_release_that_is_already_grown_adds_nothing_twice() -> None:
    """The defect `added_by_user` exists to make impossible.

    A grown release is handed around and re-planned, and the next re-plan grows
    it again. Unless the track says of itself that it was added, that second
    pass inserts it a second time, and the album's own numbering then reports
    that the addition renumbered the catalogue.
    """
    release = _release(10)
    extras = (ExtraTrack(11, "Zunto Do Vorca-Zorca (Sobra De Plimba)"),)

    once = with_extra_tracks(release, extras)
    twice = with_extra_tracks(once, extras)

    assert [track.position for track in twice.tracks] == list(range(1, 12))
    assert [track.title for track in twice.tracks] == [track.title for track in once.tracks]
    assert sum(track.added_by_user for track in twice.tracks) == 1
    assert renumbers_the_catalogue(once, extras) is False, "appending, read on a grown release"


def test_taking_the_added_tracks_out_gives_the_catalogue_its_numbering_back() -> None:
    """The inverse is exact, because the track says which one it is."""
    release = _release(10)
    grown = with_extra_tracks(release, (ExtraTrack(5, "Zorvel"),))

    back = without_extra_tracks(grown)

    assert [track.position for track in back.tracks] == list(range(1, 11))
    assert [track.title for track in back.tracks] == [f"Faixa {n}" for n in range(1, 11)]
    assert not any(track.added_by_user for track in back.tracks)
    assert without_extra_tracks(release) is release, "an untouched release is not rebuilt"


def test_a_grown_release_that_lost_its_marks_is_not_grown_again() -> None:
    """A release read back from the cache has lost the mark, and is still not regrown.

    A grown release goes into `metadata_releases` and comes back without the
    mark that says which track was added — so it looks exactly like a catalogue
    that published one more track, and growing it again would add a second
    copy: the audio at 11 and a phantom at 12 no file could hold.
    """
    release = _release(10)
    extras = (ExtraTrack(11, "Zunto Do Vorca-Zorca (Sobra De Plimba)"),)
    cached = replace(
        with_extra_tracks(release, extras),
        tracks=tuple(
            replace(track, added_by_user=False)
            for track in with_extra_tracks(release, extras).tracks
        ),
    )
    assert not any(track.added_by_user for track in cached.tracks), "the marks are gone"

    grown = with_extra_tracks(cached, extras)

    assert [track.position for track in grown.tracks] == list(range(1, 12))
    assert sum(track.title == extras[0].title for track in grown.tracks) == 1


def test_a_catalogue_track_is_never_removed_by_a_position_an_added_track_also_claims() -> None:
    """Why the title is checked and not only the position.

    Inserting in the middle means the given number names a position the
    catalogue also published. On a clean release, position 5 is the catalogue's
    own `Faixa 5`; on a grown one it is the added track. Removing by position
    alone would delete a catalogue track.
    """
    release = _release(10)
    extras = (ExtraTrack(5, "Only On This Copy"),)

    grown = with_extra_tracks(release, extras)

    titles = [track.title for track in grown.tracks]
    assert titles.count("Faixa 5") == 1, "the catalogue's fifth is still here"
    assert titles.index("Only On This Copy") == 4
    assert without_extra_tracks(release, {5: "Only On This Copy"}) is release
