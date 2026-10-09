"""Build the tree that is published, and refuse to build it if it describes anyone.

The working repository holds more than what is published. This tool takes the
tracked files of one revision, leaves out the paths listed in `PRIVATE`, and
reads every remaining file for anything that identifies or describes a person:
who they are, what they said, when they worked, what is in their music library.

    .venv/bin/python tools/export_public.py                 # rehearse, from `main`
    .venv/bin/python tools/export_public.py --working-tree  # rehearse, files as they are now
    .venv/bin/python tools/export_public.py --to DIR        # write the tree, if nothing refuses

It exits 0 when every check passes and 1 when one refuses. It never reads
standard input, never touches a remote, and writes nothing outside a temporary
folder unless `--to` is given and every check has passed. A check that could not
run says so and counts as a refusal when exporting: a check that read nothing
looks exactly like a check that found nothing.

A finding is reported as `path:line: category` and nothing else. The library
check compares against names read from a local database, and printing what
matched would copy those names into a terminal, a log or a transcript.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import tokenize
import tomllib
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# What is tracked and not published, each with the reason. A path is private
# when it starts with one of these; everything else is published, so a new
# top-level folder is public until somebody decides otherwise here.
PRIVATE: dict[str, str] = {
    "ADR/": "decision records narrate how each decision was reached, case by case",
    "CLAUDE.md": "the working agreement for tooling, written from the same cases",
    ".claude/": "session tooling written for one machine; ignored by git as well",
    "internal/": "audits, discovery studies, charter, specifications and the "
    "changelog of the versions published before the tree was rewritten",
}

# The files that spell out the patterns they refuse, and so match themselves.
DESCRIBES_THE_PATTERNS = frozenset(
    {
        "tools/export_public.py",
        "tests/packaging/test_nothing_personal_is_published.py",
    }
)

# The one page written in Portuguese on purpose.
WRITTEN_IN_PORTUGUESE = frozenset({"LEIAME.md"})

# Files that hold words of a language because reading those words is what the
# code does. The sentence check is not applied to them; every other check is.
LANGUAGE_TABLES: dict[str, str] = {
    "src/diglibrary/library/casing.py": "joining words and language markers of titles",
}

# Every binary that is published, by digest, after a person has opened it and
# looked. A changed or new binary has a digest that is not here, and the export
# refuses until somebody looks at it and records the new one.
REVIEWED_BINARIES: dict[str, str] = {
    "docs/brand/diglibrary-icon-1024.png": "ed33c283184fe5a8c70b6c1aab191122c0524bd53fdaad9e324bdc63d2ebe8ec",  # noqa: E501
    "docs/brand/diglibrary-lockup.png": "0d40f89cb9c49f9a12bf424183e1d3c200251c1d4ba4544745bb18554153d947",  # noqa: E501
    "docs/brand/diglibrary-stacked-1024.png": "96f693ecfe88812c624f1f31b23eba3d6b75ccdfa0605e9c3141748b84c0354b",  # noqa: E501
    "docs/images/install.gif": "760e6881a264c50e188f30ae42277948ec854e2e5718cd07e4719473517019f6",
    "docs/images/screen-library.png": "9520f9dd709acd44cde9b5c2fb6e60d989059edaea4da3029ac3899344b5ff6c",  # noqa: E501
    "docs/images/screen-mixing.png": "8dd49cfb6240baa0481fcee53be25393398bf8296a6999e273c4d6fe9603a3f7",  # noqa: E501
    "src/diglibrary/desktop/DigLibrary.icns": "ff6053bf880589a9242de81fada1877e697b1fcede34d8170891ba802513394e",  # noqa: E501
    "tests/fixtures/audio/tone-long.flac": "e7c786cf02864778f612031b7db200d6a05eede70c866576891373998de0c319",  # noqa: E501
    "tests/fixtures/audio/tone.aiff": "5f27e31a07c3c16564ade68039e487032e6bef9783d2320ded9fe445e95cb6cf",  # noqa: E501
    "tests/fixtures/audio/tone.flac": "e1c5123b830ef64786a672292c2d3d740c1c338646c7e58b00fd9dba4e13edba",  # noqa: E501
    "tests/fixtures/audio/tone.m4a": "5dc4607bc89a72a7d8326c77dfe6ac73cfb997ab753f5a05ae442011868721da",  # noqa: E501
    "tests/fixtures/audio/tone.mp3": "c0f5edc05824e38dcc1a88aef910ad3dcda906a262beaad08b055fd943dd54a8",  # noqa: E501
    "tests/fixtures/audio/tone.wav": "ee61e77f0b85db29c865912ab334c58c2ecf08d7949ba4f602089f633832cb10",  # noqa: E501
}


def _words(text: str) -> frozenset[str]:
    """A set of words, written the way a person writes a list of words."""
    return frozenset(text.split())


DEFAULT_DATABASE = Path.home() / ".diglibrary" / "diglibrary.sqlite3"


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    """One place in one file, and which kind of thing was found there."""

    path: str
    line: int
    category: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.category}"


@dataclass(frozen=True, slots=True)
class PublicFile:
    """A file that would be published: its path, its bytes, and its text if any."""

    path: str
    data: bytes

    @property
    def text(self) -> str | None:
        if b"\0" in self.data[:8192]:
            return None
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            return None


def is_private(path: str) -> bool:
    """Whether a tracked path stays out of the published tree."""
    return any(path == prefix or path.startswith(prefix) for prefix in PRIVATE)


# --------------------------------------------------------------------------
# Which lines of a file are prose
# --------------------------------------------------------------------------


DATA_SUFFIXES = (".json", ".tsv", ".csv")


def prose_lines(path: str, text: str) -> set[int] | None:
    """The line numbers holding comments and docstrings, for Python.

    `None` means the whole file is read as prose, which is the answer for
    a page, a stylesheet, a string table. A Python file that does not parse is
    read whole for the same reason: the strict reading is the safe one. A file
    of records holds no prose at all, so a length written `4:33` in it is a
    length; a calendar date is refused there like anywhere else.
    """
    if path.endswith(DATA_SUFFIXES):
        return set()
    if not path.endswith(".py"):
        return None
    lines: set[int] = set()
    try:
        tree = ast.parse(text)
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                lines.add(token.start[0])
    except (SyntaxError, tokenize.TokenError, IndentationError):
        return None
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


# --------------------------------------------------------------------------
# (a) words that refer to a person
# --------------------------------------------------------------------------

PERSON_WORDS = re.compile(
    r"\b(?:he|him|his|himself|owner|owners)\b(?:'s)?",
    flags=re.IGNORECASE,
)
PERSON_WORDS_PT = re.compile(
    r"\b(?:dele|o dono|do dono|ao dono|pelo dono)\b",
    flags=re.IGNORECASE,
)
OWNER_PT = re.compile(r"\bdono\b", flags=re.IGNORECASE)


def check_person_words(file: PublicFile, text: str) -> Iterator[Finding]:
    """A pronoun or a role that can only be pointing at somebody."""
    portuguese = file.path in WRITTEN_IN_PORTUGUESE
    for number, line in enumerate(text.splitlines(), start=1):
        # On the Portuguese page `dele` is an ordinary pronoun for a thing;
        # the role is refused there all the same.
        in_portuguese = OWNER_PT if portuguese else PERSON_WORDS_PT
        if PERSON_WORDS.search(line) or in_portuguese.search(line):
            yield Finding(file.path, number, "person-word")


# --------------------------------------------------------------------------
# (b) Portuguese where English is expected
# --------------------------------------------------------------------------

# Function words, which a title or a name rarely strings together and a
# sentence always does. The strong ones are not words of English or of most
# names; two of them on a line is a sentence.
PORTUGUESE_STRONG = _words(
    "não você voce são está estão também tambem já então entao porque isso isto "
    "muito aqui ainda depois antes sempre nunca agora fazer pode precisa deve "
    "foi eram tinha tudo nada cada onde quando quem qual quais essa esse esta "
    "este aquele aquela pelo pela pelos pelas numa num nas nos dos das aos às "
    "uma umas uns meu minha seu sua dele dela"
)
PORTUGUESE_WEAK = _words("que com para por sem mais mas como ser ter em de do da ao se")
WORD = re.compile(r"[^\W\d_]+", flags=re.UNICODE)


def check_portuguese(file: PublicFile, text: str) -> Iterator[Finding]:
    """A sentence in Portuguese outside the page that is written in it."""
    if file.path in WRITTEN_IN_PORTUGUESE or file.path in LANGUAGE_TABLES:
        return
    for number, line in enumerate(text.splitlines(), start=1):
        words = [word.casefold() for word in WORD.findall(line)]
        strong = sum(word in PORTUGUESE_STRONG for word in words)
        weak = sum(word in PORTUGUESE_WEAK for word in words)
        if strong >= 2 or (strong >= 1 and weak >= 2) or weak >= 4:
            yield Finding(file.path, number, "portuguese")


# --------------------------------------------------------------------------
# (d) dates and times
# --------------------------------------------------------------------------

MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
CALENDAR_DATE = re.compile(
    r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?\b"
    r"|\b\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})\b"
    rf"|\b{MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?!\s*[%x\u00d7])"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH}\b"
    rf"|\b{MONTH}\s+(?:19|20)\d{{2}}\b"
    r"|\bon the \d{1,2}(?:st|nd|rd|th)\b"
    r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b",
    flags=re.IGNORECASE,
)
CLOCK_TIME = re.compile(r"(?<![\d:.])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?![\d:])")
RELATIVE_TIME = re.compile(
    r"\b(?:yesterday|tonight|last night|this morning|this evening|overnight"
    r"|that (?:same )?(?:evening|night|morning|afternoon|day|week)"
    r"|(?:the )?(?:next|following|previous) (?:day|morning|evening|night|week)"
    r"|(?:\w+ )?(?:minutes?|hours?|days?|weeks?|months?) "
    r"(?:later|earlier|ago|after (?:this|it) shipped)"
    r"|at night|in the (?:morning|evening|afternoon))\b",
    flags=re.IGNORECASE,
)


def check_dates(file: PublicFile, text: str) -> Iterator[Finding]:
    """A day, an hour, or a phrase that places something in somebody's week.

    A calendar date is refused everywhere, data included: a fixture that
    needs one can use a day that is plainly invented, and is listed in
    `INVENTED_DAYS`. A clock time and a relative phrase are refused in prose
    only, because `4:33` in data is a track length and `16:9` is a ratio.
    """
    prose = prose_lines(file.path, text)
    for number, line in enumerate(text.splitlines(), start=1):
        dates = [match.group(0) for match in CALENDAR_DATE.finditer(line)]
        if any(not is_invented_day(found) for found in dates):
            yield Finding(file.path, number, "date")
        if prose is None or number in prose:
            if CLOCK_TIME.search(line):
                yield Finding(file.path, number, "time")
            if RELATIVE_TIME.search(line):
                yield Finding(file.path, number, "relative-time")


# A fixture needs a day; it does not need a day on which anything happened.
# Every date in data is written in one of these years, which precede the
# project, or is a release date of an invented record in the last century.
INVENTED_DAYS = re.compile(r"^(?:19\d{2}|200\d|201\d)-")


def is_invented_day(found: str) -> bool:
    """Whether a date is one a fixture may carry."""
    return bool(INVENTED_DAYS.match(found))


# --------------------------------------------------------------------------
# (e) identifiers
# --------------------------------------------------------------------------

IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:-[A-Za-z][A-Za-z0-9]*)*")
IDENTIFIER_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
PERSON_PARTS = frozenset({"he", "him", "his", "himself", "owner", "owners"})


def check_identifiers(file: PublicFile, text: str) -> Iterator[Finding]:
    """A name in code, a key or a class that is built from one of those words.

    `his_titles` holds no word boundary a plain search would find, so every
    compound is taken apart at its underscores, hyphens and capitals.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        for match in IDENTIFIER.finditer(line):
            name = match.group(0)
            parts = [part.casefold() for part in IDENTIFIER_PART.findall(name)]
            if len(parts) > 1 and PERSON_PARTS.intersection(parts):
                yield Finding(file.path, number, "identifier")
                break


