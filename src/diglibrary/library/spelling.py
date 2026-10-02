"""Telling a real difference from a difference of case or accent."""

import re
import unicodedata

from diglibrary.library.casing import is_shouted

_WHITESPACE = re.compile(r"\s+")


def is_spelling_variant(left: str, right: str) -> bool:
    """Report whether two values are the same word spelled differently.

    Case and diacritics are the two ways one word reaches this project in two
    forms: a catalogue contributor typing "NIGHT" for "Night", or typing
    "Zelu" because their keyboard would not give them "Zélu". Everything
    else is content — "Vol. 2" and "Vol 2" are different spellings a source
    deliberately chose, and are left to the source.
    """
    return _key(left) == _key(right) and left != right


def richer_spelling(left: str, right: str) -> str:
    """Return the better-written of two spellings of one word.

    The one carrying more diacritics was written by someone who could type
    them, whichever side it came from. That is what keeps "Zélu" against a
    catalogue that says "Zelu", and equally what lets "Néva Lumen" correct a
    folder that says "Neva Lumen". A tie keeps ``left``, which is the local
    value everywhere this is called; for a name, ``resolve_spelling`` then gives
    it the catalogue's case, and every other field keeps the local case.
    """
    return left if _diacritics(left) >= _diacritics(right) else right


NAMED_TAGS = frozenset({"title", "album", "artist", "albumartist"})
"""The tags whose case is written by the one rule for names."""


def resolve_spelling(local: str | None, catalogue: str) -> str:
    """Settle one value between what is on disk and what the catalogue says.

    Anything beyond case and accent is a real difference, and the catalogue
    decides it. This is the single place that rule is applied to a name before
    it is rendered or written.

    The accents come from the richer spelling and the case from the
    catalogue's, which arrives already written by the one rule for names — so a
    folder reading `Tales Of The Sea` becomes `Tales of the Sea` when it is
    planned. A value in capitals from end to end is one the rule leaves as
    written, and there the disk keeps its own case.
    """
    if local is None or not is_spelling_variant(local, catalogue):
        return catalogue
    richer = richer_spelling(local, catalogue)
    if richer == catalogue or is_shouted(catalogue):
        return richer
    return _cased_like(richer, catalogue)


def preserve_existing_spelling(
    current: dict[str, tuple[str, ...]],
    desired: dict[str, tuple[str, ...]],
    settled: frozenset[str] = frozenset(),
    cased: frozenset[str] = frozenset(),
) -> dict[str, tuple[str, ...]]:
    """Return the desired tags, resolved against the spellings already on disk.

    A value that differs from the source only in case or accent is settled by
    ``richer_spelling`` rather than overwritten, so an album kept on disk as
    "NIGHT" is not rewritten to "Night" because a catalogue contributor typed it
    that way, and "Zélu" is not flattened because a catalogue holds it without
    the accent.

    ``cased`` names the fields whose case is the rule's rather than the disk's:
    for those only the accents are settled here.
    """
    preserved: dict[str, tuple[str, ...]] = {}
    for name, values in desired.items():
        if name in settled:
            # A field whose spelling is already decided, so the disk's is not asked.
            preserved[name] = values
            continue
        existing = current.get(name, ())
        settle = resolve_spelling if name in cased else _richer_if_variant
        preserved[name] = tuple(
            settle(existing[index] if index < len(existing) else None, value)
            for index, value in enumerate(values)
        )
    return preserved


def _richer_if_variant(local: str | None, value: str) -> str:
    if local is None or not is_spelling_variant(local, value):
        return value
    return richer_spelling(local, value)


def _cased_like(letters: str, case_of: str) -> str:
    """Return ``letters`` written in the case of ``case_of``, character by character.

    The two are one word apart only in case and accent, so once composed they
    are the same length; where they are not — whitespace the comparison
    ignores — the letters are returned as they were, which is the old rule.
    """
    mine = unicodedata.normalize("NFC", letters)
    theirs = unicodedata.normalize("NFC", case_of)
    if len(mine) != len(theirs):
        return letters
    written = []
    for character, model in zip(mine, theirs, strict=True):
        if model.isupper() and len(character.upper()) == 1:
            written.append(character.upper())
        elif model.islower() and len(character.lower()) == 1:
            written.append(character.lower())
        else:
            written.append(character)
    cased = "".join(written)
    # Unchanged except for being composed is unchanged: a Mac hands back its
    # names decomposed, and returning the composed form would plan a rename
    # of `Étude` to `Étude`.
    return letters if cased == mine else cased


