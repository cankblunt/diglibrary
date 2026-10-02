"""Unit tests for album identification scoring and cross-source reconciliation."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import AudioProperties
from diglibrary.library.matching import (
    DURATION_TOLERANCE_MS,
    TITLE_MATCH_THRESHOLD,
    AlbumMatcher,
    MatchSignals,
    _agreeing_titles,
    _duration_alignment,
    _local_titles_for_release,
    _settled_by_title,
    album_titles,
    align_tracks,
    contested_stems,
    explain_evidence,
    reconcile,
    title_similarity,
    track_title_similarity,
)
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.planner import _partial_warnings


def test_durations_decide_even_when_the_folder_name_is_wrong() -> None:
    """A misleading name must not beat the audio."""
    unit = _unit("Unknown Artist - Untitled Album", (180_000, 240_000, 200_000))
    correct = _release("The Real Album", (180_500, 239_000, 201_000))
    wrong_but_well_named = _release("Unknown Artist - Untitled Album", (95_000, 300_000, 412_000))

    ranked = AlbumMatcher().rank(unit, (wrong_but_well_named, correct))

    assert ranked[0].release is correct
    assert ranked[0].confidence > 0.85
    assert "3 of 3 track lengths agreed" in ranked[0].explanation


def test_a_source_without_track_lengths_can_never_be_applied_automatically() -> None:
    """Text-only evidence is a guess, and a guess is reviewed, not written."""
    unit = _unit("Zoé of Lanterns - Quarrél", (180_000, 240_000))
    textless = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Zoé of Lanterns - Quarrél",
        artists=(),
    )

    candidate = AlbumMatcher().rank(unit, (textless,))[0]

    assert candidate.confidence <= 0.55
    assert "no track lengths published" in candidate.explanation.lower()


def test_track_order_on_disk_does_not_affect_the_match() -> None:
    """File order is part of the naming this project does not trust."""
    matcher = AlbumMatcher()
    release = _release("Album", (180_000, 240_000, 300_000))

    in_order = matcher.rank(_unit("Album", (180_000, 240_000, 300_000)), (release,))[0]
    shuffled = matcher.rank(_unit("Album", (300_000, 180_000, 240_000)), (release,))[0]

    assert in_order.confidence == shuffled.confidence


def test_a_missing_track_lowers_confidence_without_discarding_the_match() -> None:
    """An incomplete rip should still find its release, but not silently pass."""
    unit = _unit("Album", (180_000, 240_000))
    release = _release("Album", (180_000, 240_000, 300_000))

    candidate = AlbumMatcher().rank(unit, (release,))[0]

    assert candidate.confidence < 1.0
    assert "track count differs (2 local, 3 on the release)" in candidate.explanation.lower()


def test_an_unreadable_track_withholds_duration_evidence() -> None:
    """A partial duration list would look like a bad match against the right release."""
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=(
            _file("01.flac", 180_000),
            AudioFileFacts(
                path=Path("/music/Album/02.flac"),
                content_signature="broken",
                file_size_bytes=10,
                modified_at=datetime(2001, 2, 3, tzinfo=UTC),
                properties=None,
            ),
        ),
    )

    candidate = AlbumMatcher().rank(unit, (_release("Album", (180_000, 240_000)),))[0]

    assert unit.track_durations_ms is None
    assert candidate.confidence <= 0.55


def test_reconcile_keeps_the_primary_answer_but_charges_for_disagreement() -> None:
    """A source whose best answer is a different album costs confidence."""
    unit = _unit("Zoé of Lanterns", (180_000, 240_000))
    discogs = AlbumMatcher().rank(unit, (_release("Quarrél", (180_000, 240_000), year=1955),))[0]
    musicbrainz = AlbumMatcher().rank(
        unit, (_release("Ballad Of The Marrow", (180_000, 240_000), year=1955),)
    )[0]

    settled = reconcile(discogs, ((MetadataSources.MUSICBRAINZ, musicbrainz),))

    assert settled is not None
    assert settled.release.released_on == date(1955, 1, 1)
    assert settled.confidence == round(discogs.confidence - 0.2, 4)
    assert settled.signals.contradicted_by == (MetadataSources.MUSICBRAINZ,)
    assert "Contradicted by musicbrainz." in settled.explanation


def test_reconcile_does_not_mistake_another_pressing_for_another_album() -> None:
    """A reissue of an older record is the same album, not a dispute.

    When the other source's best answer is simply a different pressing, a
    penalty would push a duration-proven match into review. The album's own
    year is settled from the master anyway.
    """
    unit = _unit("Zoé of Lanterns", (180_000, 240_000))
    discogs = AlbumMatcher().rank(unit, (_release("Quarrél", (180_000, 240_000), year=1955),))[0]
    musicbrainz = AlbumMatcher().rank(unit, (_release("Quarrél", (180_000, 240_000), year=1954),))[
        0
    ]

    settled = reconcile(discogs, ((MetadataSources.MUSICBRAINZ, musicbrainz),))

    assert settled is discogs, "same title, different pressing year: no penalty"


def test_reconcile_leaves_an_agreeing_match_untouched() -> None:
    """Corroboration must not cost anything."""
    unit = _unit("Album", (180_000, 240_000))
    discogs = AlbumMatcher().rank(unit, (_release("Album", (180_000, 240_000), year=1970),))[0]
    musicbrainz = AlbumMatcher().rank(unit, (_release("Album", (180_000, 240_000), year=1970),))[0]

    settled = reconcile(discogs, ((MetadataSources.MUSICBRAINZ, musicbrainz),))

    assert settled == discogs


def test_reconcile_without_a_primary_candidate_is_not_an_error() -> None:
    """A source that returned nothing must not crash the workflow."""
    assert reconcile(None, ()) is None


def _unit(folder_name: str, durations: tuple[int, ...]) -> AlbumUnit:
    files = tuple(
        _file(f"{index:02d}.flac", duration) for index, duration in enumerate(durations, start=1)
    )
    return AlbumUnit(
        folder_path=Path("/music") / folder_name,
        unit_signature="unit",
        audio_files=files,
    )


def _file(name: str, duration_ms: int) -> AudioFileFacts:
    return AudioFileFacts(
        path=Path("/music/Album") / name,
        content_signature=f"signature-{name}-{duration_ms}",
        file_size_bytes=1_000,
        modified_at=datetime(2001, 2, 3, tzinfo=UTC),
        properties=AudioProperties(
            codec="flac", duration_ms=duration_ms, sample_rate=44_100, channels=2
        ),
    )


def _release(title: str, durations: tuple[int, ...], year: int | None = None) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=f"{title}-{year}",
        title=title,
        artists=(ArtistMetadata(name="Artist"),),
        tracks=tuple(
            TrackMetadata(title=f"Track {index}", position=index, duration_ms=duration)
            for index, duration in enumerate(durations, start=1)
        ),
        released_on=date(year, 1, 1) if year else None,
    )


def test_alignment_uses_the_existing_order_when_the_audio_confirms_it() -> None:
    """When file order already agrees with the release, that is the safest reading."""
    unit = _unit("Album", (180_000, 240_000, 300_000))
    release = _release("Album", (180_500, 239_500, 300_200))

    alignment = align_tracks(unit, release)

    assert alignment.method == "positional"
    assert alignment.is_usable
    assert [assignment.track_number for assignment in alignment.assignments] == [1, 2, 3]


def test_alignment_recovers_from_a_wrong_file_order() -> None:
    """Files named in the wrong order still reach their real tracks."""
    unit = _unit("Album", (300_000, 180_000, 240_000))
    release = _release("Album", (180_000, 240_000, 300_000))

    alignment = align_tracks(unit, release)

    assert alignment.method == "duration"
    assert alignment.is_usable
    numbers = [assignment.track_number for assignment in alignment.assignments]
    assert numbers == [3, 1, 2]


def test_two_tracks_of_the_same_length_make_the_alignment_ambiguous() -> None:
    """A file two tracks could equally claim is never renamed on a guess."""
    unit = _unit("Album", (200_000, 200_000, 400_000))
    release = _release("Album", (200_000, 200_100, 500_000))

    alignment = align_tracks(unit, release)

    assert alignment.is_ambiguous
    assert alignment.is_usable is False


def test_an_extra_local_file_is_reported_rather_than_forced() -> None:
    """A bonus track with no counterpart leaves the alignment unusable, not wrong."""
    unit = _unit("Album", (180_000, 240_000, 999_000))
    release = _release("Album", (180_000, 240_000))

    alignment = align_tracks(unit, release)

    assert len(alignment.assignments) == 2
    assert len(alignment.unmatched_files) == 1
    assert alignment.is_usable is False


def test_disc_numbers_come_from_the_release_when_it_publishes_them() -> None:
    """A two-disc release numbers its tracks per disc, not across the whole album."""
    unit = _unit("Anthology", (180_000, 240_000))
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="multi",
        title="Anthology",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(
            TrackMetadata(
                title="One", position=1, medium_number=1, position_on_medium=1, duration_ms=180_000
            ),
            TrackMetadata(
                title="Two", position=2, medium_number=2, position_on_medium=1, duration_ms=240_000
            ),
        ),
    )

    alignment = align_tracks(unit, release)

    assert [(a.disc_number, a.track_number) for a in alignment.assignments] == [(1, 1), (2, 1)]


def test_a_sleeve_time_typed_loosely_does_not_throw_out_a_correct_album() -> None:
    """One sleeve time out by just over three seconds must not block the album.

    Under a strict three-second rule that single track blocks the whole album.
    The order corroborates the match here, which is what earns the wider window.
    """
    unit = _unit("Zed Marrowby - Quarreller", (222_000, 238_000, 246_000, 294_000))
    release = _release("Quarreller", (222_200, 238_300, 246_800, 297_200))

    alignment = align_tracks(unit, release)
    candidate = AlbumMatcher().rank(unit, (release,))[0]

    assert alignment.is_usable
    assert alignment.method == "positional"
    assert candidate.confidence >= 0.90


def test_the_wider_window_is_never_used_to_pair_files_freely() -> None:
    """Without the order corroborating, pairing falls back to the strict window."""
    unit = _unit("Album", (300_000, 100_000))
    release = _release("Album", (100_010, 309_000))

    alignment = align_tracks(unit, release)

    assert alignment.method == "duration"
    assert not alignment.is_usable, "9 s apart must not pair without corroboration"


def _titled_release(titles_durations: tuple[tuple[str, int | None], ...]) -> ReleaseMetadata:
    """A release whose tracks carry real titles, for the title-evidence tests."""
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="titled",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=tuple(
            TrackMetadata(title=title, position=index, duration_ms=duration)
            for index, (title, duration) in enumerate(titles_durations, start=1)
        ),
    )


def test_titles_break_a_duration_tie_instead_of_blocking_the_album() -> None:
    """Two tracks of one length are told apart by name.

    The release lists its tracks in another order, so the positional reading
    fails and pairing falls to durations, where the two near-identical
    lengths collide.
    """
    unit = _unit("Album", (180_000, 181_000, 300_000))
    release = _titled_release((("Gamma", 300_000), ("Alpha", 180_500), ("Beta", 180_600)))

    blocked = align_tracks(unit, release)
    resolved = align_tracks(unit, release, local_titles=("Alpha", "Beta", "Gamma"))

    assert blocked.is_ambiguous, "without titles the tie still blocks"
    assert resolved.is_usable
    assert resolved.method == "titled", "names that place every file settle it before durations"
    assert [a.track.title for a in resolved.assignments] == ["Alpha", "Beta", "Gamma"]


def test_a_tie_is_still_broken_by_title_when_one_file_has_no_name_to_offer() -> None:
    """The duration tie-break survives a file the title pass cannot place.

    One nameless file is enough for the whole-album title pairing to decline, so
    this is the path that still has to work: durations pair what they can, and a
    name settles the collision between the two near-equal lengths.
    """
    unit = _unit("Album", (180_000, 181_000, 300_000))
    release = _titled_release((("Gamma", 300_000), ("Alpha", 180_500), ("Beta", 180_600)))

    resolved = align_tracks(unit, release, local_titles=("Alpha", "Beta", ""))

    assert resolved.is_usable
    assert resolved.method == "duration+title"
    assert [a.track.title for a in resolved.assignments] == ["Alpha", "Beta", "Gamma"]


def test_titles_that_cannot_separate_the_tie_leave_it_ambiguous() -> None:
    """A tie the names cannot break is still a tie; nothing is guessed."""
    unit = _unit("Album", (180_000, 181_000, 300_000))
    release = _titled_release((("Gamma", 300_000), ("Take 1", 180_500), ("Take 1", 180_600)))

    alignment = align_tracks(unit, release, local_titles=("Take 1", "Take 1", "Gamma"))

    assert alignment.is_ambiguous


def test_an_exact_title_rescues_a_file_the_sleeve_time_lost() -> None:
    """The catalogue publishes a length a minute shorter than the audio lasts.

    Every pressing may copy the same sleeve typo, so the duration can never
    match, but the file carries the track's exact name, and there is exactly
    one orphan on each side.
    """
    unit = _unit("Album", (121_000, 204_000, 164_000))
    release = _titled_release(
        (("Dawnlight", 121_000), ("Go And See A Spíre", 143_000), ("Dúskfall", 164_000))
    )

    strict = align_tracks(unit, release)
    rescued = align_tracks(
        unit, release, local_titles=("Dawnlight", "Go And See A Spíre", "Dúskfall")
    )

    assert not strict.is_usable
    assert rescued.is_usable
    assert rescued.method == "titled"
    orphan = next(a for a in rescued.assignments if a.track.title == "Go And See A Spíre")
    assert orphan.duration_delta_ms == 61_000, "the delta is recorded, not hidden"


def test_the_sleeve_typo_is_still_rescued_when_a_file_has_no_name() -> None:
    """The same sleeve typo, on the duration path, because one file is nameless."""
    unit = _unit("Album", (121_000, 204_000, 164_000))
    release = _titled_release(
        (("Dawnlight", 121_000), ("Go And See A Spíre", 143_000), ("Dúskfall", 164_000))
    )

    rescued = align_tracks(unit, release, local_titles=("", "Go And See A Spíre", "Dúskfall"))

    assert rescued.is_usable
    assert rescued.method == "duration+title"
    orphan = next(a for a in rescued.assignments if a.track.title == "Go And See A Spíre")
    assert orphan.duration_delta_ms == 61_000


def test_equally_confident_pressings_are_settled_by_the_year_written_on_the_folder() -> None:
    """Two pressings that score alike are ordered by the year the folder carries.

    Two pressings of one album score identically, same title and same lengths,
    so which one wins would come down to the order the source returned them
    in, and that changes between scans. The folder says 2006, so the 2006
    pressing wins over the original, and reversing the input does not reverse
    the answer.
    """
    unit = _unit("Album", (180_000, 240_000))
    original = _release("Album", (180_000, 240_000), year=1974)
    reissue = _release("Album", (180_000, 240_000), year=2006)
    matcher = AlbumMatcher()

    forward = matcher.rank(unit, (original, reissue), hint_year=2006)
    reversed_input = matcher.rank(unit, (reissue, original), hint_year=2006)

    assert forward[0].release.source_release_id == reissue.source_release_id
    assert [c.release.source_release_id for c in forward] == [
        c.release.source_release_id for c in reversed_input
    ], "the answer cannot depend on the order the source returned"


def test_with_no_year_written_the_earliest_pressing_wins_the_tie() -> None:
    """Nothing on disk to prefer a reissue, so the album beats the reissue."""
    unit = _unit("Album", (180_000, 240_000))
    original = _release("Album", (180_000, 240_000), year=1974)
    reissue = _release("Album", (180_000, 240_000), year=2006)

    ranked = AlbumMatcher().rank(unit, (reissue, original))

    assert ranked[0].release.source_release_id == original.source_release_id


def test_a_wrong_published_length_does_not_move_a_file_onto_its_neighbour() -> None:
    """The title decides and the duration confirms.

    The catalogue publishes a length for the second track that is several
    seconds longer than the audio. The only length inside the window is the
    next track's, so pairing by length would write a name onto the wrong audio,
    orphan the file that carries that name, and report the second track as
    having no file. Every name here agrees exactly, so nothing about the album
    is in doubt except the catalogue's minutes.
    """
    unit = _unit("Album", (245_200, 166_300, 163_200))
    release = _titled_release(
        (("Lanternia", 246_000), ("Big Marrow", 174_000), ("A Quarrelling Of Zedd", 164_000))
    )

    alignment = align_tracks(
        unit, release, local_titles=("Lanternia", "Big Marrow", "A Quarrelling Of Zedd")
    )

    assert alignment.is_usable
    assert [a.track.title for a in alignment.assignments] == [
        "Lanternia",
        "Big Marrow",
        "A Quarrelling Of Zedd",
    ]
    second = alignment.assignments[1]
    assert second.duration_delta_ms == 7_700, "the disagreement is reported, not hidden"


def test_a_length_that_fits_the_wrong_track_is_refused_when_the_name_says_otherwise() -> None:
    """The same guard on the duration path, where the title pass cannot run.

    A release with one more track than the folder holds means no whole-album
    title pairing is possible, so the file whose published length landed on its
    neighbour has to be refused where the pairing is made. Refusing leaves both
    ends loose; the rescue then puts them together by name.
    """
    unit = _unit("Album", (245_200, 166_300, 163_200))
    release = _titled_release(
        (
            ("Lanternia", 246_000),
            ("Big Marrow", 174_000),
            ("A Quarrelling Of Zedd", 164_000),
            ("Bônus Track", 190_000),
        )
    )

    alignment = align_tracks(
        unit, release, local_titles=("Lanternia", "Big Marrow", "A Quarrelling Of Zedd")
    )

    paired = {a.file.path.name: a.track.title for a in alignment.assignments}
    assert paired["02.flac"] == "Big Marrow", "the name outranks a length that fits another track"
    assert paired["03.flac"] == "A Quarrelling Of Zedd"
    assert [track.title for track in alignment.unmatched_tracks] == ["Bônus Track"]


def test_a_file_with_no_recognisable_name_vetoes_nothing() -> None:
    """`Track 03` names no track, so it cannot refuse the length that fits."""
    unit = _unit("Album", (245_200, 163_500))
    release = _titled_release((("Lanternia", 246_000), ("A Quarrelling Of Zedd", 164_000)))

    alignment = align_tracks(unit, release, local_titles=("Track 01", "Track 02"))

    assert alignment.is_usable
    assert [a.track.title for a in alignment.assignments] == ["Lanternia", "A Quarrelling Of Zedd"]


def test_a_rescue_is_refused_when_the_title_is_not_unequivocal() -> None:
    """Two leftover tracks with near-identical names rescue nothing."""
    unit = _unit("Album", (121_000, 204_000))
    release = _titled_release((("Dawnlight", 121_000), ("Intro (Reprise)", 143_000)))

    alignment = align_tracks(unit, release, local_titles=("Dawnlight", "Something Else Entirely"))

    assert not alignment.is_usable
    assert len(alignment.unmatched_files) == 1


def test_a_durationless_release_is_recognized_by_its_titles() -> None:
    """No lengths published, and the titles agree one to one."""
    unit = _unit("Album", (180_000, 240_000, 300_000))
    release = _titled_release((("Quarreler", None), ("Zedyman", None), ("Marrow Baby", None)))
    titles = ("Quarreler", "Zedyman", "Marrow Baby")

    candidate = AlbumMatcher().rank(unit, (release,), local_titles=titles)[0]
    alignment = align_tracks(unit, release, local_titles=titles)

    assert candidate.confidence >= 0.90, "full title agreement is per-track evidence"
    assert "track titles matched" in candidate.explanation
    assert alignment.is_usable
    ordered = sorted(alignment.assignments, key=lambda a: a.track.position or 0)
    assert [a.track.title for a in ordered] == list(titles)


def test_titles_decide_even_when_durations_disagree() -> None:
    """Duration only confirms.

    Identical titles with different lengths may well be a different edit of
    the same album, and that risk is accepted: disagreement is ignored rather
    than charged. What still guards the live-versus-studio case is the edition
    marker in the text itself, tested below.
    """
    unit = _unit("Album", (180_000, 240_000))
    release = _titled_release((("Alpha", 500_000), ("Beta", 600_000)))

    candidate = AlbumMatcher().rank(unit, (release,), local_titles=("Alpha", "Beta"))[0]

    assert candidate.confidence >= 0.9, "text decides; disagreement costs nothing"


def test_no_title_in_agreement_is_a_veto() -> None:
    """Coincidental lengths cannot carry an album whose names all miss.

    An unrelated record can score on a folder because some of its published
    lengths fall inside the window by chance, with none of the titles agreeing.
    """
    unit = _unit("Zoé Marrowby - Quarrel and Dance", (180_000, 240_000, 200_000, 210_000))
    coincidence = _titled_release(
        (
            ("Count The Lanterns", 180_500),
            ("In The Presence", 500_000),
            ("Somebody Needs Marrow", 620_000),
            ("Out Of Great Quarrels", 740_000),
        )
    )

    candidate = AlbumMatcher().rank(
        unit,
        (coincidence,),
        local_titles=(
            "Hallo Zoélle",
            "Quarrel of the Isle",
            "Mélody of the Hen",
            "There Goes Zedd",
        ),
    )[0]

    assert candidate.confidence <= 0.15, "not one title met: this is not the album"


def test_the_veto_lifts_when_durations_agree_broadly() -> None:
    """Foreign or garbled local names measure nothing; the audio still speaks."""
    unit = _unit("Album", (180_000, 240_000))
    release = _titled_release((("Совсем другое название", 180_200), ("Ещё одно", 240_300)))

    candidate = AlbumMatcher().rank(unit, (release,), local_titles=("Track 1", "Track 2"))[0]

    assert candidate.confidence > 0.15, "full duration agreement excuses the names"


def test_an_edition_marker_the_candidate_lacks_costs_it() -> None:
    """An acoustic or live set must not pass as the studio album."""
    unit = _unit("Band - Album (Live)", (180_000, 240_000))
    studio = _titled_release((("Alpha", None), ("Beta", None)))

    plain = AlbumMatcher().rank(
        _unit("Band - Album", (180_000, 240_000)),
        (studio,),
        local_titles=("Alpha", "Beta"),
    )[0]
    live_folder = AlbumMatcher().rank(unit, (studio,), local_titles=("Alpha", "Beta"))[0]

    assert live_folder.confidence < plain.confidence
    assert live_folder.signals.edition_mismatch


def test_without_titles_a_durationless_release_stays_capped() -> None:
    """The old ceiling still holds when there is no per-track evidence at all."""
    unit = _unit("Album", (180_000, 240_000))
    release = _titled_release((("Alpha", None), ("Beta", None)))

    candidate = AlbumMatcher().rank(unit, (release,))[0]

    assert candidate.confidence <= 0.55


def test_an_evidence_less_disagreement_is_an_absence_not_a_contradiction() -> None:
    """A source that did not find the album cannot veto one that did.

    When the losing source's best answer is a text guess for some other record,
    charging that guess as a contradiction would lower a proven match.
    """
    unit = _unit("Album", (180_000, 240_000))
    proven = AlbumMatcher().rank(unit, (_release("Lantern Quarrel", (180_000, 240_000)),))[0]
    guess = AlbumMatcher().rank(
        unit,
        (
            ReleaseMetadata(
                source=MetadataSources.DISCOGS,
                source_release_id="wrong",
                title="Marrowby Playground",
                artists=(ArtistMetadata(name="Someone Else"),),
                tracks=(
                    TrackMetadata(title="A", position=1),
                    TrackMetadata(title="B", position=2),
                ),
            ),
        ),
    )[0]

    settled = reconcile(proven, ((MetadataSources.DISCOGS, guess),))

    assert guess.signals.durations_compared == 0
    assert settled is proven, "a text guess is an absence, not a dispute"


def test_a_durationless_track_is_rescued_at_the_scoring_threshold() -> None:
    """What the score accepts, the alignment must not refuse.

    A durationless release can score in full on titles while one pair sits
    between the scoring threshold and the stricter rescue bar, which exists for
    overriding a *disagreeing* duration. With no duration published there is
    nothing to override.
    """
    unit = _unit("Album", (180_000, 240_000))
    release = _titled_release((("Na-Na (Means I Need You)", None), ("Gamma", None)))
    titles = ("Na Na Means I Need You", "Gamma")

    candidate = AlbumMatcher().rank(unit, (release,), local_titles=titles)[0]
    alignment = align_tracks(unit, release, local_titles=titles)

    assert candidate.signals.titles_agreeing == 2, "the score accepts this pair"
    assert alignment.is_usable, "so the alignment must accept it too"


def test_ordered_titles_pair_a_durationless_release_in_full() -> None:
    """Order plus name is what a person reads off a sleeve.

    Two tracks sharing a title would block the album; with the counts equal,
    the release publishing no lengths, and every pair agreeing in order, the
    order itself resolves them.
    """
    unit = _unit("Album", (180_000, 181_000, 900_000))
    release = _titled_release((("Take 1", None), ("Take 1", None), ("Unmistakable Name", None)))

    alignment = align_tracks(unit, release, local_titles=("Take 1", "Take 1", "Unmistakable Name"))

    assert alignment.is_usable
    assert alignment.method == "titled-positional"


def test_one_contested_pair_does_not_abandon_the_clear_ones() -> None:
    """A tie between two files must not orphan a third whose name is unambiguous.

    Out of order, the ordered reading does not apply, and the contested pair
    still blocks the album while the clear pairing is preserved for review.
    """
    unit = _unit("Album", (180_000, 181_000, 900_000))
    release = _titled_release((("Unmistakable Name", None), ("Take 1", None), ("Take 1", None)))

    alignment = align_tracks(unit, release, local_titles=("Take 1", "Take 1", "Unmistakable Name"))

    assert not alignment.is_usable, "the contested pair still blocks the album"
    assert any(
        assignment.track.title == "Unmistakable Name" for assignment in alignment.assignments
    ), "but the clear pairing was still made, so the review shows only the real tie"


@pytest.mark.parametrize(
    ("local", "release", "expected"),
    [
        # A dash and an edition word against a bracketed one.
        ("O Quarrelo - Bônus", "É O Quarrelo (Bonus Track)", 0.90),
        ("Lanternport - Bônus", "Lanternport (Bonus Track)", 1.0),
        # A mix suffix on one side only.
        ("Zedd Marro", "Zedd Marro (Quarry Club Mix)", 1.0),
    ],
)
def test_a_decorated_title_is_the_same_title(local: str, release: str, expected: float) -> None:
    """A recognized suffix hides a suffix, not a different song."""
    assert title_similarity(local, release) >= expected


@pytest.mark.parametrize(
    ("local", "release"),
    [
        # Decoration removed from both sides, stems that do not agree: the
        # removal must count for nothing.
        ("Mad Lantern (Bonus Track)", "Quarrelida (Bonus Track)"),
        # Removing decoration must not make different songs similar.
        ("Going Back", "Going Back To The Quarry"),
        ("Mad Lantern", "Quarrelida"),
    ],
)
def test_stripping_decoration_does_not_invent_agreement(local: str, release: str) -> None:
    """The stem must still agree at 0.90 or the removal counts for nothing."""
    assert title_similarity(local, release) < TITLE_MATCH_THRESHOLD


def test_a_decoration_that_collides_goes_to_review_rather_than_guessing() -> None:
    """The uniqueness guards are what bound the risk of seeing past a decoration.

    A release carrying both "Song" and "Song (Live Version)" reduces to one
    stem twice. That collision must leave the album unresolved, never paired
    at random: renaming a file onto the wrong track is the worst mistake
    this project can make.
    """
    within = [
        TrackMetadata(title="Song", position=1, duration_ms=200_000),
        TrackMetadata(title="Song (Live Version)", position=2, duration_ms=200_000),
    ]

    assert _settled_by_title("Song", within) is None


def _titled_release(tracks: tuple[tuple[str, int], ...]) -> ReleaseMetadata:
    """A release whose tracks carry real names, which is what settles a tie."""
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="titled",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=tuple(
            TrackMetadata(title=title, position=index, duration_ms=duration)
            for index, (title, duration) in enumerate(tracks, start=1)
        ),
    )


def test_a_rival_claimed_by_its_own_name_is_not_a_tie() -> None:
    """A track already taken by its own file, by name, is not a second candidate.

    The first file fits two tracks by length and its own title names neither,
    so the choice would be made on length alone and the album held back. But
    the second of those tracks is claimed by another file whose name is the
    track's, so there is no tie to break.
    """
    # Out of release order, so the reading that needs no tie-break is refused
    # and the pairing really does fall through to lengths.
    unit = _unit("Album", (205_000, 400_000, 203_400))
    release = _titled_release(
        (("Zoélinia", 204_400), ("Forever Quarrels", 203_000), ("Ló Is A Zedd", 400_000))
    )

    alignment = align_tracks(
        unit,
        release,
        local_titles=("Zoelinia 5:31 / Dawn Arrives Again", "Ló Is A Zedd", "Forever Quarrels"),
    )

    assert alignment.method != "positional", "or this proves nothing about tie-breaking"
    assert (
        not alignment.is_ambiguous
    ), "the rival was claimed by another file, so nothing was guessed"
    assert alignment.is_usable
    assert [assignment.track.title for assignment in alignment.assignments] == [
        "Zoélinia",
        "Ló Is A Zedd",
        "Forever Quarrels",
    ]


def test_a_rival_nobody_claims_is_still_a_tie() -> None:
    """The guarantee that matters: a real coin flip still refuses to rename."""
    unit = _unit("Album", (205_000,))
    release = _titled_release((("One", 204_800), ("Two", 205_200)))

    alignment = align_tracks(unit, release, local_titles=("Something Else Entirely",))

    assert alignment.is_ambiguous
    assert not alignment.is_usable


def _compilation(entries: tuple[tuple[str, str, int], ...]) -> ReleaseMetadata:
    """A Various Artists release that credits each track, as a compilation does."""
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="compilation",
        title="A Compilation",
        artists=(ArtistMetadata(name="Various"),),
        tracks=tuple(
            TrackMetadata(
                title=title,
                position=index,
                duration_ms=duration,
                artists=(ArtistMetadata(name=credit),),
            )
            for index, (credit, title, duration) in enumerate(entries, start=1)
        ),
    )


def _titled_unit(entries: tuple[tuple[str, int], ...]) -> tuple[AlbumUnit, tuple[str, ...]]:
    """An album on disk whose files carry the titles given, in the order given."""
    files = tuple(
        _file(f"{index:02d}.flac", duration) for index, (_, duration) in enumerate(entries, start=1)
    )
    unit = AlbumUnit(folder_path=Path("/music/Album"), unit_signature="unit", audio_files=files)
    return unit, tuple(title for title, _ in entries)


def test_a_tag_that_holds_the_credit_as_well_as_the_title_still_pairs() -> None:
    """A `title` tag that reads `Artist - Title` still pairs with the bare title.

    On some compilations every `title` tag holds the credit as well, and the
    `artist` tag holds the label, so the performer exists nowhere else on the
    file. The catalogue publishes the bare title, and comparing the two whole
    can fall just short of the rescue bar over one capital letter. The
    published lengths cannot help when the pressing is a different edition.
    """
    release = _compilation(
        (
            ("Mirénna Zoéllers", "Lamp-Shade Lilác (Instrumental) (Exclusive to Vinyl)", 253_000),
            ("Zeddie Bazar", "Feast For A Marrow King (Quarrels To Zed Mix)", 226_000),
        )
    )
    unit, titles = _titled_unit(
        (
            # The credit in the tag, one letter of case apart, and a length from
            # another edition entirely.
            ("Mirénna Zoéllers - Lamp-shade Lilác (Instrumental) (Exclusive to Vinyl)", 120_900),
            ("Zeddie Bazar - Feast For A Marrow King (Quarrels To Zed Mix)", 226_900),
        )
    )

    alignment = align_tracks(unit, release, local_titles=titles)

    assert alignment.method == "titled"
    assert alignment.is_usable, "both files name their track once the credit is read"
    assert [assignment.track_number for assignment in alignment.assignments] == [1, 2]


def test_a_translated_decoration_pairs_when_the_credit_carries_the_rest() -> None:
    """One track whose decoration is written in two languages.

    On its own the pair scores under every bar and the file is orphaned. The
    stem comparison is what rescues it, and it only reaches far enough once the
    credit is on both sides: credit and title are then identical on both, so
    the decoration no longer decides.
    """
    release = _compilation(
        (
            ("Zeddrick Antónius Of Marrowby", "Nobody Is A Lantérn (Inédit)", 145_000),
            ("Da Marrowby", "Lanternas", 204_000),
        )
    )
    unit, titles = _titled_unit(
        (
            ("Zeddrick Antónius Of Marrowby - Nobody Is A Lantérn (Unreleased)", 175_900),
            ("Da Marrowby - Lanternas", 204_900),
        )
    )

    alignment = align_tracks(unit, release, local_titles=titles)

    assert alignment.is_usable
    assert alignment.assignments[0].track.title == "Nobody Is A Lantérn (Inédit)"


def test_the_credit_never_makes_two_tracks_of_one_artist_interchangeable() -> None:
    """The risk this rule runs, refused: a shared credit must not blur two names.

    Two tracks by one performer share the prefix that is compared, so a rule
    that let the prefix carry the match would pair either file with either track
    — the worst mistake this project can make. What decides is still the title;
    the credit only stops being noise.
    """
    release = _compilation(
        (
            ("Zeddrick Antónius Of Marrowby", "Alone In The Quarry", 278_000),
            ("Zeddrick Antónius Of Marrowby", "Nobody Is A Lantérn", 145_000),
        )
    )
    unit, titles = _titled_unit(
        (
            # Deliberately out of order on disk, and with lengths that say nothing.
            ("Zeddrick Antónius Of Marrowby - Nobody Is A Lantérn", 1_000),
            ("Zeddrick Antónius Of Marrowby - Alone In The Quarry", 2_000),
        )
    )

    alignment = align_tracks(unit, release, local_titles=titles)

    assert alignment.is_usable
    paired = {
        assignment.file.path.name: assignment.track.title for assignment in alignment.assignments
    }
    assert paired == {"01.flac": "Nobody Is A Lantérn", "02.flac": "Alone In The Quarry"}


def test_an_album_with_no_credit_anywhere_compares_exactly_as_before() -> None:
    """The rule is inert where there is no credit to read, which is most albums.

    A release whose tracks name no artist and whose album names none either has
    one comparable form per track, so nothing about its pairing changes.
    """
    from diglibrary.library.matching import written_forms

    bare = TrackMetadata(title="Fate of Quarrels", position=1, duration_ms=180_000)
    assert written_forms(bare) == ("Fate of Quarrels",)
    assert written_forms(bare, "Zoé of Lanterns") == (
        "Fate of Quarrels",
        "Zoé of Lanterns - Fate of Quarrels",
    )
    credited = TrackMetadata(
        title="Lanternas",
        position=1,
        duration_ms=180_000,
        artists=(ArtistMetadata(name="Da Marrowby"),),
    )
    # The track's own credit wins over the album's, because on a compilation the
    # album's is "Various".
    assert written_forms(credited, "Various") == ("Lanternas", "Da Marrowby - Lanternas")


def test_a_guest_written_after_the_title_names_the_same_track() -> None:
    """`A & B & C - Title` and `A - Title Feat. B & C` are one record.

    A catalogue puts every artist in front of the title; a file may lead with
    one and hang the rest off the end. Against the two plain forms such a name
    scores under the title check and under the rescue bar, so the track is
    reported as having no file while the file sits in the folder. The two
    forms added for it carry the source's own artists in the other order.

    Nothing here looks for the word `Feat.`.
    """
    from diglibrary.library.matching import (
        TITLE_RESCUE_THRESHOLD,
        track_title_similarity,
        written_forms,
    )

    track = TrackMetadata(
        title="Teerz",
        position=7,
        duration_ms=159_000,
        artists=(
            ArtistMetadata(name="Zed Marrows Affair"),
            ArtistMetadata(name="Mia Yields"),
            ArtistMetadata(name="The Quarls"),
        ),
    )

    forms = written_forms(track)

    assert "Zed Marrows Affair - Teerz Mia Yields & The Quarls" in forms
    assert "Teerz Mia Yields & The Quarls" in forms
    named = "02. Zed Marrows Affair - Teerz Feat. Mia Yields & The Quarls"
    tagged = "Teerz Feat. Mia Yields & The Quarls"
    assert track_title_similarity(named, track) >= TITLE_RESCUE_THRESHOLD
    assert track_title_similarity(tagged, track) >= TITLE_RESCUE_THRESHOLD
    # And a track with one artist is untouched: two forms, exactly as before.
    alone = TrackMetadata(
        title="Lantan",
        position=7,
        duration_ms=159_000,
        artists=(ArtistMetadata(name="Zed Marrows Affair"),),
    )
    assert written_forms(alone) == ("Lantan", "Zed Marrows Affair - Lantan")


def test_a_title_the_release_publishes_twice_deadlocks_until_it_is_paired_by_hand() -> None:
    """A release that lists one title at two positions cannot be paired by name.

    The file carrying that title scores in full against both entries, so the
    rescue refuses it: a rescue must beat its runner-up, and here the runner-up
    is the same words. Both tracks then report no file beside a file plainly in
    the folder.

    Nothing here guesses which of the two it is, because the two are
    indistinguishable in everything the application can see and the choice
    decides what number gets written onto the file. A pairing made by hand
    settles it.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="repeated",
        title="Corners Of Saint Quarrel",
        artists=(ArtistMetadata(name="Marrowby"),),
        tracks=(
            TrackMetadata(title="Lanty", position=1),
            TrackMetadata(title="Hallo, Hallo Quarry", position=2),
            TrackMetadata(title="Lanty", position=3),
        ),
    )
    unit, titles = _titled_unit((("Lanty", 248_600), ("Hallo Hallo Quarry", 196_600)))
    repeated = unit.audio_files[0]

    deadlocked = align_tracks(unit, release, local_titles=titles)

    assert [file.path.name for file in deadlocked.unmatched_files] == ["01.flac"]
    assert [track.position for track in deadlocked.unmatched_tracks] == [1, 3]

    paired = align_tracks(
        unit, release, local_titles=titles, pinned={repeated.content_signature: 1}
    )

    assert paired.method.startswith("by hand")
    assert not paired.unmatched_files
    assert [
        (assignment.track.position, assignment.file.path.name) for assignment in paired.assignments
    ] == [(1, "01.flac"), (2, "02.flac")]
    # The duplicate entry is what is left over: the release has a track the
    # folder does not, which is an ordinary incomplete album and not an
    # ambiguity that blocks it.
    assert [track.position for track in paired.unmatched_tracks] == [3]
    assert not paired.is_ambiguous
    assert paired.is_partial

    # And the other reading can be made just as easily.
    last = align_tracks(unit, release, local_titles=titles, pinned={repeated.content_signature: 3})
    assert [assignment.track.position for assignment in last.assignments] == [2, 3]


