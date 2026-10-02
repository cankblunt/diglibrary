"""Two entries a catalogue indexed inside one track are one recording.

A vinyl side that runs two songs together is listed by Discogs as `A5.1` and
`A5.2` — one physical track, printed as two lines because two songs are in it.
Every rip of that side is one file, so read as printed the album has one more
track than any copy of it can hold, and the extra one stays without a file: the
album sits in review one track short, with nothing that could complete it.

**The same notation means the opposite thing elsewhere, and getting that wrong
would destroy an album.** `1.01` and `1.02` on a two-disc release are disc one,
tracks one and two — merging them would halve the album. Measured on the
tracklists of a real library, the two readings separate with no ambiguous case:

    all positions indexed, numeric prefix   disc.track — never merge
    only a minority indexed                 inside one track — merge

That is the rule below, and it is deliberately a rule about *shape* rather than
a judgement about titles: a disc's numbering covers the disc, and an index
inside a track is a handful of lines in a list of plain ones.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace

from diglibrary.application.contracts import ReleaseMetadata, TrackMetadata

_INDEXED = re.compile(r"^([A-Za-z]*\d+)\.(\d+)$")
_LETTERED = re.compile(r"^([A-Za-z]*\d+)([a-z])$")
"""The other way the same idea is written, and it cannot decide anything alone.

`1a` and `1b` is `A5.1` and `A5.2` said differently, and the dotted pattern
cannot see it. Releases that use it are rare and do not separate the way the
dotted form does: one release letters *every* position and is a merge, another
letters only `11a` and `11b` and is also a merge, so the dotted form's "all
positions indexed means disc.track" reading has no counterpart here.

So this shape is only ever *recognized*, never trusted to authorize a fold on
its own. It says which entries would group; something that counts the album says
whether they should.
"""

JOINER = " / "
"""What joins two titles that share one track.

Chosen for the *tag*, where it survives: a `/` can never survive in a file name
— `sanitize_component` turns it into ` - ` because a slash in a path component
is a directory that was never asked for. So `First Song / Second Song` is the
tag and `First Song - Second Song` is the file, and the two are the same
answer written where each can be written.

Nothing is invented by this: both titles are what the source published, and the
separator says they are two pieces rather than one name.
"""


def indexed_position(position: str | None) -> bool:
    """Return whether this position is an entry indexed inside a track.

    Public because the identification workflow asks it before spending a request
    on a witness: a tracklist with no indexed entry has no fold to authorize.
    """
    return _base(position) is not None


def _base(position: str | None) -> str | None:
    """Return the track an entry is indexed inside, or ``None`` for a plain one.

    The dashed form (`A1-1`, `BD1-1`) is deliberately not read here. `1-1` is
    commonly disc one track one, and a release may list `A1` beside `A1-1` and
    `A1-2`, each with a length of its own — whether those children are joined
    or discarded is undecided, and a pattern that quietly folded them would
    decide it.
    """
    text = (position or "").strip()
    match = _INDEXED.match(text) or _LETTERED.match(text)
    return match.group(1) if match else None


def merge_indexed_tracks(
    release: ReleaseMetadata, audio_files: int, album_tracks: int | None = None
) -> ReleaseMetadata:
    """Fold entries the source indexed inside one track into one track.

    Three things speak here and they are not the same kind of statement:

    - ``audio_files`` says what *this copy* holds. It is the guard already
      built, and it is the reason this cannot live in the metadata mapper: a
      reissue that splits a medley across two files is a real copy of a real
      album, and folding for that copy would leave a spare file with nowhere
      to go.
    - ``album_tracks`` says what *the album* has — a witness's count, when one
      was asked and answered. It is a verdict and not a catalogue: no
      character of it reaches a name or a tag, and it is consulted only to
      choose between two readings of the source's *own* tracklist.
      It outranks the file count, because a copy can be incomplete and an album
      cannot.
    - the shape says *which* entries would group. It never authorizes on its
      own — the dotted form has one reading its own shape settles, and the
      lettered form has none.

    Returns the release unchanged whenever nothing fires, which is the
    overwhelming majority: few tracklists use either form.
    """
    tracks = release.tracks
    if len(tracks) < 2:
        return release

    bases = [_base(track.published_position) for track in tracks]
    indexed = [base for base in bases if base is not None]
    if not indexed:
        return release

    # Disc.track, not an index inside a track: a disc's numbering covers the
    # whole disc, so every position carries it. The letter is the other half —
    # `A5.1` names a vinyl side, and a side is not a disc. It is a
    # statement about the *dotted* form only, which is why the lettered one has
    # to be authorized by a count rather than by its own shape.
    dotted = [
        base
        for base, track in zip(bases, tracks, strict=True)
        if base is not None and _INDEXED.match((track.published_position or "").strip())
    ]
    numbered_only = all(base[0].isdigit() for base in indexed)
    if len(dotted) == len(indexed) == len(tracks) and numbered_only:
        return release

    lettered = len(dotted) < len(indexed)
    if album_tracks is not None:
        # The witness counted the album. Folding is right when the folded
        # reading is the count, and wrong when the printed one already is —
        # measured, not inferred.
        would_fold = len(_folded(tracks, bases))
        if album_tracks == len(tracks):
            return release
        if album_tracks != would_fold:
            return release
    elif lettered:
        # Nobody counted the album, and this shape says nothing on its own.
        return release
    elif audio_files == len(tracks):
        # The files already hold every entry separately, so the source is right
        # as printed and this is somebody else's copy of the album.
        return release

    folded = _folded(tracks, bases)

    if len(folded) == len(tracks):
        return release
    # Renumbered, because `position` is this application's own count of what a
    # copy of this album holds, and it now holds fewer.
    return replace(
        release,
        tracks=tuple(
            replace(track, position=number) for number, track in enumerate(folded, start=1)
        ),
    )


def _folded(tracks: Sequence[TrackMetadata], bases: Sequence[str | None]) -> list[TrackMetadata]:
    """Return the tracklist with each group of indexed entries as one track."""
    groups: dict[str, list[int]] = defaultdict(list)
    for index, base in enumerate(bases):
        if base is not None:
            groups[base].append(index)

    folded: list[TrackMetadata] = []
    consumed: set[int] = set()
    for index, track in enumerate(tracks):
        if index in consumed:
            continue
        base = bases[index]
        members = groups.get(base or "", [])
        if base is None or len(members) < 2:
            folded.append(track)
            continue
        consumed.update(members)
        folded.append(_one_track([tracks[member] for member in members]))
    return folded


def _one_track(members: list[TrackMetadata]) -> TrackMetadata:
    """Make one track out of the entries a source indexed inside it.

    The title carries every published title, in the order the source printed
    them. The length is their sum when the source gave all of them, because the
    file holds all of them — and nothing at all when even one is missing, since
    a partial sum is a wrong number rather than an incomplete one, and the
    matcher weighs length against the file.
    """
    lengths = [member.duration_ms for member in members]
    first = members[0]
    return replace(
        first,
        title=JOINER.join(member.title for member in members),
        duration_ms=sum(lengths) if all(length is not None for length in lengths) else None,
        isrcs=tuple(isrc for member in members for isrc in member.isrcs),
    )
