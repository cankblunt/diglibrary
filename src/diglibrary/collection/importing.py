"""Read a list of records a user already keeps, and say what importing it would do.

Pure: bytes and a mapping in, a plan out. Nothing here touches the database or a
catalogue, so the rehearsal the user reads is the same computation the import
then writes.

**The fields are an allow-list.** A user's list may carry any other column —
prices, freight, order numbers, notes about money — and none of them can enter,
because no field here can hold them: a column the user does not point at a field
is not read, and there is no field to point it at.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from diglibrary.collection.model import MEDIA_GRADES, SLEEVE_GRADES, ImportedRecord

FIELDS: tuple[str, ...] = (
    "artist",
    "album",
    "year",
    "label",
    "catalog_number",
    "edition",
    "country",
    "categories",
    "media_grade",
    "sleeve_grade",
    "location",
    "notes",
)
"""Every field a column can be pointed at, in the order the screen offers them."""

REPEATABLE: frozenset[str] = frozenset({"categories", "notes"})
"""Fields several columns may feed: genres spread over three columns, two note columns."""

_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    # Header words a list is likely to use, in the three languages this
    # application reads. A suggestion only: the user confirms every
    # column on screen before anything is read.
    "artist": ("artist", "artista", "artists", "interprete"),
    "album": ("album", "disco", "titulo", "title", "release"),
    "year": ("year", "ano da obra", "ano original", "ano"),
    "label": ("label", "gravadora", "selo", "sello", "record label"),
    "catalog_number": (
        "catno",
        "cat",
        "cat#",
        "cat no",
        "catalog",
        "catalogue",
        "catalog number",
        "catalogue number",
        "catalogo",
        "numero de catalogo",
        "n de catalogo",
    ),
    "edition": ("edition", "edicao", "edicion", "versao", "version", "prensagem", "pressing"),
    "country": ("country", "pais"),
    "categories": (
        "genre",
        "genres",
        "genero",
        "generos",
        "style",
        "styles",
        "estilo",
        "estilos",
        "category",
        "categories",
        "categoria",
        "categorias",
        "tag",
        "tags",
    ),
    "media_grade": (
        "media",
        "media condition",
        "disc",
        "disco estado",
        "estado da midia",
        "estado do disco",
        "condicao do disco",
        "vinyl condition",
    ),
    "sleeve_grade": (
        "sleeve",
        "sleeve condition",
        "capa",
        "estado da capa",
        "condicao da capa",
        "cover condition",
    ),
    "location": ("location", "local", "lugar", "prateleira", "shelf", "ubicacion"),
    "notes": ("notes", "note", "observacoes", "observacao", "obs", "notas", "comentarios"),
}

_GRADE_WORDS: dict[str, str] = {
    "m": "Mint (M)",
    "mint": "Mint (M)",
    "nm": "Near Mint (NM or M-)",
    "m-": "Near Mint (NM or M-)",
    "nm or m-": "Near Mint (NM or M-)",
    "near mint": "Near Mint (NM or M-)",
    "vg+": "Very Good Plus (VG+)",
    "very good plus": "Very Good Plus (VG+)",
    "vg": "Very Good (VG)",
    "very good": "Very Good (VG)",
    "g+": "Good Plus (G+)",
    "good plus": "Good Plus (G+)",
    "g": "Good (G)",
    "good": "Good (G)",
    "f": "Fair (F)",
    "fair": "Fair (F)",
    "p": "Poor (P)",
    "poor": "Poor (P)",
    "generic": "Generic",
    "no cover": "No Cover",
}
"""The abbreviations a list writes grades in, each read as one grade and no other."""


@dataclass(frozen=True, slots=True)
class Table:
    """A list as read: its column names and its rows, empty rows dropped.

    ``lines`` holds the line each row stands on in the file, because that is the
    number a spreadsheet shows: dropping the empty ones must not move it.
    """

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    lines: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """What importing a list would do, before anything is written."""

    records: tuple[ImportedRecord, ...] = ()
    skipped: tuple[tuple[int, str], ...] = ()
    already_here: tuple[tuple[int, str], ...] = ()
    ungraded: int = 0
    no_year: int = 0
    new_locations: tuple[str, ...] = ()
    new_categories: tuple[str, ...] = ()
    problems: tuple[str, ...] = field(default=())


def read_table(raw: bytes) -> Table:
    """Read a CSV the way a spreadsheet exports it.

    UTF-8, with or without the mark Excel writes, and failing that the Western
    code page an older Excel uses. The separator is a comma, a semicolon — what
    a spreadsheet set to Portuguese writes — or a tab, sniffed from the file.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    numbered = [(reader.line_num, tuple(cell.strip() for cell in row)) for row in reader]
    numbered = [(number, line) for number, line in numbered if any(line)]
    if not numbered:
        return Table(headers=(), rows=())
    width = max(len(line) for _, line in numbered)
    first = numbered[0][1]
    headers = tuple(
        name or f"Column {index}"
        for index, name in enumerate(first + ("",) * (width - len(first)), start=1)
    )
    body = numbered[1:]
    return Table(
        headers=headers,
        rows=tuple(line + ("",) * (width - len(line)) for _, line in body),
        lines=tuple(number for number, _ in body),
    )


def suggest_mapping(headers: Sequence[str]) -> dict[int, str]:
    """Propose a field for each column whose name says which one it is."""
    wanted = {word: name for name, words in _SUGGESTIONS.items() for word in words}
    suggested: dict[int, str] = {}
    taken: set[str] = set()
    for index, header in enumerate(headers):
        key = _plain(header)
        name = wanted.get(key) or wanted.get(re.sub(r"\s*\d+$", "", key))
        if name is None or (name in taken and name not in REPEATABLE):
            continue
        suggested[index] = name
        taken.add(name)
    return suggested