def test_a_pairing_made_by_hand_takes_a_file_from_whatever_the_matcher_had_paired_it_with() -> None:
    """Repairing a pairing the matcher made confidently is the point.

    Two tracks of one published length are told apart by nothing but the names,
    and a file can be paired at a low similarity purely because the seconds
    agree. So a pairing made by hand has to be able to take a file away from a
    track that already holds it.

    What happens to the track that was emptied is not a second decision: the
    file left loose and the track left loose go back through the ordinary
    ladder, on the ordinary evidence, and here they meet. Correcting one end of
    a swap performs the swap, and the matcher does the second half on evidence.
    """
    release = _release("Album", (180_000, 180_000))
    unit = _unit("Album", (180_000, 180_000))
    second = unit.audio_files[1]

    by_the_matcher = align_tracks(unit, release)
    assert [
        (assignment.track.position, assignment.file.path.name)
        for assignment in by_the_matcher.assignments
    ] == [(1, "01.flac"), (2, "02.flac")]

    moved = align_tracks(unit, release, pinned={second.content_signature: 1})

    assert [
        (assignment.track.position, assignment.file.path.name) for assignment in moved.assignments
    ] == [(1, "02.flac"), (2, "01.flac")]
    assert not moved.unmatched_files and not moved.unmatched_tracks
    assert moved.method.startswith("by hand")


