"""Recognising that several albums are volumes of one thing.

A library holds runs: a compilation series in eight volumes, another in six.
The catalogues name those volumes inconsistently — one writes
`Vol. 5 - The Second Wave`, the next writes `6: The Second Wave`, and the first
volume of a run is often not numbered at all — so one series ends up spelled
several different ways.

This module only *recognises*. It splits a title into the part that repeats, the
volume, and whatever that volume alone carries; and it groups albums that share
the repeating part. It renames nothing and decides nothing about what a series
should look like — that is the naming template, and it lives elsewhere.

The volume rule is deliberately narrow, because the alternative is a heuristic
that eats real titles. `House of 7 Doors, Vol. 1` has a seven in its name and
a volume of one, and no rule that hunts for digits can tell those apart.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_DASHES = "-–—"  # noqa: RUF001 — hyphen, en dash and em dash all appear in real titles

_MARKERS = r"vol|volume|pt|part|no|nº|n°|#"
"""The words that name a volume of a run.

`CD` and `Disc` are deliberately absent. `VA - Some Anthology (CD1 - 2007)` and
its three siblings are one release split across four discs, not four volumes of
a series, and disc folders are handled by the scanner. Reading a disc as a
volume would offer to renumber a boxed set.
"""

_EXPLICIT_VOLUME = re.compile(
    rf"[\s,]*\b(?:{_MARKERS})\b\.?\s*(\d{{1,3}})\s*$",
    re.IGNORECASE,
)
"""An explicit marker at the end of the repeating part: `Vol. 5`, `Pt 2`, `#3`."""

_TRAILING_NUMBER = re.compile(r"\s+(\d{1,3})\s*$")
"""A bare number where a volume would be. Only trusted with a sibling to vouch for it."""

_SUBTITLE = re.compile(rf"\s*(?::|\s[{_DASHES}]\s|\()")
"""Where a title may stop repeating and start describing this volume alone."""

_STRONG_SUBTITLE = re.compile(r"\s*(?::|\()")
"""A colon or a bracket, which a title uses for nothing else.

