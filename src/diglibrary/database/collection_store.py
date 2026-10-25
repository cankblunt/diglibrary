"""Persistence for the physical collection.

Its own store rather than more methods on `LibraryStore`, so that the module
comes out whole. The one table it shares is `metadata_releases`, which it only
points at: releases are recorded and read through `LibraryStore`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable

from diglibrary.collection.model import (
    Candidate,
    Category,
    ImportedRecord,
    Location,
    PhysicalRecord,
)
from diglibrary.database.connection import Database

_RECORD_COLUMNS = (
    "id, artist, album, year, release_row_id, identified_by, identified_at, "
    "media_grade, sleeve_grade, location_id, notes, label, catalog_number, edition, "
    "country, candidates_asked_at, candidates_error, "
    "(SELECT COUNT(*) FROM physical_record_candidates c WHERE c.record_id = physical_records.id "
    "AND c.is_group = 0)"
)


class CollectionStore:
    """Read and write records, locations and categories."""

    def __init__(self, database: Database, logger: logging.Logger) -> None:
        self._database = database
        self._logger = logger

    # --- locations and categories -------------------------------------------

    def locations(self) -> tuple[Location, ...]:
        """Return the user's locations, in the order of their names."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name FROM collection_locations ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return tuple(Location(int(row[0]), str(row[1])) for row in rows)

    def categories(self) -> tuple[Category, ...]:
        """Return the user's categories, in the order of their names."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name FROM collection_categories ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return tuple(Category(int(row[0]), str(row[1])) for row in rows)

    def add_location(self, name: str) -> int | None:
        """Add a location and return its id, or ``None`` if the name is taken."""
        return self._add_name("collection_locations", name)

    def add_category(self, name: str) -> int | None:
        """Add a category and return its id, or ``None`` if the name is taken."""
        return self._add_name("collection_categories", name)

    def rename_location(self, location_id: int, name: str) -> bool | None:
        """Rename one location: ``True`` done, ``False`` unknown, ``None`` name taken."""
        return self._rename("collection_locations", location_id, name)

    def rename_category(self, category_id: int, name: str) -> bool | None:
        """Rename one category: ``True`` done, ``False`` unknown, ``None`` name taken."""
        return self._rename("collection_categories", category_id, name)

    def remove_location(self, location_id: int) -> bool:
        """Remove one location; the records kept there keep no location."""
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM collection_locations WHERE id = ?", (location_id,)
            )
        return bool(cursor.rowcount)

    def remove_category(self, category_id: int) -> bool:
        """Remove one category from the list and from every record filed under it."""
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM collection_categories WHERE id = ?", (category_id,)
            )
        return bool(cursor.rowcount)

    def location_uses(self, location_id: int) -> int:
        """How many records are kept at this location."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM physical_records WHERE location_id = ?", (location_id,)
            ).fetchone()
        return int(row[0])

    def category_uses(self, category_id: int) -> int:
        """How many records are filed under this category."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM physical_record_categories WHERE category_id = ?",
                (category_id,),
            ).fetchone()
        return int(row[0])

    def _add_name(self, table: str, name: str) -> int | None:
        # The look and the write are one write block: a `SELECT` runs in
        # autocommit here, so deciding and then inserting apart would be two
        # transactions (CLAUDE.md, `sqlite3` and transactions).
        with self._database.connect(write=True) as connection:
            taken = connection.execute(
                f"SELECT 1 FROM {table} WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            if taken is not None:
                return None
            cursor = connection.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
            return int(cursor.lastrowid or 0)

    def _rename(self, table: str, row_id: int, name: str) -> bool | None:
        with self._database.connect(write=True) as connection:
            known = connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone()
            if known is None:
                return False
            taken = connection.execute(
                f"SELECT 1 FROM {table} WHERE name = ? COLLATE NOCASE AND id != ?",
                (name, row_id),
            ).fetchone()
            if taken is not None:
                return None
            connection.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, row_id))
        return True

    # --- records --------------------------------------------------------------

    def records(self) -> tuple[PhysicalRecord, ...]:
        """Return every record on the shelf, newest first."""
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM physical_records ORDER BY id DESC"
            ).fetchall()
            filed = _categories_by_record(connection)
        return tuple(_record(row, filed.get(int(row[0]), ())) for row in rows)

    def record(self, record_id: int) -> PhysicalRecord | None:
        """Return one record, or ``None`` if there is no such record."""
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM physical_records WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            filed = _categories_by_record(connection, record_id)
        return _record(row, filed.get(record_id, ()))

    def add_record(
        self,
        artist: str,
        album: str,
        year: int | None,
        release_row_id: int | None = None,
        identified_by: str | None = None,
    ) -> int:
        """Put one record on the shelf and return its id."""
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "INSERT INTO physical_records "
                "(artist, album, year, release_row_id, identified_by, identified_at) "
                "VALUES (?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END)",
                (artist, album, year, release_row_id, identified_by, release_row_id),
            )
            return int(cursor.lastrowid or 0)

    def add_imported(self, entries: Iterable[ImportedRecord]) -> tuple[int, ...]:
        """Put every record of an import on the shelf, all of it or none of it.

        One write block: a list read halfway is not a list anybody chose to
        import, so a failure partway leaves the shelf as it was. Locations and
        categories the list names and the user's lists lack are created here,
        by name, in the same block.
        """
        added: list[int] = []
        with self._database.connect(write=True) as connection:
            for entry in entries:
                location_id = (
                    _id_for_name(connection, "collection_locations", entry.location)
                    if entry.location
                    else None
                )
                cursor = connection.execute(
                    "INSERT INTO physical_records (artist, album, year, label, catalog_number, "
                    "edition, country, media_grade, sleeve_grade, location_id, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.artist,
                        entry.album,
                        entry.year,
                        entry.label,
                        entry.catalog_number,
                        entry.edition,
                        entry.country,
                        entry.media_grade,
                        entry.sleeve_grade,
                        location_id,
                        entry.notes,
                    ),
                )
                record_id = int(cursor.lastrowid or 0)
                category_ids = {
                    _id_for_name(connection, "collection_categories", name)
                    for name in entry.categories
                }
                connection.executemany(
                    "INSERT INTO physical_record_categories (record_id, category_id) "
                    "VALUES (?, ?)",
                    [(record_id, category_id) for category_id in sorted(category_ids)],
                )
                added.append(record_id)
        return tuple(added)

    # --- candidates -------------------------------------------------------------

    def candidates(self, record_id: int) -> tuple[Candidate, ...]:
        """The pressings a search offered for one record, in the order offered."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT source, release_id, title, artist, year, country, formats, labels, "
                "catalog_numbers, group_id, original_year, is_group "
                "FROM physical_record_candidates WHERE record_id = ? "
                "ORDER BY position",
                (record_id,),
            ).fetchall()
        return tuple(
            Candidate(
                source=str(row[0]),
                release_id=str(row[1]),
                title=str(row[2]),
                artist=str(row[3]),
                year=None if row[4] is None else int(row[4]),
                country=str(row[5]),
                formats=tuple(json.loads(row[6])),
                labels=tuple(json.loads(row[7])),
                catalog_numbers=tuple(json.loads(row[8])),
                group_id=str(row[9]),
                original_year=None if row[10] is None else int(row[10]),
                is_group=bool(row[11]),
            )
            for row in rows
        )

    def keep_candidates(
        self, record_id: int, candidates: Iterable[Candidate], error: str | None = None
    ) -> bool:
        """Replace one record's candidates with what a search just answered.

        ``error`` is the catalogue's own words when it refused; the candidates
        are then emptied, so a refusal never leaves an older answer standing as
        if it were this one. Returns whether the record exists.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE physical_records SET candidates_asked_at = CURRENT_TIMESTAMP, "
                "candidates_error = ? WHERE id = ?",
                (error, record_id),
            )
            if not cursor.rowcount:
                return False
            connection.execute(
                "DELETE FROM physical_record_candidates WHERE record_id = ?", (record_id,)
            )
            connection.executemany(
                "INSERT INTO physical_record_candidates (record_id, position, source, "
                "release_id, title, artist, year, country, formats, labels, catalog_numbers, "
                "group_id, original_year, is_group) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record_id,
                        position,
                        candidate.source,
                        candidate.release_id,
                        candidate.title,
                        candidate.artist,
                        candidate.year,
                        candidate.country,
                        json.dumps(list(candidate.formats)),
                        json.dumps(list(candidate.labels)),
                        json.dumps(list(candidate.catalog_numbers)),
                        candidate.group_id,
                        candidate.original_year,
                        int(candidate.is_group),
                    )
                    for position, candidate in enumerate(candidates, start=1)
                ],
            )
        return True

    def drop_candidates(self, record_id: int) -> None:
        """Forget one record's candidates: chosen from, or none of them was it."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM physical_record_candidates WHERE record_id = ?", (record_id,)
            )

    def records_never_asked(self) -> tuple[int, ...]:
        """Records with no release that no search has been asked about yet, oldest first."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM physical_records WHERE release_row_id IS NULL "
                "AND candidates_asked_at IS NULL ORDER BY id"
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def identify_record(self, record_id: int, release_row_id: int, identified_by: str) -> bool:
        """Tie one record to a release, and say whether the record exists."""
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE physical_records SET release_row_id = ?, identified_by = ?, "
                "identified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (release_row_id, identified_by, record_id),
            )
            # The pressing is chosen, so the offer it was chosen from is spent.
            connection.execute(
                "DELETE FROM physical_record_candidates WHERE record_id = ?", (record_id,)
            )
        return bool(cursor.rowcount)

    def update_record(
        self,
        record_id: int,
        media_grade: str | None,
        sleeve_grade: str | None,
        location_id: int | None,
        notes: str,
        category_ids: Iterable[int],
    ) -> bool:
        """Write what the user says about one record, all of it at once.

        The categories are replaced rather than merged: the set on screen is the
        set the record is filed under. Both statements are one write block, so a
        record is never left with half its categories.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE physical_records SET media_grade = ?, sleeve_grade = ?, "
                "location_id = ?, notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (media_grade, sleeve_grade, location_id, notes, record_id),
            )
            if not cursor.rowcount:
                return False
            connection.execute(
                "DELETE FROM physical_record_categories WHERE record_id = ?", (record_id,)
            )
            connection.executemany(
                "INSERT INTO physical_record_categories (record_id, category_id) VALUES (?, ?)",
                [(record_id, category_id) for category_id in sorted(set(category_ids))],
            )
        return True

    def remove_record(self, record_id: int) -> bool:
        """Take one record off the shelf, and say whether it was there."""
        with self._database.connect(write=True) as connection:
            cursor = connection.execute("DELETE FROM physical_records WHERE id = ?", (record_id,))
        return bool(cursor.rowcount)


