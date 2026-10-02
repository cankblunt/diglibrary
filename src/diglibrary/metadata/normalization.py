"""Pure normalization functions for canonical metadata values."""

import re
import unicodedata
from dataclasses import replace
from datetime import date

from diglibrary.application.contracts import TrackMetadata

_WHITESPACE = re.compile(r"\s+")
_DISCOGS_DISAMBIGUATION = re.compile(r"\s*\(\d+\)$")


def normalize_text(value: str | None) -> str | None:
    """Normalize Unicode and whitespace while preserving a meaningful value."""
    if value is None:
        return None
    normalized = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()
    return normalized or None


def fold_accents(value: str) -> str:
    """Return the text with its diacritics removed, for comparison and for search.

    Catalogues index what was typed, and what was typed is frequently
    unaccented: an artist may be credited without the accent the sleeve
    carries. Folding is never applied to a value that will be written — only to
    what is compared and to what is asked.
    """
    decomposed = unicodedata.normalize("NFD", value)
    return unicodedata.normalize(
        "NFC",
        "".join(character for character in decomposed if not unicodedata.combining(character)),
    )


def normalize_discogs_artist_name(value: str) -> str:
    """Remove Discogs' numeric artist disambiguation suffix from a display name."""
    normalized = normalize_text(value) or ""
    return _DISCOGS_DISAMBIGUATION.sub("", normalized)


def normalize_identifier(value: str | int | None) -> str | None:
    """Convert a provider identifier into a non-empty canonical string."""
    return normalize_text(str(value)) if value is not None else None


def parse_partial_date(value: str | None) -> date | None:
    """Parse ISO year, year-month, or full-date values using their known precision.

    A catalogue writes an unknown component as zero: Discogs publishes dates
    such as ``1951-05-00``, meaning that month with no day. Zero is not a day
    any calendar accepts, and discarding the whole date for it would name the
    album without its year. An unknown component means imprecision, never
    absence — it falls back to the first, exactly as a missing component does.
    """
    normalized = normalize_text(value)
    if normalized is None:
        return None
    parts = normalized.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return date(year, month or 1, day or 1)
    except (TypeError, ValueError):
        return None


def parse_duration_ms(value: str | int | None) -> int | None:
    """Parse MusicBrainz milliseconds or a Discogs ``MM:SS`` duration string."""
    if isinstance(value, int):
        return value if value >= 0 else None
    normalized = normalize_text(value)
    if normalized is None:
        return None
    if normalized.isdigit():
        return int(normalized)
    try:
        minutes, seconds = normalized.split(":", maxsplit=1)
        return (int(minutes) * 60 + int(seconds)) * 1000
    except ValueError:
        return None


def renumber_after_drop(tracks: tuple[TrackMetadata, ...]) -> tuple[TrackMetadata, ...]:
    """Number the tracks that remain after a non-audio entry was removed.

    A catalogue numbers what is on the disc, and an enhanced CD's data portion
    is on the disc: the audio then starts at two. A sleeve and a file listing
    both start at one. So the moment something is dropped, what is left is
    numbered as it reads on the sleeve — per medium, in order.

    Only when something was dropped. Every other release keeps the numbers its
    catalogue gave it, which is what keeps folders that are already organised
    exactly as they are.
    """
    seen: dict[int, int] = {}
    renumbered: list[TrackMetadata] = []
    for index, track in enumerate(tracks, start=1):
        medium = track.medium_number or 1
        seen[medium] = seen.get(medium, 0) + 1
        renumbered.append(replace(track, position=index, position_on_medium=seen[medium]))
    return tuple(renumbered)
