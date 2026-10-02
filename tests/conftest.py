"""Shared test fixtures.

Cover-art tests need real images rather than arbitrary bytes: the whole point of
``describe_bytes`` is that it reads a header, and mutagen writes what it is
given. These builders produce structurally valid files of any size, which keeps
the suite offline and free of binary fixtures.
"""

import struct
import zlib
from collections.abc import Callable

import pytest


def build_jpeg(width: int, height: int) -> bytes:
    """Return a JPEG with the requested dimensions in its frame header."""
    start_of_image = b"\xff\xd8"
    jfif = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return start_of_image + jfif + frame + b"\xff\xd9"


def build_png(width: int, height: int) -> bytes:
    """Return a PNG with the requested dimensions in its header chunk."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IEND", b"")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


@pytest.fixture
def jpeg() -> Callable[[int, int], bytes]:
    """Build a JPEG of a given size."""
    return build_jpeg


@pytest.fixture
def png() -> Callable[[int, int], bytes]:
    """Build a PNG of a given size."""
    return build_png
