"""Identify and keep records on a shelf, through the catalogues already composed.

Purpose: the physical collection's application service.
Responsibilities: turn a pasted link, a barcode or a search into candidates, tie
a record to the release the user chose, and write what the user says about a
record. Boundaries: it asks catalogues only through `MetadataService`, the one
place a client is called, and it never chooses a release by itself —
a search answers with candidates and the user picks one. Nothing about
money is read or written here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from diglibrary.application.contracts import (
    AlbumBehindPressings,
    MetadataQuery,
    MetadataSourceId,
    MetadataSources,
    ReleaseMetadata,
    written_credit,
)
from diglibrary.collection.importing import ImportPlan, Table, plan_import, record_key
from diglibrary.collection.model import (
    MEDIA_GRADES,
    SLEEVE_GRADES,
    Candidate,
    Category,
    Location,
    PhysicalRecord,
    clean_name,
    grade_or_none,
)
from diglibrary.database.collection_store import CollectionStore
from diglibrary.database.library_store import LibraryStore
from diglibrary.metadata.service import MetadataService


@dataclass(frozen=True, slots=True)
class Candidates:
    """What a search answered, or the text of what stopped it.

    ``releases`` is what the catalogue returned; ``offers`` is the same answer
    as the user is shown it — each album heading its own pressings.
    """

    releases: tuple[ReleaseMetadata, ...] = ()
    error: str | None = None
    offers: tuple[Candidate, ...] = ()


@dataclass(frozen=True, slots=True)
class Added:
    """The record a gesture put on the shelf, or the text of what stopped it."""

    record_id: int | None = None
    error: str | None = None


class CollectionService:
    """The physical collection's gestures, over its store and the catalogues."""

    def __init__(
        self,
        store: CollectionStore,
        releases: LibraryStore,
        metadata: MetadataService,
        logger: logging.Logger,
    ) -> None:
        self._store = store
        self._releases = releases
        self._metadata = metadata
        self._logger = logger

    # --- reading ----------------------------------------------------------------

    def records(self) -> tuple[PhysicalRecord, ...]:
        """Every record on the shelf, newest first."""
        return self._store.records()

    def record(self, record_id: int) -> PhysicalRecord | None:
        """One record, or ``None``."""
        return self._store.record(record_id)

    def release_of(self, record: PhysicalRecord) -> ReleaseMetadata | None:
        """The catalogue's release behind a record, from the cache, or ``None``."""
        if record.release_row_id is None:
            return None
        return self._releases.release(record.release_row_id)

    def locations(self) -> tuple[Location, ...]:
        return self._store.locations()

    def categories(self) -> tuple[Category, ...]:
        return self._store.categories()

    # --- finding a release ------------------------------------------------------

    def search(self, artist: str, album: str, year: int | None) -> Candidates:
        """Ask the catalogue for releases matching the words a user typed.

        **The year orders what came back and never removes any of it**.
        The year a list keeps is usually the work's, and a search answers with
        pressings, each dated by its own year: filtering by it dropped every
        reissue of the record the user was looking at.
        """
        if not artist.strip() and not album.strip():
            return Candidates(error="Type an artist or an album to search for.")
        found = self._search(
            MetadataQuery(artist=artist.strip() or None, album=album.strip() or None)
        )
        if found.error:
            return found
        return self._offered(_by_year(found.releases, year))

    def search_barcode(self, barcode: str) -> Candidates:
        """Ask the catalogue for the pressings that carry this barcode."""
        digits = "".join(character for character in barcode if not character.isspace())
        if not digits:
            return Candidates(error="Type the barcode printed on the record.")
        found = self._search(MetadataQuery(barcode=digits))
        return found if found.error else self._offered(found.releases)

    def _offered(self, releases: Sequence[ReleaseMetadata]) -> Candidates:
        """A search's answer with each album heading the pressings that are its own."""
        return Candidates(
            releases=tuple(releases), offers=_under_their_albums(releases, self._albums(releases))
        )

    def _albums(self, releases: Sequence[ReleaseMetadata]) -> dict[str, AlbumBehindPressings]:
        """What the catalogue says of each album these pressings belong to.

        One question an album, asked once however many pressings name it. An
        album the catalogue would not describe is left out and nothing is said
        in its place: its pressings are then offered as pressings of no album.
        """
        albums: dict[str, AlbumBehindPressings] = {}
        for group_id in dict.fromkeys(
            release.release_group_id for release in releases if release.release_group_id
        ):
            try:
                album = self._metadata.describe_group(MetadataSources.DISCOGS, group_id)
            except Exception:
                self._logger.warning(
                    "The album behind a pressing could not be described.",
                    extra={"operation": "collection.album.failure"},
                )
                continue
            if album is not None:
                albums[group_id] = album
        return albums

    def _search(self, query: MetadataQuery) -> Candidates:
        try:
            releases = self._metadata.search_source(MetadataSources.DISCOGS, query)
        except Exception as error:
            # The catalogue's own words, never a diagnosis this application did
            # not measure (a screen never names a cause it did not measure).
            self._logger.warning(
                "A catalogue search for the collection failed.",
                extra={"operation": "collection.search.failure"},
            )
            return Candidates(error=str(error) or type(error).__name__)
        return Candidates(releases=tuple(releases))

    # --- importing a list ------------------------------------------

    def plan_import(self, table: Table, mapping: Mapping[int, str]) -> ImportPlan:
        """What importing this list would do, against the shelf as it stands."""
        return plan_import(
            table,
            mapping,
            existing=self._shelf_keys(),
            locations=(location.name for location in self._store.locations()),
            categories=(category.name for category in self._store.categories()),
        )

    def apply_import(
        self, table: Table, mapping: Mapping[int, str]
    ) -> tuple[ImportPlan, tuple[int, ...]]:
        """Import the list, planned again here rather than trusted from the screen."""
        plan = self.plan_import(table, mapping)
        if plan.problems:
            return plan, ()
        added = self._store.add_imported(plan.records)
        self._logger.info(
            "A list was imported into the collection.",
            extra={"operation": "collection.import", "records": len(added)},
        )
        return plan, added

    def _shelf_keys(self) -> set[tuple[str, str, str, str]]:
        keys: set[tuple[str, str, str, str]] = set()
        for record in self._store.records():
            release = self.release_of(record)
            label = record.label or (release.labels[0] if release and release.labels else "")
            number = record.catalog_number or (
                release.catalog_numbers[0] if release and release.catalog_numbers else ""
            )
            keys.add(record_key(record.artist, record.album, label, number))
        return keys

    # --- candidates ---------------------------------------------------

    def records_never_asked(self) -> tuple[int, ...]:
        """Records with no release that no search has been asked about yet."""
        return self._store.records_never_asked()

    def ask_candidates(self, record_id: int) -> str | None:
        """Search the catalogue for one record and keep what it offers.

        By the catalogue number and label first, which name one pressing on its
        sleeve, and by artist and album when those find nothing. Nothing is
        chosen: the offers wait for the user. Returns the catalogue's own words
        when it refused, which are kept beside the record.
        """
        record = self._store.record(record_id)
        if record is None or record.identified:
            return None
        found = Candidates()
        for query in _candidate_queries(record):
            found = self._search(query)
            if found.error or found.releases:
                break
        releases = _by_year(found.releases, record.year)[:CANDIDATES_KEPT]
        self._store.keep_candidates(
            record_id, _under_their_albums(releases, self._albums(releases)), found.error
        )
        return found.error

    def candidates(self, record_id: int) -> tuple[Candidate, ...]:
        return self._store.candidates(record_id)

    def dismiss_candidates(self, record_id: int) -> None:
        """None of the offers is the user's record: forget them."""
        self._store.drop_candidates(record_id)

    # --- putting records on the shelf -----------------------------------------

    def add_release(self, source: MetadataSourceId, release_id: str, via: str) -> Added:
        """Put a record of this release on the shelf, fetched in full first."""
        release = self._fetch(source, release_id)
        if isinstance(release, str):
            return Added(error=release)
        row = self._releases.record_release(release)
        record_id = self._store.add_record(
            artist=written_credit(release.artists) or "",
            album=release.title,
            year=None if release.released_on is None else release.released_on.year,
            release_row_id=row,
            identified_by=via,
        )
        self._logger.info(
            "A record was put on the shelf.",
            extra={"operation": "collection.add", "record_id": record_id, "via": via},
        )
        return Added(record_id=record_id)

    def add_unidentified(self, artist: str, album: str, year: int | None) -> Added:
        """Put a record on the shelf in the user's own words, with no release."""
        if not artist.strip() and not album.strip():
            return Added(error="Type at least an artist or an album.")
        record_id = self._store.add_record(artist.strip(), album.strip(), year)
        self._logger.info(
            "A record was put on the shelf without a release.",
            extra={"operation": "collection.add", "record_id": record_id, "via": None},
        )
        return Added(record_id=record_id)

    def identify(
        self, record_id: int, source: MetadataSourceId, release_id: str, via: str
    ) -> Added:
        """Tie a record already on the shelf to the release the user chose."""
        if self._store.record(record_id) is None:
            return Added(error="That record is not on the shelf.")
        release = self._fetch(source, release_id)
        if isinstance(release, str):
            return Added(error=release)
        row = self._releases.record_release(release)
        self._store.identify_record(record_id, row, via)
        return Added(record_id=record_id)

    def _fetch(self, source: MetadataSourceId, release_id: str) -> ReleaseMetadata | str:
        try:
            release = self._metadata.fetch_release(source, release_id)
        except Exception as error:
            self._logger.warning(
                "A release for the collection could not be fetched.",
                extra={"operation": "collection.fetch.failure", "source": str(source)},
            )
            return str(error) or type(error).__name__
        if release is None:
            return f"{source} cannot give the details of a release."
        return release

    def release_of_group(self, source: MetadataSourceId, group_id: str) -> str | None:
        """The release a master stands for, when the catalogue says."""
        return self._metadata.resolve_group(source, group_id)

    # --- what the user says about a record ------------------------------------

    def describe(
        self,
        record_id: int,
        media_grade: object,
        sleeve_grade: object,
        location_id: int | None,
        notes: str,
        category_ids: Iterable[int],
    ) -> str | None:
        """Write a record's condition, location, notes and categories.

        Returns ``None`` when written, or the reason nothing was: a grade the
        scale does not have, a location or category that is not on the user's
        list, or a record that is not on the shelf.
        """
        try:
            media = grade_or_none(media_grade, MEDIA_GRADES)
            sleeve = grade_or_none(sleeve_grade, SLEEVE_GRADES)
        except ValueError as error:
            return str(error)
        if location_id is not None and location_id not in {
            location.location_id for location in self._store.locations()
        }:
            return "That location is not on your list."
        known = {category.category_id for category in self._store.categories()}
        wanted = set(category_ids)
        if not wanted <= known:
            return "A category is not on your list."
        written = self._store.update_record(
            record_id, media, sleeve, location_id, notes.strip(), wanted
        )
        return None if written else "That record is not on the shelf."

    def remove(self, record_id: int) -> bool:
        """Take a record off the shelf."""
        return self._store.remove_record(record_id)

    # --- the user's lists -------------------------------------------------------

    def add_location(self, name: str) -> str | None:
        """Add a location; ``None`` when added, or the reason it was not."""
        return self._add(self._store.add_location, name)

    def add_category(self, name: str) -> str | None:
        """Add a category; ``None`` when added, or the reason it was not."""
        return self._add(self._store.add_category, name)

    def rename_location(self, location_id: int, name: str) -> str | None:
        return self._rename(self._store.rename_location, location_id, name)

    def rename_category(self, category_id: int, name: str) -> str | None:
        return self._rename(self._store.rename_category, category_id, name)

    def remove_location(self, location_id: int) -> bool:
        return self._store.remove_location(location_id)

    def remove_category(self, category_id: int) -> bool:
        return self._store.remove_category(category_id)

    def location_uses(self, location_id: int) -> int:
        return self._store.location_uses(location_id)

    def category_uses(self, category_id: int) -> int:
        return self._store.category_uses(category_id)

    @staticmethod
    def _add(add: Callable[[str], int | None], name: str) -> str | None:
        try:
            cleaned = clean_name(name)
        except ValueError as error:
            return str(error)
        added = add(cleaned)
        return None if added is not None else f"“{cleaned}” is already on your list."

    @staticmethod
    def _rename(rename: Callable[[int, str], bool | None], row_id: int, name: str) -> str | None:
        try:
            cleaned = clean_name(name)
        except ValueError as error:
            return str(error)
        renamed = rename(row_id, cleaned)
        if renamed is None:
            return f"“{cleaned}” is already on your list."
        return None if renamed else "That entry is not on your list."


