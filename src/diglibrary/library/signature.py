"""Content signatures that identify audio across renames and tag edits."""

import hashlib
import json
from collections.abc import Iterable

from diglibrary.library.audio import AudioProperties


def content_signature(properties: AudioProperties | None, file_size_bytes: int) -> str:
    """Return a stable signature for one file's audio content.

    The signature is derived only from stream properties, never from the path or
    the tags, so renaming a file or rewriting its tags leaves it unchanged. That
    is what lets a scan skip work it already did.

    It identifies *content*, not a *recording*: two different tracks with the
    same codec, length, sample rate, and channel count collide. That is
    acceptable for deciding what to re-examine, and it is why proving two files
    hold the same recording is left to acoustic fingerprinting.

    A file whose stream cannot be parsed falls back to its size, which does
    change when tags are rewritten. Such a file is simply re-examined later.
    """
    if properties is None:
        payload: list[object] = ["unparsed", file_size_bytes]
    else:
        payload = [
            properties.codec,
            # Exact sample count when the format reports it, since it is the
            # most discriminating value available; otherwise seconds at
            # millisecond precision, which is all a lossy format can promise.
            (
                properties.total_samples
                if properties.total_samples is not None
                else round(properties.duration_ms / 1000, 3)
            ),
            properties.sample_rate,
            properties.channels,
            properties.bit_depth,
        ]
    return _digest(payload)


def unit_signature(content_signatures: Iterable[str]) -> str:
    """Return a stable signature for a whole album unit.

    Member signatures are sorted rather than taken in file order, so that
    renaming the tracks inside a folder does not produce a new unit.
    """
    return _digest(sorted(content_signatures))


def _digest(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
