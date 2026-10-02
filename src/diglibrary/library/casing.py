"""One way of writing a title's capitals, whoever typed it into the catalogue.

A catalogue's titles arrive in the case each contributor happened to type, and
a real library holds a sizeable share that differ from the rule below —
`Tales Of The Sea` beside `Tales of the sea`. The rule: every word takes a
capital, the words that join them do not, and the first and last word of a
title — or of any part of it — always do.

Only case moves. No letter, accent, space or punctuation is added or removed,
so what the catalogue wrote is still what is written, and everything this
module does is undone by comparing without case.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import replace
from itertools import pairwise

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)

_PORTUGUESE = frozenset(
    {
        "a", "à", "às", "ao", "aos", "o", "os", "as", "e", "ou", "nem",
        "de", "do", "dos", "da", "das", "d'",
        "em", "no", "nos", "na", "nas", "num", "numa", "nuns", "numas",
        "por", "pelo", "pela", "pelos", "pelas",
        "para", "pra", "pro", "pras", "pros", "com", "sem",
    }
)  # fmt: skip
_ENGLISH = frozenset(
    {
        "a", "an", "the", "and", "or", "nor", "but",
        "of", "in", "on", "at", "to", "by", "for", "from", "with",
    }
)  # fmt: skip
_SPANISH = frozenset(
    {
        "de", "del", "la", "las", "el", "los", "y", "e", "o", "u",
        "en", "con", "por", "para", "a", "al", "sin",
    }
)  # fmt: skip
_SMALL = {"pt": _PORTUGUESE, "en": _ENGLISH, "es": _SPANISH}
"""The joining words each language writes in lower case, and nothing else.

A closed list, and deliberately short: `um`, `que`, `se`, `sobre`, `entre`,
`até` and `contra` stay capitalized, because each of them is also a numeral,
a pronoun or a verb.
"""

_CONTENT_ELSEWHERE = {
    "no": frozenset({"en", "es"}),
    "do": frozenset({"en"}),
    "da": frozenset({"en"}),
    "na": frozenset({"en"}),
    "em": frozenset({"en"}),
    "as": frozenset({"en"}),
    "d'": frozenset({"en"}),
    "dos": frozenset({"es"}),
    "nos": frozenset({"es"}),
    "la": frozenset({"en", "pt"}),
    "sin": frozenset({"en"}),
    "al": frozenset({"en"}),
    "u": frozenset({"en"}),
    "for": frozenset({"pt"}),
}
"""The joining words that are a word of their own in another of the three.

`Kids of No Town` and `(I Won't Say No) Tonight` are English, where
`No` is the whole point of the line; lowering it as a Portuguese preposition
is the commonest mistake a rule without this table makes. These are decided by
the language of the part they sit in, then of the release; where neither says,
``_PRESUMED`` answers, because leaving the catalogue's case alone would keep
more names wrong than the presumption gets wrong among names with no marker
word.
"""
_PRESUMED = "pt"

_ARTICLES = frozenset({"o", "a", "os", "as", "the", "el", "la", "los", "las"})

_MARKERS = {
    "pt": frozenset(
        {
            "não", "você", "eu", "meu", "minha", "meus", "minhas", "seu", "sua",
            "nós", "são", "é", "mais", "vou", "vem", "quando", "onde", "coração",
            "pra", "pro", "pelo", "pela", "pelos", "pelas", "com", "sem",
            "ao", "aos", "à", "às", "num", "numa", "nem", "ou", "das", "uma",
        }
    ),
    "en": frozenset(
        {
            "the", "of", "and", "to", "in", "on", "at", "by", "from",
            "with", "you", "your", "i", "i'm", "my", "it", "it's", "is", "are",
            "don't", "can't", "ain't", "love", "baby", "what", "we", "all",
            "get", "got", "this", "that", "be", "when", "how", "just", "like",
            "let's", "up", "down", "out", "night", "time", "man", "woman",
            "girl", "know", "want", "need", "feel", "dance", "who", "why",
            "there", "was", "have", "has", "gonna", "yeah",
        }
    ),
    "es": frozenset(
        {
            "el", "los", "las", "y", "del", "con", "en", "yo", "mi", "muy",
            "qué", "corazón", "un", "una", "tú",
        }
    ),
}  # fmt: skip
"""Words that say which language a part of a title is in.