CANDIDATES_KEPT = 20
"""How many offers a record keeps: most records have a handful."""


def _by_year(releases: Sequence[ReleaseMetadata], year: int | None) -> tuple[ReleaseMetadata, ...]:
    """The releases dated ``year`` first, every other one after, each in the order found."""
    if year is None:
        return tuple(releases)
    first = [
        release for release in releases if release.released_on and release.released_on.year == year
    ]
    return tuple(first + [release for release in releases if release not in first])


def _candidate_queries(record: PhysicalRecord) -> tuple[MetadataQuery, ...]:
    queries: list[MetadataQuery] = []
    if record.catalog_number:
        queries.append(
            MetadataQuery(catalog_number=record.catalog_number, label=record.label or None)
        )
    if record.artist or record.album:
        queries.append(MetadataQuery(artist=record.artist or None, album=record.album or None))
    return tuple(queries)


def _under_their_albums(
    releases: Sequence[ReleaseMetadata], albums: Mapping[str, AlbumBehindPressings]
) -> tuple[Candidate, ...]:
    """Every album first on its own line, its pressings under it, the rest after.

    The album with the most pressings in the answer comes first, and albums
    with as many keep the order the answer had them in; under each, its
    pressings keep that order too. A pressing of no album, or of one the
    catalogue did not describe, follows them all. Nothing is left out.
    """
    under: dict[str, list[ReleaseMetadata]] = {}
    alone: list[ReleaseMetadata] = []
    for release in releases:
        if release.release_group_id in albums:
            under.setdefault(str(release.release_group_id), []).append(release)
        else:
            alone.append(release)
    offers: list[Candidate] = []
    for group_id in sorted(under, key=lambda group: -len(under[group])):
        album = albums[group_id]
        offers.append(
            Candidate(
                source=str(album.source),
                release_id="",
                title=album.title,
                artist=written_credit(album.artists) or "",
                year=None,
                country="",
                formats=(),
                labels=(),
                catalog_numbers=(),
                group_id=group_id,
                original_year=album.first_released,
                is_group=True,
            )
        )
        offers.extend(_candidate(release, album) for release in under[group_id])
    offers.extend(_candidate(release) for release in alone)
    return tuple(offers)


def _candidate(release: ReleaseMetadata, album: AlbumBehindPressings | None = None) -> Candidate:
    return Candidate(
        source=str(release.source),
        release_id=release.source_release_id,
        title=release.title,
        artist=written_credit(release.artists),
        year=None if release.released_on is None else release.released_on.year,
        country=release.country or "",
        formats=tuple(release.formats),
        labels=tuple(release.labels),
        catalog_numbers=tuple(release.catalog_numbers),
        group_id="" if album is None else album.group_id,
        original_year=None if album is None else album.first_released,
    )