def test_a_pairing_whose_file_or_track_is_gone_does_not_stop_the_album() -> None:
    """A stored pairing can outlive the folder it was made in, and must not block it.

    The paired file may have been deleted since; the position it was paired to
    may not exist on a release with fewer tracks. Either way the rest of the
    album is still readable. The window is where the leftover is reported,
    because it is the only place it can be acted on.
    """
    release = _release("Album", (180_000, 240_000))
    unit = _unit("Album", (180_000, 240_000))

    stale_file = align_tracks(unit, release, pinned={"a-signature-from-another-life": 1})
    stale_track = align_tracks(unit, release, pinned={unit.audio_files[0].content_signature: 99})

    assert stale_file.is_usable
    assert stale_track.is_usable


def test_the_same_words_in_another_order_are_the_same_title() -> None:
    """The same words in another order, with a semicolon, are one title.

    Compared as a sequence the two score under every bar, so the file stays
    loose and the row claims there is no file for that track while it sits in
    the folder.

    Deliberately narrow: the *same* words, all of them. Two different tracks of
    one release rarely share a whole word list, so this does not make a track
    look like its neighbour.
    """
    assert title_similarity("Angel Zeddrick Marrowby", "Zeddrick Marrowby; Angel") == 1.0
    # And the album pairs.
    release = _release_named(("Under The Quarry", "Zeddrick Marrowby; Angel"))
    unit, titles = _titled_unit(
        (("Under The Quarry", 252_000), ("Angel Zeddrick Marrowby", 155_000))
    )

    alignment = align_tracks(unit, release, local_titles=titles)

    assert alignment.is_usable
    assert not alignment.unmatched_files