# --------------------------------------------------------------------------
# (f) machines and accounts
# --------------------------------------------------------------------------

INVENTED_ACCOUNTS = r"(?:someone|somebody|me|test|user|username|you|name|example|runner)"
HOME_PATH = re.compile(
    rf"/(?:Users|home)/(?!{INVENTED_ACCOUNTS}\b)[A-Za-z][A-Za-z0-9._-]+"
    r"|/Volumes/(?!Music\b|Example\b|External\b)[A-Za-z][\w .-]*"
    r"|[A-Za-z]:\\Users\\(?!someone\b|test\b)[A-Za-z][\w.-]+",
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_OF_NOBODY = re.compile(
    r"@(?:[a-z0-9.-]+\.)?(?:example\.(?:com|org|net)|example|invalid|test|localhost)$"
    r"|@users\.noreply\.github\.com$",
    flags=re.IGNORECASE,
)
FILE_NOT_HOST = re.compile(
    r"\.(?:js|py|ts|tsx|md|html|css|json|toml|yml|yaml|sh|txt|sql|cfg|ini)$",
    flags=re.IGNORECASE,
)
LOCAL_HOST = re.compile(
    r"\b(?!(?:example|host|name|printer|machine)\.local\b)[A-Za-z][A-Za-z0-9-]*\.local\b"
)
HANDLE = re.compile(
    r"(?<![\w/.@`])@(?!media\b|import\b|font-face\b|keyframes\b)[A-Za-z][\w-]{2,}\b"
)
TIMEZONE = re.compile(
    r"\b(?:GMT|UTC)\s?[+\u2212-]\s?\d{1,2}(?::?\d{2})?\b"
    r"|(?<![\w.,-])[+\u2212-](?:0\d|1[0-4])(?::?(?:00|30|45))\b(?!\s*(?:Hz|dB|ms|px|%))"
    r"|\b(?:America|Europe|Asia|Africa|Australia|Pacific|Atlantic)/[A-Z][A-Za-z_]+\b",
)


def check_machines_and_accounts(file: PublicFile, text: str) -> Iterator[Finding]:
    """A home folder, an address, a host, an account, a time zone."""
    prose = prose_lines(file.path, text)
    python = file.path.endswith(".py")
    for number, line in enumerate(text.splitlines(), start=1):
        if HOME_PATH.search(line):
            yield Finding(file.path, number, "path")
        for match in EMAIL.finditer(line):
            address = match.group(0).rstrip(".")
            if not EMAIL_OF_NOBODY.search(address) and not FILE_NOT_HOST.search(address):
                yield Finding(file.path, number, "email")
        if LOCAL_HOST.search(line):
            yield Finding(file.path, number, "host")
        # In Python a line of code that begins with `@` is a decorator.
        in_prose = prose is None or number in prose
        if (in_prose or not python) and HANDLE.search(EMAIL.sub("", line)):
            yield Finding(file.path, number, "handle")
        if TIMEZONE.search(line):
            yield Finding(file.path, number, "timezone")


# --------------------------------------------------------------------------
# rules that can only be written by naming what they refuse
# --------------------------------------------------------------------------

# A place, the abbreviation of one time zone: a rule about either names it,
# and this file is published. They are kept beside the history instead, in a
# folder that is not, each with a line it has to refuse. A tree without the
# file has none of these rules, and says so; a working copy that holds what is
# private and has lost the file is refused, like every check that could not
# run.
LOCAL_RULES = PROJECT / "internal" / "export_rules.toml"


@dataclass(frozen=True, slots=True)
class LocalRule:
    """One pattern, the category it reports, and a line it must refuse."""

    category: str
    pattern: re.Pattern[str]
    example: str
    on_the_portuguese_page: bool


def read_local_rules(path: Path) -> tuple[LocalRule, ...]:
    """The rules in `path`, or none when there is no such file.

    A file that is there and cannot be read raises: half a set of rules read
    as the whole set is the failure this tool exists to prevent.
    """
    if not path.is_file():
        return ()
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    return tuple(
        LocalRule(
            category=str(entry["category"]),
            pattern=re.compile(
                str(entry["pattern"]), flags=re.IGNORECASE if entry.get("any_case") else 0
            ),
            example=str(entry["example"]),
            on_the_portuguese_page=bool(entry.get("on_the_portuguese_page", True)),
        )
        for entry in written.get("refuse", [])
    )


LOCAL = read_local_rules(LOCAL_RULES)


def holds_what_is_private(root: Path) -> bool:
    """Whether this tree is a working copy rather than a published one.

    Asked of what git tracks: a folder that anybody may create beside a clone
    does not make the clone a working copy. A folder that is no repository at
    all tracks nothing.
    """
    try:
        return any(is_private(path) for path in tracked_paths(root, None))
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_local_rules(file: PublicFile, text: str) -> Iterator[Finding]:
    """Whatever the rules kept beside the history refuse."""
    portuguese = file.path in WRITTEN_IN_PORTUGUESE
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in LOCAL:
            if portuguese and not rule.on_the_portuguese_page:
                continue
            if rule.pattern.search(line):
                yield Finding(file.path, number, rule.category)


# --------------------------------------------------------------------------
# references to the private record
# --------------------------------------------------------------------------

RECORD_REFERENCE = re.compile(r"\bADRs?\b|\bdecision records?\b", flags=re.IGNORECASE)


def check_record_references(file: PublicFile, text: str) -> Iterator[Finding]:
    """A pointer to a record that the published tree does not contain."""
    for number, line in enumerate(text.splitlines(), start=1):
        if RECORD_REFERENCE.search(line):
            yield Finding(file.path, number, "record-reference")


# --------------------------------------------------------------------------
# names of people who committed
# --------------------------------------------------------------------------


def committer_words(root: Path) -> list[str]:
    """Every word of four letters or more in a name that authored a commit.

    Read from the history rather than written here, so this file names nobody.

    Only a working copy is asked. Its history is this project's own, and a name
    on it has no reason to be in a file. The history of a published tree
    belongs to whoever commits to it: a name of theirs in a file is theirs to
    write, and one that is also a word of the code would refuse every file that
    uses the word.
    """
    if not holds_what_is_private(root):
        return []
    log = subprocess.run(
        ["git", "log", "--format=%an%n%cn"], cwd=root, capture_output=True, text=True
    )
    if log.returncode != 0:
        return []
    return sorted(
        {word for line in log.stdout.splitlines() for word in line.split() if len(word) >= 4}
    )


def check_committer_names(file: PublicFile, text: str, words: Iterable[str]) -> Iterator[Finding]:
    """The name on the commits is allowed on the commits and nowhere else."""
    patterns = [re.compile(rf"\b{re.escape(word)}\b", flags=re.IGNORECASE) for word in words]
    for number, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in patterns):
            yield Finding(file.path, number, "committer-name")


