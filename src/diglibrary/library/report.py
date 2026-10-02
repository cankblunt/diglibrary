"""Writing a from/to report of everything a run changed, as a spreadsheet.

One row per album, not per file: what changed about the folder is read at a
glance, and what changed inside it sits beside it. Within a cell, unchanged
text is black, what arrived is blue, and what left is red and struck through,
so a name is read as one sentence rather than as two columns to compare.

The format is the minimum valid XLSX: a zip of a handful of XML parts. It is
written by hand rather than with a library because a report is not worth a
runtime dependency — the project ships two, and each had to earn it.
"""

# ruff: noqa: E501 — the XLSX part declarations are fixed strings from the
# OOXML specification; wrapping them would change the bytes, not the reading.
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Changes" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="4">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><u/><sz val="11"/><color rgb="FF1155CC"/><name val="Calibri"/></font>
</fonts>
<fills count="4">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF2F3B52"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF3F6FB"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border/>
<border><bottom style="hair"><color rgb="FFD0D7E2"/></bottom></border>
</borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="6">
<xf xfId="0"/>
<xf xfId="0" fontId="1" fillId="2" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf xfId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
<xf xfId="0" fontId="2" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
<xf xfId="0" fontId="3" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
<xf xfId="0" fillId="3" borderId="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
</cellXfs>
</styleSheet>"""

_HEADER = 1
_CELL = 2
_ALBUM = 3
_LINK = 4
_TRACKS = 5

COLUMNS: tuple[tuple[str, int], ...] = (
    ("Album", 34),
    ("Folder name", 62),
    ("Year", 12),
    ("Edition", 12),
    ("Format", 14),
    ("Tracks", 64),
    ("Tags", 44),
    ("Artwork", 20),
    ("Audio", 13),
    ("Verified", 13),
    ("Witnesses", 22),
    ("Source", 26),
    ("State", 11),
)
"""What changed about the folder first, then what changed inside it."""


class Ink(StrEnum):
    """How one run of text reads: unchanged, arrived, or left."""

    PLAIN = "plain"
    ADDED = "added"
    REMOVED = "removed"
    MUTED = "muted"


_INK_PROPERTIES = {
    Ink.PLAIN: "",
    Ink.ADDED: '<color rgb="FF1155CC"/><b/>',
    Ink.REMOVED: '<strike/><color rgb="FFC0392B"/>',
    Ink.MUTED: '<color rgb="FF8A8A8A"/>',
}


@dataclass(frozen=True, slots=True)
class Run:
    """One stretch of text in a cell, and how it should read."""

    text: str
    ink: Ink = Ink.PLAIN


@dataclass(frozen=True, slots=True)
class AlbumReport:
    """Purpose: carry everything that happened to one album, ready to render.

    Responsibilities: hold the album's identity, the folder-level facts that
    changed, the per-track moves, and the source that justified them.
    Boundaries: it decides nothing and reads nothing — the API builds it from
    the executed trail. Dependencies: ``Run``. Collaborators:
    ``write_change_report``. Constraints: every field is already a sequence of
    runs, because colour is the report's whole point and a bare string cannot
    carry it.
    """

    album: str
    folder: Sequence[Run] = ()
    year: Sequence[Run] = ()
    edition: Sequence[Run] = ()
    container_format: Sequence[Run] = ()
    tracks: Sequence[Run] = ()
    tags: Sequence[Run] = ()
    artwork: Sequence[Run] = ()
    audio: Sequence[Run] = ()
    verified: Sequence[Run] = ()
    witnesses: Sequence[Run] = ()
    source_label: str = ""
    source_url: str = ""
    state: str = "applied"
    changed: bool = True

    def cells(self) -> tuple[Sequence[Run], ...]:
        """Return the row in column order."""
        return (
            (Run(self.album),),
            self.folder,
            self.year,
            self.edition,
            self.container_format,
            self.tracks,
            self.tags,
            self.artwork,
            self.audio,
            self.verified,
            self.witnesses,
            (Run(self.source_label),),
            (Run(self.state),),
        )


@dataclass
class _Links:
    """The hyperlink relationships one sheet accumulates as it is written."""

    entries: list[tuple[str, str]] = field(default_factory=list)

    def add(self, reference: str, url: str) -> str:
        self.entries.append((reference, url))
        return f"rId{len(self.entries)}"


def change(before: str, after: str) -> tuple[Run, ...]:
    """Render one value's move as coloured runs: unchanged, or old then new."""
    if before == after:
        return (Run(before),) if before else ()
    if not before:
        return (Run(after, Ink.ADDED),)
    if not after:
        return (Run(before, Ink.REMOVED),)
    return (Run(before, Ink.REMOVED), Run("  →  ", Ink.MUTED), Run(after, Ink.ADDED))


