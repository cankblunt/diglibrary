"""Proving that the music itself is untouched, whatever happened to its labels.

Every container this project writes offers a way to fingerprint the audio
stream without the tags around it. FLAC is the gift: the encoder already
stored an MD5 of the *decoded* audio in STREAMINFO, so reading the proof costs
one header — and when an encoder skipped that field, the frames themselves are
hashed instead. The others take one pass over the file, hashing only
the bytes that carry sound: MP3's frames between its tag blocks, MP4's ``mdat``
box, and the sample chunk of WAV and AIFF.

The proof is deliberately about the audio and nothing else. Tags, names, and
pictures are supposed to change; the recording is not, ever.
"""

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

from mutagen.flac import FLAC

_CHUNK = 1 << 20
_ID3V1_SIZE = 128
_APE_FOOTER_SIZE = 32


@dataclass(frozen=True, slots=True)
class AudioProof:
    """Purpose: fingerprint one file's audio stream, excluding all metadata.

    Responsibilities: pair the method used with the digest it produced, so two
    proofs are only ever compared like with like. Boundaries: it proves nothing
    about tags or names — those are supposed to change. Dependencies: none.
    Collaborators: the API's apply path, which captures one before writing and
    one after. Constraints: ``method`` names how the digest was taken, because
    a FLAC STREAMINFO MD5 and a frame-range SHA-256 are not comparable values.
    """

    method: str
    digest: str


def audio_proof(path: Path) -> AudioProof | None:
    """Fingerprint the audio stream of one file, or ``None`` when it cannot be.

    ``None`` is an honest answer, not a failure: a container this cannot parse
    is reported as unverifiable rather than guessed at, and the certificate
    says so.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            return _flac_proof(path)
        if suffix == ".mp3":
            return _framed_proof(path, "mp3-frames-sha256")
        if suffix == ".m4a":
            return _mp4_proof(path)
        if suffix in (".wav", ".aiff", ".aif"):
            return _chunk_proof(path)
    except OSError:
        return None
    return None


def _flac_proof(path: Path) -> AudioProof | None:
    """Prove a FLAC's audio: the encoder's own MD5 when there is one, else its frames."""
    try:
        signature = getattr(FLAC(path).info, "md5_signature", 0)
    except Exception:
        signature = 0
    if signature:
        return AudioProof(method="flac-streaminfo-md5", digest=f"{signature:032x}")
    # An encoder is allowed to skip the field, and a real library holds files
    # written by one that did. Zero means unset, so there is no decoded-audio
    # digest to read — but the frames are right there, and hashing them is the
    # same proof an MP3 gets.
    return _flac_frames_proof(path)


def _flac_frames_proof(path: Path) -> AudioProof | None:
    """Hash a FLAC's frame stream: everything after the last metadata block.

    The metadata blocks are exactly what a retag rewrites — including the
    padding it grows into — and the frames are exactly what it must not touch.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        start = _flac_frames_start(handle, size)
        if start is None:
            return None
        return AudioProof(
            method="flac-frames-sha256",
            digest=_digest_range(handle, start, _audio_end(handle, size)),
        )


def _flac_frames_start(handle, size: int) -> int | None:
    """Return where the frames begin, walking the metadata blocks to find out."""
    handle.seek(0)
    header = handle.read(10)
    position = 0
    if len(header) == 10 and header[:3] == b"ID3":
        # Unusual but legal: an ID3v2 block may precede the FLAC signature.
        footer = 10 if header[5] & 0x10 else 0
        position = 10 + _synchsafe(header[6:10]) + footer
    handle.seek(position)
    if handle.read(4) != b"fLaC":
        return None
    position += 4
    while position + 4 <= size:
        handle.seek(position)
        block = handle.read(4)
        if len(block) < 4:
            return None
        last = bool(block[0] & 0x80)
        position += 4 + int.from_bytes(block[1:4], "big")
        if last:
            return position if position <= size else None
    return None


