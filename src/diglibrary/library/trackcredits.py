"""Purpose: decide when a file's own artist credit outlives a silent catalogue.

Responsibilities: read each file's ``artist`` tag, judge whether those tags
tell an album's tracks apart, and say which files carry a credit the release
would otherwise replace without publishing one of its own.
Boundaries: nothing here decides whether an album is a compilation, and
nothing here is consulted where the source published a credit for the track.
Dependencies: the canonical metadata contracts and a tag reader.
Collaborators: the change planner, which keeps the credit, and the
identification workflow, which declines to stop at a source that would drop it.
Constraints: one judgement for both, because the planner keeping what the
workflow did not notice — or the reverse — is one rule applied by half.
"""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from diglibrary.application.contracts import (
    ArtistMetadata,
    ReleaseMetadata,
    TrackMetadata,
    written_credit,
)
from diglibrary.library.matching import TrackAlignment
from diglibrary.library.models import AlbumUnit
from diglibrary.library.tags import TagStore
from diglibrary.metadata.normalization import fold_accents

FileCredits = Mapping[Path, tuple[str, ...]]
"""Each file's ``artist`` tag as the file holds it, by path. No entry, no tag."""


def read_file_credits(unit: AlbumUnit, tag_store: TagStore | None) -> dict[Path, tuple[str, ...]]:
    """Return the ``artist`` tag of every file that has one and can be read.

    A file that cannot be read contributes nothing rather than an empty
    credit: that is an absence of evidence, and the track is planned as it
    would have been without this module.
    """
    credits: dict[Path, tuple[str, ...]] = {}
    if tag_store is None:
        return credits
    for file in unit.audio_files:
        try:
            values = tag_store.read(file.path).get("artist", ())
        except Exception:
            continue
        kept = tuple(value.strip() for value in values if value and value.strip())
        if kept:
            credits[file.path] = kept
    return credits


def _folded(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(fold_accents(value).casefold().strip() for value in values)


def credits_distinguish(credits: FileCredits) -> bool:
    """Whether these artist tags tell the album's tracks apart at all.

    The same question ``tag_titles_distinguish`` asks of titles, with the
    opposite default: a credit every file repeats is a fact about the album,
    which is the release's to state, so it is only where the files disagree
    among themselves that a file's credit says something about its own track.
    """
    return len({_folded(values) for values in credits.values()}) > 1


def _is_the_releases_credit(values: tuple[str, ...], release: ReleaseMetadata) -> bool:
    """Whether a file's credit is the release's own, however it is spelled.

    Compared folded, so a file that writes the album's artist without an accent
    or in another case is still read as agreeing and gets the catalogue's
    spelling. Compared whole: a file crediting one of two artists a release
    names is not crediting the release.
    """
    names = tuple(artist.name for artist in release.artists if artist.name.strip())
    if not names:
        return False
    folded = _folded(values)
    return set(folded) == set(_folded(names)) or folded == _folded(
        (written_credit(release.artists),)
    )


def _keeps_its_own(
    track: TrackMetadata, values: tuple[str, ...] | None, release: ReleaseMetadata
) -> bool:
    """Whether this pairing is one where the file's credit stands.

    Only where the source said nothing for the track. A credit the catalogue
    published, or one the user typed, is never set aside for a tag.
    """
    if track.artists or (track.artist_credit and track.artist_credit.strip()):
        return False
    return values is not None and not _is_the_releases_credit(values, release)


def drops_file_credits(
    release: ReleaseMetadata, alignment: TrackAlignment, credits: FileCredits
) -> bool:
    """Whether this release publishes no credit where the files hold differing ones.

    A release credits itself to somebody and leaves every track bare, which is
    complete for an ordinary album and silent for a tribute, a split or a
    compilation filed under a name: written as it stands, every track takes
    the release's credit and the performers the files name are gone. This does
    not refuse the release. It says the answer leaves a question open that
    another source may answer.
    """
    if not credits_distinguish(credits):
        return False
    return any(
        _keeps_its_own(assignment.track, credits.get(assignment.file.path), release)
        for assignment in alignment.assignments
    )


def keeping_file_credits(
    release: ReleaseMetadata, alignment: TrackAlignment, credits: FileCredits
) -> TrackAlignment:
    """Return the pairing with each file's own credit on a track the source left bare.

    Leaving a value alone is not inventing one: the source did not publish who
    performs the track, the file says, and the album's files do not all say the
    same thing — so the release's credit is not an answer about this track. The
    values are carried exactly as the tag holds them.

    Put on the pairing rather than applied to the tags afterwards, because the
    pairing is what both the tag and the file name are rendered from: kept in
    the tag alone, a compilation's file names would lose the credit the tags
    still carried.
    """
    if not credits_distinguish(credits):
        return alignment
    assignments = tuple(
        (
            replace(
                assignment,
                track=replace(
                    assignment.track,
                    artists=tuple(ArtistMetadata(name=value) for value in values),
                ),
            )
            if (values := credits.get(assignment.file.path)) is not None
            and _keeps_its_own(assignment.track, values, release)
            else assignment
        )
        for assignment in alignment.assignments
    )
    return replace(alignment, assignments=assignments)
