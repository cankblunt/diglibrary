"""Reading a list of records and rehearsing its import.

Pure functions over bytes, so every case here is the exact computation the
import writes afterwards: what the rehearsal says is what happens.
"""

from __future__ import annotations

from diglibrary.collection.importing import (
    mapping_problems,
    plan_import,
    read_table,
    record_key,
    suggest_mapping,
)

LIST = (
    "Artista;Álbum;Ano;Selo;Catálogo;Gênero 1;Gênero 2;Estado do disco;Estado da capa;"
    "Local;Obs;Preço\n"
    "Ana Luz;Noite Clara;1976;Odeon;MOFB 1;MPB;Samba;VG+;NM;Estante 1;capa dupla;120,00\n"
    ";;;;;;;;;;;\n"
    "Bia Mar;;19xx;;;mpb;;Excelente;;estante 1;;50\n"
    ";;;;;;;;;;; \n"
    ";;1980;Som Livre;;;;;;;sem artista nem álbum;10\n"
)


def _table():
    return read_table(LIST.encode("utf-8-sig"))


def test_a_list_exported_by_a_portuguese_spreadsheet_is_read() -> None:
    table = _table()

    assert table.headers[0] == "Artista" and table.headers[-1] == "Preço"
    assert len(table.rows) == 3, "empty lines are not records"


def test_an_older_excel_code_page_and_a_tab_separator_are_read_too() -> None:
    table = read_table("Artist\tAlbum\nZélio\tZunção\n".encode("cp1252"))

    assert table.headers == ("Artist", "Album")
    assert table.rows == (("Zélio", "Zunção"),)


def test_a_column_with_no_name_is_named_by_its_place() -> None:
    table = read_table(b"Artist,,Album\nA,x,B\n")

    assert table.headers == ("Artist", "Column 2", "Album")


def test_the_suggestion_reads_the_headers_and_never_offers_a_field_for_money() -> None:
    table = _table()

    suggested = {
        table.headers[index]: name for index, name in suggest_mapping(table.headers).items()
    }

    assert suggested == {
        "Artista": "artist",
        "Álbum": "album",
        "Ano": "year",
        "Selo": "label",
        "Catálogo": "catalog_number",
        "Gênero 1": "categories",
        "Gênero 2": "categories",
        "Estado do disco": "media_grade",
        "Estado da capa": "sleeve_grade",
        "Local": "location",
        "Obs": "notes",
    }


def test_a_mapping_that_cannot_be_used_says_why() -> None:
    assert mapping_problems({0: "artist", 1: "artist"}, 3) == (
        "Only one column can be the artist.",
    )
    assert mapping_problems({0: "price"}, 3)[0] == "Not a field: 'price'."
    assert "Point a column at the artist or at the album." in mapping_problems({2: "year"}, 3)
    assert mapping_problems({9: "artist"}, 3)[0] == "Column 10 is not in the list."
    assert mapping_problems({0: "categories", 1: "categories", 2: "album"}, 3) == ()


def test_the_rehearsal_reads_every_field_and_invents_nothing() -> None:
    table = _table()
    plan = plan_import(table, suggest_mapping(table.headers))

    first, second = plan.records
    assert (first.artist, first.album, first.year) == ("Ana Luz", "Noite Clara", 1976)
    assert (first.label, first.catalog_number) == ("Odeon", "MOFB 1")
    assert (first.media_grade, first.sleeve_grade) == (
        "Very Good Plus (VG+)",
        "Near Mint (NM or M-)",
    )
    assert first.categories == ("MPB", "Samba") and first.location == "Estante 1"
    assert first.notes == "capa dupla"
    assert second.year is None and second.media_grade is None, "a guess was written"
    assert second.categories == ("mpb",)
    assert (plan.no_year, plan.ungraded) == (1, 1)
    assert plan.skipped == ((6, "no artist and no album"),)


def test_new_locations_and_categories_are_named_once_whatever_their_case() -> None:
    table = _table()

    plan = plan_import(table, suggest_mapping(table.headers), categories=("Samba",))

    assert plan.new_locations == ("Estante 1",)
    assert plan.new_categories == ("MPB",), "Samba exists, and mpb is MPB"


def test_a_record_already_on_the_shelf_is_reported_and_left_out() -> None:
    table = _table()
    here = record_key("ana luz", "NOITE CLARA", "odeon", "mofb 1")

    plan = plan_import(table, suggest_mapping(table.headers), existing=(here,))

    assert [record.artist for record in plan.records] == ["Bia Mar"]
    assert plan.already_here == ((2, "Ana Luz - Noite Clara"),)


def test_the_same_record_twice_in_one_list_is_two_copies() -> None:
    table = read_table(b"Artist,Album\nA,B\nA,B\n")

    plan = plan_import(table, {0: "artist", 1: "album"})

    assert len(plan.records) == 2, "a second copy on the list was taken for a re-import"
