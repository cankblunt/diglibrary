"""Tests for the key that tells two recordings of the same shape apart."""

import shutil
from pathlib import Path

import mutagen
import pytest

from diglibrary.library.audio_key import audio_key

FIXTURES = Path(__file__).parents[1] / "fixtures" / "audio"

EVERY_FORMAT = ("tone.flac", "tone.mp3", "tone.wav", "tone.aiff", "tone.m4a")


@pytest.mark.parametrize("name", EVERY_FORMAT)
def test_every_supported_container_answers(name: str) -> None:
    """A format that cannot answer would silently keep the defect it exists to fix.

    Each of these is a different walk to the same place — mutagen's frame
    offset for MPEG, the metadata block chain for FLAC, a chunk header for WAV
    and AIFF, an atom for MP4 — so each is asserted rather than assumed.
    """
    assert audio_key(FIXTURES / name) is not None


def test_a_flac_answers_from_the_md5_it_already_carries() -> None:
    """FLAC records an md5 of its own decoded samples, so the exact answer is free."""
    key = audio_key(FIXTURES / "tone.flac")

    assert key is not None
    assert key.startswith("flac:")


def test_two_different_recordings_do_not_share_a_key() -> None:
    """One measurement per audio, not per declared shape."""
    assert audio_key(FIXTURES / "tone.flac") != audio_key(FIXTURES / "tone-long.flac")


@pytest.mark.parametrize("name", ["tone.mp3", "tone.flac"])
def test_a_retag_leaves_the_key_alone(tmp_path, name: str) -> None:
    """A key that moved when a tag was written would discard the measurement.

    A cached measurement is only worth keeping if its key survives a retag and
    a rename. A window taken from the start of the *file* would fail here,
    because writing a longer tag pushes the audio further down — which is
    exactly what this asserts by writing one far longer than what was there.
    """
    copy = tmp_path / name
    shutil.copy2(FIXTURES / name, copy)
    before = audio_key(copy)

    tags = mutagen.File(copy, easy=True)
    tags["title"] = ["A" * 5_000]
    tags["artist"] = ["B" * 5_000]
    tags.save()

    assert copy.stat().st_size > (FIXTURES / name).stat().st_size
    assert audio_key(copy) == before


def test_a_flac_whose_encoder_left_no_md5_still_answers(tmp_path) -> None:
    """Encoders often leave the md5 at zero, so the fallback is a common path.

    Without it, two recordings of the same declared shape share one key, and a
    lossless file is shown the verdict measured on the other.
    """
    copy = tmp_path / "silent-md5.flac"
    shutil.copy2(FIXTURES / "tone.flac", copy)
    blob = bytearray(copy.read_bytes())
    # STREAMINFO opens the file after the four-byte magic and a four-byte block
    # header, and its last sixteen bytes are the md5 an encoder may leave unset.
    blob[26:42] = b"\x00" * 16
    copy.write_bytes(bytes(blob))

    key = audio_key(copy)

    assert key is not None
    assert key.startswith("win:")


def test_a_flac_answers_the_same_after_its_tag_block_grows_past_the_padding(tmp_path) -> None:
    """The block chain has to be walked, not guessed at a fixed offset.

    Writing a long tag moves the audio further down the file. A key read from
    a fixed offset would change, and the measurement of audio nobody touched
    would be thrown away.
    """
    copy = tmp_path / "grown.flac"
    shutil.copy2(FIXTURES / "tone.flac", copy)
    blob = bytearray(copy.read_bytes())
    blob[26:42] = b"\x00" * 16
    copy.write_bytes(bytes(blob))
    before = audio_key(copy)

    tags = mutagen.File(copy, easy=True)
    tags["title"] = ["X" * 20_000]
    tags.save()

    assert audio_key(copy) == before


def test_a_container_this_does_not_know_is_answered_with_none(tmp_path) -> None:
    """`None` is an ordinary answer: the file keeps behaving exactly as it does today."""
    unknown = tmp_path / "something.ogg"
    unknown.write_bytes(b"OggS" + b"\x00" * 4_000)

    assert audio_key(unknown) is None


def test_a_file_that_cannot_be_read_is_reported_rather_than_raised(tmp_path) -> None:
    """One unreadable file must never end a survey of a library."""
    broken = tmp_path / "truncated.flac"
    broken.write_bytes(b"fLaC\x00")

    assert audio_key(broken) is None
