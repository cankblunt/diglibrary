"""Every byte reader here is fed rubbish on purpose, because it will be.

These three read raw bytes out of files this application did not create: an
album downloaded from a stranger, an old rip, a file truncated by a drive that
filled up. Each one walks declared lengths and offsets, and each
one decides where to seek next from a number the file itself supplied — the
shape where a hostile or simply broken value becomes an unhandled exception, an
enormous allocation, or a loop that never ends.

The contract they all share is that an unreadable file is an ordinary answer:
`None`, and the album goes on. This is that contract measured rather than
assumed, over a seeded and fast set of mutated inputs.

Seeded because a test that fails now and then on a random input nobody
recorded is worse than no test. When this does fail, the seed and the index name
the exact bytes.
"""

import random
import signal
from pathlib import Path

import pytest

from diglibrary.library.artwork import describe_bytes
from diglibrary.library.audio_key import audio_key
from diglibrary.library.integrity import audio_proof

SEED = 10007

CONTAINERS = {
    ".flac": b"fLaC\x00\x00\x00\x22" + bytes(34) + b"\x00" * 200,
    ".mp3": b"ID3\x04\x00\x00\x00\x00\x00\x20\xff\xfb\x90\x00" + b"\x00" * 200,
    ".wav": (
        b"RIFF\xf0\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        + b"\x00" * 40
        + b"data\x80\x00\x00\x00"
        + b"\x00" * 128
    ),
    ".aiff": (
        b"FORM\xf0\x00\x00\x00AIFFCOMM\x00\x00\x00\x12"
        + b"\x00" * 18
        + b"SSND\x00\x00\x00\x80"
        + b"\x00" * 128
    ),
    ".m4a": bytes([0, 0, 0, 0x14])
    + b"ftypM4A "
    + bytes(8)
    + bytes([0, 0, 0, 0x20])
    + b"mdat"
    + b"\x00" * 200,
}

IMAGES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 40,
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
)


def _mutate(source: bytes, entropy: random.Random) -> bytes:
    """Damage these bytes the way a truncated download or a hostile file does."""
    damaged = bytearray(source)
    for _ in range(entropy.randint(1, 15)):
        if not damaged:
            break
        at = entropy.randrange(len(damaged))
        choice = entropy.random()
        if choice < 0.45:
            damaged[at] = entropy.randrange(256)
        elif choice < 0.70:
            # Four bytes at once, which is how a declared length becomes absurd.
            damaged[at : at + 4] = entropy.randbytes(4)
        elif choice < 0.85:
            del damaged[at : at + entropy.randint(1, 16)]
        else:
            damaged[at : at + 1] = entropy.randbytes(entropy.randint(1, 16))
    return bytes(damaged)


class _TookTooLongError(Exception):
    pass


def _within(seconds: int):
    """Fail rather than hang: a loop that never ends is the defect, not a delay."""

    def ring(*_: object) -> None:
        raise _TookTooLongError()

    return signal.signal(signal.SIGALRM, ring), signal.alarm(seconds)


def test_a_damaged_image_header_is_never_more_than_an_unreadable_image() -> None:
    """`describe_bytes` decides an image's size from lengths inside it, and it
    is fed whatever the Cover Art Archive or a file's own tag supplied."""
    entropy = random.Random(SEED)

    for index in range(4000):
        damaged = _mutate(IMAGES[index % len(IMAGES)], entropy)
        try:
            facts = describe_bytes(damaged)
        except Exception as error:
            pytest.fail(f"input {index} raised {type(error).__name__}: {error}")
        assert facts is None or (facts.width > 0 and facts.height > 0)


@pytest.mark.parametrize("suffix", sorted(CONTAINERS))
def test_a_damaged_container_is_never_more_than_an_unreadable_file(
    tmp_path: Path, suffix: str
) -> None:
    """`audio_key` and `audio_proof` seek by numbers the file declares about
    itself. A file that cannot be read is `None` and the album goes on — an
    exception here stops a scan, and a hang stops the window."""
    entropy = random.Random(SEED)
    target = tmp_path / f"probe{suffix}"

    previous, _ = _within(0)
    try:
        for index in range(400):
            target.write_bytes(_mutate(CONTAINERS[suffix], entropy))
            for name, read in (("audio_key", audio_key), ("audio_proof", audio_proof)):
                signal.alarm(10)
                try:
                    read(target)
                except _TookTooLongError:
                    pytest.fail(f"{name} did not finish on input {index} of {suffix}")
                except Exception as error:
                    pytest.fail(f"{name} raised on input {index} of {suffix}: {error!r}")
                finally:
                    signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, previous)
