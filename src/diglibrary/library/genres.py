"""One spelling per genre, whichever catalogue said it and however it was typed.

Discogs chooses its genres and styles from a fixed list and writes them one way;
MusicBrainz's are typed by contributors and arrive in lower case, so one genre
reaches a library in several spellings — `Jazz-Funk` and `jazz-funk`, `MPB` and
`mpb`, and Discogs itself holding one style both as one word and as two.
"""

import re
import unicodedata
from collections.abc import Iterable

from diglibrary.application.contracts import MetadataSources, ReleaseMetadata

GENRE_SPELLINGS: tuple[str, ...] = (
    "Bossa Nova",
    "Contemporary R&B",
    "Jazz-Funk",
    "MPB",
)
"""The one spelling a value matching any of these is written as.

A catalogue spelling correction, holding only what the rules below would get
wrong: Discogs writes one style both as one word and as two, and the list keeps
the two-word form; `mpb` and `contemporary r&b` arrive from MusicBrainz, where raising
each word's first letter gives `Mpb` and `Contemporary R&b`; and `Jazz-Funk`
gathers the variants below under one spelling.

A value matches when it is the same word once case, accents, spaces and hyphens
are set aside, so `jazz funk`, `Jazz-funk` and `jazzfunk` all become `Jazz-Funk`.
`[naming] genre_spellings` in the configuration adds entries and, for a word
already listed, replaces its spelling.
"""

_NOT_A_LETTER = re.compile(r"[^a-z0-9]")
_WORD_START = re.compile(r"(^|[\s-])([a-z])")


def spell_genres(
    release: ReleaseMetadata, values: Iterable[str], extra: Iterable[str] = ()
) -> tuple[str, ...]:
    """Return ``values`` of ``release`` in one spelling each, in order and without repeats.

    A value on the list takes the list's spelling. Otherwise a Discogs value is
    kept as Discogs wrote it, since that catalogue already writes each one a
    single way, and any other source's value has each word's first letter
    raised — `alternative hip hop` becomes `Alternative Hip Hop`.
    """
    from_discogs = release.source == MetadataSources.DISCOGS
    spellings = {_key(spelling): spelling for spelling in (*GENRE_SPELLINGS, *extra)}
    spelled: dict[str, str] = {}
    for value in values:
        key = _key(value)
        if not key or key in spelled:
            continue
        if key in spellings:
            spelled[key] = spellings[key]
        elif from_discogs:
            spelled[key] = value.strip()
        else:
            spelled[key] = _WORD_START.sub(
                lambda match: match.group(1) + match.group(2).upper(), value.strip()
            )
    return tuple(spelled.values())


def _key(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return _NOT_A_LETTER.sub("", "".join(ch for ch in folded if not unicodedata.combining(ch)))
