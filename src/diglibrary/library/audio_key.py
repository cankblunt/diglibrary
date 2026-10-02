"""A key for the audio a file carries, when the content signature is not enough.

``content_signature`` identifies a stream by its declared shape — codec, sample
count, sample rate, channels, bit depth. That is what makes it survive a rename
and a retag, and it is deliberately blind to the bytes. The cost is that two
*different* recordings of the same exact length in the same format are one
signature, and a measurement kept per signature would be written by the first
track measured and worn by the second. In a real library such collisions are
common, and a lossless file then displays the verdict of a lossy one.

So the quality engine keys its rows by the signature *and* this key. It answers
the one question the signature cannot: are these the same audio? It is not a
replacement for the signature and nothing else in the application uses it — the
acoustic fingerprints, the manual pairings and the user's own verdicts stay
keyed exactly as they are.

Two properties matter and both are deliberate. It must survive a retag, or a
measurement would be discarded every time a tag is written — so nothing here
reads a tag, and the window is taken at a fixed distance from where the *audio*
begins rather than from the start of the file, which moves when a tag block
grows. And it must cost almost nothing, because it is asked of a file that is
merely being listed: a FLAC usually answers from its own header, and everything
else answers from one seek and one read.
"""

import hashlib
import logging
import struct
from pathlib import Path

import mutagen

WINDOW_BYTES = 128 * 1024
"""How much of the audio to hash when the container does not name it itself.

Enough that two different recordings cannot agree across it, and small enough
that the read is one seek on any disk. It is taken from the start of the audio,
which is the only offset that stays put when a tag is rewritten.
"""

_logger = logging.getLogger(__name__)


def audio_key(path: Path) -> str | None:
    """Return a key for this file's audio, or ``None`` when none can be read.

    ``None`` is an ordinary answer, not a failure: it means this file cannot be
    told apart from another of the same shape, so the quality engine keeps
    treating it exactly as it did before this existed. Nothing raises — a file
    that cannot be opened is a fact to record, not an exception to propagate
    mid-survey.
    """
    try:
        recorded = _declared_md5(path)
        if recorded is not None:
            return recorded
        offset = _audio_offset(path)
        if offset is None:
            return None
        with path.open("rb") as handle:
            handle.seek(offset)
            window = handle.read(WINDOW_BYTES)
    except Exception as error:
        _logger.warning(
            "No audio key could be read from this file.",
            extra={
                "operation": "library.audio_key.unreadable",
                "file": str(path),
                "error": str(error),
            },
        )
        return None
    if not window:
        return None
    return f"win:{hashlib.sha256(window).hexdigest()[:32]}"


def _declared_md5(path: Path) -> str | None:
    """Return the md5 a FLAC keeps of its own decoded audio, when it wrote one.

    This is the exact answer and it is free: FLAC's STREAMINFO block carries the
    md5 of the *unencoded* samples, so it is immune to a retag by construction
    and two files agree here only when they hold the same music. Encoders are
    allowed to leave it zero and many do, which is why the window below exists
    at all.
    """
    if path.suffix.lower() != ".flac":
        return None
    parsed = mutagen.File(path)
    recorded = getattr(getattr(parsed, "info", None), "md5_signature", 0)
    if not recorded:
        return None
    return f"flac:{recorded:032x}"


def _audio_offset(path: Path) -> int | None:
    """Return where this file's audio begins, past whatever tags precede it."""
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return _mpeg_offset(path)
    if suffix == ".flac":
        return _flac_offset(path)
    if suffix == ".wav":
        return _riff_offset(path)
    if suffix in {".aiff", ".aif"}:
        return _aiff_offset(path)
    if suffix == ".m4a":
        return _mp4_offset(path)
    return None


def _mpeg_offset(path: Path) -> int | None:
    """Return where the first MPEG frame sits, which mutagen already found."""
    parsed = mutagen.File(path)
    offset = getattr(getattr(parsed, "info", None), "frame_offset", None)
    return int(offset) if offset is not None else None


def _flac_offset(path: Path) -> int | None:
    """Return where the FLAC frames begin, after walking its metadata blocks.

    The block chain is self-describing: one byte carrying the last-block flag
    and the type, then three bytes of length. Walking it is the only way to
    know where the tags stop, and the tags are exactly what must not be hashed.
    """
    with path.open("rb") as handle:
        if handle.read(4) != b"fLaC":
            return None
        while True:
            header = handle.read(4)
            if len(header) < 4:
                return None
            last = bool(header[0] & 0x80)
            length = int.from_bytes(header[1:4], "big")
            handle.seek(length, 1)
            if last:
                return handle.tell()


def _riff_offset(path: Path) -> int | None:
    """Return where a WAV's samples begin, which is inside its `data` chunk."""
    return _chunked_offset(path, b"RIFF", b"WAVE", b"data", "<I", payload_skip=0)


def _aiff_offset(path: Path) -> int | None:
    """Return where an AIFF's samples begin, past the `SSND` chunk's two words."""
    # `SSND` opens with an offset and a block size before the samples themselves,
    # so the audio starts eight bytes into the chunk rather than at its head.
    return _chunked_offset(path, b"FORM", b"AIFF", b"SSND", ">I", payload_skip=8)


def _chunked_offset(
    path: Path,
    magic: bytes,
    form: bytes,
    wanted: bytes,
    size_format: str,
    payload_skip: int,
) -> int | None:
    """Walk a RIFF-shaped file's chunks and return where one chunk's payload is.

    WAV and AIFF are the same container read with opposite endianness, so they
    are the same walk with one format string between them.
    """
    with path.open("rb") as handle:
        if handle.read(4) != magic:
            return None
        handle.seek(4, 1)
        if handle.read(4) != form:
            return None
        while True:
            header = handle.read(8)
            if len(header) < 8:
                return None
            name = header[:4]
            (length,) = struct.unpack(size_format, header[4:])
            if name == wanted:
                return handle.tell() + payload_skip
            # Chunks are word-aligned, so an odd length is followed by a pad
            # byte that belongs to nobody and would derail the next header.
            handle.seek(length + (length % 2), 1)


def _mp4_offset(path: Path) -> int | None:
    """Return where an MP4's `mdat` payload begins, walking the top-level atoms."""
    with path.open("rb") as handle:
        while True:
            header = handle.read(8)
            if len(header) < 8:
                return None
            (length,) = struct.unpack(">I", header[:4])
            name = header[4:]
            if length == 1:
                # The 64-bit form: the real length follows the name, and it is
                # the one an `mdat` of any size actually uses.
                extended = handle.read(8)
                if len(extended) < 8:
                    return None
                (length,) = struct.unpack(">Q", extended)
                if name == b"mdat":
                    return handle.tell()
                handle.seek(length - 16, 1)
                continue
            if name == b"mdat":
                return handle.tell()
            if length < 8:
                return None
            handle.seek(length - 8, 1)
