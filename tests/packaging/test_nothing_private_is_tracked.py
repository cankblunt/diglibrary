"""What the repository must never carry.

Every check here is over `git ls-files` — what would be published — rather than
over the working tree, because the working tree is full of things that are
correctly ignored and the question is only ever about the tracked set.

Three criteria: no secret literal, no database, no home path. `.gitignore` is a
promise about a pattern and these are the promise about the result: a file
named `diglibrary.sqlite3?mode=ro` is not matched by `*.sqlite3`.
`test_nothing_personal_is_published.py` asks the wider question, of what a
file says rather than of what it is.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

from export_public import (  # noqa: E402
    DESCRIBES_THE_PATTERNS,
    committer_words,
    holds_what_is_private,
    is_private,
)

# The files that write out examples of what is refused, this one among them.
WRITES_EXAMPLES = DESCRIBES_THE_PATTERNS | {"tests/packaging/test_nothing_private_is_tracked.py"}


def _tracked() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(name) for name in listing.stdout.split("\0") if name]


def _tracked_text() -> list[tuple[Path, str]]:
    """Every tracked file that can be read as text, with its contents."""
    readable = []
    for relative in _tracked():
        path = PROJECT / relative
        if not path.is_file():
            continue
        try:
            readable.append((relative, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return readable


def test_no_database_is_tracked() -> None:
    """A database here is a whole library: every folder name, every album,
    every measurement. It is ignored by `.gitignore`, and that only helps for
    names the glob anticipated: `*.sqlite3` does not match a name that
    continues past the suffix.
    """
    databases = [
        name
        for name in _tracked()
        if re.search(r"\.sqlite3?($|[^a-z])|\.db$", str(name), flags=re.IGNORECASE)
    ]

    assert databases == [], f"a database is tracked: {databases}"


def test_no_log_or_copy_or_cache_is_tracked() -> None:
    """The log carries the full path of every folder scanned, and a copy of the
    database is the database. Both are ignored; both are one mistyped name away
    from being tracked anyway."""
    runtime_folders = ("logs/", "database-copies/", "cache/", "backup/", "reports/", "temp/")
    leaked = [
        name
        for name in _tracked()
        if name.name != ".gitkeep"
        and (str(name).endswith(".jsonl") or str(name).startswith(runtime_folders))
    ]

    assert leaked == [], f"runtime state is tracked: {leaked}"


def test_no_tracked_file_names_a_real_person_s_home() -> None:
    """An absolute home path says who wrote this and where their music lives;
    a test that needs one uses a plainly invented account."""
    offenders = [
        (name, match.group(0))
        for name, text in _tracked_text()
        if (match := re.search(r"/(?:Users|home)/(?!someone\b|test\b)[a-z][a-z0-9._-]{2,}", text))
        and str(name) not in WRITES_EXAMPLES
        and not is_private(str(name))
    ]

    assert offenders == [], f"a tracked file names somebody's home: {offenders}"


def test_no_tracked_file_carries_a_credential() -> None:
    """The project's first non-negotiable is that a secret lives in an
    environment variable. What is tracked names the *variable*, never a value.
    """
    # A credential is a name followed by something long enough to be one. The
    # environment-variable names this project uses end in the name and stop.
    looks_like_one = re.compile(r"""(?ix)
        \b (?: api[_-]?key | client[_-]?secret | access[_-]?token
             | auth[_-]?token | password )
        \s* [:=] \s* ["']? ([A-Za-z0-9/+_-]{16,}) ["']?
        """)
    offenders = []
    for name, text in _tracked_text():
        if str(name) in WRITES_EXAMPLES:
            continue
        for match in looks_like_one.finditer(text):
            value = match.group(1)
            # An environment-variable name is the thing this project *does*
            # write down, and it is upper case with underscores.
            if value.isupper() or value.startswith("DIGLIBRARY_"):
                continue
            offenders.append((str(name), match.group(0)[:40]))

    assert offenders == [], f"something shaped like a credential is tracked: {offenders}"


def test_the_claude_working_directory_is_never_tracked() -> None:
    """`.claude/` holds tooling written for one machine, absolute paths
    included. `.gitignore` keeps it out, which is a promise about a pattern;
    this is the promise about the result, and it also refuses the one route
    `.gitignore` cannot stop, which is `git add -f`.
    """
    inside = [name for name in _tracked() if str(name).startswith(".claude/")]

    assert inside == [], f"the Claude working directory is tracked: {inside}"


def test_no_tracked_file_carries_a_real_email_address() -> None:
    """An address is a person, and this project already writes down the two kinds
    that are not: the reserved example domains of RFC 2606, which resolve to
    nobody and are what a test needs when it asserts on a `User-Agent`; and
    GitHub's `users.noreply` form, which exists so that a commit can be authored
    without publishing the account's real address.

    Prose is full of things shaped like an address and not one — `boot@app.js`
    for the function `boot` in `app.js` — so the domain has to end in something
    that is a top-level domain rather than a file extension.
    """
    allowed = re.compile(
        r"@(?:[a-z0-9.-]+\.)?(?:example\.(?:com|org|net)|example|invalid|test|localhost)$"
        r"|@users\.noreply\.github\.com$",
        flags=re.IGNORECASE,
    )
    # A domain whose last label is a file extension is a code reference, not a
    # host. Written as the extensions this repository writes, plus the shape.
    not_a_host = re.compile(
        r"\.(?:js|py|ts|tsx|md|html|css|json|toml|yml|yaml|sh|txt|sql|cfg|ini)$",
        flags=re.IGNORECASE,
    )
    addresses = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    offenders = []
    for name, text in _tracked_text():
        if str(name) in WRITES_EXAMPLES:
            continue
        for match in addresses.finditer(text):
            address = match.group(0).rstrip(".")
            if allowed.search(address) or not_a_host.search(address):
                continue
            offenders.append((str(name), address))

    assert offenders == [], f"a tracked file carries an email address: {offenders}"


def test_every_commit_is_authored_without_a_real_address() -> None:
    """The rule above is about the files; this is about the envelope. A commit
    carries an author and a committer address, `git log` publishes both, and
    neither is written by anyone reviewing a diff — a `user.email` set globally
    and forgotten is enough. Every commit is authored in the `users.noreply`
    form, and this keeps that a fact.
    """
    allowed = re.compile(
        r"@users\.noreply\.github\.com$" r"|@(?:example\.(?:com|org|net)|invalid|localhost)$",
        flags=re.IGNORECASE,
    )
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H %ae %ce"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    offenders = []
    for line in log.stdout.splitlines():
        commit, author, committer = line.split(" ")
        for address in {author, committer}:
            if not allowed.search(address):
                offenders.append((commit[:8], address))

    assert offenders == [], f"a commit publishes an address: {sorted(set(offenders))[:10]}"


def test_no_tracked_file_names_a_person_who_committed() -> None:
    """The copyright holder is the project's account, so no tracked file has a
    reason to spell a committer's name — not the LICENSE, not a test. Written
    as *every tracked file*, so a file added later is asked the question.

    The names are read from the history rather than written here: a check that
    spelled one out would itself be a tracked file naming a person. And they
    are read in a working copy only, which is where what gets published is
    decided; the history of a published tree belongs to whoever commits to it.
    """
    if not holds_what_is_private(PROJECT):
        pytest.skip("NOT RUN: the names on commits are compared in a working copy")
    names = committer_words(PROJECT)
    assert names, "the history was read and held no name at all"
    offenders = [
        (str(name), word)
        for name, text in _tracked_text()
        for word in names
        if re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE)
    ]

    assert offenders == [], f"a tracked file names a committer: {offenders}"
