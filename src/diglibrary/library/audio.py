"""Reading an audio file's stream properties, deliberately ignoring its tags."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import mutagen

AUDIO_EXTENSIONS = frozenset({".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp3"})
"""Lossless formats plus MP3, per the formats decision for v1."""


@dataclass(frozen=True, slots=True)
class AudioProperties:
    """Purpose: describe one audio stream independently of its container's tags.

    Responsibilities: carry the technical facts that identify the audio itself.
    Boundaries: it holds no artist, title, or any other tag value, and performs
    no I/O. Dependencies: none. Collaborators: ``AudioProbe`` implementations
    and the content signature. Constraints: every field here comes from the
    stream header, so editing tags never changes any of them — which is what
    makes the content signature survive a re-tag.
    """

    codec: str
    duration_ms: int
    sample_rate: int
    channels: int
    total_samples: int | None = None
    bit_depth: int | None = None
    bitrate: int | None = None


class AudioProbe(Protocol):
    """Read stream properties from one audio file, or report that it cannot.

    Injected rather than called directly so that scanning is testable without
    real media, and so a future ffprobe-backed probe can replace this one.
    """

    def read(self, path: Path) -> AudioProperties | None:
        """Return stream properties, or ``None`` when the file cannot be parsed."""


class MutagenAudioProbe:
    """Purpose: read stream properties with mutagen, across the supported formats.

    Responsibilities: normalize the differing ``info`` attributes of FLAC, WAV,
    AIFF, MP4, and MP3 into one shape. Boundaries: it never reads or writes a
    tag, and it never raises for an unreadable file — an unparseable file is a
    fact to record, not a failure to propagate mid-scan. Dependencies: mutagen.
    Collaborators: ``LibraryScanner``. Constraints: a format that reports no
    sample rate or channel count is treated as unreadable, because a partial
    signature would silently compare unequal to itself later.
    """

    def read(self, path: Path) -> AudioProperties | None:
        """Return stream properties, or ``None`` when the file cannot be parsed."""
        try:
            parsed = mutagen.File(path)
        except Exception:
            return None
        info = getattr(parsed, "info", None)
        if info is None:
            return None
        sample_rate = _integer(getattr(info, "sample_rate", None))
        channels = _integer(getattr(info, "channels", None))
        if not sample_rate or not channels:
            return None
        return AudioProperties(
            codec=str(getattr(info, "codec", None) or type(parsed).__name__).lower(),
            duration_ms=round(float(getattr(info, "length", 0.0)) * 1000),
            sample_rate=sample_rate,
            channels=channels,
            total_samples=_integer(getattr(info, "total_samples", None)),
            bit_depth=_integer(getattr(info, "bits_per_sample", None)),
            bitrate=_integer(getattr(info, "bitrate", None)),
        )


def _integer(value: object) -> int | None:
    return (
        int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    )
