"""The physical collection, from its store to the window's gestures.

Every test runs over a real database in a temporary folder and a catalogue that
answers from fixed releases, so nothing reaches the network.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from diglibrary.application.api import LibraryApi
from diglibrary.application.collection import CollectionService
from diglibrary.application.contracts import (
    AlbumBehindPressings,
    ArtistMetadata,
    MetadataQuery,
    MetadataSources,
    ReleaseMetadata,
)
from diglibrary.database.collection_store import CollectionStore
from diglibrary.database.connection import Database
from diglibrary.database.library_store import LibraryStore
from diglibrary.metadata.service import MetadataService


def _pressing(
    identifier: str, year: int | None = 1976, title: str = "Night Songs"
) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=identifier,
        title=title,
        artists=(ArtistMetadata(name="The Harbour Trio"),),
        released_on=None if year is None else date(year, 1, 1),
        country="XX",
        labels=("Sample Records",),
        catalog_numbers=("SR-101",),
        formats=("Vinyl", "LP", "Album"),
        release_group_id="900",
    )


class Catalogue:
    """Answer searches, details and masters from fixed releases, or fail on demand."""

    def __init__(self, releases: tuple[ReleaseMetadata, ...] = ()) -> None:
        self._releases = {release.source_release_id: release for release in releases}
        self.queries: list[MetadataQuery] = []
        self.failure: str | None = None

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        self.queries.append(query)
        if self.failure:
            raise RuntimeError(self.failure)
        return tuple(self._releases.values())

    def get_release(self, release_id: str) -> ReleaseMetadata:
        if self.failure:
            raise RuntimeError(self.failure)
        return self._releases[release_id]

    def main_release_id(self, group_id: str) -> str | None:
        return "101" if group_id == "900" else None


def _service(tmp_path: Path, catalogue: Catalogue) -> tuple[CollectionService, Database]:
    logger = logging.getLogger("test.collection")
    database = Database(tmp_path / "library.sqlite3", logger)
    database.initialize()
    service = CollectionService(
        CollectionStore(database, logger),
        LibraryStore(database, logger),
        MetadataService(((MetadataSources.DISCOGS, catalogue),)),  # type: ignore[arg-type]
        logger,
    )
    return service, database


def _api(service: CollectionService, tmp_path: Path) -> LibraryApi:
    logger = logging.getLogger("test.collection.api")
    database = Database(tmp_path / "unused.sqlite3", logger)
    database.initialize()
    return LibraryApi(
        pipeline_factory=lambda settings: None,  # type: ignore[arg-type, return-value]
        store=LibraryStore(database, logger),
        artwork_store=None,  # type: ignore[arg-type]
        logger=logger,
        collection=service,
    )


def test_a_record_no_catalogue_knows_keeps_the_users_words(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())

    added = service.add_unidentified("  Some Band ", "Some Album", 1981)

    assert added.error is None
    (record,) = service.records()
    assert (record.artist, record.album, record.year) == ("Some Band", "Some Album", 1981)
    assert record.identified is False and record.identified_by is None
    assert service.add_unidentified("", "  ", None).error, "an empty record was accepted"


def test_a_search_answers_candidates_and_puts_nothing_on_the_shelf(tmp_path: Path) -> None:
    catalogue = Catalogue((_pressing("101", 1976), _pressing("102", 1988), _pressing("103", None)))
    service, _ = _service(tmp_path, catalogue)

    found = service.search("The Harbour Trio", "Night Songs", 1976)

    assert [release.source_release_id for release in found.releases] == [
        "101",
        "102",
        "103",
    ], (
        "the year orders the answer and removes nothing: a list keeps the work's year, "
        "and a reissue dated by its own year is still the record"
    )
    assert catalogue.queries[-1] == MetadataQuery(artist="The Harbour Trio", album="Night Songs")
    assert service.records() == (), "a search chose a record by itself"


def test_a_barcode_is_searched_as_printed_without_its_spaces(tmp_path: Path) -> None:
    catalogue = Catalogue((_pressing("101"),))
    service, _ = _service(tmp_path, catalogue)

    service.search_barcode(" 7 891234 567890 ")

    assert catalogue.queries[-1] == MetadataQuery(barcode="7891234567890")


def test_a_chosen_release_is_fetched_whole_and_the_record_points_at_it(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue((_pressing("101"),)))

    added = service.add_release(MetadataSources.DISCOGS, "101", "search")

    record = service.record(added.record_id or 0)
    assert record is not None and record.identified and record.identified_by == "search"
    release = service.release_of(record)
    assert release is not None and release.catalog_numbers == ("SR-101",)
    assert (record.artist, record.album, record.year) == ("The Harbour Trio", "Night Songs", 1976)


def test_a_catalogue_that_fails_is_quoted_not_diagnosed(tmp_path: Path) -> None:
    catalogue = Catalogue((_pressing("101"),))
    service, _ = _service(tmp_path, catalogue)
    catalogue.failure = "Discogs answered 401: You must authenticate to access this resource."

    found = service.search("The Harbour Trio", "", None)
    added = service.add_release(MetadataSources.DISCOGS, "101", "link")

    assert found.error == catalogue.failure
    assert added.error == catalogue.failure
    assert service.records() == ()


def test_an_unidentified_record_is_identified_in_place(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue((_pressing("101"),)))
    first = service.add_unidentified("harbour trio", "night songs", None).record_id or 0

    service.identify(first, MetadataSources.DISCOGS, "101", "barcode")

    (record,) = service.records()
    assert record.record_id == first and record.identified_by == "barcode"
    assert record.artist == "harbour trio", "the user's own words are kept beside the release"


def test_what_the_user_says_is_checked_against_the_scales_and_the_lists(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())
    record_id = service.add_unidentified("A", "B", None).record_id or 0
    assert service.add_location("Shelf 1") is None
    assert service.add_category("Soul") is None
    assert service.add_category("Imported") is None
    shelf = service.locations()[0].location_id
    soul, imported = (category.category_id for category in service.categories())

    assert "Not a grade" in (service.describe(record_id, "Great", None, None, "", ()) or "")
    assert (
        service.describe(record_id, None, None, 999, "", ()) == "That location is not on your list."
    )
    assert (
        service.describe(record_id, None, None, None, "", (999,))
        == "A category is not on your list."
    )
    assert (
        service.describe(
            record_id, "Very Good Plus (VG+)", "No Cover", shelf, " gatefold ", (soul, imported)
        )
        is None
    )

    record = service.record(record_id)
    assert record is not None
    assert (record.media_grade, record.sleeve_grade, record.location_id) == (
        "Very Good Plus (VG+)",
        "No Cover",
        shelf,
    )
    assert record.notes == "gatefold" and set(record.category_ids) == {soul, imported}

    service.describe(record_id, None, None, None, "", (soul,))
    record = service.record(record_id)
    assert record is not None and record.category_ids == (
        soul,
    ), "categories are replaced, not merged"
    assert record.media_grade is None, "no grade is written as nothing, never as a grade"


def test_the_users_lists_refuse_a_second_name_that_differs_only_in_case(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())

    assert service.add_location("Living room") is None
    assert service.add_location("living ROOM") == "“living ROOM” is already on your list."
    assert service.add_location("   ") == "A name cannot be empty."
    other = service.add_location("Box 2")
    assert other is None
    box = next(location for location in service.locations() if location.name == "Box 2")
    assert (
        service.rename_location(box.location_id, "Living Room")
        == "“Living Room” is already on your list."
    )
    assert service.rename_location(box.location_id, "Box 3") is None


def test_removing_a_location_leaves_its_records_on_the_shelf(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())
    record_id = service.add_unidentified("A", "B", None).record_id or 0
    service.add_location("Shelf 1")
    shelf = service.locations()[0].location_id
    service.describe(record_id, None, None, shelf, "", ())

    assert service.location_uses(shelf) == 1
    assert service.remove_location(shelf)

    record = service.record(record_id)
    assert record is not None and record.location_id is None


def test_the_window_adds_by_a_pasted_link_to_a_pressing_or_to_its_album(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue((_pressing("101"),)))
    api = _api(service, tmp_path)

    by_pressing = api.collection_add(link="https://www.discogs.com/release/101-The-Harbour-Trio")
    by_album = api.collection_add(link="https://www.discogs.com/master/900-Night-Songs")

    assert by_pressing["ok"] and by_album["ok"], (by_pressing, by_album)
    records = api.collection()["records"]
    assert [record["identified_by"] for record in records] == ["link", "link"]
    assert records[0]["release"]["catalog_numbers"] == ["SR-101"]
    assert records[0]["release"]["url"] == "https://www.discogs.com/release/101"
    refused = api.collection_add(link="https://example.com/not-a-release")
    assert refused["ok"] is False and "not a release page" in str(refused["error"])


def test_the_window_asks_before_removing_an_entry_records_use(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())
    api = _api(service, tmp_path)
    record_id = api.collection_add_unidentified("A", "B", "1980")["record_id"]
    assert api.collection_list_add("category", "Soul")["ok"]
    soul = api.collection()["categories"][0]["id"]
    api.collection_describe(record_id, category_ids=[soul])

    asked = api.collection_list_remove("category", soul)
    assert asked == {"ok": False, "confirm": True, "uses": 1}
    assert api.collection()["categories"], "it was removed without asking"

    assert api.collection_list_remove("category", soul, confirmed=True) == {"ok": True}
    assert api.collection()["records"][0]["category_ids"] == []
    assert api.collection_list_add("shelf", "x")["ok"] is False, "an unknown list was accepted"


def test_a_year_that_is_not_a_year_is_refused_by_name(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())
    api = _api(service, tmp_path)

    answer = api.collection_add_unidentified("A", "B", "nineteen")

    assert answer == {"ok": False, "error": "“nineteen” is not a year."}


@pytest.mark.parametrize("gesture", ["collection", "collection_remove"])
def test_without_the_module_every_gesture_says_it_is_absent(tmp_path: Path, gesture: str) -> None:
    logger = logging.getLogger("test.collection.absent")
    database = Database(tmp_path / "library.sqlite3", logger)
    database.initialize()
    api = LibraryApi(
        pipeline_factory=lambda settings: None,  # type: ignore[arg-type, return-value]
        store=LibraryStore(database, logger),
        artwork_store=None,  # type: ignore[arg-type]
        logger=logger,
    )

    answer = getattr(api, gesture)(*([] if gesture == "collection" else [1]))

    assert answer["ok"] is False and "not part of this installation" in str(answer["error"])


# --- importing a list and the candidates behind it -----------------

from diglibrary.collection.importing import read_table  # noqa: E402

LIST = (
    "Artista;Álbum;Ano;Selo;Catálogo;Gênero;Local;Preço\n"
    "The Harbour Trio;Night Songs;1976;Sample Records;SR-101;Soul, Jazz;Shelf 1;120,00\n"
    "Some Band;Some Album;1981;;;Soul;;50\n"
).encode("utf-8-sig")


class Asked(Catalogue):
    """Answer a query by its fields, so the order they are asked in shows."""

    def __init__(self, answers: dict[str, tuple[ReleaseMetadata, ...]]) -> None:
        super().__init__(tuple(release for found in answers.values() for release in found))
        self._answers = answers

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        self.queries.append(query)
        if self.failure:
            raise RuntimeError(self.failure)
        key = "catalog" if query.catalog_number else "words"
        return self._answers.get(key, ())


def test_an_imported_list_puts_every_field_on_the_shelf_and_creates_the_names(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path, Catalogue())
    table = read_table(LIST)
    mapping = {
        0: "artist",
        1: "album",
        2: "year",
        3: "label",
        4: "catalog_number",
        5: "categories",
        6: "location",
    }

    plan, added = service.apply_import(table, mapping)

    assert len(added) == 2 and plan.problems == ()
    trio = service.record(added[0])
    assert trio is not None
    assert (trio.label, trio.catalog_number, trio.year) == ("Sample Records", "SR-101", 1976)
    assert [location.name for location in service.locations()] == ["Shelf 1"]
    assert sorted(category.name for category in service.categories()) == ["Jazz", "Soul"]
    assert len(trio.category_ids) == 2 and trio.identified is False


def test_a_list_imported_twice_does_not_fill_the_shelf_twice(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, Catalogue())
    table = read_table(LIST)
    mapping = {0: "artist", 1: "album", 3: "label", 4: "catalog_number"}
    service.apply_import(table, mapping)

    again, added = service.apply_import(table, mapping)

    assert added == () and len(again.already_here) == 2
    assert len(service.records()) == 2


def test_candidates_are_asked_by_the_catalogue_number_first(tmp_path: Path) -> None:
    catalogue = Asked({"catalog": (_pressing("101"),), "words": (_pressing("102"),)})
    service, _ = _service(tmp_path, catalogue)
    _, (trio, other) = service.apply_import(
        read_table(LIST), {0: "artist", 1: "album", 3: "label", 4: "catalog_number"}
    )

    service.ask_candidates(trio)
    service.ask_candidates(other)

    assert catalogue.queries[0] == MetadataQuery(catalog_number="SR-101", label="Sample Records")
    assert catalogue.queries[1] == MetadataQuery(
        artist="Some Band", album="Some Album"
    ), "a record with no catalogue number is asked by its words"
    assert [c.release_id for c in service.candidates(trio)] == ["101"]
    record = service.record(trio)
    assert record is not None and record.identified is False, "a candidate was chosen by itself"
    assert record.candidate_count == 1


def test_when_the_catalogue_number_finds_nothing_the_words_are_asked(tmp_path: Path) -> None:
    catalogue = Asked({"words": (_pressing("102"),)})
    service, _ = _service(tmp_path, catalogue)
    _, (trio, _other) = service.apply_import(
        read_table(LIST), {0: "artist", 1: "album", 4: "catalog_number"}
    )

    service.ask_candidates(trio)

    assert [query.catalog_number for query in catalogue.queries] == ["SR-101", None]
    assert [c.release_id for c in service.candidates(trio)] == ["102"]


def test_a_record_keeps_at_most_twenty_offers(tmp_path: Path) -> None:
    many = tuple(_pressing(str(200 + index)) for index in range(35))
    service, _ = _service(tmp_path, Asked({"words": many}))
    record_id = service.add_unidentified("A", "B", None).record_id or 0

    service.ask_candidates(record_id)

    assert len(service.candidates(record_id)) == 20


def test_a_refusal_is_kept_beside_the_record_and_leaves_no_old_offer_standing(
    tmp_path: Path,
) -> None:
    catalogue = Asked({"words": (_pressing("101"),)})
    service, _ = _service(tmp_path, catalogue)
    record_id = service.add_unidentified("A", "B", None).record_id or 0
    service.ask_candidates(record_id)
    catalogue.failure = "Discogs answered 429: too many requests."

    refused = service.ask_candidates(record_id)

    record = service.record(record_id)
    assert refused == catalogue.failure and record is not None
    assert record.candidates_error == catalogue.failure and record.candidate_count == 0


def test_choosing_or_dismissing_spends_the_offer(tmp_path: Path) -> None:
    catalogue = Asked({"words": (_pressing("101"), _pressing("102"))})
    service, _ = _service(tmp_path, catalogue)
    first = service.add_unidentified("A", "B", None).record_id or 0
    second = service.add_unidentified("C", "D", None).record_id or 0
    service.ask_candidates(first)
    service.ask_candidates(second)

    chosen = service.identify(first, MetadataSources.DISCOGS, "101", "search")
    service.dismiss_candidates(second)

    assert chosen.error is None, chosen.error

    assert service.candidates(first) == () and service.candidates(second) == ()
    remaining = service.record(second)
    assert remaining is not None and remaining.identified is False


def test_the_window_imports_a_chosen_list_and_asks_behind_it(tmp_path: Path) -> None:
    listed = tmp_path / "records.csv"
    listed.write_bytes(LIST)
    service, _ = _service(tmp_path, Asked({"catalog": (_pressing("101"),)}))
    api = _api(service, tmp_path)
    api._list_picker = lambda: str(listed)

    opened = api.collection_import_open()
    assert opened["ok"] and opened["lines"] == 2
    assert "7" not in opened["suggested"], "the price column was offered a field"
    mapping = {"0": "artist", "1": "album", "3": "label", "4": "catalog_number"}
    preview = api.collection_import_preview(mapping)
    assert preview["records"] == 2 and api.collection()["records"] == [], "the rehearsal wrote"

    applied = api.collection_import_apply(mapping)
    assert applied == {"ok": True, "added": 2, "asking": True}
    assert api._collection_asker is not None
    api._collection_asker.join(timeout=10)

    events = [event["type"] for event in api.events()]
    assert events.count("collection-asking") == 2 and events[-1] == "collection-asked"
    counts = {record["artist"]: record["candidate_count"] for record in api.collection()["records"]}
    assert counts == {"The Harbour Trio": 1, "Some Band": 0}


# --- the album heading its own pressings ----------------------------------------


class Albums(Catalogue):
    """A catalogue that can also say what the album behind a pressing is."""

    def __init__(self, releases: tuple[ReleaseMetadata, ...] = ()) -> None:
        super().__init__(releases)
        self.described: list[str] = []
        self.silent: set[str] = set()

    def describe_group(self, group_id: str) -> AlbumBehindPressings:
        self.described.append(group_id)
        if group_id in self.silent:
            raise RuntimeError("the catalogue answered 429")
        return AlbumBehindPressings(
            source=MetadataSources.DISCOGS,
            group_id=group_id,
            title={"900": "Night Songs", "950": "Night Songs / Day Songs"}[group_id],
            artists=(ArtistMetadata(name="The Harbour Trio"),),
            first_released={"900": 1976, "950": 1977}[group_id],
            main_release_id="101",
        )


def _of(release: ReleaseMetadata, group_id: str | None) -> ReleaseMetadata:
    return replace(release, release_group_id=group_id)


def _lines(offers: object) -> list[tuple[str, str, int | None, int | None]]:
    return [
        (
            "album" if offer.is_group else "pressing",
            offer.release_id or offer.group_id,
            offer.original_year,
            offer.year,
        )
        for offer in offers  # type: ignore[attr-defined]
    ]


def test_an_album_heads_its_own_pressings_and_each_line_says_both_years(tmp_path: Path) -> None:
    catalogue = Albums(
        (
            _of(_pressing("301", 1977), "950"),
            _pressing("101", 1976),
            _of(_pressing("401", 1990), None),
            _pressing("102", 1988),
        )
    )
    service, _ = _service(tmp_path, catalogue)

    found = service.search("The Harbour Trio", "Night Songs", None)

    assert _lines(found.offers) == [
        ("album", "900", 1976, None),
        ("pressing", "101", 1976, 1976),
        ("pressing", "102", 1976, 1988),
        ("album", "950", 1977, None),
        ("pressing", "301", 1977, 1977),
        ("pressing", "401", None, 1990),
    ], "the album with the most pressings first, a pressing of no album last, none left out"
    assert catalogue.described == ["950", "900"], "each album is asked about once"
    assert found.offers[0].title == "Night Songs" and found.offers[0].artist == "The Harbour Trio"
    assert service.records() == ()


def test_an_album_the_catalogue_will_not_describe_costs_no_pressing(tmp_path: Path) -> None:
    catalogue = Albums((_pressing("101", 1976), _of(_pressing("301", 1977), "950")))
    catalogue.silent = {"900"}
    service, _ = _service(tmp_path, catalogue)

    found = service.search_barcode("7 891234 567890")

    assert found.error is None
    assert _lines(found.offers) == [
        ("album", "950", 1977, None),
        ("pressing", "301", 1977, 1977),
        ("pressing", "101", None, 1976),
    ], "a pressing whose album went unanswered is offered as it is, with no year invented"


def test_the_offers_kept_for_a_record_keep_the_album_and_count_only_pressings(
    tmp_path: Path,
) -> None:
    catalogue = Albums((_pressing("101", 1976), _pressing("102", 1988)))
    service, _ = _service(tmp_path, catalogue)
    record_id = service.add_unidentified("The Harbour Trio", "Night Songs", None).record_id or 0

    service.ask_candidates(record_id)

    assert _lines(service.candidates(record_id)) == [
        ("album", "900", 1976, None),
        ("pressing", "101", 1976, 1976),
        ("pressing", "102", 1976, 1988),
    ]
    kept = _api(service, tmp_path).collection_candidates(record_id)["candidates"]
    assert [offer["url"] for offer in kept] == [
        "https://www.discogs.com/master/900",
        "https://www.discogs.com/release/101",
        "https://www.discogs.com/release/102",
    ], "an offer kept for a record is as checkable as one a search just answered"
    record = service.record(record_id)
    assert (
        record is not None and record.candidate_count == 2
    ), "the album's line is not one more pressing waiting to be chosen"


def test_the_window_is_told_which_line_is_the_album_and_where_its_page_is(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path, Albums((_pressing("101", 1976),)))
    api = _api(service, tmp_path)

    album, pressing = api.collection_find(artist="The Harbour Trio", album="Night Songs")[
        "candidates"
    ]

    assert (album["is_group"], album["group_id"], album["release_id"]) == (True, "900", "")
    assert album["url"] == "https://www.discogs.com/master/900"
    assert album["original_year"] == 1976
    assert (pressing["is_group"], pressing["group_id"]) == (False, "900")
    assert (pressing["original_year"], pressing["year"]) == (1976, 1976)
    assert pressing["url"] == "https://www.discogs.com/release/101"
    assert api.open_url(album["url"])["ok"] is False, "no opener in this test, and no refusal"


def test_choosing_the_album_adds_the_pressing_the_catalogue_takes_for_the_main_one(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path, Albums((_pressing("101", 1976), _pressing("102", 1988))))
    api = _api(service, tmp_path)

    added = api.collection_add(source="discogs", via="search", group_id="900")
    nameless = api.collection_add(source="discogs", via="search", group_id="777")
    by_pressing = api.collection_add(
        source="discogs", release_id="102", via="search", group_id="900"
    )

    assert added["ok"], added
    assert nameless["ok"] is False and "does not name a pressing" in str(nameless["error"])
    assert by_pressing["ok"], by_pressing
    urls = sorted(record["release"]["url"] for record in api.collection()["records"])
    assert urls == [
        "https://www.discogs.com/release/101",
        "https://www.discogs.com/release/102",
    ], "a pressing chosen by name is that pressing, whatever album it also names"
    assert {record["identified_by"] for record in api.collection()["records"]} == {"search"}
