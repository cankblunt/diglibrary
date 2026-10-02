"""Two entries indexed inside one track are one recording — and the twin that isn't.

The dangerous half of this is not the merge, it is the notation that looks
identical and means the opposite: `1.01` and `1.02` are disc one's first two
tracks, and folding those would take a 28-track album down to 14. Both are
proven here, because a rule that only ever fires correctly on the case it was
written for is a rule nobody has tested.
"""

from dataclasses import replace

from diglibrary.application.contracts import MetadataSources, ReleaseMetadata, TrackMetadata
from diglibrary.library.medleys import merge_indexed_tracks


def _release(*positions_and_titles: tuple[str, str], length: int | None = None) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="An album",
        artists=(),
        tracks=tuple(
            TrackMetadata(
                title=title,
                position=number,
                published_position=position,
                duration_ms=length,
            )
            for number, (position, title) in enumerate(positions_and_titles, start=1)
        ),
    )


INDEXED_SIDE = (
    ("A1", "Zorvapé"),
    ("A2", "Plimba Zuleca"),
    ("A3", "Vorco De Zunir"),
    ("A4", "Nani Zere Plohau"),
    ("A5.1", "Zoidera"),
    ("A5.2", "Quelmo No Vurco"),
    ("B1", "De Zolho Na Plinta"),
    ("B2", "Zassim Vreto - Plasa Zanca"),
    ("B3", "Zantiga Plesma Vurteza"),
    ("B4", "Zarecendo Pliqui"),
    ("B5", "Daqui Da Zilha"),
)


def test_a_side_that_runs_two_songs_together_is_one_track() -> None:
    """Eleven entries against ten files, where two entries are one file.

    Without the merge the album stays in review at 10/11, with one track
    reported as having no file although its song is inside the file above it.
    """
    merged = merge_indexed_tracks(_release(*INDEXED_SIDE), audio_files=10)

    assert len(merged.tracks) == 10
    assert merged.tracks[4].title == "Zoidera / Quelmo No Vurco"
    # Renumbered, because `position` counts what a copy of this album holds.
    assert [track.position for track in merged.tracks] == list(range(1, 11))
    assert merged.tracks[5].title == "De Zolho Na Plinta"


def test_disc_and_track_is_never_folded() -> None:
    """Two discs of 14 tracks are 28 tracks. Folding them would halve the album.

    The shape is what separates them: a disc's numbering covers every position,
    an index inside a track is a few lines among plain ones.
    """
    positions = [
        (f"{disc}.{track:02d}", f"Song {disc}-{track}") for disc in (1, 2) for track in range(1, 15)
    ]
    kept = merge_indexed_tracks(_release(*positions), audio_files=28)

    assert len(kept.tracks) == 28
    assert kept.tracks[0].title == "Song 1-1"


def test_a_copy_that_holds_them_separately_is_left_alone() -> None:
    """A reissue that splits the medley is a real copy of a real album.

    Merging there would leave a spare file with nowhere to go, so the file
    count is the guard — and it is why this cannot live in the mapper.
    """
    kept = merge_indexed_tracks(_release(*INDEXED_SIDE), audio_files=11)

    assert len(kept.tracks) == 11
    assert kept.tracks[4].title == "Zoidera"


def test_the_length_is_the_sum_only_when_the_source_gave_every_part() -> None:
    """The file holds both songs, so it is as long as both.

    A partial sum is a wrong number rather than an incomplete one, and the
    matcher weighs length against the file — so a missing part means no length
    at all rather than half of one.
    """
    whole = merge_indexed_tracks(_release(*INDEXED_SIDE, length=120_000), audio_files=10)
    assert whole.tracks[4].duration_ms == 240_000

    half = _release(*INDEXED_SIDE, length=120_000)
    half = replace(
        half,
        tracks=tuple(
            replace(track, duration_ms=None) if track.position == 6 else track
            for track in half.tracks
        ),
    )
    assert merge_indexed_tracks(half, audio_files=10).tracks[4].duration_ms is None


def test_an_ordinary_album_is_returned_untouched() -> None:
    """Which is nearly every album: few tracklists carry an index at all."""
    plain = _release(("A1", "One"), ("A2", "Two"), ("B1", "Three"))
    assert merge_indexed_tracks(plain, audio_files=3) is plain
    assert merge_indexed_tracks(plain, audio_files=2) is plain


LETTERED_THROUGHOUT = tuple(
    (f"{number}{letter}", f"Song {number}{letter}")
    for number in range(1, 9)
    for letter in ("a", "b")
)
"""An index written with a letter, which the dotted form's shape rule cannot read.

Lettered indexes do not separate the way the dotted form does: a release that
letters every position can be a merge, and so can one that letters a single
pair. There is no shape rule here to find.
"""


def test_a_lettered_index_is_never_folded_on_the_strength_of_its_shape() -> None:
    """The shape says nothing, so a count has to say it instead.

    Discogs' position is free text filled in by people, so no pattern closes it
    and every extension invites one more form. Left to itself this rule folds
    nothing here.
    """
    release = _release(*LETTERED_THROUGHOUT)

    assert merge_indexed_tracks(release, audio_files=8).tracks == release.tracks
    assert merge_indexed_tracks(release, audio_files=16).tracks == release.tracks


def test_the_witness_count_is_what_authorizes_the_fold() -> None:
    """A witness's track count confirms that two entries are one track.

    A count is a verdict and not a catalogue: no character of what the witness
    published reaches a name or a tag, and every title still comes from the
    source in use. What the count settles is a question about the shape of that
    source's own answer.
    """
    release = _release(*LETTERED_THROUGHOUT)

    folded = merge_indexed_tracks(release, audio_files=8, album_tracks=8)

    assert len(folded.tracks) == 8, "the witness counted eight, and eight is the folded reading"
    assert folded.tracks[0].title == "Song 1a / Song 1b"
    assert [track.position for track in folded.tracks] == list(range(1, 9))


def test_a_witness_that_counts_what_is_printed_refuses_the_fold() -> None:
    """The count answers in both directions, and the refusal is the valuable half.

    A copy of an album can be incomplete, so the file count alone cannot tell a
    medley from a missing track: nine files against ten entries folds today on
    nothing more than the disagreement. A witness saying the album has ten is
    what stops that being a guess.
    """
    release = _release(*INDEXED_SIDE)

    kept = merge_indexed_tracks(release, audio_files=9, album_tracks=len(release.tracks))

    assert kept.tracks == release.tracks, (
        "the album has as many tracks as the source printed, so this copy is "
        "missing one — folding would hide that"
    )


def test_a_disc_and_track_position_is_still_never_folded_by_a_count() -> None:
    """`1-1` is commonly disc one, track one.

    A release may also list `A1` beside `A1-1` and `A1-2`, each with a length of
    its own. Whether those children are joined or discarded is undecided, so
    nothing here answers it.
    """
    release = _release(("1-1", "First"), ("1-2", "Second"), ("2-1", "Third"), ("2-2", "Fourth"))

    assert merge_indexed_tracks(release, audio_files=4, album_tracks=2).tracks == release.tracks