# --------------------------------------------------------------------------
# (c) names from a local library
# --------------------------------------------------------------------------

# Columns that hold a name or a path made of names. Written as the text
# columns of the tables that describe a library; a table added later is not
# read until it is named here, and `unread_tables` reports it so that the
# omission is a decision.
NAME_COLUMNS: dict[str, tuple[str, ...]] = {
    "album_units": ("folder_path",),
    "audio_files": ("path",),
    "downloads": ("directory", "folder", "landed_path"),
    "metadata_releases": ("title", "primary_artist", "payload"),
    "manual_corrections": ("value", "replaced"),
    "playlists": ("name",),
    "playlist_tracks": ("remembered_track", "remembered_artist", "remembered_album"),
    "quality_bench_albums": ("folder_path",),
    "quality_bench_files": ("file_name",),
    "quality_bench_roots": ("path",),
    "change_operations": ("target_path", "before_state", "after_state", "backup_path"),
    "collection_categories": ("name",),
    "collection_locations": ("name",),
    "physical_records": ("artist", "album", "notes", "label", "edition"),
    "physical_record_candidates": ("title", "artist", "labels"),
}

# Tables that hold no name: digests, verdicts, measurements, bookkeeping.
NO_NAMES_IN = frozenset(
    {
        "acoustic_fingerprints",
        "acoustic_prints",
        "apply_proofs",
        "change_plans",
        "duplicate_decisions",
        "identifications",
        "physical_record_categories",
        "quality_overrides",
        "quality_words",
        "schema_migrations",
        "settings",
        "track_harmonics",
        "track_quality",
        "sqlite_sequence",
    }
)

