"""What is published describes the application and nobody who works on it.

`tools/export_public.py` builds the published tree and refuses to build it when
a file names a person, quotes one, dates what they did, or names something from
a music library. These are the same checks, run over the tracked files on every
run of the suite, so a refusal arrives while the line is being written rather
than on the day of a release.

The first half proves each check refuses: an example of every category is
planted and has to be found, and a line that only resembles one has to pass. A
check that has never been seen refusing is not known to work.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

import export_public  # noqa: E402
from export_public import Finding, PublicFile  # noqa: E402


def _categories(path: str, text: str) -> set[str]:
    """What the text checks find in one invented file."""
    found = export_public.findings_in_text([PublicFile(path, text.encode("utf-8"))])
    return {finding.category for finding in found}


# --------------------------------------------------------------------------
# each check, seen refusing
# --------------------------------------------------------------------------

REFUSED = [
    ("person-word", "notes.md", "The shelf is drawn from his folders."),
    ("person-word", "notes.md", "He asked for the column to stay."),
    ("person-word", "notes.md", "It is the owner's decision."),
    ("person-word", "notes.md", "Decisão do dono, sem exceção."),
    ("person-word", "LEIAME.md", "A pasta do dono fica onde está."),
    ("portuguese", "module.py", "# isso não pode ser feito antes de gravar"),
    (
        "portuguese",
        "notes.md",
        "> Isso ainda não pode ser gravado, porque você precisa conferir antes.",
    ),
    ("date", "module.py", "# Measured on 2031-03-09 against the shelf."),
    ("date", "module.py", 'STAMP = "2031-03-09T10:00:00"'),
    ("date", "recorded.json", '{"requestedAt": "2031-03-09T10:00:00Z"}'),
    ("date", "notes.md", "It was found on Mar 9 while reading the log."),
    ("date", "notes.md", "It was found on the 21st."),
    ("date", "notes.md", "The report arrived on a Tuesday."),
    ("date", "notes.md", "Reported 09/03/2031."),
    ("time", "notes.md", "The album was organized at 09:15."),
    ("relative-time", "notes.md", "The same defect came back two days later."),
    ("relative-time", "notes.md", "It was reported that evening."),
    ("identifier", "module.py", "his_titles = local_titles(album)"),
    ("identifier", "app.js", "const ownerWord = payload.word;"),
    ("identifier", "styles.css", ".owner-note { color: red; }"),
    ("path", "module.py", 'ROOT = "/Users/margaret/Music"'),
    ("path", "module.py", 'ROOT = "/Volumes/Archive 2/Albums"'),
    ("email", "notes.md", "Write to margaret@mailbox.org about it."),
    ("host", "notes.md", "Signed as margaret@studio-mac.local by default."),
    ("handle", "notes.md", "Thanks to @margaret for the report."),
    ("timezone", "notes.md", "The machine runs at GMT+9."),
    ("timezone", "notes.md", "The log is in UTC and the clock reads +0900."),
    ("timezone", "notes.md", "Set to Asia/Tokyo."),
    ("record-reference", "module.py", "# The pairing rule (ADR-032)."),
    ("record-reference", "notes.md", "See the decision records for the reason."),
]

ACCEPTED = [
    ("module.py", "# The file is left where the scan found it."),
    ("module.py", "def helper(theme: str, shell: str) -> str: ..."),
    ("module.py", 'RELEASED = "1973-05-01"'),
    ("module.py", 'LENGTH = "4:33"'),
    ("recorded.json", '{"requestedAt": "2001-02-03T04:05:06.5Z", "elapsedTime": "00:00:13.64"}'),
    ("module.py", "@dataclass(frozen=True, slots=True)\nclass Thing:\n    pass\n"),
    ("styles.css", "@media (prefers-color-scheme: dark) { body { color: white; } }"),
    ("notes.md", "Write to somebody@example.com, or open `/Users/someone/Music`."),
    ("notes.md", "The noise floor sits at -96 dB and the cut at 16 kHz."),
    ("notes.md", "Para Velame e Quirema"),
    ("LEIAME.md", "Abra a pasta e veja o que há dentro dela antes de gravar."),
    ("notes.md", "https://github.com/cankblunt/diglibrary/releases/latest"),
]


@pytest.mark.parametrize(("category", "path", "text"), REFUSED)
def test_a_planted_example_of_each_category_is_refused(category, path, text) -> None:
    assert category in _categories(path, text), f"`{text}` passed the {category} check"


@pytest.mark.parametrize(("path", "text"), ACCEPTED)
def test_a_line_that_only_resembles_one_is_accepted(path, text) -> None:
    assert _categories(path, text) == set(), f"`{text}` was refused"


def test_every_rule_kept_beside_the_history_is_seen_refusing() -> None:
    """A place can only be refused by naming it, so that rule is not written here.

    Such rules are read from a folder that is not published, each with a line
    it has to refuse. A tree without that folder has none of them, and nothing
    to prove.
    """
    rules = export_public.LOCAL
    if not rules:
        pytest.skip("NOT RUN: no rules are kept beside this tree")

    for rule in rules:
        assert rule.category in _categories(
            "notes.md", rule.example
        ), f"a planted line passed the {rule.category} check"


def test_a_tree_without_those_rules_reads_none(tmp_path: Path) -> None:
    assert export_public.read_local_rules(tmp_path / "export_rules.toml") == ()


def test_a_path_is_read_like_a_line_of_the_file() -> None:
    """A file name is published with the file."""
    found = export_public.findings_in_text([PublicFile("docs/AUDIT-2031-03.md", b"Nothing.\n")])

    assert Finding("docs/AUDIT-2031-03.md", 0, "date") in found


def test_a_name_that_authored_a_commit_is_refused_in_a_file() -> None:
    file = PublicFile("notes.md", b"Written by Margaret Hollis.\n")

    found = export_public.findings_in_text([file], names=["Margaret", "Hollis"])

    assert {finding.category for finding in found} == {"committer-name"}


def _commit(folder: Path, path: str) -> None:
    """One more tracked file in an invented repository, under an invented name."""
    target = folder / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Nothing.\n", encoding="utf-8")
    identity = ["-c", "user.name=Margaret Hollis", "-c", "user.email=someone@example.com"]
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "--", path],
        ["git", *identity, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "one file"],
    ):
        subprocess.run(command, cwd=folder, check=True, capture_output=True)


def test_names_on_commits_are_compared_in_a_working_copy_and_nowhere_else(tmp_path) -> None:
    """The history of a published tree belongs to whoever commits to it.

    A name there that is also a word of the code would refuse every file using
    the word, and a contributor writing their own name is not what this
    refuses. A tree that tracks what is private is a working copy, and there
    the names on its commits are looked for in every file.
    """
    _commit(tmp_path, "README.md")
    assert not export_public.holds_what_is_private(tmp_path)
    assert export_public.committer_words(tmp_path) == []

    (tmp_path / "CLAUDE.md").write_text("Not tracked.\n", encoding="utf-8")
    assert export_public.committer_words(tmp_path) == []

    _commit(tmp_path, "internal/notes.md")
    assert export_public.holds_what_is_private(tmp_path)
    assert export_public.committer_words(tmp_path) == ["Hollis", "Margaret"]


def test_a_binary_is_refused_until_its_digest_is_recorded(monkeypatch) -> None:
    picture = PublicFile("docs/images/new.png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
    assert picture.text is None

    assert [finding.category for finding in export_public.findings_in_binaries([picture])] == [
        "binary-not-reviewed"
    ]

    import hashlib

    digest = hashlib.sha256(picture.data).hexdigest()
    monkeypatch.setitem(export_public.REVIEWED_BINARIES, picture.path, digest)
    assert export_public.findings_in_binaries([picture]) == []


def test_what_is_private_is_left_out_by_prefix() -> None:
    assert export_public.is_private("ADR/ADR-001.md")
    assert export_public.is_private("CLAUDE.md")
    assert export_public.is_private("internal/CHANGELOG.md")
    assert not export_public.is_private("README.md")
    assert not export_public.is_private("docs/CLAUDE.md.html")
    assert not export_public.is_private("src/diglibrary/ADR/module.py")


# --------------------------------------------------------------------------
# the library check, against an invented library
# --------------------------------------------------------------------------


@pytest.fixture
def invented_library(tmp_path: Path) -> Path:
    database = tmp_path / "library.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE album_units (folder_path TEXT)")
    connection.execute("CREATE TABLE audio_files (path TEXT)")
    connection.execute(
        "CREATE TABLE metadata_releases (title TEXT, primary_artist TEXT, payload TEXT)"
    )
    connection.execute("CREATE TABLE track_quality (reason TEXT)")
    connection.execute("CREATE TABLE something_new (label TEXT)")
    connection.execute(
        "INSERT INTO album_units VALUES (?)",
        ("/Users/someone/Music/Orquestra Zanzibá - Noite de Âmbar (1974) [FLAC]",),
    )
    connection.execute(
        "INSERT INTO audio_files VALUES (?)",
        ("/Users/someone/Music/Quartzo Lunar - Marés (1981)/03 - Vidro Fosco.flac",),
    )
    connection.execute(
        "INSERT INTO metadata_releases VALUES (?, ?, ?)",
        ("Open Water", "Summer", '{"tracklist": [{"title": "Velvet Turbine"}]}'),
    )
    connection.commit()
    connection.close()
    return database


def _library_findings(database: Path, text: str) -> list[Finding]:
    library = export_public.read_library_names(database)
    file = PublicFile("tests/test_example.py", text.encode("utf-8"))
    return export_public.findings_against_library([file], library)


@pytest.mark.parametrize(
    "text",
    [
        'album = "Noite de Ambar"',
        "# the ORQUESTRA ZANZIBA case",
        'title = "Vidro Fosco"',
        'stem = "03 - Vidro Fosco"',
        'artist = "quartzo lunar"',
        'track = "Velvet Turbine"',
        'album = "Open Water"',
    ],
)
def test_a_name_from_the_library_is_refused_however_it_is_spelled(invented_library, text) -> None:
    found = _library_findings(invented_library, text)

    assert [finding.category for finding in found] == ["library-name"], text


@pytest.mark.parametrize(
    "text",
    [
        "# nothing is written while the water is still open",
        "# the scan runs in summer and in winter",
        'heading = "Summer"',
        'album = "Invented Record"',
        'path = "/Users/someone/Music"',
    ],
)
def test_ordinary_words_are_not_a_name_until_they_are_written_as_one(
    invented_library, text
) -> None:
    if not export_public.ordinary_words():
        pytest.skip(f"NOT RUN: no word list at {export_public.DICTIONARY}")

    assert _library_findings(invented_library, text) == [], text


def test_a_column_that_is_a_list_of_names_is_read(tmp_path: Path) -> None:
    """A JSON list of bare strings has no key to say that each one is a name.

    Read only through keys, such a column is classified, counted as read and
    contributes nothing — the same answer as a table that holds no name.
    """
    database = tmp_path / "library.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE physical_record_candidates (title TEXT, artist TEXT, labels TEXT)"
    )
    connection.execute(
        "INSERT INTO physical_record_candidates VALUES (?, ?, ?)",
        ("Noite de Âmbar", "Orquestra Zanzibá", '["Discos Vidro Fosco"]'),
    )
    connection.commit()
    connection.close()

    assert export_public.read_library_names(database).unread_tables == ()
    for text in ('label = "Discos Vidro Fosco"', 'album = "Noite de Ambar"'):
        found = _library_findings(database, text)
        assert [finding.category for finding in found] == ["library-name"], text


def test_the_database_is_opened_read_only(invented_library) -> None:
    before = invented_library.read_bytes()

    export_public.read_library_names(invented_library)

    assert invented_library.read_bytes() == before
    assert not invented_library.with_name(invented_library.name + "-wal").exists()


def test_a_table_nobody_has_classified_is_reported(invented_library) -> None:
    """A table added later holds names or does not, and somebody has to say which."""
    library = export_public.read_library_names(invented_library)

    assert library.unread_tables == ("something_new",)


def test_a_finding_never_says_what_matched(invented_library) -> None:
    found = _library_findings(invented_library, 'album = "Noite de Ambar"')

    assert str(found[0]) == "tests/test_example.py:1: library-name"


# --------------------------------------------------------------------------
# the tracked files
# --------------------------------------------------------------------------


def _published() -> list[PublicFile]:
    return export_public.files_of_the_working_tree(PROJECT)


def _refusal(found: list[Finding]) -> str:
    shown = "\n".join(str(finding) for finding in found[:40])
    return f"{len(found)} finding(s) in what would be published:\n{shown}"


def test_something_is_read() -> None:
    """A check over no files passes, and proves nothing."""
    published = _published()

    assert len(published) > 100
    assert sum(file.text is not None for file in published) > 100
    assert not any(export_public.is_private(file.path) for file in published)


def test_no_published_file_describes_a_person() -> None:
    found = export_public.findings_in_text(_published(), export_public.committer_words(PROJECT))

    assert found == [], _refusal(found)


def test_every_published_binary_has_been_looked_at() -> None:
    found = export_public.findings_in_binaries(_published())

    assert found == [], _refusal(found)


def test_no_published_file_names_something_from_the_local_library() -> None:
    database = export_public.DEFAULT_DATABASE
    if not database.is_file():
        pytest.skip(f"NOT RUN: there is no library database at {database}")
    if not export_public.ordinary_words():
        pytest.skip(f"NOT RUN: no word list at {export_public.DICTIONARY}")

    library = export_public.read_library_names(database)
    assert library.names, "the database was read and held no name at all"
    assert (
        library.unread_tables == ()
    ), f"tables nobody has classified as holding names or not: {library.unread_tables}"

    found = export_public.findings_against_library(_published(), library)

    assert found == [], _refusal(found)