Only words that belong to one of the three; `de`, `que`, `para` and every word
in ``_CONTENT_ELSEWHERE`` say nothing, because they are exactly what is being
decided. `uma` is Portuguese and `una` Spanish, so each counts for its own.
"""
_LETTERS = {"pt": "ãõçâêô", "en": "", "es": "ñ"}
"""Letters one of the three writes and the other two do not."""

_APOSTROPHE = "\u2019"
"""The typographic apostrophe, which catalogues write as often as the plain one."""
_STAND_INS = "`\u00b4"
"""The grave and acute marks, typed for an apostrophe by whoever had no other key."""
_DASHES = "\u2013\u2014"
_TOKEN = re.compile(rf"[^\W_]+(?:['{_APOSTROPHE}{_STAND_INS}][^\W_]+)*")
_INITIALISM = re.compile(r"(?<![^\W_])(?:[^\W\d_]\.){2,}")
_BREAK = re.compile(rf"\s[-{_DASHES}]\s|[:/()\[\]{{}}{_DASHES}!?¿¡“”«»\"•·]")
_ARTIST_BREAK = re.compile(rf"\s[-{_DASHES}]\s|[:/()\[\]{{}}{_DASHES}!?¿¡“”«»\"•·&,+]")
_DISAMBIGUATOR = re.compile(r"\s\(\d+\)$")


def normalize_release(
    release: ReleaseMetadata, user_titles: frozenset[int] = frozenset()
) -> ReleaseMetadata:
    """Return the release with its album, track and artist names written one way.

    Applied to what a catalogue published, and only that: a release read from
    the user's own tags is their words and comes back untouched, and so does a
    track they added, a title they corrected (``user_titles``, by position) and
    a credit they wrote.
    """
    if release.source == MetadataSources.TAGS:
        return release
    names, fallback = _credited_names(release), _release_language(release)
    return replace(
        release,
        title=normalize_title(release.title, names, fallback),
        artists=tuple(_artist(artist, fallback) for artist in release.artists),
        tracks=tuple(
            _named_track(track, names, fallback, title_from_user=track.position in user_titles)
            for track in release.tracks
        ),
    )


def normalize_album_names(release: ReleaseMetadata) -> ReleaseMetadata:
    """Return the release with its title and credit in one case, its tracks as published.

    For the screens that name an album and none of its tracks — the shelf's
    cards are drawn hundreds at a time, and normalizing every track title to
    read one line is a cost that grows with the whole shelf.
    """
    if release.source == MetadataSources.TAGS:
        return release
    fallback = _release_language(release)
    return replace(
        release,
        title=normalize_title(release.title, _credited_names(release), fallback),
        artists=tuple(_artist(artist, fallback) for artist in release.artists),
    )


def normalize_track(
    track: TrackMetadata, release: ReleaseMetadata, *, title_from_user: bool = False
) -> TrackMetadata:
    """Return one of the release's tracks under the same rule as the release.

    Asked of each paired track as well as of the release, because a file's name
    and tags are drawn from the track its pairing carries.
    """
    if release.source == MetadataSources.TAGS:
        return track
    return _named_track(
        track, _credited_names(release), _release_language(release), title_from_user=title_from_user
    )


def _named_track(
    track: TrackMetadata,
    names: tuple[str, ...],
    fallback: str | None,
    *,
    title_from_user: bool,
) -> TrackMetadata:
    if track.added_by_user:
        return track
    title = track.title if title_from_user else normalize_title(track.title, names, fallback)
    artists = track.artists
    if track.artist_credit is None:
        artists = tuple(_artist(artist, fallback) for artist in track.artists)
    return replace(track, title=title, artists=artists)


def normalize_title(
    text: str, proper_names: Iterable[str] = (), fallback: str | None = None
) -> str:
    """Return ``text`` with each word capitalized and each joining word lowered.

    ``proper_names`` are the names credited on the release, so an article that
    opens one keeps its capital (`The Return of The Night Band`).
    ``fallback`` is the language the release as a whole is written in, asked
    only where the part a word sits in does not say.
    """
    return _normalize(text, frozenset(_folded_names(proper_names)), fallback, artist=False)


def normalize_artist(name: str, fallback: str | None = None) -> str:
    """Return an artist's name under the same rule, with its articles kept.

    A name is a proper noun from end to end, so every article in it belongs to
    one (`Fulano & Os Amigos`); and `&`, `,` and `+` open a new name.
    """
    return _normalize(name, frozenset(), fallback, artist=True)


def is_shouted(text: str) -> bool:
    """Report whether a value is written entirely in capitals.

    Such a value is left exactly as the catalogue wrote it — lowering it would
    also lower every acronym inside, which nothing can tell apart — and, since
    the rule did not speak, the disk keeps its own case against it.
    """
    cased = [character for character in text if character.isupper() or character.islower()]
    return len(cased) >= 2 and all(character.isupper() for character in cased)


def _normalize(text: str, names: frozenset[str], fallback: str | None, *, artist: bool) -> str:
    if not text or is_shouted(text):
        return text
    breaks = _ARTIST_BREAK if artist else _BREAK
    kept = [match.span() for match in _INITIALISM.finditer(text)]
    languages = _part_languages(text, breaks)
    pieces: list[str] = []
    cursor = 0
    for match in _TOKEN.finditer(text):
        start, end = match.span()
        word = match.group()
        pieces.append(text[cursor:start])
        cursor = end
        if _left_alone(word, text, start, end, kept):
            pieces.append(word)
            continue
        # The first and the last word of every part take a capital: the last
        # is where a joining word stops joining, as `On` does at the end of
        # `Carry It On`.
        edge = _opens_a_part(text[:start], breaks) or _closes_a_part(text[end:], breaks)
        language = languages(start) or fallback or _PRESUMED
        pieces.append(_cased(word, text[start:], edge, language, names, artist))
    pieces.append(text[cursor:])
    return "".join(pieces)


def _left_alone(word: str, text: str, start: int, end: int, kept: list[tuple[int, int]]) -> bool:
    """A number, an initialism, an abbreviation, or a word cased inside (`iPhone`).

    Also the second half of a word joined by a point with no space, which is
    still one word (`inter.lude`), so it takes no capital of its own.
    """
    if word[0].isdigit():
        return True
    if start >= 2 and text[start - 1] == "." and text[start - 2].isalpha():
        return True
    if any(low <= start and end <= high for low, high in kept):
        return True
    if text[end : end + 1] == "." and (len(word) == 1 or _small_anywhere(word.casefold())):
        return True
    return word[0].islower() and any(character.isupper() for character in word[1:])


def _cased(
    word: str,
    rest: str,
    edge: bool,
    language: str | None,
    names: frozenset[str],
    artist: bool,
) -> str:
    folded = word.casefold()
    elided = folded[:2] in ("d'", f"d{_APOSTROPHE}") and len(folded) > 2
    key = "d'" if elided else folded
    if edge or not _small_anywhere(key):
        return _capitalized(word, elided)
    if key in _ARTICLES and (artist or _opens_a_name(rest, word, names)):
        return _capitalized(word, elided)
    elsewhere = _CONTENT_ELSEWHERE.get(key, frozenset())
    if not elsewhere or (language is not None and key in _SMALL[language]):
        return _lowered(word, elided)
    if language in elsewhere:
        return _capitalized(word, elided)
    return word


def _small_anywhere(folded: str) -> bool:
    return any(folded in small for small in _SMALL.values())


def _capitalized(word: str, elided: bool) -> str:
    if elided:
        return word[:2].capitalize() + _first_up(word[2:])
    return _first_up(word)


def _lowered(word: str, elided: bool) -> str:
    if elided:
        return word[:2].lower() + _first_up(word[2:])
    return word.lower()


def _first_up(word: str) -> str:
    raised = word[:1].upper()
    return (raised if len(raised) == 1 else word[:1]) + word[1:]


def _opens_a_part(before: str, breaks: re.Pattern[str]) -> bool:
    """Whether nothing but punctuation stands between a word and the last break."""
    last = 0
    for found in breaks.finditer(before):
        last = found.end()
    return not any(character.isalnum() for character in before[last:])


def _closes_a_part(after: str, breaks: re.Pattern[str]) -> bool:
    """Whether nothing but punctuation stands between a word and the next break."""
    found = breaks.search(after)
    return not any(character.isalnum() for character in after[: found.start() if found else None])


def _opens_a_name(rest: str, word: str, names: frozenset[str]) -> bool:
    """Whether the article begins, or stands before, a name credited on the release."""
    folded = rest.casefold()
    after = folded[len(word) :].lstrip()
    return any(_starts_with_name(candidate, names) for candidate in (folded, after))


def _starts_with_name(text: str, names: frozenset[str]) -> bool:
    for name in names:
        if text.startswith(name) and not text[len(name) : len(name) + 1].isalnum():
            return True
    return False


def _part_languages(text: str, breaks: re.Pattern[str]):
    """Return a lookup from a position in ``text`` to the language of its part."""
    bounds = [0, *(found.end() for found in breaks.finditer(text)), len(text) + 1]
    parts = list(pairwise(bounds))
    decided = [_language_of([text[low:high]], at_least=1, times=1) for low, high in parts]

    def lookup(position: int) -> str | None:
        for (low, high), language in zip(parts, decided, strict=True):
            if low <= position < high:
                return language
        return None

    return lookup


def _language_of(parts: Iterable[str], *, at_least: int, times: int) -> str | None:
    """The one language whose words outnumber every other's ``times`` over, or ``None``.

    A part of a title is decided by a plain majority of one; the release as a
    whole, which is only asked when a part says nothing, needs two words and
    twice the runner-up, because it is answering for a line that gave no sign.
    """
    counts = dict.fromkeys(_MARKERS, 0)
    for part in parts:
        for token in _TOKEN.findall(part):
            folded = token.casefold().replace(_APOSTROPHE, "'")
            for language, markers in _MARKERS.items():
                if folded in markers or any(letter in folded for letter in _LETTERS[language]):
                    counts[language] += 1
        counts["es"] += part.count("¿") + part.count("¡")
    (best, top), (_, second) = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:2]
    if top >= at_least and top > second * times:
        return best
    return None


def _release_language(release: ReleaseMetadata) -> str | None:
    """The language of the release as a whole, from every title it carries."""
    titles = [release.title, *(track.title for track in release.tracks)]
    return _language_of(titles, at_least=2, times=2)


def _credited_names(release: ReleaseMetadata) -> tuple[str, ...]:
    artists = [*release.artists, *(a for track in release.tracks for a in track.artists)]
    return tuple(artist.name for artist in artists if artist.name.strip())


def _folded_names(names: Iterable[str]) -> Iterable[str]:
    for name in names:
        folded = _DISAMBIGUATOR.sub("", unicodedata.normalize("NFC", name)).strip().casefold()
        if len(folded) >= 3:
            yield folded


def _artist(artist: ArtistMetadata, fallback: str | None) -> ArtistMetadata:
    return replace(
        artist,
        name=normalize_artist(artist.name, fallback),
        joined_by=_joining(artist.joined_by, fallback),
    )


def _joining(words: str | None, fallback: str | None) -> str | None:
    """The words a source prints between two names, cased as words inside one.

    They reach the folder's name through ``written_credit`` — `Fulano E Beltrano`,
    `Banda Do Bairro Com Fulano` — so they answer to the same rule; `Feat.` and
    `Presents` are not joining words in any of the three and keep their capital.
    Framed by a word on each side so that neither end reads as the start of a
    name, then unframed, which the rule allows because it moves only case.
    """
    if not words or not words.strip():
        return words
    return _normalize(f"x {words} x", frozenset(), fallback, artist=False)[2:-2]