# JSON keys under which a catalogue answer carries a name.
NAME_KEYS = frozenset({"title", "name", "artist", "artists", "album", "label", "sort-name"})

# Words that are in a library as a name and in this project as vocabulary.
# Each is here because a check reported a line on which it was the only
# candidate and the line was about the application, not about a record. A
# name made only of these words, or of function words, is not compared.
PROJECT_VOCABULARY = _words("""
    album albums artist artists track tracks title titles disc disk cd vinyl
    music library libraries download downloads documents desktop users volumes
    unknown various untitled intro outro interlude bonus live remix remaster
    remastered edition deluxe original version instrumental single side
    flac mp3 wav aiff m4a ogg opus aac cover covers folder folders backup
    diglibrary soulseek slskd musicbrainz discogs acoustid bandcamp spotify
    rekordbox claude github pypi homebrew python sqlite windows linux macos
    home compilation mixtape automation terminal escape
    riff wave vivo versao acustico voce minha corazon coracao bossa nova mpb
    """)
FUNCTION_WORDS = _words("""
    a an the of and or in on at to for with from by is it as be this that not no
    o os as ao aos um uma de do da dos das e em no na com para por que se
    el la los las y en del un una
    """)
DECORATION = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
LEADING_NUMBER = re.compile(r"^\s*(?:[A-Da-d]?\d{1,3})\s*[-.)_ ]\s*")
AUDIO_SUFFIX = re.compile(
    r"\.(?:flac|mp3|wav|aiff?|m4a|ogg|opus|aac|wv|ape|jpg|jpeg|png|cue|log)$", re.I
)
LONGEST_NAME = 8