def one_word_apart(local: str | None, catalogue: str) -> str:
    """Return the local spelling when it differs from the catalogue in one near word.

    It decides nothing: it marks a line for the user to look at. Case and
    accent are already settled by ``resolve_spelling``; everything beyond them
    belongs to the catalogue, and that is right almost every time — most
    differences are punctuation and case the catalogue is simply right about.

    Narrowed to *one word differing, and the two spellings within an edit or a
    transposition*, what is left is rare and runs both ways: a catalogue
    misspells a word as often as a folder does. **A rule that picked a side
    would be wrong about half the time**, and no dictionary settles a pair such
    as `Lantern` against `Lanterns`, where both are words. So this returns what is
    on disk and the window shows both: the application asks before it writes.

    Four letters at least, because below that a single edit is most of the word
    and the two are likely different words. One word only: two differences is
    a different title, not a typo.
    """
    if not local:
        return ""
    mine, theirs = _WORDS.findall(local), _WORDS.findall(catalogue)
    if len(mine) != len(theirs):
        return ""
    apart = [(a, b) for a, b in zip(mine, theirs, strict=True) if _key(a) != _key(b)]
    if len(apart) != 1:
        return ""
    a, b = (_key(word) for word in apart[0])
    if len(a) < _WORD_FLOOR or len(b) < _WORD_FLOOR:
        return ""
    distance = _distance(a, b)
    if distance == 1 or (distance == 2 and sorted(a) == sorted(b)):
        return local
    return ""


def a_whole_word_apart(local: str | None, catalogue: str) -> str:
    """Return the local spelling when exactly one whole word of the title differs.

    Like its neighbour it decides nothing — it marks a line. The
    two are asked in order and answer different questions. ``one_word_apart``
    asks *did somebody slip a letter*, and it has to be narrow because it
    carries no evidence: it is looking at two spellings and nothing else, so a
    pair three edits apart could be a typo or could be two different words, and
    it refuses rather than guess.

    This one is only ever consulted when a witness has already agreed with
    **every** title on the album, which is evidence the letters cannot supply.
    With it the narrowness stops paying for itself, so the question becomes the
    plain one: does the catalogue write a different word here than the file
    does. Two real words three edits apart are exactly the shape the letters
    cannot settle, and the witness settles it.

    One word and not two, and the same count of them: a title differing in two
    places is a different title, and one differing in how many words it has is
    an edition's parenthesis rather than a spelling.

    Most lines this marks are already marked by ``one_word_apart``; the ones
    this rule exists for are those the letters refuse, such as `From`/`For` or
    a short joining word written two ways.

    **And something has to agree.** A title of one word has no rest of itself to
    hold still, so *every* pair of one-word titles differs in exactly one word,
    and the rule would say so about two names with nothing in common — a whole
    title replaced, not a word disputed. The rule is about a word inside a
    title that otherwise matches, so it asks for a title that has an otherwise.
    """
    if not local:
        return ""
    mine, theirs = _WORDS.findall(local), _WORDS.findall(catalogue)
    if len(mine) != len(theirs) or len(mine) < 2:
        return ""
    apart = [(a, b) for a, b in zip(mine, theirs, strict=True) if _key(a) != _key(b)]
    return local if len(apart) == 1 else ""


_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)
"""What counts as a word here: letters only, so punctuation and numbers cannot
be the single difference. `Main street, part 2` against `Main Street Part 2`
differs in no word at all, which keeps the great majority of differences from
being marked."""

_WORD_FLOOR = 4
"""How long a word must be before one edit inside it can be called a slip."""


def _distance(left: str, right: str) -> int:
    """The edits between two words, counted no further than this needs."""
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for index, one in enumerate(left, start=1):
        current = [index]
        for other_index, other in enumerate(right, start=1):
            current.append(
                min(
                    previous[other_index] + 1,
                    current[other_index - 1] + 1,
                    previous[other_index - 1] + (one != other),
                )
            )
        previous = current
    return previous[-1]


def _key(value: str) -> str:
    """Reduce a value to the word it is, without case or accent."""
    stripped = "".join(
        character
        for character in unicodedata.normalize("NFD", value)
        if not unicodedata.combining(character)
    )
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", stripped)).strip().casefold()


def _diacritics(value: str) -> int:
    """Count the combining marks a spelling carries."""
    return sum(
        1 for character in unicodedata.normalize("NFD", value) if unicodedata.combining(character)
    )