def test_a_conjunction_is_a_conjunction_however_it_is_spelled() -> None:
    """A duo joined by a word on the folder and by `&` on the sleeve is one duo.

    Compared whole the two spellings lower the artist similarity of an
    unmistakable album.
    """
    assert title_similarity("Zeddy e Mirén", "Zeddy & Mirén") == 1.0
    assert (
        title_similarity("Quarrel Lanterns E Marrow Of North", "Quarrel Lanterns & Marrow Of North")
        == 1.0
    )


def test_a_title_with_no_readable_words_never_matches_by_words() -> None:
    """A title with no readable words never matches by its word list.

    An ASCII-only word pattern reduces titles written in another script to the
    same empty list, which would make different tracks look like one song.
    Words are read in any script, and an empty list never counts.
    """
    assert title_similarity("ランタン・ダンス", "マロウ") < 0.5
    assert title_similarity("...", "!!!") < 1.0


def _release_named(titles: tuple[str, ...]) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="named",
        title="Zeddy & Mirén",
        artists=(ArtistMetadata(name="Zeddy Marrow & Mirén Of Quarry"),),
        tracks=tuple(
            TrackMetadata(title=title, position=index)
            for index, title in enumerate(titles, start=1)
        ),
    )


def _instrumental_album() -> tuple[AlbumUnit, ReleaseMetadata, tuple[str, ...]]:
    """Two songs and their two instrumentals, which is how the release lists them.

    The vocal takes come first, then the instrumental ones. The files sit in
    the folder in pairs, so the order alone never reads across, and one of them
    holds the *vocal* audio under an instrumental name, which is why its length
    is several seconds off the track it names.
    """
    names_durations = (
        ("Amber Moon - Instrumental.flac", 420_800),
        ("Amber Moon.flac", 420_800),
        ("Drifting Low - Instrumental.flac", 323_300),
        ("Drifting Low.flac", 322_900),
        # A file name with words the catalogue's title lacks, which clears no
        # bar. One file like it is enough to make `_titled_alignment` give up
        # on the whole album, so the album takes the duration path.
        ("Humming Bass - In The Yard.flac", 397_200),
    )
    unit = AlbumUnit(
        folder_path=Path("/music/Amber Moon"),
        unit_signature="amber-moon",
        audio_files=tuple(_file(name, duration) for name, duration in names_durations),
    )
    release = _titled_release(
        (
            ("Amber Moon", 420_800),
            ("Drifting Low", 322_900),
            ("Amber Moon (instrumental)", 403_900),
            ("Drifting Low (instrumental)", 323_300),
            ("Humming Bass", 397_200),
        )
    )
    titles = tuple(Path(name).stem for name, _ in names_durations)
    return unit, release, titles