def normalize(text: str) -> tuple[str, ...]:
    """Words of a name, without case, accents or punctuation."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return tuple(re.findall(r"[a-z0-9]+", plain.casefold()))


def _names_in(value: str) -> Iterator[str]:
    """Every name a stored value holds: the whole, and the parts it is built from."""
    for component in re.split(r"[/\\]", value):
        component = AUDIO_SUFFIX.sub("", component.strip())
        if not component:
            continue
        bare = DECORATION.sub(" ", component)
        for candidate in (component, bare, LEADING_NUMBER.sub("", bare)):
            yield candidate
            yield from re.split(r"\s+[-\u2013\u2014]\s+|\s*_-_\s*", candidate)


def _names_in_json(payload: object) -> Iterator[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in NAME_KEYS and isinstance(value, str):
                yield value
            else:
                yield from _names_in_json(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _names_in_json(item)


@dataclass(frozen=True, slots=True)
class LibraryNames:
    """The names to compare against, and what they were read from."""

    names: frozenset[tuple[str, ...]]
    rows_read: int
    tables_read: tuple[str, ...]
    unread_tables: tuple[str, ...]


def is_comparable(words: tuple[str, ...]) -> bool:
    """Whether a name says enough to be worth looking for.

    A name of one short word, a number, or nothing but vocabulary and
    function words would match every file in the project and identify
    nothing.
    """
    if not words or len(words) > LONGEST_NAME:
        return False
    telling = [
        word
        for word in words
        if word not in FUNCTION_WORDS and word not in PROJECT_VOCABULARY and not word.isdigit()
    ]
    if not telling:
        return False
    return len("".join(telling)) >= 4


def read_library_names(database: Path) -> LibraryNames:
    """Read every name from a library database, opened read-only."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        present = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        names: set[tuple[str, ...]] = set()
        rows_read = 0
        tables_read = []
        for table, columns in NAME_COLUMNS.items():
            if table not in present:
                continue
            existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            wanted = [column for column in columns if column in existing]
            if not wanted:
                continue
            tables_read.append(table)
            quoted = ", ".join(f'"{column}"' for column in wanted)
            for row in connection.execute(f'SELECT {quoted} FROM "{table}"'):
                rows_read += 1
                for column, value in zip(wanted, row, strict=True):
                    if not isinstance(value, str) or not value:
                        continue
                    found: Iterable[str]
                    if column == "payload" or value[:1] in "{[":
                        try:
                            parsed = json.loads(value)
                        except ValueError:
                            found = _names_in(value)
                        else:
                            found = list(_names_in_json(parsed))
                            # A column that is itself a list of names, such as
                            # the labels of a pressing: no key says what each
                            # string is, so the column being named above does.
                            if isinstance(parsed, list):
                                found += [item for item in parsed if isinstance(item, str)]
                    else:
                        found = _names_in(value)
                    for name in found:
                        words = normalize(name)
                        if is_comparable(words):
                            names.add(words)
        unread = sorted(present - set(NAME_COLUMNS) - NO_NAMES_IN)
        return LibraryNames(frozenset(names), rows_read, tuple(tables_read), tuple(unread))
    finally:
        connection.close()


