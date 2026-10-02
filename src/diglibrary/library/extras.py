"""Tracks a user adds to a release because their copy holds them.

A release is what a catalogue published, and a copy sometimes holds a recording
that catalogue never listed. Such a file may be left alone, or the user may say
which track it is. The position is the one the user gives — never read out of
the file's own name, because that number describes the order of the copy while
the plan being written renumbers everything to the release's order, and one
figure must not carry two meanings.

Nothing here invents a value. The title comes from the file the user pointed
at, and only ever from it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from diglibrary.application.contracts import ReleaseMetadata, TrackMetadata


@dataclass(frozen=True, slots=True)
class ExtraTrack:
    """Purpose: name one file the user adopted and the position given to it.

    Responsibilities: carry that number, the title read from the file itself,
    and what that file measured. Boundaries: it decides nothing and reads no
    disk. Dependencies: none. Collaborators: the release expansion below.
    Constraints: the title is the file's, never a catalogue's and never
    invented; the position is the user's, never taken from a file name.
    """

    position: int
    title: str
    duration_ms: int | None = None


def next_free_position(release: ReleaseMetadata) -> int:
    """Return the position after the release's last, which is the offered default.

    The append case is the one that costs nothing: the catalogue's own tracks
    keep the numbers it gave them, and the new one sits beyond anything the
    catalogue claims. A copy holding one recording more than the release lists
    is the common case, and with this default it needs no typing at all.
    """
    positions = [
        track.position
        for track in without_extra_tracks(release).tracks
        if track.position is not None
    ]
    return max(positions, default=0) + 1


def claims(extras: tuple[ExtraTrack, ...]) -> dict[int, str]:
    """Say which position holds which added title, for ``without_extra_tracks``."""
    return {extra.position: extra.title for extra in extras}


def with_extra_tracks(release: ReleaseMetadata, extras: tuple[ExtraTrack, ...]) -> ReleaseMetadata:
    """Return the release with the added tracks inserted, renumbering what follows.

    Inserting at a position the release already uses moves that track and every
    one after it down by one. *Insert at N* and *renumber the rest* are one
    operation seen from two sides, which is why they are not two functions and
    why building only the append case would have to be rebuilt the first time a
    middle track appeared.

    When more than one has been added, each number is the one that track holds in
    the **finished** list, not an offset into the catalogue's numbering. Two
    added to a three-track release at 1 and at 4 end up first and fourth, and
    the third catalogue track is pushed past the second of them. That is the
    reading the screen can keep its promise about, because the list on screen
    is the finished one.

    A track's `published_position` is deliberately **not** shifted: it is what
    the source printed, verbatim, and it goes on being true about the source
    however the copy is renumbered. `position_on_medium`
    follows `position` only where the two already agreed, so a multi-disc
    release keeps its own per-disc count rather than being given a made-up one.
    """
    if not extras:
        return without_extra_tracks(release)
    tracks = sorted(
        without_extra_tracks(release, claims(extras)).tracks,
        key=lambda track: (track.position is None, track.position or 0),
    )
    for extra in sorted(extras, key=lambda one: one.position):
        moved = []
        for track in tracks:
            if track.position is not None and track.position >= extra.position:
                shifted = track.position + 1
                on_medium = (
                    track.position_on_medium + 1
                    if track.position_on_medium == track.position
                    else track.position_on_medium
                )
                moved.append(replace(track, position=shifted, position_on_medium=on_medium))
            else:
                moved.append(track)
        moved.append(
            TrackMetadata(
                title=extra.title,
                position=extra.position,
                position_on_medium=extra.position,
                duration_ms=extra.duration_ms,
                added_by_user=True,
            )
        )
        tracks = sorted(moved, key=lambda track: (track.position is None, track.position or 0))
    return replace(release, tracks=tuple(tracks))


def without_extra_tracks(
    release: ReleaseMetadata, claimed: Mapping[int, str] | None = None
) -> ReleaseMetadata:
    """Return the release as the catalogue published it, with added tracks taken out.

    **Growing is done from here, always.** A release that has already been grown
    is handed around, re-planned and grown again; without this an added track
    is inserted a second time and the numbering reports that the addition
    renumbered the catalogue. The track says of itself that it was added
    (`added_by_user`), so removing them is exact rather than inferred, and the
    numbering closes back up behind them.

    ``claimed`` is the same question asked of the *store* rather than of the
    track — each adopted position, against the title the adopted file carries
    there — and it exists because the mark does not survive being cached: a
    release is written to `metadata_releases` and read back without it, so a
    grown release that made that round trip looks exactly like a catalogue that
    published one more track. Grown again, the album would carry the added
    track twice, the second time at a position no file could hold.

    **The title is checked, not only the position**, and that is the whole
    safety of it. A claimed position is a position the catalogue may also have
    published: on an insertion, track 5 is the added one on a grown release and
    the catalogue's own on a clean one, and removing by position alone would
    delete a real track the first time one is inserted in the middle. Matching
    the title being put there answers *is this the one I would insert* rather
    than *is something sitting where I would insert*. Neither way invents a
    value; both read what is already there.
    """
    named = dict(claimed or {})
    kept = [
        track
        for track in release.tracks
        if not track.added_by_user
        and not (track.position in named and track.title == named[track.position])
    ]
    if len(kept) == len(release.tracks):
        return release
    renumbered = []
    for index, track in enumerate(
        sorted(kept, key=lambda one: (one.position is None, one.position or 0)), start=1
    ):
        if track.position is None:
            renumbered.append(track)
            continue
        on_medium = (
            index if track.position_on_medium == track.position else (track.position_on_medium)
        )
        renumbered.append(replace(track, position=index, position_on_medium=on_medium))
    return replace(release, tracks=tuple(renumbered))


def renumbers_the_catalogue(release: ReleaseMetadata, extras: tuple[ExtraTrack, ...]) -> bool:
    """Whether any added position moves a track the catalogue had numbered.

    The append case creates no disagreement: nothing the catalogue numbered is
    renumbered. An insertion does, and the folder then carries numbers that no
    longer match the release named in its own header. That is allowed, on
    condition the screen stops calling that numbering the catalogue's.
    """
    if not extras:
        return False
    last = max(
        (
            track.position
            for track in without_extra_tracks(release, claims(extras)).tracks
            if track.position is not None
        ),
        default=0,
    )
    return any(extra.position <= last for extra in extras)