def test_an_instrumental_is_a_different_track_from_the_song_it_decorates() -> None:
    """A song and its instrumental on one release are two tracks.

    A decoration is normally noise, so `title_similarity` sees past it. On a
    release that publishes a song *and* its instrumental, that shortcut makes
    the two one name: nothing can claim either track and the whole album is
    refused as ambiguous.

    Without the rule the pairing is also backwards: the file named
    `Instrumental` takes the vocal track by being 0 ms from it.
    """
    unit, release, titles = _instrumental_album()

    alignment = align_tracks(unit, release, local_titles=titles)

    assert alignment.method == "duration+title", (
        "one unrecognisable name sends the whole album down the duration path, "
        "which is the path under test"
    )
    assert not alignment.is_ambiguous, "the decoration is what tells these two apart"
    assert alignment.is_usable
    paired = {a.track.title: a.file.path.name for a in alignment.assignments}
    assert paired["Amber Moon"] == "Amber Moon.flac"
    assert paired["Amber Moon (instrumental)"] == "Amber Moon - Instrumental.flac"
    assert paired["Drifting Low"] == "Drifting Low.flac"
    assert paired["Drifting Low (instrumental)"] == "Drifting Low - Instrumental.flac"


def test_a_pairing_the_name_settled_still_reports_a_length_that_disagrees() -> None:
    """A pairing settled by name passes, and the row reports the length that disagrees.

    The name is what pairs the file called `Instrumental` with the instrumental
    track, and the published length disagrees anyway, because the audio in that
    file is the vocal take. Blocking the album for it would punish the pairs
    that are right; losing the number would hide the one thing that says the
    file is not what its own name claims.
    """
    unit, release, titles = _instrumental_album()

    alignment = align_tracks(unit, release, local_titles=titles)

    off = {a.track.title: (a.file.path.name, a.duration_delta_ms) for a in alignment.assignments}
    # The file *and* the number: both takes run 420_800 ms, so the gap alone
    # cannot tell whether the pairing that carries it is the right one.
    assert off["Amber Moon (instrumental)"] == ("Amber Moon - Instrumental.flac", 16_900)
    assert off["Amber Moon"] == ("Amber Moon.flac", 0)
    assert alignment.is_usable, "it passes, and the row says the length is off"


def test_a_decoration_no_second_track_shares_is_still_seen_past() -> None:
    """Seeing past a decoration is switched off *only* where it blinds.

    One bonus track spelled two ways is the case that rule exists for, and a
    release that publishes it once has nothing for the decoration to distinguish.
    """
    assert contested_stems(_titled_release((("Alpha", 1), ("Alpha (Bonus Track)", 2))).tracks) == {
        "alpha"
    }
    assert contested_stems(_titled_release((("Alpha", 1), ("Beta (Bonus Track)", 2))).tracks) == (
        frozenset()
    )


def test_a_phrase_every_track_carries_stops_deciding_anything() -> None:
    """A phrase every title carries tells none of them apart.

    When every file's title ends with the same edition phrase and the release
    names its tracks plainly, each title scores under the title check. Nothing
    is then spoken for by name and the album falls back to duration alone,
    which can swap files of similar length two by two.
    """
    from diglibrary.library.matching import _without_the_words_every_track_carries

    stripped = _without_the_words_every_track_carries(
        (
            "A Quarry - Remastered Version",
            "Lullabize - Remastered Version",
            "O Divãn - Remastered Version",
        )
    )

    assert stripped == ("A Quarry", "Lullabize", "O Divãn")


def test_the_phrase_may_sit_at_either_end() -> None:
    """A live album says it in front; a remaster says it behind. One rule."""
    from diglibrary.library.matching import _without_the_words_every_track_carries

    assert _without_the_words_every_track_carries(
        ("In Concert - Sóng A", "In Concert - Sóng B", "In Concert - Sóng C")
    ) == ("Sóng A", "Sóng B", "Sóng C")


def test_tracks_that_really_do_share_their_whole_name_are_left_alone() -> None:
    """The album that must stay ambiguous.

    Two takes of one song, titled alike, are not a shared decoration: they
    are the thing this must not resolve. Stripping would leave nothing at all,
    so nothing is stripped.
    """
    from diglibrary.library.matching import _without_the_words_every_track_carries

    same = ("Take One", "Take One", "Take One")
    assert _without_the_words_every_track_carries(same) == same


def test_too_few_titles_to_call_anything_shared() -> None:
    """Two titles agreeing on a word is a coincidence, not a decoration."""
    from diglibrary.library.matching import _without_the_words_every_track_carries

    pair = ("In Concert - One", "In Concert - Other")
    assert _without_the_words_every_track_carries(pair) == pair