# A word of the language, which a name may also be. A name made of several
# such words is reported only where the text writes it as a name, with a
# capital on every telling word. One such word alone is never reported: a
# column heading that reads `Progress` names no record.
DICTIONARY = Path("/usr/share/dict/words")


def ordinary_words() -> frozenset[str]:
    """Words of English, from the system's list when there is one."""
    if not DICTIONARY.is_file():
        return frozenset()
    text = DICTIONARY.read_text(encoding="utf-8", errors="ignore")
    return frozenset(word.casefold() for word in text.split() if word.islower())


# The list holds `step` and not `steps`, `share` and not `shared`.
INFLECTIONS = ("s", "es", "ed", "d", "ing", "ly", "er", "ers", "ies", "ied")


class EveryWord(frozenset):
    """The word list of a page written in a language the system has no list for.

    Every word of such a page is taken for a word of its language, so a name is
    reported there only where it is written as a name.
    """

    def __contains__(self, word: object) -> bool:
        return True


def is_ordinary(word: str, ordinary: frozenset[str]) -> bool:
    """Whether a word is one of the language, in any of its common forms."""
    if word in ordinary or word.isdigit():
        return True
    for ending in INFLECTIONS:
        if not word.endswith(ending) or len(word) - len(ending) < 3:
            continue
        stem = word[: -len(ending)]
        if stem in ordinary or f"{stem}e" in ordinary or f"{stem}y" in ordinary:
            return True
        if len(stem) > 3 and stem[-1] == stem[-2] and stem[:-1] in ordinary:
            return True
    return False


