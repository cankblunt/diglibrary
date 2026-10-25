"""What a record on a shelf is, and the words its condition is graded in.

The catalogue's facts about a pressing live in `metadata_releases`, as they do
for an album of files; what is new here is the object itself — its condition,
where it is kept, what was written about it — and the words a user typed for
a record no catalogue has identified yet.
"""

from __future__ import annotations

from dataclasses import dataclass

MEDIA_GRADES: tuple[str, ...] = (
    "Mint (M)",
    "Near Mint (NM or M-)",
    "Very Good Plus (VG+)",
    "Very Good (VG)",
    "Good Plus (G+)",
    "Good (G)",
    "Fair (F)",
    "Poor (P)",
)
"""The Goldmine scale for the disc, in the catalogue's exact words.

No grade is a real state and is written as nothing: the screen says
*ungraded*, and a grade is never inferred from anything.
"""

SLEEVE_GRADES: tuple[str, ...] = (*MEDIA_GRADES, "Generic", "No Cover")
"""The same scale for the sleeve, plus the two states only a sleeve can be in."""

IDENTIFIED_BY: tuple[str, ...] = ("link", "barcode", "search")
"""How a record was tied to a release: the three ways of entering evidence."""


@dataclass(frozen=True, slots=True)
class Location:
    """A place on a user's own shelf, from the list that user keeps."""

    location_id: int
    name: str


@dataclass(frozen=True, slots=True)
class Category:
    """A category a user files records under, from the list that user keeps."""

    category_id: int
    name: str


@dataclass(frozen=True, slots=True)
class PhysicalRecord:
    """One record on the shelf, as stored.

    ``release_row_id`` is ``None`` for a record no catalogue has identified; its
    artist, album and year are then the words a user typed, and they are shown
    as that user's, never as a catalogue's.
    """

    record_id: int
    artist: str
    album: str
    year: int | None
    release_row_id: int | None
    identified_by: str | None
    identified_at: str | None
    media_grade: str | None
    sleeve_grade: str | None
    location_id: int | None
    notes: str
    category_ids: tuple[int, ...]
    label: str = ""
    catalog_number: str = ""
    edition: str = ""
    country: str = ""
    candidates_asked_at: str | None = None
    candidates_error: str | None = None
    candidate_count: int = 0

    @property
    def identified(self) -> bool:
        """Whether a catalogue's release stands behind this record."""
        return self.release_row_id is not None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A pressing a search offered for a record, kept until the user answers.

    Catalogue text only, as the search returned it. Choosing one is
    what ties the record to a release, fetched in full at that moment.

    ``is_group`` marks the line that is the album itself rather than a copy of
    it — a Discogs master — which heads the pressings carrying its
    ``group_id``. It has no ``release_id``: choosing it asks the catalogue which
    pressing it takes for the principal one. ``original_year`` is the year the
    album first came out, on the album's line and on each of its pressings,
    beside the ``year`` of the pressing itself.
    """

    source: str
    release_id: str
    title: str
    artist: str
    year: int | None
    country: str
    formats: tuple[str, ...]
    labels: tuple[str, ...]
    catalog_numbers: tuple[str, ...]
    group_id: str = ""
    original_year: int | None = None
    is_group: bool = False


@dataclass(frozen=True, slots=True)
class ImportedRecord:
    """One line of a list, in the fields the user pointed it at."""

    artist: str
    album: str
    year: int | None = None
    label: str = ""
    catalog_number: str = ""
    edition: str = ""
    country: str = ""
    media_grade: str | None = None
    sleeve_grade: str | None = None
    location: str = ""
    notes: str = ""
    categories: tuple[str, ...] = ()


def grade_or_none(value: object, scale: tuple[str, ...]) -> str | None:
    """Return ``value`` if it is a grade on ``scale``, ``None`` for no grade.

    Raises ``ValueError`` for anything else: a word the scale does not have is
    not quietly stored as if it were one.
    """
    if value is None or value == "":
        return None
    if value in scale:
        return str(value)
    raise ValueError(f"Not a grade on this scale: {value!r}.")


def clean_name(value: object) -> str:
    """Return a location or category name with its outer whitespace removed.

    Raises ``ValueError`` for an empty name, which could never be told apart
    from another empty one on the screen.
    """
    name = str(value or "").strip()
    if not name:
        raise ValueError("A name cannot be empty.")
    return name
