"""Unit tests for the album-by-album from/to report."""

import zipfile
from pathlib import Path
from xml.etree import ElementTree

from diglibrary.library.report import COLUMNS, AlbumReport, Ink, Run, change, write_change_report

_NAMESPACE = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _rows(path: Path) -> list[list[str]]:
    """Read each cell's visible text, whatever runs it is built from."""
    with zipfile.ZipFile(path) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    return [
        ["".join(cell.itertext()) for cell in row]
        for row in sheet.findall("x:sheetData/x:row", _NAMESPACE)
    ]


def _run_colours(path: Path, row: int, column: int) -> list[tuple[str, str]]:
    """Return each run's text paired with the colour it is written in."""
    with zipfile.ZipFile(path) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cells = sheet.findall("x:sheetData/x:row", _NAMESPACE)[row].findall("x:c", _NAMESPACE)
    coloured = []
    for entry in cells[column].findall("x:is/x:r", _NAMESPACE):
        colour = entry.find("x:rPr/x:color", _NAMESPACE)
        text = entry.findtext("x:t", default="", namespaces=_NAMESPACE)
        coloured.append((text, colour.get("rgb") if colour is not None else ""))
    return coloured


def _album(**overrides: object) -> AlbumReport:
    fields: dict[str, object] = {
        "album": "Vorrel - Zunto Brumal (1990) [FLAC]",
        "folder": change(
            "Vorrel - Zunto Brumal Ed. 2000 (1990) [FLAC]",
            "Vorrel - Zunto Brumal (1990) [FLAC]",
        ),
        "state": "applied",
    }
    fields.update(overrides)
    return AlbumReport(**fields)  # type: ignore[arg-type]


def test_the_report_is_a_readable_workbook(tmp_path: Path) -> None:
    """It has to open in a spreadsheet, which means every part has to be there."""
    written = write_change_report(tmp_path / "changes.xlsx", (_album(),))

    with zipfile.ZipFile(written) as archive:
        assert archive.testzip() is None
        assert "[Content_Types].xml" in archive.namelist()
        assert "xl/workbook.xml" in archive.namelist()
    rows = _rows(written)
    assert rows[0] == [name for name, _ in COLUMNS]
    assert rows[1][0] == "Vorrel - Zunto Brumal (1990) [FLAC]"


def test_one_row_per_album_not_one_per_file(tmp_path: Path) -> None:
    """A row per track is unreadable at the size of a library; an album is one line."""
    tracks = (
        *change("01.flac", "01. Plimwick Vorrel Song.flac"),
        Run("\n"),
        *change("02.flac", "02. Zunto Brumal.flac"),
    )

    rows = _rows(write_change_report(tmp_path / "changes.xlsx", (_album(tracks=tracks),)))

    assert len(rows) == 2, "one header, one album"
    assert "Plimwick Vorrel Song" in rows[1][5] and "Zunto Brumal.flac" in rows[1][5]


def test_what_left_is_red_and_what_arrived_is_blue(tmp_path: Path) -> None:
    """The whole point: a name reads as one sentence, not two columns."""
    written = write_change_report(tmp_path / "changes.xlsx", (_album(),))

    runs = _run_colours(written, row=1, column=1)
    removed = [text for text, colour in runs if colour == "FFC0392B"]
    added = [text for text, colour in runs if colour == "FF1155CC"]

    assert removed == ["Vorrel - Zunto Brumal Ed. 2000 (1990) [FLAC]"]
    assert added == ["Vorrel - Zunto Brumal (1990) [FLAC]"]


def test_an_unchanged_value_is_written_once_in_black(tmp_path: Path) -> None:
    """An unchanged value is written once, uncoloured, not as a removal and an arrival."""
    written = write_change_report(tmp_path / "changes.xlsx", (_album(year=change("1990", "1990")),))

    runs = _run_colours(written, row=1, column=2)

    assert [text for text, _ in runs] == ["1990"]
    assert all(colour == "" for _, colour in runs), "unchanged text carries no colour"


def test_the_source_is_a_real_hyperlink(tmp_path: Path) -> None:
    """Each album links to the catalogue entry its changes came from."""
    album = _album(
        source_label="discogs 1234567",
        source_url="https://www.discogs.com/release/1234567",
    )

    written = write_change_report(tmp_path / "changes.xlsx", (album,))

    with zipfile.ZipFile(written) as archive:
        relationships = archive.read("xl/worksheets/_rels/sheet1.xml.rels").decode()
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "https://www.discogs.com/release/1234567" in relationships
    assert '<hyperlink ref="L2"' in sheet


def test_a_name_a_catalogue_published_survives_the_report(tmp_path: Path) -> None:
    """Titles carry ampersands, accents and quotes; XML carries none of them raw."""
    album = _album(album="Zé & Plimbyra", folder=(Run('Cê "na praça" — 1º', Ink.ADDED),))

    rows = _rows(write_change_report(tmp_path / "changes.xlsx", (album,)))

    assert rows[1][0] == "Zé & Plimbyra"
    assert rows[1][1] == 'Cê "na praça" — 1º'


def test_an_empty_run_still_produces_a_valid_file(tmp_path: Path) -> None:
    """A report of nothing is a header, not a corrupt archive."""
    assert _rows(write_change_report(tmp_path / "changes.xlsx", ())) == [
        [name for name, _ in COLUMNS]
    ]
