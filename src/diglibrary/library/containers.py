"""Telling an album folder apart from a folder that merely holds albums."""

import os
from collections.abc import Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import NamedTuple


def names_the_folder(own_tracks: int, foreign_tracks: int) -> bool:
    """Report whether an album may give its folder a name.

    The whole rule: **a folder that shelves albums is never renamed.**
    ``foreign_tracks`` counts audio in subfolders that is not part of this
    album, so one such file is enough to leave the name alone — a folder of
    discographies must keep its name even when a stray track lands in it.

    Two things deliberately do not count as shelving. Audio sitting *directly*
    beside the album is that album's own mess, not somebody else's record. And
    a disc subfolder is the album itself, so ``CD1``/``CD2`` never make a
    multi-disc album look like a shelf.
    """
    return own_tracks > 0 and foreign_tracks == 0


def count_audio_below(folder: Path, audio_extensions: Iterable[str]) -> int:
    """Count the audio in subfolders of ``folder``, at any depth, by reading the disk.

    Audio sitting directly in the folder is deliberately not counted: a stray
    file beside an album is that album's own mess, not somebody else's record.
    What makes a folder a container is what it *shelves*.

    This does not consult a scan result. It is the second of two independent
    locks on a folder rename, and a lock that trusts the thing it guards is
    one lock. Hidden entries are skipped and symbolic links are not
    followed, matching what the scanner considers to exist at all.
    """
    extensions = frozenset(extension.lower() for extension in audio_extensions)
    total = 0
    for directory, subdirectories, filenames in os.walk(folder):
        subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
        if Path(directory) == folder:
            continue
        total += sum(
            1
            for name in filenames
            if not name.startswith(".")
            and Path(name).suffix.lower() in extensions
            and not (Path(directory) / name).is_symlink()
        )
    return total


SHORTEST_RUN = 4
"""How long a run of track numbers has to be before it means anything.

A folder where every file is tagged track 1 is sloppy tagging, and there are
plenty of those: measured on a real library, requiring a run of at least four
is what separates the folders that really hold more than one album from the
ones whose numbers are simply unfilled.
"""

_BITRATE_GAP = 32_000
"""How far two bitrates must sit apart before one copy is the better one.

A real difference in rip quality shows up as tens of kilobits per second,
while VBR noise on one rip shows up as a few.
"""

_SAME_SONG = 0.85
"""How alike two titles must read before they count as the same song.

`One Two Three` and `One, Two, Three` are one song written twice, and that is the
difference between a folder holding two copies of an album and a folder holding
two different albums.
"""


class NumberedTrack(NamedTuple):
    """One file's own claim about where it sits, and how good it is.

    ``disc`` is the scanner's answer where it has one, because a file out of a
    `CD2` folder is on disc two whatever its tags omit. ``bitrate`` is read from
    the stream header, so it costs nothing beyond the scan that already happened
    — which is what lets a duplicated rip say which copy is worth keeping.
    """

    disc: str
    number: int
    title: str
    bitrate: int | None = None


def numbers_deny_one_album(numbered: Sequence[NumberedTrack], files: int) -> str:
    """Say what a folder's own track numbers prove about it, or nothing.

    Arithmetic, not a heuristic. Two twelve-track albums in one folder means every
    number from 1 to 12 is present exactly twice and nothing else is present at
    all — so the folder cannot be one album, whatever any catalogue says, and
    pairing half of it is answering the wrong question.

    ``numbered`` is one entry per file that carries a numeric track number, each
    ``(disc, number, title)``. Returns a sentence when the numbers deny that this
    is one album, and ``""`` when they do not — which is almost always.

    The sentence distinguishes the two shapes by the titles, because the count
    alone cannot: a repeated number carrying *the same song* is one album ripped
    twice, and carrying *different songs* is different albums. Measured on a
    real library, it fires on very few folders and is right about each.

    Deliberately silent unless everything lines up. A file with no number, a
    second disc, or an uneven count all mean the arithmetic does not close, and a
    folder this cannot speak about is a folder it says nothing about.
    """
    if files <= 0 or len(numbered) != files:
        return ""
    if len({track.disc for track in numbered}) != 1:
        return ""
    songs: dict[int, list[NumberedTrack]] = {}
    for track in numbered:
        if track.number < 1:
            return ""
        songs.setdefault(track.number, []).append(track)
    longest = max(songs)
    if longest < SHORTEST_RUN or sorted(songs) != list(range(1, longest + 1)):
        return ""
    counts = {len(titles) for titles in songs.values()}
    if len(counts) != 1:
        return ""
    copies = counts.pop()
    if copies < 2:
        return ""
    repeated_same_song = sum(1 for group in songs.values() if _one_song(group))
    if repeated_same_song > longest - repeated_same_song:
        return (
            f"This folder holds {copies} copies of the same {longest}-track album: every "
            f"number from 1 to {longest} appears {copies} times, on the same songs. "
            f"{_which_copy_wins(songs)}Keep one copy and scan again."
        )
    if repeated_same_song < longest - repeated_same_song:
        return (
            f"The track numbers here describe {copies} different albums of {longest} "
            f"tracks: every number from 1 to {longest} appears {copies} times, on "
            "different songs. Put each album in its own folder and scan again."
        )
    return (
        f"The track numbers here describe {copies} runs of {longest} tracks, and the "
        f"titles do not say whether that is {copies} albums or {copies} copies of one. "
        "Sort the folder out and scan again."
    )


def _one_song(group: Sequence[NumberedTrack]) -> bool:
    """Report whether every one of these titles reads as the same song."""
    first = group[0].title.casefold()
    return all(
        SequenceMatcher(None, first, other.title.casefold()).ratio() >= _SAME_SONG
        for other in group[1:]
    )


def _which_copy_wins(songs: Mapping[int, Sequence[NumberedTrack]]) -> str:
    """Say which copy measures better, when the streams themselves say so.

    Counted number by number rather than by any grouping of the files, because
    nothing here knows which file belongs to which copy — one rip is named
    `01 - Song.mp3` and the other `01. Song.mp3`, and reading a convention out of
    that would be a guess. What can be measured is per number: how often the two
    copies differ, and by how much.

    Silent when the streams do not disagree, which is the honest answer for two
    copies of one rip. The application says which copy is better and the user
    does the discarding: nothing here deletes audio.
    """
    gaps: list[tuple[int, int]] = []
    for group in songs.values():
        rates = [track.bitrate for track in group if track.bitrate]
        if len(rates) != len(group) or len(rates) < 2:
            continue
        if max(rates) - min(rates) >= _BITRATE_GAP:
            gaps.append((max(rates), min(rates)))
    if not gaps or len(gaps) <= len(songs) / 2:
        return ""
    best = round(sum(high for high, _ in gaps) / len(gaps) / 1000)
    worst = round(sum(low for _, low in gaps) / len(gaps) / 1000)
    return (
        f"On {len(gaps)} of them the two copies differ in bitrate, averaging "
        f"{best} against {worst} kbps — keep the higher one. "
    )