def _categories_by_record(
    connection: sqlite3.Connection, record_id: int | None = None
) -> dict[int, tuple[int, ...]]:
    query = "SELECT record_id, category_id FROM physical_record_categories"
    arguments: tuple[int, ...] = ()
    if record_id is not None:
        query += " WHERE record_id = ?"
        arguments = (record_id,)
    filed: dict[int, list[int]] = {}
    for record, category_id in connection.execute(query + " ORDER BY category_id", arguments):
        filed.setdefault(int(record), []).append(int(category_id))
    return {record: tuple(ids) for record, ids in filed.items()}


def _record(row: sqlite3.Row | tuple[object, ...], category_ids: tuple[int, ...]) -> PhysicalRecord:
    return PhysicalRecord(
        record_id=int(row[0]),
        artist=str(row[1]),
        album=str(row[2]),
        year=None if row[3] is None else int(row[3]),
        release_row_id=None if row[4] is None else int(row[4]),
        identified_by=None if row[5] is None else str(row[5]),
        identified_at=None if row[6] is None else str(row[6]),
        media_grade=None if row[7] is None else str(row[7]),
        sleeve_grade=None if row[8] is None else str(row[8]),
        location_id=None if row[9] is None else int(row[9]),
        notes=str(row[10]),
        category_ids=category_ids,
        label=str(row[11]),
        catalog_number=str(row[12]),
        edition=str(row[13]),
        country=str(row[14]),
        candidates_asked_at=None if row[15] is None else str(row[15]),
        candidates_error=None if row[16] is None else str(row[16]),
        candidate_count=int(row[17]),
    )


def _id_for_name(connection: sqlite3.Connection, table: str, name: str) -> int:
    """The id of the entry with this name in a user's list, created if absent."""
    row = connection.execute(
        f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cursor = connection.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    return int(cursor.lastrowid or 0)