def test_the_rule_is_derived_and_not_a_list_of_words() -> None:
    """A word list is a set written by naming the members somebody knew, and the
    next pressing says it in a language nobody thought of.
    """
    source = Path(__file__).resolve().parent.parent.parent / "src/diglibrary/library/matching.py"
    body = re.search(
        r"def _without_the_words_every_track_carries\(.+?\n\n\ndef ",
        source.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert body, "no _without_the_words_every_track_carries() to read"
    for word in ("remaster", "remasteriz", "ao vivo", "live", "deluxe", "bonus"):
        assert word not in body.group(0).lower().split('"""')[-1], (
            f"the rule names {word!r}, so it is a list of the decorations "
            "somebody already knew about"
        )


def test_a_doubt_about_two_tracks_does_not_hold_back_the_rest_of_the_album() -> None:
    """A release that prints one title twice, a few seconds apart in length.

    Each file matches its own track exactly, but each *also* fits the other
    within the tolerance, and the titles are identical, so nothing breaks the
    tie by name or by length. Blocking the whole album for it would leave
    every other file, which nobody is unsure about, unplanned.

    What is certain is settled, the doubt is offered rather than written, and
    the album is partial rather than blocked.
    """
    # The number is in the middle of the file name, behind an artist and an
    # album, where a front-anchored reader would not see it.
    prefix = "Zed Marrow - A Quarrel With Lanterns - "
    files = (
        _file(f"{prefix}01 - Do You Hold Other Lanterns_.flac", 306_000),
        _file(f"{prefix}018 - Do You Hold Other Lanterns_.flac", 311_000),
        _file(f"{prefix}03 - Marrows.flac", 264_000),
    )
    tracks = (
        TrackMetadata(title="Do You Hold Other Lanterns?", position=1, duration_ms=306_000),
        TrackMetadata(title="Do You Hold Other Lanterns?", position=18, duration_ms=311_000),
        TrackMetadata(title="Marrows", position=3, duration_ms=264_000),
    )

    alignment = _duration_alignment(
        files, tracks, DURATION_TOLERANCE_MS, local_titles=tuple(f.path.stem for f in files)
    )

    # The number each file already carries settles it outright: `01` is
    # position 1, `018` is position 18, and the titles agree with both, so
    # there is no tie here at all.
    assert not alignment.is_ambiguous
    assert alignment.is_usable, "nothing is left in doubt, so the album can be applied"
    assert not alignment.suggested
    paired = {one.file.path.name: one.track.position for one in alignment.settled}
    assert paired[f"{prefix}01 - Do You Hold Other Lanterns_.flac"] == 1
    assert paired[f"{prefix}018 - Do You Hold Other Lanterns_.flac"] == 18


def test_without_a_number_the_same_album_is_offered_rather_than_written() -> None:
    """The same two files, stripped of the numbers their names carry.

    Each file still matches its own track exactly and still fits the other
    within tolerance, so both sides of the doubt are offered, the certain
    files are planned anyway, and nothing is written from a suggestion.
    """
    files = (
        _file("Do You Hold Other Lanterns_.flac", 306_000),
        _file("Do You Hold Other Lanterns_ (2).flac", 311_000),
        _file("Marrows.flac", 264_000),
    )
    tracks = (
        TrackMetadata(title="Do You Hold Other Lanterns?", position=1, duration_ms=306_000),
        TrackMetadata(title="Do You Hold Other Lanterns?", position=18, duration_ms=311_000),
        TrackMetadata(title="Marrows", position=3, duration_ms=264_000),
    )

    alignment = _duration_alignment(
        files, tracks, DURATION_TOLERANCE_MS, local_titles=tuple(f.path.stem for f in files)
    )

    assert alignment.is_ambiguous, "the doubt is real and the album stays in review"
    assert not alignment.is_usable, "nothing is applied automatically"
    assert alignment.is_partial, "but what is certain can be applied by hand"
    assert [one.file.path.name for one in alignment.settled] == ["Marrows.flac"]
    # BOTH sides of the doubt are offered, not just the file read first: a rival
    # freed by elimination is freed by the very guess in question.
    assert len(alignment.suggested) == 2


def test_a_true_tie_is_still_blocked_whole() -> None:
    """A true tie is not loosened by the partial apply.

    No assignment here is exact and none is more nearly right than another, so
    nothing is settled and nothing can be applied. What the partial apply turns
    on is whether a unique exact assignment exists beside the doubt, not
    whether a doubt exists at all.
    """
    files = (_file("a.flac", 400_000), _file("b.flac", 400_000))
    tracks = (
        TrackMetadata(title="Amber Moon", position=1, duration_ms=400_000),
        TrackMetadata(title="Amber Moon", position=2, duration_ms=400_000),
    )

    alignment = _duration_alignment(files, tracks, DURATION_TOLERANCE_MS)

    assert alignment.is_ambiguous
    assert not alignment.is_usable
    assert not alignment.settled, "nothing here is certain, so nothing may be written"
    assert not alignment.is_partial, "and there is no partial apply to offer"


def test_a_decorated_name_breaks_the_tie_before_the_length_does() -> None:
    """Closer names first, then closer lengths.

    A release printing a song and its instrumental gives a file called
    `… (Instrumental)` a plain reason to prefer that track, and the comparison
    used here reads the whole written title on purpose, because the one the
    matcher normally uses sees *past* decorations, which is the entire question
    at this point.
    """
    files = (_file("02 - Marrows (Instrumental).flac", 300_000),)
    tracks = (
        # The nearer length is the plain one, so length alone would pick it.
        TrackMetadata(title="Marrows", position=1, duration_ms=300_000),
        TrackMetadata(title="Marrows (Instrumental)", position=2, duration_ms=303_000),
    )

    alignment = _duration_alignment(
        files, tracks, DURATION_TOLERANCE_MS, local_titles=("02 - Marrows (Instrumental)",)
    )

    # It is not even a suggestion: the written name settles it outright,
    # through `_settled_by_title`.
    assert not alignment.suggested
    assert alignment.settled[0].track.position == 2, "the name decided it, not the gap"


def test_a_number_only_ever_separates_tracks_the_name_already_admits() -> None:
    """A number only breaks a tie between tracks the name already admits.

    A file is called `01.flac` because it is the first file in the folder, not
    because anybody checked it is track one. Here its own title reads
    `Something Else Entirely` and matches neither rival, so the number is not
    heard at all and the album keeps refusing.
    """
    files = (_file("01.flac", 205_000),)
    tracks = (
        TrackMetadata(title="One", position=1, duration_ms=204_800),
        TrackMetadata(title="Two", position=2, duration_ms=205_200),
    )

    alignment = _duration_alignment(
        files, tracks, DURATION_TOLERANCE_MS, local_titles=("Something Else Entirely",)
    )

    assert alignment.is_ambiguous, "the number must not turn folder order into evidence"
    assert not alignment.is_usable


def test_a_partial_plan_says_everything_it_leaves_behind_including_the_doubt() -> None:
    """One list holds every sentence about what a partial plan leaves behind.

    Two lists, the blocked plan's reasons and the offered plan's warnings,
    would each say that release tracks have no file, and a window showing both
    prints one fact twice. An ambiguous alignment can still be partial, so the
    sentence about the ambiguous pairing belongs in the offered plan's own
    list, and once it is there the other list has nothing left to add.

    Asserted on the warnings themselves rather than through the dialog, because
    what matters is which list holds which sentence.
    """
    files = (
        _file("Do You Hold Other Lanterns_.flac", 306_000),
        _file("Do You Hold Other Lanterns_ (2).flac", 311_000),
        _file("Marrows.flac", 264_000),
        _file("Something Else.flac", 100_000),
    )
    tracks = (
        TrackMetadata(title="Do You Hold Other Lanterns?", position=1, duration_ms=306_000),
        TrackMetadata(title="Do You Hold Other Lanterns?", position=18, duration_ms=311_000),
        TrackMetadata(title="Marrows", position=3, duration_ms=264_000),
        TrackMetadata(title="Nobody Has This One", position=4, duration_ms=999_000),
    )
    alignment = _duration_alignment(
        files, tracks, DURATION_TOLERANCE_MS, local_titles=tuple(f.path.stem for f in files)
    )
    assert alignment.is_ambiguous and alignment.is_partial, "the case under test"

    warnings = _partial_warnings(alignment)

    joined = " ".join(warnings)
    assert (
        "fits more than one release track" in joined or "fit more than one" in joined
    ), "the doubt is stated in the offered plan's own list"
    # It is counted, not named. The names are in the plan's own table, which
    # draws every one of these on a row with a mark and a gesture, so a list
    # above it would be the table said again.
    assert "1 release track has no file" in joined
    assert "Nobody Has This One" not in joined, "the list above the table is the table"
    # The point of the whole change: one list, so nothing can be printed twice.
    assert len(warnings) == len(set(warnings)), "a warning is repeated"


def test_one_true_track_on_a_stranger_s_compilation_is_still_not_this_album() -> None:
    """One genuine track on somebody else's compilation does not make it this album.

    A compilation may carry one track of the album's artist, so one of the
    titles really agrees. A veto that asks for *zero* agreements is disarmed
    by that one track for the whole album, and a plan is offered to rename
    every file. The veto therefore asks for a share of the names.
    """
    # Nine files against eleven tracks: five lengths agree within six seconds
    # and one name does.
    unit = _unit(
        "Lanterns from Quarry, Vol. 1",
        (323_200, 208_700, 202_100, 303_800, 255_000, 421_800, 254_400, 351_400, 358_200),
    )
    compilation = _titled_release(
        (
            ("Quarrelin' The Marrow", 199_000),
            ("Keep On Lanterning On (Fender Mix)", 359_000),
            ("Doomsday Marrow", 278_000),
            ("Free Our Lanterns", 241_000),
            ("This Quarrel", 303_000),
            ("Portal Of Marrow", 275_000),
            ("For Zeddon And McMarrowby (The Tin Remix)", 249_000),
            ("Quarrelitat 2", 296_000),
            ("3rd Quarrel Around (Some Day Version)", 345_000),
            ("Sentimental Lantern", 419_000),
            ("Zedd Marrow Blues (Forest Mix)", 413_000),
        )
    )
    titles = (
        "All Those Quarrels",
        "Drumming With Zeddo",
        "Lanternk",
        "Mirén",
        "Strong Quarry",
        "Zeddrick Cat",
        "Marrowtodo",
        "The Crickets",
        "For Zeddon e McMarrowby",
    )

    candidate = AlbumMatcher().rank(
        unit,
        (compilation,),
        search_hint="Lanterns from Quarry, Vol. 1",
        local_titles=titles,
        hint_album="Lanterns from Quarry, Vol. 1",
    )[0]

    assert candidate.signals.titles_agreeing == 1, "the one true overlap is really there"
    assert candidate.signals.durations_agreeing == 5, "and five lengths agree by chance"
    assert candidate.vetoed_by_the_names, "one name out of nine is not this record"
    assert candidate.confidence <= 0.15


def test_the_name_takes_its_track_before_a_length_read_earlier_can() -> None:
    """A file that fits a track by length must not take it from the file named after it.

    The first file sorts before the second and fits the second track within
    the window, so it would take the track, and by the time the file that
    carries that name is read, the track it names is gone. A guard that asks
    what is still on the table gives an answer about the order the folder was
    read in.

    Text decides and duration only corroborates, in every pairing.
    """
    unit = _unit("Album", (254_400, 358_200))
    release = _titled_release(
        (
            ("Keep On Lanterning On", 359_000),
            ("For Zeddon And McMarrowby", 249_000),
        )
    )

    alignment = align_tracks(
        unit,
        release,
        local_titles=("Marrowtodo", "For Zeddon e McMarrowby"),
    )

    paired = {
        assignment.file.properties.duration_ms: assignment.track.title
        for assignment in alignment.assignments
    }
    assert paired.get(358_200) == "For Zeddon And McMarrowby", (
        "the file whose own name is the track's was given a neighbour's name, and "
        "the track it names was written onto another file"
    )
    assert 254_400 not in paired, "and nothing else may take that track by a length"


def test_every_pair_says_what_holds_it_together() -> None:
    """A pairing the names agree on and one held by a length are told apart.

    Each assignment says what holds it, so a screen does not draw two
    different facts as one mark.
    """
    unit = _unit("Album", (200_000, 300_000))
    release = _titled_release((("Lanternk", 199_000), ("Sentimental Lantern", 299_000)))

    alignment = align_tracks(unit, release, local_titles=("Lanternk", "Something Else Entirely"))

    holds = {assignment.track.title: assignment.paired_by for assignment in alignment.assignments}
    assert holds == {"Lanternk": "name", "Sentimental Lantern": "length"}


def test_a_pairing_made_by_hand_says_so() -> None:
    """A pairing made by hand is neither a name nor a length, and the row says so."""
    unit = _unit("Album", (200_000, 300_000))
    release = _titled_release((("Lanternk", 199_000), ("Sentimental Lantern", 299_000)))
    pinned_file = unit.audio_files[1]

    alignment = align_tracks(
        unit,
        release,
        local_titles=("Lanternk", "Something Else Entirely"),
        pinned={pinned_file.content_signature: 2},
    )

    assert {
        assignment.track.title: assignment.paired_by for assignment in alignment.assignments
    } == {"Lanternk": "name", "Sentimental Lantern": "hand"}


def test_a_remixes_compilation_is_not_the_album_it_is_made_of() -> None:
    """A remixes record is not the album whose songs it carries.

    A remixes record carries the album's own songs under the album's own names,
    with `(… Remix)` on each one, and its own title may say nothing about it.
    Most names agree and few lengths do, so it would score well enough to put
    a plan on screen.

    The guard therefore reads the track titles as well as the release's title,
    and in both directions.
    """
    unit = _unit("Lanterns from Quarry, Vol. 2", (281_300, 291_900, 204_500, 320_700))
    remixes = _titled_release(
        (
            ("Free Lantern (Marrow Circuit remix)", 329_000),
            ("Under The Quarry (Zedd Lantern Massive Remix)", 305_000),
            ("Mystere At Depot 24 (Zoc Marrow Remix)", 345_000),
            ("True Quarrel (4 Zero Remix)", 432_000),
        )
    )
    titles = ("Free Lantern", "Under the Quarry", "Mystère at Dépôt 24", "True Quarrel")

    candidate = AlbumMatcher().rank(
        unit,
        (remixes,),
        search_hint="Lanterns from Quarry, Vol. 2",
        local_titles=titles,
        hint_album="Lanterns from Quarry, Vol. 2",
    )[0]

    assert (
        candidate.signals.edition_mismatch
    ), "every track of the candidate says Remix and not one of the files does"
    assert candidate.confidence < 0.4


def test_a_remix_album_whose_files_say_so_is_not_punished_for_being_one() -> None:
    """A remix record whose own files carry the marker is a plain match.

    The rule must read the two sides against each other and never the word on
    its own.
    """
    unit = _unit("Lanterna Remixes", (281_300, 291_900))
    remixes = _titled_release(
        (("Lanterna (Zedd Marrow Remix)", 281_000), ("Quarrelle (Mirén Zoélle Remix)", 292_000))
    )
    titles = ("Lanterna (Zedd Marrow Remix)", "Quarrelle (Mirén Zoélle Remix)")

    candidate = AlbumMatcher().rank(
        unit,
        (remixes,),
        search_hint="Lanterna Remixes",
        local_titles=titles,
        hint_album="Lanterna Remixes",
    )[0]

    assert not candidate.signals.edition_mismatch, "both sides say Remix, so nobody disagrees"
    assert candidate.confidence > 0.8


def test_a_marker_inside_another_word_is_not_an_edition() -> None:
    """`Delivery` and `Alive` are not live albums.

    The rule reads every track title, not only the album title, so a marker
    matched anywhere inside a word would find an edition on nearly any record.
    """
    unit = _unit("Album", (200_000, 300_000))
    release = _titled_release((("Delivery", 199_000), ("Feelin' Alive", 299_000)))

    candidate = AlbumMatcher().rank(
        unit,
        (release,),
        search_hint="Album",
        local_titles=("Delivery", "Feelin' Alive"),
        hint_album="Album",
    )[0]

    assert not candidate.signals.edition_mismatch


def test_an_edition_stamped_on_every_track_is_not_part_of_any_name() -> None:
    """An edition written after every title is part of none of them.

    A digital shop writes the edition into every line and a catalogue publishes
    the name alone, so each local title carries a fixed tail and scores under
    the bar against the bare name; the shorter the real name, the worse it
    does.

    Nothing here knows the words of the tail. What it knows is that a tail
    every title shares distinguishes none of them.
    """
    catalogue = (
        "Zedivara",
        "Living In Quarry",
        "The Law Of Marrow",
        "Mirel",
        "A Lantern",
        "Boy From The Quarry",
    )
    local = tuple(f"{title} - New Mixdown 2015" for title in catalogue)
    release = _release_named(catalogue)

    assert _agreeing_titles(local, release.tracks) == 0, "the failure being fixed"
    repaired = _local_titles_for_release(local, release.tracks)
    assert _agreeing_titles(repaired, release.tracks) == len(catalogue)


def test_an_edition_both_sides_agree_on_is_left_alone() -> None:
    """A live album matched against a catalogue that also says so still pairs.

    The reprieve is for a label one side stamped and the other did not. Trimming
    a tail both sides carry would break albums that pair perfectly today, so the
    rule asks the release before it touches anything.
    """
    catalogue = ("Dawnlight (Live)", "Duskfall (Live)", "Nightfall (Live)")
    release = _release_named(catalogue)

    assert _local_titles_for_release(catalogue, release.tracks) == catalogue


def test_a_tail_that_cuts_a_bracket_in_half_is_not_an_edition() -> None:
    """A shared ending that cuts a bracket in half is not an edition.

    When every track ends `(… remix)`, the longest ending they all share is
    `remix)`, past a space and long enough for every other guard. Removing it
    would leave a name with a bracket hanging open. The unclosed bracket
    refuses it without anybody knowing the word.
    """
    local = (
        "Make A Quarrel (Zoni Marrow remix)",
        "Lantern Carnival (4Zero remix)",
        "Dear Marrowby (Quarry Communication remix)",
    )
    release = _release_named(("Make A Quarrel", "Lantern Carnival", "Dear Marrowby"))

    assert _local_titles_for_release(local, release.tracks) == local


def test_the_evidence_sentence_omits_what_the_caller_already_says() -> None:
    """One builder, told what its screen states, rather than two sentences.

    The album dialog gives the titles in the source panel beside a link and the
    track counts twice over, so those two clauses are dropped for that caller.
    Everything that reads the sentence on its own keeps them: a candidate row,
    a below-threshold reason, a search message.
    """
    signals = MatchSignals(
        titles_agreeing=10,
        titles_compared=12,
        durations_agreeing=12,
        durations_compared=13,
        local_track_count=12,
        release_track_count=13,
        text_similarity=1.0,
        artist_similarity=1.0,
        album_similarity=1.0,
        year_agreement=1.0,
        titles_in_order=False,
    )

    whole = explain_evidence(signals)
    for_the_dialog = explain_evidence(signals, said_elsewhere=frozenset({"titles", "track_count"}))

    assert "10 of 12 track titles matched" in whole
    assert "track count differs (12 local, 13 on the release)" in whole
    assert "track titles" not in for_the_dialog
    assert "track count" not in for_the_dialog
    assert "12 of 13 track lengths agreed within 6s" in for_the_dialog
    # The sentence capitalises whatever now leads it, and with the titles gone
    # that is this clause.
    assert for_the_dialog.startswith("Year agrees; ")


def test_the_lengths_come_out_too_where_the_proof_line_states_them() -> None:
    """The dialog draws `lengths match the release` under this sentence, so a
    clause counting the same lengths above it is one fact in two wordings.

    Subtracting three facts can leave nothing at all, which is the ordinary
    case for a clean album. An empty sentence is the honest answer, and
    indexing its first character must not raise.
    """
    signals = MatchSignals(
        titles_agreeing=12,
        titles_compared=12,
        durations_agreeing=12,
        durations_compared=12,
        local_track_count=12,
        release_track_count=12,
        text_similarity=1.0,
        artist_similarity=1.0,
        album_similarity=1.0,
        year_agreement=1.0,
        titles_in_order=True,
    )
    everything = frozenset({"titles", "track_count", "durations"})

    assert "track lengths" in explain_evidence(signals, frozenset({"titles", "track_count"}))
    assert explain_evidence(signals, everything) == "Year agrees."

    nothing_left = explain_evidence(
        MatchSignals(
            titles_agreeing=12,
            titles_compared=12,
            durations_agreeing=12,
            durations_compared=12,
            local_track_count=12,
            release_track_count=12,
            text_similarity=1.0,
            artist_similarity=1.0,
            album_similarity=1.0,
            year_agreement=None,
            titles_in_order=True,
        ),
        everything,
    )

    assert nothing_left == "", "a sentence with no clause left is no sentence"


def test_a_conclusive_similarity_is_a_number_standing_in_for_yes() -> None:
    """A similarity that rounds to one is dropped for every caller.

    `artist similarity 1.00` says the names are the same, which is what the rest
    of the sentence is already about. A low similarity is the only kind that
    explains how a wrong record was arrived at, and it stays, decimals included.
    """

    def signals(artist: float, album: float) -> MatchSignals:
        return MatchSignals(
            titles_agreeing=0,
            titles_compared=11,
            durations_agreeing=0,
            durations_compared=4,
            local_track_count=11,
            release_track_count=4,
            text_similarity=0.5,
            artist_similarity=artist,
            album_similarity=album,
            year_agreement=1.0,
            titles_in_order=False,
        )

    assert "similarity" not in explain_evidence(signals(1.0, 1.0))
    assert "similarity" not in explain_evidence(
        signals(0.995, 1.0)
    ), "0.995 is the same statement as 1.00"
    weak = explain_evidence(signals(0.65, 0.42))
    assert "artist similarity 0.65" in weak and "album title similarity 0.42" in weak


def test_two_lengths_overrule_one_name_when_two_files_wear_one_title() -> None:
    """One song in two versions, named the other way round by the two sides.

    The files call the longer version plain and the shorter one after its
    remixer; the catalogue calls the shorter track plain and the longer one
    `(Remix 2)`. The name alone would settle the plain file onto the plain
    track, most of a minute apart, leave the shorter file with no track and
    report the longer track missing. Two lengths agreeing crosswise against a
    name two files wear is what tells the versions apart.
    """
    unit = _unit("Album", (342_150, 290_683, 254_743))
    release = _titled_release(
        (
            ("Out Of Quarry", 290_000),
            ("Out Of Quarry (Remix 2)", 340_000),
            ("Here Unto Méire", 254_000),
        )
    )

    alignment = align_tracks(
        unit,
        release,
        local_titles=(
            "Out Of Quarry",
            "Out Of Quarry - Zedd Marrow Remix",
            "Here Unto Meire - The Brand New Zoélle Q-Vo Mix",
        ),
    )

    paired = {a.file.properties.duration_ms: a.track.title for a in alignment.assignments}
    assert paired[342_150] == "Out Of Quarry (Remix 2)", "the longer file is the longer track"
    assert paired[290_683] == "Out Of Quarry", "and the shorter file is the shorter track"
    assert alignment.unmatched_files == () and alignment.unmatched_tracks == ()
    assert all(a.duration_delta_ms <= DURATION_TOLERANCE_MS for a in alignment.assignments)
    assert alignment.is_usable


def test_the_swap_is_also_made_where_every_file_is_named() -> None:
    """The same rule on the all-names path, or it is a rule applied by half.

    Both files name a track unequivocally — plain and `(Remix 2)` — and both
    lengths refute the name crosswise. Two named pairs are re-made as one swap.
    """
    unit = _unit("Album", (342_150, 290_683))
    release = _titled_release((("Out Of Quarry", 290_000), ("Out Of Quarry (Remix 2)", 340_000)))

    alignment = align_tracks(
        unit, release, local_titles=("Out Of Quarry", "Out Of Quarry (Remix 2)")
    )

    assert alignment.method == "titled"
    paired = {a.file.properties.duration_ms: a.track.title for a in alignment.assignments}
    assert paired == {342_150: "Out Of Quarry (Remix 2)", 290_683: "Out Of Quarry"}
    assert all(
        a.paired_by == "length" for a in alignment.assignments
    ), "what holds each pair is the clock, and the row says so"


def test_a_loose_file_wearing_another_song_overrules_nothing() -> None:
    """A name is beaten only by two lengths *and* one shared title.

    The second file fits the plain track by length, but it is not another
    version of that song, so the plain file keeps the track it is named after,
    the disagreement is reported, and the loose file stays loose.
    """
    unit = _unit("Album", (342_150, 290_683, 254_743))
    release = _titled_release(
        (
            ("Out Of Quarry", 290_000),
            ("Out Of Quarry (Remix 2)", 340_000),
            ("Here Unto Méire", 254_000),
        )
    )

    alignment = align_tracks(
        unit, release, local_titles=("Out Of Quarry", "Solútion", "Here Unto Meire")
    )

    paired = {a.file.properties.duration_ms: a.track.title for a in alignment.assignments}
    assert paired[342_150] == "Out Of Quarry", "text decides; the clock only reports"
    assert [
        a.duration_delta_ms for a in alignment.assignments if a.track.title == "Out Of Quarry"
    ] == [52_150]
    assert [f.properties.duration_ms for f in alignment.unmatched_files] == [290_683]


def test_two_loose_files_that_both_fit_the_claimed_track_swap_nothing() -> None:
    """A swap has to be the only reading; two candidates for one side is a doubt, not a swap.

    A fourth, unrelated file is here so the words every title carries — which
    the matcher strips before reading names — are not the song itself.
    """
    unit = _unit("Album", (342_150, 290_683, 291_000, 254_743))
    release = _titled_release(
        (
            ("Out Of Quarry", 290_000),
            ("Out Of Quarry (Remix 2)", 340_000),
            ("Here Unto Méire", 254_000),
        )
    )

    alignment = align_tracks(
        unit,
        release,
        local_titles=(
            "Out Of Quarry",
            "Out Of Quarry - Zedd Marrow Remix",
            "Out Of Quarry - Other Remix",
            "Here Unto Meire",
        ),
    )

    paired = {a.file.properties.duration_ms: a.track.title for a in alignment.assignments}
    assert paired[342_150] == "Out Of Quarry", "nothing unequivocal refuted the name"
    assert sorted(f.properties.duration_ms for f in alignment.unmatched_files) == [290_683, 291_000]


def test_a_second_file_of_the_same_audio_stays_on_the_table_when_the_first_is_paired_by_hand() -> (
    None
):
    """One recording present twice, on a release that lists it twice.

    Pairing one of them by hand must not take both off the table: the pairing
    is held by signature, and they share one, so the other track would have
    nothing it could be offered. The file the pairing did not reach is still a
    file; the ladder pairs it to the one open track of its length, and the row
    says the length holds it, not the hand.
    """
    twice = AudioFileFacts(
        path=Path("/music/Album/05.flac"),
        content_signature="one-recording",
        file_size_bytes=1_000,
        modified_at=datetime(2001, 2, 3, tzinfo=UTC),
        properties=AudioProperties(
            codec="flac", duration_ms=392_466, sample_rate=44_100, channels=2
        ),
    )
    again = AudioFileFacts(
        path=Path("/music/Album/12.flac"),
        content_signature="one-recording",
        file_size_bytes=1_000,
        modified_at=datetime(2001, 2, 3, tzinfo=UTC),
        properties=AudioProperties(
            codec="flac", duration_ms=392_466, sample_rate=44_100, channels=2
        ),
    )
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=(_file("01.flac", 207_553), twice, again),
    )
    release = _titled_release(
        (
            ("Marrowel", 207_000),
            ("Days Of Quarrel", 394_000),
            ("Days Of Quarrel (Remix 2)", 394_000),
        )
    )

    alignment = align_tracks(
        unit,
        release,
        local_titles=("Marrowel", "Days Of Quarrel", "Days Of Quarrel"),
        pinned={"one-recording": 2},
    )

    assert not alignment.unmatched_files and not alignment.unmatched_tracks
    holds = {
        assignment.track.position: assignment.paired_by for assignment in alignment.assignments
    }
    assert holds == {1: "name", 2: "hand", 3: "length"}
    assert {
        assignment.track.position: assignment.file.path.name for assignment in alignment.assignments
    }[2] == "05.flac"


