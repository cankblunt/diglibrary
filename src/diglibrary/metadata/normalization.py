"""Pure normalization functions for canonical metadata values."""

import re
import unicodedata
from dataclasses import replace
from datetime import date

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)

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


PARALLEL_JOIN = " = "
"""How a catalogue writes one name in two languages: `original = translation`."""


def parallel_forms(value: str) -> tuple[str, ...]:
    """Return each spelling of a name written `one = other`, or the name alone.

    A catalogue that prints a title in two scripts joins them with an equals
    sign between spaces. Each side is the whole name, so each side is what a
    file's own tag may hold.
    """
    parts = tuple(part.strip() for part in value.split(PARALLEL_JOIN))
    return tuple(part for part in parts if part) or (value,)


def is_latin(text: str) -> bool:
    """Whether every letter of this text belongs to the Latin script.

    The first four blocks of Unicode are Latin whatever a character is called
    there: `º` in `Nº 1` is a letter named for what it indicates, not for its
    script.
    """
    return all(
        ord(character) < 0x250 or unicodedata.name(character, "").startswith("LATIN")
        for character in text
        if character.isalpha()
    )


def latin_spelling(value: str) -> str:
    """Return the Latin spelling of a name printed in two scripts, where it has one.

    A catalogue prints such a name as `original = translation`, leading with
    the sleeve's script. A library is sorted, grouped and searched by typing,
    so the side written in Latin letters is the one kept, and the other is
    dropped rather than carried behind an equals sign.

    Only where one side is wholly Latin and another is not. Two titles in two
    Latin-script languages are both kept, in the catalogue's order, and so is a
    title that merely contains an equals sign; a name with no Latin side is
    returned as the catalogue wrote it. A side that repeats an earlier one,
    compared without case or accents, is written once.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for part in parallel_forms(value):
        folded = fold_accents(part).casefold()
        if folded not in seen:
            seen.add(folded)
            kept.append(part)
    latin = [part for part in kept if is_latin(part)]
    if latin and len(latin) < len(kept):
        kept = latin
    return PARALLEL_JOIN.join(kept)


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


_PARALLEL_CREDIT_JOIN = "="
"""What Discogs prints between one credit and the same credit in another script."""


def artists_in_latin_spelling(artists: tuple[ArtistMetadata, ...]) -> tuple[ArtistMetadata, ...]:
    """Name each artist once, in Latin letters where the catalogue has them.

    A sleeve printed in two scripts is entered as the same artists twice, the
    two credits joined by an equals sign, each entry carrying the spelling one
    side printed. Read as a list, that is every artist named twice, and written
    by the catalogue's own name it is one name on both sides of the sign.

    Each artist is kept once, in the order first met. The name is the
    catalogue's own when that is in Latin letters, and otherwise the first
    Latin spelling a sleeve credit gives; an artist with neither keeps the
    catalogue's name. The same choice is made for an artist entered once, so a
    name is not written in one script on one record and another on the next
    for want of an equals sign.

    Reading its own answer changes nothing, because a stored release is read
    through it again.
    """
    if not any((artist.joined_by or "").strip() == _PARALLEL_CREDIT_JOIN for artist in artists):
        return tuple(_named_in_latin(artist, (artist.credited_as,)) for artist in artists)
    met: dict[str, list[ArtistMetadata]] = {}
    for artist in artists:
        key = artist.external_ids.get(MetadataSources.DISCOGS) or artist.name.casefold()
        met.setdefault(key, []).append(artist)
    named: list[ArtistMetadata] = []
    for position, entries in enumerate(met.values(), start=1):
        joins = [(entry.joined_by or "").strip() for entry in entries]
        # What joins this artist to the next is read from the side printed in
        # Latin letters, and the last artist left is joined to nobody.
        join = next(
            (one for one in joins if one and one != _PARALLEL_CREDIT_JOIN and is_latin(one)), None
        )
        named.append(
            replace(
                _named_in_latin(entries[0], tuple(entry.credited_as for entry in entries)),
                joined_by=join if position < len(met) else None,
            )
        )
    return tuple(named)


def _named_in_latin(artist: ArtistMetadata, printed: tuple[str | None, ...]) -> ArtistMetadata:
    """Return the artist under a Latin name, given how the sleeve spelled them."""
    latin = next((spelling for spelling in printed if spelling and is_latin(spelling)), None)
    if is_latin(artist.name):
        if latin is None:
            # Nothing Latin was printed: what stands beside the name is the
            # other script, kept for comparison, and it stays there.
            return artist
        return replace(artist, credited_as=latin if latin != artist.name else None)
    if latin is None:
        return artist
    # The catalogue's own spelling stays beside the one written, for comparison
    # only: files tagged in the original script are still files by this artist.
    return replace(artist, name=latin, credited_as=artist.name)


def release_in_latin_spelling(release: ReleaseMetadata) -> ReleaseMetadata:
    """Return a Discogs release as the rule in force names it.

    The spelling is chosen when a catalogue's answer is mapped, and a release
    is stored as mapped. One stored before the rule existed still holds both
    scripts, and an album planned again around the release on record asks no
    catalogue, so it would go on being named by a rule no longer in force.
    Everything the rule reads is in what was stored, so it is applied again on
    the way out. A release already in that form comes back equal.

    Only the catalogue whose convention this is. Another catalogue's title may
    hold an equals sign that joins no translation.
    """
    if release.source != MetadataSources.DISCOGS:
        return release
    return replace(
        release,
        title=latin_spelling(release.title),
        artists=artists_in_latin_spelling(release.artists),
        tracks=tuple(
            replace(
                track,
                title=latin_spelling(track.title),
                artists=artists_in_latin_spelling(track.artists),
            )
            for track in release.tracks
        ),
    )
