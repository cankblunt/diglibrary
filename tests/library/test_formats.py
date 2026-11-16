"""Unit tests for telling which files are not in their album's format."""

from datetime import datetime
from pathlib import Path

from diglibrary.library.audio import AudioProperties
from diglibrary.library.formats import files_in_another_format
from diglibrary.library.models import AudioFileFacts


def _file(name: str, rate: int, depth: int | None, codec: str = "flac") -> AudioFileFacts:
    return AudioFileFacts(
        path=Path(f"/music/album/{name}"),
        content_signature=f"sig-{name}",
        file_size_bytes=1,
        modified_at=datetime(2001, 2, 3),
        properties=AudioProperties(
            codec=codec, duration_ms=200_000, sample_rate=rate, channels=2, bit_depth=depth
        ),
    )


def _album(*formats: tuple[int, int | None]) -> tuple[AudioFileFacts, ...]:
    return tuple(
        _file(f"{index:02d}.flac", rate, depth) for index, (rate, depth) in enumerate(formats, 1)
    )


def test_the_one_file_in_another_format_is_named_with_both_formats() -> None:
    """Eight files at one rate and depth and one at another: the one is singled out."""
    files = _album(*[(44_100, 16)] * 7, (96_000, 24), (44_100, 16))

    found = files_in_another_format(files)

    assert list(found) == [Path("/music/album/08.flac")]
    other = found[Path("/music/album/08.flac")]
    assert (other.sample_rate, other.bit_depth) == (96_000, 24)
    assert (other.album_sample_rate, other.album_bit_depth, other.sharing) == (44_100, 16, 8)


def test_an_album_in_one_format_singles_out_nothing() -> None:
    """The ordinary album has nothing to say."""
    assert files_in_another_format(_album(*[(48_000, 24)] * 10)) == {}


def test_a_difference_in_bit_depth_alone_is_a_difference() -> None:
    """The same rate at another depth is another format."""
    found = files_in_another_format(_album((44_100, 16), (44_100, 16), (44_100, 24)))

    assert list(found) == [Path("/music/album/03.flac")]


def test_without_a_strict_majority_nothing_is_the_albums_format() -> None:
    """Half and half names no file: neither half is the one that does not belong."""
    assert files_in_another_format(_album(*[(44_100, 16)] * 4, *[(96_000, 24)] * 4)) == {}
    assert files_in_another_format(_album((44_100, 16), (96_000, 24))) == {}


def test_every_file_outside_the_majority_is_named_and_not_only_a_lone_one() -> None:
    """The rule is the complement of the album's format, however many files that is."""
    found = files_in_another_format(_album(*[(44_100, 16)] * 10, (96_000, 24), (48_000, 24)))

    assert sorted(path.name for path in found) == ["11.flac", "12.flac"]


def test_a_depth_one_side_does_not_declare_is_not_a_difference() -> None:
    """A lossy file at the album's rate has no depth to disagree with."""
    files = (*_album(*[(44_100, 16)] * 3), _file("04.mp3", 44_100, None, codec="mp3"))

    assert files_in_another_format(files) == {}


def test_a_file_that_could_not_be_read_is_neither_counted_nor_named() -> None:
    """No properties means nothing is known, which is not another format."""
    unread = AudioFileFacts(
        path=Path("/music/album/09.flac"),
        content_signature="sig-09",
        file_size_bytes=1,
        modified_at=datetime(2001, 2, 3),
    )

    assert files_in_another_format((*_album((44_100, 16), (44_100, 16)), unread)) == {}
    assert files_in_another_format((unread,)) == {}