A dash is not here on purpose: `Some Label - Winter Selection` puts a dash
inside the name itself, and cutting there throws the volume away with the
second half of the title.
"""


@dataclass(frozen=True, slots=True)
class TitleParts:
    """Purpose: hold one title split into what repeats and what does not.

    Responsibilities: carry the stem, the volume when the title states one, and
    the subtitle this volume alone carries. Boundaries: it proposes no name.
    Dependencies: none. Collaborators: ``group_series``. Constraints: ``volume``
    is ``None`` when the title states no number, which is ordinary — most first
    volumes are unnumbered — and is never guessed at here.
    """

    stem: str
    volume: int | None
    subtitle: str
    volume_is_explicit: bool = False

    @property
    def shape(self) -> str:
        """Return how this title is written, with its own words removed.

        Two members of a series with the same shape are spelled alike; two with
        different shapes are the reason this module exists.
        """
        return f"{'#' if self.volume is not None else ''}|{'sub' if self.subtitle else ''}"


@dataclass(frozen=True, slots=True)
class SeriesMember:
    """Purpose: pair one album with how its title reads.

    Responsibilities: carry whatever identifies the album to the caller, its
    title, and the parts that title splits into. Boundaries: holds no path and
    no metadata — the caller's ``reference`` is whatever it needs back.
    Dependencies: ``TitleParts``. Collaborators: ``Series``.
    """

    reference: object
    title: str
    parts: TitleParts


@dataclass(frozen=True, slots=True)
class Series:
    """Purpose: report that several albums are volumes of one run.

    Responsibilities: carry the artist and the repeating title, its members in
    volume order, and whether they are spelled alike. Boundaries: it holds no
    template and renames nothing. Dependencies: ``SeriesMember``. Collaborators:
    the window, which shows a run whose members disagree. Constraints: a run of
    one is not a series, because a single album cannot disagree with itself.
    """

    artist: str
    stem: str
    members: tuple[SeriesMember, ...]

    @property
    def is_aligned(self) -> bool:
        """Return whether every member is spelled the same way."""
        return len({member.parts.shape for member in self.members}) == 1

    @property
    def unnumbered(self) -> tuple[SeriesMember, ...]:
        """Return the members whose title states no volume.

        These are the ones a template cannot number without asking a catalogue
        what they are: an unnumbered album in a run is usually the first, and is
        sometimes the third.
        """
        return tuple(member for member in self.members if member.parts.volume is None)


def split_title(title: str) -> TitleParts:
    """Split one title into what repeats, its volume, and what it alone carries.

    A volume is read only from an explicit marker — `Vol`, `Pt`, `#` —
    at the end of the repeating part. A bare trailing number is reported so a
    caller with siblings to compare against can decide, but it is not treated
    as a volume here: `Red Lantern 2` and `House of 7 Doors` are the same
    shape to a regular expression and opposite things to a person.
    """
    head, subtitle = _split_subtitle(title.strip())
    explicit = _EXPLICIT_VOLUME.search(head)
    if explicit:
        return TitleParts(
            stem=head[: explicit.start()].strip(" ," + _DASHES),
            volume=int(explicit.group(1)),
            subtitle=subtitle,
            volume_is_explicit=True,
        )
    return TitleParts(stem=head.strip(" ," + _DASHES), volume=None, subtitle=subtitle)


def group_series(
    albums: Iterable[tuple[object, str, str]],
) -> tuple[Series, ...]:
    """Group albums into series, from ``(reference, artist, title)`` triples.

    Two albums belong to one series when their artists agree and their stems
    agree. A bare trailing number is promoted to a volume only when a sibling
    vouches for it — either by carrying an explicit marker, or by being the
    same stem with a different trailing number. That is what keeps `House of 7
    Doors` whole while `Deeper Still 4` and `Deeper Still 6` become
    volumes of one run.
    """
    parsed = [(reference, artist, title, split_title(title)) for reference, artist, title in albums]
    by_stem: dict[tuple[str, str], list[tuple[object, str, str, TitleParts]]] = {}
    for entry in parsed:
        by_stem.setdefault((_fold(entry[1]), _fold(entry[3].stem)), []).append(entry)

    promoted = _promote_bare_numbers(parsed, by_stem)
    series: list[Series] = []
    for (_, _), entries in sorted(promoted.items(), key=lambda item: item[0]):
        # A run needs at least one member the catalogue actually numbered.
        # Without that, albums sharing a title are duplicates, an instrumental,
        # or two pressings, and calling those a series would offer to number
        # things that have no order.
        if len(entries) < 2 or not any(entry[3].volume is not None for entry in entries):
            continue
        members = tuple(
            SeriesMember(reference=reference, title=title, parts=parts)
            for reference, _, title, parts in sorted(
                entries, key=lambda entry: (entry[3].volume is None, entry[3].volume or 0)
            )
        )
        series.append(Series(artist=entries[0][1], stem=entries[0][3].stem, members=members))
    return tuple(series)


def _promote_bare_numbers(
    parsed: list[tuple[object, str, str, TitleParts]],
    by_stem: dict[tuple[str, str], list[tuple[object, str, str, TitleParts]]],
) -> dict[tuple[str, str], list[tuple[object, str, str, TitleParts]]]:
    """Re-read bare trailing numbers as volumes where a sibling vouches for them."""
    candidates: dict[tuple[str, str], list[tuple[object, str, str, TitleParts]]] = {}
    for reference, artist, title, parts in parsed:
        bare = _TRAILING_NUMBER.search(parts.stem) if parts.volume is None else None
        if bare is None:
            continue
        shorter = parts.stem[: bare.start()].strip(" ," + _DASHES)
        candidates.setdefault((_fold(artist), _fold(shorter)), []).append(
            (
                reference,
                artist,
                title,
                TitleParts(stem=shorter, volume=int(bare.group(1)), subtitle=parts.subtitle),
            )
        )

    promoted = {key: list(value) for key, value in by_stem.items()}
    for key, entries in candidates.items():
        # A sibling vouches for the number: another album with the same shorter
        # stem, whether it is numbered explicitly, numbered bare, or the
        # unnumbered first volume of the run.
        vouched = len(entries) > 1 or key in promoted
        if not vouched:
            continue
        for entry in entries:
            longer = (key[0], _fold(f"{entry[3].stem} {entry[3].volume}"))
            if longer in promoted:
                promoted[longer] = [item for item in promoted[longer] if item[0] is not entry[0]]
                if not promoted[longer]:
                    del promoted[longer]
        promoted.setdefault(key, [])
        promoted[key].extend(entries)
    return promoted


def _split_subtitle(title: str) -> tuple[str, str]:
    """Return the repeating head and the subtitle this volume alone carries.

    A separator cuts where the half before it ends in a volume, because that is
    the shape of a numbered run: `Deeper Still Vol. 5 - The Second Wave`.
    Failing that, only a colon or a bracket cuts — a dash on its own is
    as likely to be inside the name as beside it.
    """
    for match in _SUBTITLE.finditer(title):
        head = title[: match.start()]
        if _EXPLICIT_VOLUME.search(head) or _TRAILING_NUMBER.search(head):
            return _cut(title, match.start())
    strong = _STRONG_SUBTITLE.search(title)
    return _cut(title, strong.start()) if strong else (title, "")


def _cut(title: str, position: int) -> tuple[str, str]:
    """Split a title at one position, tidying the punctuation on both sides."""
    subtitle = title[position:].lstrip(" :" + _DASHES)
    if subtitle.startswith("("):
        subtitle = subtitle[1:].rstrip(")")
    return title[:position].strip(), subtitle.strip()


def _fold(text: str) -> str:
    """Return a comparison key: accents, case and punctuation removed."""
    stripped = "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zA-Z]+", " ", stripped)).strip().lower()