TEXT_WORD = re.compile(r"[^\W_]+", flags=re.UNICODE)


def check_library_names(
    file: PublicFile, text: str, library: LibraryNames, ordinary: frozenset[str]
) -> Iterator[Finding]:
    """A name that is in the local library, written in a published file."""
    for number, line in enumerate(text.splitlines(), start=1):
        spans = [(match.group(0), match.start(), match.end()) for match in TEXT_WORD.finditer(line)]
        words = []
        for raw, start, end in spans:
            for word in normalize(raw):
                words.append((word, raw, start, end))
        if _line_names_something(words, line, library, ordinary):
            yield Finding(file.path, number, "library-name")


def _line_names_something(
    words: list[tuple[str, str, int, int]],
    line: str,
    library: LibraryNames,
    ordinary: frozenset[str],
) -> bool:
    for at in range(len(words)):
        for length in range(1, LONGEST_NAME + 1):
            window = words[at : at + length]
            if len(window) < length:
                break
            key = tuple(word for word, _, _, _ in window)
            if key not in library.names:
                continue
            telling = [item for item in window if item[0] not in FUNCTION_WORDS]
            if any(not is_ordinary(word, ordinary) for word, _, _, _ in telling):
                return True
            written_as_a_name = all(raw[:1].isupper() for _, raw, _, _ in telling)
            if not written_as_a_name:
                continue
            if len(telling) >= 2:
                return True
    return False


# --------------------------------------------------------------------------
# binaries
# --------------------------------------------------------------------------


def check_binary(file: PublicFile) -> Iterator[Finding]:
    """A picture or a recording is published only after somebody has looked."""
    digest = hashlib.sha256(file.data).hexdigest()
    if REVIEWED_BINARIES.get(file.path) != digest:
        yield Finding(file.path, 0, "binary-not-reviewed")


# --------------------------------------------------------------------------
# reading a tree
# --------------------------------------------------------------------------


def tracked_paths(root: Path, revision: str | None) -> list[str]:
    """What git tracks, at a revision or in the index."""
    command = ["git", "ls-files", "-z"]
    if revision is not None:
        command = ["git", "ls-tree", "-r", "-z", "--name-only", revision]
    listing = subprocess.run(command, cwd=root, capture_output=True, check=True)
    return [name for name in listing.stdout.decode("utf-8").split("\0") if name]


def files_of_the_working_tree(root: Path) -> list[PublicFile]:
    """Every tracked, published file, with the bytes it has on disk now."""
    files = []
    for path in tracked_paths(root, None):
        on_disk = root / path
        if is_private(path) or not on_disk.is_file():
            continue
        files.append(PublicFile(path, on_disk.read_bytes()))
    return files


def files_of_a_folder(folder: Path) -> list[PublicFile]:
    """Every file under a folder that was exported."""
    return [
        PublicFile(path.relative_to(folder).as_posix(), path.read_bytes())
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    ]