def _noise_titled_signals(**overrides: object) -> MatchSignals:
    """Signals of a folder whose titles are noise, against another artist's record.

    Four local files, a two-track release of another artist, and two lengths
    that agree inside a second or so by coincidence.
    """
    fields: dict[str, object] = {
        "local_track_count": 4,
        "release_track_count": 2,
        "durations_compared": 2,
        "durations_agreeing": 2,
        "text_similarity": 0.5,
        "titles_compared": 4,
        "titles_agreeing": 0,
        "artist_similarity": 0.2727,
        "album_similarity": 0.7619,
        "edition_marker_side": "release",
    }
    fields.update(overrides)
    return MatchSignals(**fields)  # type: ignore[arg-type]


def test_the_edition_penalty_reaches_the_candidate_whose_titles_were_called_noise() -> None:
    """The edition penalty applies on every exit of the confidence function.

    `titles_are_noise` returns before the last subtraction, so a candidate
    whose text was declared unreadable, the one case where the lengths do all
    the work, would pay nothing for disagreeing about the edition.
    """
    matcher = AlbumMatcher()
    disagreeing = _noise_titled_signals()
    agreeing = _noise_titled_signals(edition_marker_side="")

    assert matcher._confidence(agreeing) == 0.7372
    assert matcher._confidence(disagreeing) == 0.4872
    assert matcher._confidence(disagreeing) < matcher._confidence(agreeing)