def write_change_report(destination: Path, albums: Iterable[AlbumReport]) -> Path:
    """Write the album-by-album from/to workbook, and return where it went."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    links = _Links()
    rows = [_header_row()]
    for number, album in enumerate(albums, start=2):
        rows.append(_album_row(number, album, links))
    last = len(rows)

    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{_columns()}</cols>"
        f"<sheetData>{''.join(rows)}</sheetData>"
        f'<autoFilter ref="A1:{_reference(len(COLUMNS) - 1, max(last, 1))}"/>'
        f"{_hyperlinks(links)}"
        "</worksheet>"
    )
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        if links.entries:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", _sheet_rels(links))
    return destination


def _columns() -> str:
    return "".join(
        f'<col min="{index + 1}" max="{index + 1}" width="{width}" customWidth="1"/>'
        for index, (_, width) in enumerate(COLUMNS)
    )


def _header_row() -> str:
    cells = "".join(
        f'<c r="{_reference(index, 1)}" s="{_HEADER}" t="inlineStr">'
        f"<is><t>{escape(name)}</t></is></c>"
        for index, (name, _) in enumerate(COLUMNS)
    )
    return f'<row r="1" ht="26" customHeight="1">{cells}</row>'


def _album_row(number: int, album: AlbumReport, links: _Links) -> str:
    values = album.cells()
    cells: list[str] = []
    for index, runs in enumerate(values):
        reference = _reference(index, number)
        style = _ALBUM if index == 0 else _CELL
        if index == 5:
            style = _TRACKS
        if index == 11 and album.source_url:
            style = _LINK
            links.add(reference, album.source_url)
        cells.append(_cell(reference, style, runs))
    return f'<row r="{number}">{"".join(cells)}</row>'


def _cell(reference: str, style: int, runs: Sequence[Run]) -> str:
    if not runs:
        return f'<c r="{reference}" s="{style}"/>'
    body = "".join(
        f'<r><rPr>{_INK_PROPERTIES[run.ink]}<sz val="11"/><rFont val="Calibri"/></rPr>'
        f'<t xml:space="preserve">{_text(run.text)}</t></r>'
        for run in runs
        if run.text
    )
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is>{body}</is></c>'


def _hyperlinks(links: _Links) -> str:
    if not links.entries:
        return ""
    body = "".join(
        f'<hyperlink ref="{reference}" r:id="rId{index}"/>'
        for index, (reference, _) in enumerate(links.entries, start=1)
    )
    return f"<hyperlinks>{body}</hyperlinks>"


def _sheet_rels(links: _Links) -> str:
    body = "".join(
        f'<Relationship Id="rId{index}"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"'
        f' Target={quoteattr(url)} TargetMode="External"/>'
        for index, (_, url) in enumerate(links.entries, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{body}</Relationships>"
    )


def _reference(index: int, row: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


def _text(value: str) -> str:
    """Escape one run's text, keeping the line breaks that make a cell readable."""
    kept = "".join(character for character in value if character >= " " or character in "\n\t")
    return escape(kept).replace("\n", "&#10;")