def mapping_problems(mapping: Mapping[int, str], width: int) -> tuple[str, ...]:
    """Why a mapping cannot be used, or nothing when it can."""
    problems: list[str] = []
    seen: dict[str, int] = {}
    for index, name in mapping.items():
        if not 0 <= index < width:
            problems.append(f"Column {index + 1} is not in the list.")
        if name not in FIELDS:
            problems.append(f"Not a field: {name!r}.")
        seen[name] = seen.get(name, 0) + 1
    for name, count in seen.items():
        if count > 1 and name not in REPEATABLE:
            problems.append(f"Only one column can be the {name.replace('_', ' ')}.")
    if "artist" not in seen and "album" not in seen:
        problems.append("Point a column at the artist or at the album.")
    return tuple(problems)


def plan_import(
    table: Table,
    mapping: Mapping[int, str],
    existing: Iterable[tuple[str, str, str, str]] = (),
    locations: Iterable[str] = (),
    categories: Iterable[str] = (),
) -> ImportPlan:
    """Say what importing ``table`` through ``mapping`` would put on the shelf.

    ``existing`` holds the keys of the records already on the shelf (see
    ``record_key``); a line with the same key is reported and left out, so a
    list imported twice does not fill the shelf twice. A grade that is not on
    the Goldmine scale is left out and counted — the record comes in ungraded,
    never with a guessed grade — and so is a year that is not a year.
    """
    problems = mapping_problems(mapping, len(table.headers))
    if problems:
        return ImportPlan(problems=problems)
    known = set(existing)
    known_locations = {_plain(name) for name in locations}
    known_categories = {_plain(name) for name in categories}
    records: list[ImportedRecord] = []
    skipped: list[tuple[int, str]] = []
    already: list[tuple[int, str]] = []
    new_locations: dict[str, str] = {}
    new_categories: dict[str, str] = {}
    ungraded = 0
    no_year = 0
    numbers = table.lines or tuple(range(2, len(table.rows) + 2))
    for line_number, row in zip(numbers, table.rows, strict=True):
        values = _values(row, mapping)
        artist = values.get("artist", "")
        album = values.get("album", "")
        if not artist and not album:
            skipped.append((line_number, "no artist and no album"))
            continue
        year = _year(values.get("year", ""))
        if values.get("year") and year is None:
            no_year += 1
        media = _grade(values.get("media_grade", ""), MEDIA_GRADES)
        sleeve = _grade(values.get("sleeve_grade", ""), SLEEVE_GRADES)
        ungraded += sum(
            1
            for raw, read in (
                (values.get("media_grade"), media),
                (values.get("sleeve_grade"), sleeve),
            )
            if raw and read is None
        )
        record = ImportedRecord(
            artist=artist,
            album=album,
            year=year,
            label=values.get("label", ""),
            catalog_number=values.get("catalog_number", ""),
            edition=values.get("edition", ""),
            country=values.get("country", ""),
            media_grade=media,
            sleeve_grade=sleeve,
            location=values.get("location", ""),
            notes=values.get("notes", ""),
            categories=_split_categories(values.get("categories", "")),
        )
        key = record_key(record.artist, record.album, record.label, record.catalog_number)
        if key in known:
            already.append((line_number, " - ".join(part for part in (artist, album) if part)))
            continue
        records.append(record)
        if record.location and _plain(record.location) not in known_locations:
            new_locations.setdefault(_plain(record.location), record.location)
        for name in record.categories:
            if _plain(name) not in known_categories:
                new_categories.setdefault(_plain(name), name)
    return ImportPlan(
        records=tuple(records),
        skipped=tuple(skipped),
        already_here=tuple(already),
        ungraded=ungraded,
        no_year=no_year,
        new_locations=tuple(new_locations.values()),
        new_categories=tuple(new_categories.values()),
    )


def record_key(
    artist: str, album: str, label: str, catalog_number: str
) -> tuple[str, str, str, str]:
    """What makes two lines the same record: artist, album, label and catalogue number."""
    return (_plain(artist), _plain(album), _plain(label), _plain(catalog_number))


def _values(row: Sequence[str], mapping: Mapping[int, str]) -> dict[str, str]:
    """The fields of one row; a repeatable field joins every column that feeds it."""
    gathered: dict[str, list[str]] = {}
    for index in sorted(mapping):
        text = row[index].strip() if index < len(row) else ""
        if text:
            gathered.setdefault(mapping[index], []).append(text)
    return {
        name: (
            ("; " if name == "categories" else " · ").join(parts)
            if name in REPEATABLE
            else parts[0]
        )
        for name, parts in gathered.items()
    }


def _year(text: str) -> int | None:
    stripped = text.strip()
    return int(stripped) if re.fullmatch(r"\d{4}", stripped) else None


def _grade(text: str, scale: tuple[str, ...]) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped in scale:
        return stripped
    read = _GRADE_WORDS.get(stripped.casefold())
    return read if read in scale else None


def _split_categories(text: str) -> tuple[str, ...]:
    names: dict[str, str] = {}
    for part in re.split(r"[;,]", text):
        name = part.strip()
        if name:
            names.setdefault(_plain(name), name)
    return tuple(names.values())


def _plain(text: str) -> str:
    """Casefolded, accents removed and spaces collapsed: how two names are compared."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", bare.casefold().replace("º", "").replace("°", "")).strip()