def test_the_edition_penalty_reaches_a_candidate_with_no_title_evidence_at_all() -> None:
    """The second of the two exits that lifted a score and returned early."""
    matcher = AlbumMatcher()
    common: dict[str, object] = {
        "local_track_count": 2,
        "release_track_count": 2,
        "durations_compared": 2,
        "durations_agreeing": 2,
        "text_similarity": 0.9,
        "titles_compared": 0,
        "titles_agreeing": 0,
        "album_similarity": 0.9,
    }
    disagreeing = MatchSignals(**common, edition_marker_side="local")  # type: ignore[arg-type]
    agreeing = MatchSignals(**common)  # type: ignore[arg-type]

    assert matcher._confidence(disagreeing) == round(
        matcher._confidence(agreeing) - 0.25, 4
    ), "the penalty is the same number on every path that pays it"


@pytest.mark.parametrize(
    ("side", "sentence"),
    [
        ("local", "your files name an edition this release does not"),
        ("release", "this release names an edition your files do not"),
        ("each", "your files and this release name different editions"),
    ],
)
def test_the_sentence_says_which_side_the_edition_word_is_on(side: str, sentence: str) -> None:
    """A screen never names a cause it did not measure.

    One sentence for every direction would name the folder when the marker is
    the catalogue's word, on a track of the record being offered.
    """
    assert sentence in explain_evidence(_noise_titled_signals(edition_marker_side=side))


def test_the_marker_side_is_measured_from_whichever_side_carries_the_word() -> None:
    """The signal itself, over the two shapes an album actually takes."""
    unit = _unit("Mirén Zoélle - Clear Quarrel", (200_000, 300_000))
    live_release = _titled_release((("Ópen Lantern (Live)", 199_000), ("North Marrow", 299_000)))

    from_the_release = AlbumMatcher().rank(
        unit,
        (live_release,),
        search_hint="Mirén Zoélle - Clear Quarrel",
        local_titles=("Ópen Lantern", "North Marrow"),
        hint_album="Clear Quarrel",
    )[0]

    assert from_the_release.signals.edition_marker_side == "release"

    studio = _titled_release((("Ópen Lantern", 199_000), ("North Marrow", 299_000)))
    from_the_files = AlbumMatcher().rank(
        unit,
        (studio,),
        search_hint="Mirén Zoélle - Clear Quarrel (Live)",
        local_titles=("Ópen Lantern", "North Marrow"),
        hint_album="Clear Quarrel (Live)",
    )[0]

    assert from_the_files.signals.edition_marker_side == "local"
    assert from_the_files.signals.edition_mismatch, "the flag is derived from the side"


def test_both_sides_lose_the_phrase_every_track_carries_before_they_are_compared() -> None:
    """Both sides lose a phrase every title carries before they are compared.

    Four files of one length to the millisecond, each tagged with its own
    track number and its own title, and the release is those same tags. So
    every title on both sides begins with the same phrase.

    If the phrase is taken off the local titles only, the short remainder
    scores under the bar a veto needs against the release's full title, no
    file's name vetoes the positional pairing, and with four identical
    durations position is the only thing deciding: every file would be renamed
    to another's name.
    """
    titles = (
        "New Marrow - Refreshed Remix",
        "New Marrow - Refreshed Dub Remix",
        "New Marrow - Quiet Intrumental",
        "New Marrow - Quiet Dubmix",
    )
    length = 245_000
    release = _titled_release(tuple((title, length) for title in titles))
    # On disk in the order their names sort, which here is the reverse of the
    # order their tags number them in.
    on_disk = tuple(reversed(titles))
    unit = AlbumUnit(
        folder_path=Path("/music/New Marrow"),
        unit_signature="unit",
        audio_files=tuple(_file(f"{title}.flac", length) for title in on_disk),
    )

    alignment = align_tracks(unit, release, local_titles=on_disk)

    paired = {
        assignment.file.path.stem: assignment.track.title for assignment in alignment.assignments
    }
    assert paired == {title: title for title in titles}, (
        "each file belongs to the track its own tag names, and nothing here is "
        "distinguished by a duration"
    )


def test_the_shared_phrase_is_only_taken_off_the_side_that_carries_it() -> None:
    """A remastered album whose files carry the suffix: why the pruning exists.

    Every file reads `<song> - <edition phrase>` and the catalogue names the
    songs plainly, so the phrase belongs to one side and taking it off that side
    is what makes the comparison possible. Reading it as shared would put it back.
    """
    songs = ("A Hillock", "Beneath The Lanterns", "How Long Is The Quárry Road")
    release = _titled_release(tuple((song, 200_000 + index) for index, song in enumerate(songs)))
    local = tuple(f"{song} - Remastered Version" for song in songs)
    pruned = album_titles(release.tracks).pruned

    assert pruned == {}, "the release carries no phrase of its own to lose"
    for song, spoken in zip(songs, local, strict=True):
        track = next(track for track in release.tracks if track.title == song)
        assert track_title_similarity(song, track, "", album_titles(release.tracks)) == 1.0
        assert spoken.startswith(song)


def test_an_order_no_swap_would_cost_anything_is_not_corroborated_by_the_audio() -> None:
    """An order that no swap would make worse is not corroborated by the audio.

    Four files of one length against four tracks of one length pair positionally
    at zero apart, and so would any other arrangement of them. The audio chose
    nothing; the order the folder was read in did. The alignment says so, and
    every guard downstream already refuses to apply an ambiguous one on its own.

    Its opposite is a sequence that pairs within a few seconds while every swap
    lands well outside the window: there the audio does corroborate the order,
    which is what earns the wider window.
    """
    length = 245_000
    flat = _titled_release(tuple((f"Piece {index}", length) for index in range(1, 5)))
    unit = AlbumUnit(
        folder_path=Path("/music/Flat"),
        unit_signature="unit",
        audio_files=tuple(_file(f"{index:02d}.flac", length) for index in range(1, 5)),
    )

    alignment = align_tracks(unit, flat)

    assert alignment.method == "positional"
    assert alignment.is_ambiguous, "nothing here chose this reading over its mirror"
    assert not alignment.is_usable
    assert len(alignment.suggested) == 4, (
        "the refusal names the rows it is about rather than giving a count " "with no table"
    )

    # And a name is still enough to settle it: the file says which track it is.
    named = align_tracks(unit, flat, local_titles=tuple(f"Piece {i}" for i in range(1, 5)))
    assert not named.is_ambiguous, "a title that names the track outranks the coincidence"


def _published_and_typed(typed: dict[int, str]) -> tuple[ReleaseMetadata, ReleaseMetadata]:
    """A three-track release as a catalogue published it, and with titles typed over it."""
    published = _release_named(("Lantern Road", "Copper Kettle", "Salt Meadow"))
    corrected = replace(
        published,
        tracks=tuple(
            replace(track, title=typed.get(track.position, track.title))
            for track in published.tracks
        ),
    )
    return published, corrected


def test_a_typed_title_no_file_carries_does_not_unpair_the_file() -> None:
    """A title typed over the catalogue's is the name to write, not evidence against a file.

    The files carry the names the catalogue published, which is what every
    organized album's files carry. Aligned against the typed title alone, the
    file of that track answers to no track, and where its length does not settle
    it the track is reported as having no file.
    """
    published, corrected = _published_and_typed({2: "Something Else Entirely"})
    # Lengths that agree with nothing, so that only a name can pair these files.
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=(
            _file("01. Lantern Road.flac", 111_000),
            _file("02. Copper Kettle.flac", 222_000),
            _file("03. Salt Meadow.flac", 333_000),
        ),
    )
    titles = ("Lantern Road", "Copper Kettle", "Salt Meadow")

    as_published = align_tracks(unit, published, local_titles=titles)
    assert as_published.is_usable, "the pairing this test starts from has to stand"

    aligned = align_tracks(unit, corrected, local_titles=titles, published=published)

    assert aligned.is_usable, "typing a title took the file away from its track"
    assert [(one.file.path.name, one.track.position) for one in aligned.assignments] == [
        (one.file.path.name, one.track.position) for one in as_published.assignments
    ]
    second = next(one for one in aligned.assignments if one.track.position == 2)
    assert second.track.title == "Something Else Entirely", "the typed title is what is written"
    assert second.paired_by == "name", "the file's own name agrees with the track it holds"


def test_a_typed_title_a_file_carries_is_the_name_that_pairs_it() -> None:
    """Where a file answers to the typed title, the typed title is the evidence.

    An album applied with a correction holds files named by it, and the
    catalogue's own title for that track then matches no file.
    """
    published, corrected = _published_and_typed({2: "Something Else Entirely"})
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=(
            _file("01. Lantern Road.flac", 111_000),
            _file("02. Something Else Entirely.flac", 222_000),
            _file("03. Salt Meadow.flac", 333_000),
        ),
    )
    titles = ("Lantern Road", "Something Else Entirely", "Salt Meadow")

    aligned = align_tracks(unit, corrected, local_titles=titles, published=published)

    assert aligned.is_usable
    assert [one.track.title for one in aligned.assignments] == [
        "Lantern Road",
        "Something Else Entirely",
        "Salt Meadow",
    ]


def test_two_titles_typed_across_each_other_pair_by_the_typed_ones() -> None:
    """Typing each of two tracks the other's name says which file is which.

    Each file then answers to a typed title and to a published one equally, and
    the typed one is the user's statement, so it is the one that pairs.
    """
    published, corrected = _published_and_typed({1: "Copper Kettle", 2: "Lantern Road"})
    unit = AlbumUnit(
        folder_path=Path("/music/Album"),
        unit_signature="unit",
        audio_files=(
            _file("01. Lantern Road.flac", 111_000),
            _file("02. Copper Kettle.flac", 222_000),
            _file("03. Salt Meadow.flac", 333_000),
        ),
    )
    titles = ("Lantern Road", "Copper Kettle", "Salt Meadow")

    aligned = align_tracks(unit, corrected, local_titles=titles, published=published)

    assert {one.file.path.name: one.track.position for one in aligned.assignments} == {
        "01. Lantern Road.flac": 2,
        "02. Copper Kettle.flac": 1,
        "03. Salt Meadow.flac": 3,
    }
