"""Telling which files of an album are not in the format the album is in."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from diglibrary.library.models import AudioFileFacts


@dataclass(frozen=True, slots=True)
class OtherFormat:
    """Purpose: say that one file's format is not the one its album is in.

    Responsibilities: carry the file's own sample rate and bit depth, the ones
    most of the album shares, and how many files share them. Boundaries: it is
    an observation about declared stream properties and claims nothing about
    where the file came from or what recording it holds. Dependencies: none.
    Collaborators: the album payload, which puts it on the file's own row.
    Constraints: a bit depth is ``None`` for a codec that declares none.
    """

    sample_rate: int
    bit_depth: int | None
    album_sample_rate: int
    album_bit_depth: int | None
    sharing: int


def _differs(own: tuple[int, int | None], album: tuple[int, int | None]) -> bool:
    """Return whether two formats are provably different.

    A bit depth that one side does not declare is not a difference: a lossy
    file beside lossless ones is already said by the codec, and it has no depth
    to compare.
    """
    if own[0] != album[0]:
        return True
    return own[1] is not None and album[1] is not None and own[1] != album[1]


def files_in_another_format(files: Sequence[AudioFileFacts]) -> dict[Path, OtherFormat]:
    """Return the files whose format is not the one most of the album shares.

    An album ripped or bought as one thing has one sample rate and one bit
    depth, so a file that has another came from somewhere else. The album's
    format is the one a strict majority of its readable files share; without
    such a majority nothing is the album's format, and no file is singled out.
    """
    formats = {
        file.path: (file.properties.sample_rate, file.properties.bit_depth)
        for file in files
        if file.properties is not None and file.properties.sample_rate > 0
    }
    if not formats:
        return {}
    album, sharing = Counter(formats.values()).most_common(1)[0]
    if sharing * 2 <= len(formats):
        return {}
    return {
        path: OtherFormat(
            sample_rate=own[0],
            bit_depth=own[1],
            album_sample_rate=album[0],
            album_bit_depth=album[1],
            sharing=sharing,
        )
        for path, own in formats.items()
        if _differs(own, album)
    }