def extract(root: Path, revision: str, into: Path) -> None:
    """Write the published files of a revision into an empty folder."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision], cwd=root, capture_output=True, check=True
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        members = [
            member for member in tar.getmembers() if member.isfile() and not is_private(member.name)
        ]
        tar.extractall(into, members=members, filter="data")


# --------------------------------------------------------------------------
# running every check
# --------------------------------------------------------------------------

TEXT_CHECKS = (
    check_person_words,
    check_portuguese,
    check_dates,
    check_identifiers,
    check_machines_and_accounts,
    check_local_rules,
    check_record_references,
)


def findings_in_text(files: Iterable[PublicFile], names: Iterable[str] = ()) -> list[Finding]:
    """Every check that needs nothing but the files themselves."""
    words = list(names)
    found: list[Finding] = []
    for file in files:
        if file.path in DESCRIBES_THE_PATTERNS:
            continue
        text = file.text
        # A path is published too, and is read as one more line of the file.
        named = PublicFile(file.path, b"")
        for check in TEXT_CHECKS:
            found.extend(Finding(file.path, 0, item.category) for item in check(named, file.path))
        if text is None:
            continue
        for check in TEXT_CHECKS:
            found.extend(check(file, text))
        found.extend(check_committer_names(file, text, words))
    return sorted(set(found))


def findings_in_binaries(files: Iterable[PublicFile]) -> list[Finding]:
    return sorted(finding for file in files if file.text is None for finding in check_binary(file))


def findings_against_library(files: Iterable[PublicFile], library: LibraryNames) -> list[Finding]:
    ordinary = ordinary_words()
    found: list[Finding] = []
    for file in files:
        if file.path in DESCRIBES_THE_PATTERNS:
            continue
        found.extend(
            Finding(file.path, 0, item.category)
            for item in check_library_names(file, file.path, library, ordinary)
        )
        if file.text is not None:
            words = EveryWord() if file.path in WRITTEN_IN_PORTUGUESE else ordinary
            found.extend(check_library_names(file, file.text, library, words))
    return sorted(set(found))


def report(findings: list[Finding], out=sys.stdout) -> None:
    for finding in findings:
        print(finding, file=out)
    by_category = Counter(finding.category for finding in findings)
    for category, count in sorted(by_category.items()):
        files = len({finding.path for finding in findings if finding.category == category})
        print(f"  {category}: {count} in {files} file(s)", file=out)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--revision", default="main", help="what to export (default: main)")
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="read the tracked files as they are on disk instead of a revision",
    )
    parser.add_argument("--to", type=Path, help="write the tree here if nothing refuses")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--only", nargs="*", default=[], help="report these paths only")
    parser.add_argument("--summary", action="store_true", help="print the counts only")
    options = parser.parse_args(arguments)

    if options.to is not None and options.working_tree:
        print("refused: a tree is exported from a revision, not from files in progress")
        return 1
    if options.to is not None and options.to.exists() and any(options.to.iterdir()):
        print(f"refused: {options.to} is not empty")
        return 1

    with tempfile.TemporaryDirectory(prefix="diglibrary-public-") as temporary:
        staged = Path(temporary) / "tree"
        staged.mkdir()
        if options.working_tree:
            files = files_of_the_working_tree(PROJECT)
            source = "the working tree"
        else:
            extract(PROJECT, options.revision, staged)
            files = files_of_a_folder(staged)
            source = f"revision {options.revision}"

        print(f"{len(files)} files from {source}; left out by rule:")
        for prefix, reason in PRIVATE.items():
            print(f"  {prefix} — {reason}")

        names = committer_words(PROJECT)
        if holds_what_is_private(PROJECT):
            print(f"names on commits: {len(names)} word(s) compared with every file")
        else:
            print("names on commits: not compared, this is a published tree")
        findings = findings_in_text(files, names)
        findings += findings_in_binaries(files)

        could_not_run = []
        if LOCAL_RULES.is_file():
            print(f"rules kept beside the history: {len(LOCAL)} pattern(s)")
        elif holds_what_is_private(PROJECT):
            could_not_run.append(
                f"rules kept beside the history: NOT RUN, there is no {LOCAL_RULES}"
            )
        else:
            print("rules kept beside the history: none in this tree")
        if options.database.is_file():
            library = read_library_names(options.database)
            findings += findings_against_library(files, library)
            print(
                f"library names: {len(library.names)} distinct names from {library.rows_read} "
                f"rows of {len(library.tables_read)} tables, compared with every file"
            )
            if library.unread_tables:
                could_not_run.append(
                    "library names: tables nobody has classified: "
                    + ", ".join(library.unread_tables)
                )
            if not ordinary_words():
                could_not_run.append(f"library names: no word list at {DICTIONARY}")
        else:
            could_not_run.append(
                f"library names: NOT RUN, there is no database at {options.database}"
            )

        if options.only:
            wanted = tuple(options.only)
            folders = tuple(f"{path.rstrip('/')}/" for path in wanted)
            asked = [file for file in files if file.path in wanted or file.path.startswith(folders)]
            print(f"--only: {len(asked)} of those files are under the paths given")
            if not asked:
                print("REFUSED: the paths given name no published file, so nothing was checked")
                return 1
            findings = [
                finding
                for finding in findings
                if finding.path in wanted or finding.path.startswith(folders)
            ]

        findings = sorted(set(findings))
        if options.summary:
            report([], sys.stdout)
            by_category = Counter(finding.category for finding in findings)
            for category, count in sorted(by_category.items()):
                touched = len({f.path for f in findings if f.category == category})
                print(f"  {category}: {count} in {touched} file(s)")
        else:
            report(findings)
        for line in could_not_run:
            print(line)

        if findings or could_not_run:
            print(f"REFUSED: {len(findings)} finding(s), {len(could_not_run)} check(s) not run")
            return 1

        print("every check passed")
        if options.to is not None:
            options.to.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged, options.to, dirs_exist_ok=True)
            print(f"written to {options.to}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
