"""What the audio proof must survive, and what it must catch."""

import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from diglibrary.library.integrity import audio_proof

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "audio"
_MD5_OFFSET = 4 + 4 + 18
"""Where STREAMINFO keeps its MD5: past ``fLaC``, past the block header, past
the eighteen bytes of stream properties that precede it."""


def _flac_without_md5(destination: Path) -> Path:
    """Copy the fixture and blank the field an encoder is allowed to skip."""
    shutil.copy(FIXTURES / "tone.flac", destination)
    raw = bytearray(destination.read_bytes())
    raw[_MD5_OFFSET : _MD5_OFFSET + 16] = bytes(16)
    destination.write_bytes(bytes(raw))
    assert FLAC(destination).info.md5_signature == 0
    return destination


def test_flac_with_a_signature_still_reads_it(tmp_path: Path) -> None:
    """The cheap proof stays the first choice: one header, no pass over the file."""
    copied = tmp_path / "tone.flac"
    shutil.copy(FIXTURES / "tone.flac", copied)

    proof = audio_proof(copied)

    assert proof is not None
    assert proof.method == "flac-streaminfo-md5"


def test_flac_without_a_signature_is_proved_by_its_frames(tmp_path: Path) -> None:
    """An encoder may leave the MD5 blank; such a file is still verifiable."""
    proof = audio_proof(_flac_without_md5(tmp_path / "tone.flac"))

    assert proof is not None, "an absent MD5 is not an absent proof"
    assert proof.method == "flac-frames-sha256"


def test_retagging_leaves_the_frame_proof_alone(tmp_path: Path) -> None:
    """The whole point: tags are supposed to change, the recording is not."""
    path = _flac_without_md5(tmp_path / "tone.flac")
    before = audio_proof(path)

    tags = FLAC(path)
    tags["title"] = ["Something else entirely"]
    tags["comment"] = ["padding grows, frames do not move"] * 40
    tags.save()

    assert audio_proof(path) == before


def test_a_touched_frame_is_caught(tmp_path: Path) -> None:
    """A proof that survives the audio changing would be decoration."""
    path = _flac_without_md5(tmp_path / "tone.flac")
    before = audio_proof(path)

    raw = bytearray(path.read_bytes())
    raw[-32] ^= 0xFF
    path.write_bytes(bytes(raw))

    after = audio_proof(path)
    assert after is not None
    assert after.digest != before.digest


def test_a_trailing_tag_is_not_audio(tmp_path: Path) -> None:
    """An ID3v1 block appended to a FLAC is a label, and labels are excluded."""
    path = _flac_without_md5(tmp_path / "tone.flac")
    before = audio_proof(path)

    with path.open("ab") as handle:
        handle.write(b"TAG" + bytes(125))

    assert audio_proof(path) == before


@pytest.mark.parametrize("content", [b"", b"fLaC", b"not a flac at all"])
def test_an_unreadable_flac_says_so(tmp_path: Path, content: bytes) -> None:
    """A container this cannot parse is reported unverifiable, never guessed at."""
    path = tmp_path / "broken.flac"
    path.write_bytes(content)

    assert audio_proof(path) is None