def _framed_proof(path: Path, method: str) -> AudioProof:
    """Hash an MP3's bytes between its leading and trailing tag blocks.

    ID3v2 sits at the front and declares its own size; ID3v1 and an APEv2
    footer sit at the end at fixed, detectable offsets. What remains is the
    frame stream — and the frame stream is the recording.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        start = 0
        header = handle.read(10)
        if len(header) == 10 and header[:3] == b"ID3":
            declared = _synchsafe(header[6:10])
            footer = 10 if header[5] & 0x10 else 0
            start = 10 + declared + footer
        return AudioProof(
            method=method, digest=_digest_range(handle, start, _audio_end(handle, size))
        )


def _audio_end(handle, size: int) -> int:
    """Return where the audio stops: before an ID3v1 tag or an APEv2 block, if any."""
    end = size
    if size >= _ID3V1_SIZE:
        handle.seek(size - _ID3V1_SIZE)
        if handle.read(3) == b"TAG":
            end = size - _ID3V1_SIZE
    ape_end = _ape_start(handle, end)
    return ape_end if ape_end is not None else end


def _ape_start(handle, end: int) -> int | None:
    """Return where an APEv2 block starts, when one ends at ``end``."""
    if end < _APE_FOOTER_SIZE:
        return None
    handle.seek(end - _APE_FOOTER_SIZE)
    footer = handle.read(_APE_FOOTER_SIZE)
    if len(footer) != _APE_FOOTER_SIZE or footer[:8] != b"APETAGEX":
        return None
    declared = int.from_bytes(footer[12:16], "little")
    flags = int.from_bytes(footer[20:24], "little")
    header = 32 if flags & (1 << 31) else 0
    start = end - declared - header
    return start if start >= 0 else None


def _mp4_proof(path: Path) -> AudioProof | None:
    """Hash the ``mdat`` box, which is where an MP4 container keeps the sound."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        position = 0
        while position + 8 <= size:
            handle.seek(position)
            header = handle.read(16)
            if len(header) < 8:
                return None
            length = int.from_bytes(header[:4], "big")
            kind = header[4:8]
            offset = 8
            if length == 1 and len(header) >= 16:
                length = int.from_bytes(header[8:16], "big")
                offset = 16
            elif length == 0:
                length = size - position
            if length < offset:
                return None
            if kind == b"mdat":
                return AudioProof(
                    method="mp4-mdat-sha256",
                    digest=_digest_range(handle, position + offset, position + length),
                )
            position += length
    return None


def _chunk_proof(path: Path) -> AudioProof | None:
    """Hash the sample chunk of a RIFF or AIFF file: ``data`` or ``SSND``."""
    with path.open("rb") as handle:
        magic = handle.read(12)
        if len(magic) < 12:
            return None
        if magic[:4] == b"RIFF":
            wanted, big_endian = b"data", False
        elif magic[:4] == b"FORM":
            wanted, big_endian = b"SSND", True
        else:
            return None
        size = path.stat().st_size
        position = 12
        while position + 8 <= size:
            handle.seek(position)
            header = handle.read(8)
            if len(header) < 8:
                return None
            kind = header[:4]
            length = int.from_bytes(header[4:8], "big" if big_endian else "little")
            if kind == wanted:
                return AudioProof(
                    method=f"{wanted.decode('ascii').lower()}-chunk-sha256",
                    digest=_digest_range(handle, position + 8, position + 8 + length),
                )
            position += 8 + length + (length & 1)
    return None


def _digest_range(handle, start: int, end: int) -> str:
    """SHA-256 one byte range of an open file."""
    digest = hashlib.sha256()
    handle.seek(start)
    remaining = max(0, end - start)
    while remaining:
        block = handle.read(min(_CHUNK, remaining))
        if not block:
            break
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


def _synchsafe(raw: bytes) -> int:
    """Decode ID3v2's seven-bit size encoding."""
    value = 0
    for byte in struct.unpack("4B", raw):
        value = (value << 7) | (byte & 0x7F)
    return value
