"""Everything this application reads at runtime has to be in the package.

The failure this guards is invisible on a developer machine and total on
anybody else's: a file that is not `.py` is only installed if `package-data`
names it, and a checkout finds it anyway because the file is right there. The
window's HTML, its script, its stylesheet and the configuration template are
all in that category — without them a fresh install opens a window that draws
nothing.
"""

import fnmatch
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
SOURCE = PROJECT / "src" / "diglibrary"

# Suffixes that are code or build noise, and so are not data anybody ships.
NOT_DATA = {".py", ".pyc", ".pyo"}


def _declared() -> dict[str, list[str]]:
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]


def _is_declared(relative: Path, declared: dict[str, list[str]]) -> bool:
    """Whether some package's glob covers this file."""
    for package, patterns in declared.items():
        folder = Path(package.replace(".", "/")).relative_to("diglibrary")
        try:
            within = relative.relative_to(folder)
        except ValueError:
            continue
        if any(fnmatch.fnmatch(str(within), pattern) for pattern in patterns):
            return True
    return False


def test_every_file_the_application_reads_is_one_it_ships() -> None:
    """Written by walking the tree rather than by listing what we remember.

    A list of the data files somebody already knew about makes the next one
    invisible, and here that means a first run drawing an empty window.
    """
    declared = _declared()
    missing = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file() or path.suffix in NOT_DATA:
            continue
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(SOURCE)
        if not _is_declared(relative, declared):
            missing.append(str(relative))
    assert not missing, (
        "these files are read at runtime and would not be installed, so a "
        f"fresh install has them missing: {missing}"
    )


def test_nothing_of_the_project_has_fallen_into_a_table_below_it() -> None:
    """TOML gives a bare key to the heading above it, and packaging says nothing.

    With `[project.urls]` written above `dependencies`, the runtime requirements
    become `project.urls.dependencies` and the built wheel asks for nothing. It
    builds, it installs, and it fails on the first import on a machine that does
    not already have them.

    The check is written as the shape a URL table may hold rather than as a list
    of the keys that are in it today: anything under `[project.urls]` is a label
    pointing at an address, so a value that is not one is something that fell in.
    """
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    assert project.get("dependencies"), (
        "this package declares no runtime dependencies, which it has three of — "
        "check whether they have fallen under the table heading below them"
    )

    strays = {
        label: value
        for label, value in project.get("urls", {}).items()
        if not (isinstance(value, str) and value.startswith("https://"))
    }
    assert strays == {}, f"these are under [project.urls] and are not addresses: {strays}"


def test_the_page_a_package_index_shows_says_which_machine_this_runs_on() -> None:
    """A stranger on Linux should learn it from the page, not from a dead window.

    macOS-only is the single fact that decides whether this is usable at all.
    The classifiers are what an index renders before anybody installs, so the
    claim lives there and not only in prose somebody may not scroll to.
    """
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = config["project"]["classifiers"]

    operating_systems = [line for line in classifiers if line.startswith("Operating System ::")]
    assert operating_systems == ["Operating System :: MacOS :: MacOS X"], (
        "the page must claim macOS and nothing else while that is what runs: "
        f"{operating_systems}"
    )

    assert not [line for line in classifiers if line.startswith("License ::")], (
        "`license` is already the SPDX expression; a License classifier beside it "
        "is what current packaging refuses"
    )


def test_the_package_carries_no_personal_address() -> None:
    """The package metadata names no person's address.

    A package index page is public and every mirror copies it, and a version that
    has been published cannot have a field taken back out of it. Issues are the
    channel, and that link is on the page. Adding an address later is always
    possible; removing one is not.
    """
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    addressed = [
        person
        for field in ("authors", "maintainers")
        for person in project.get(field, [])
        if person.get("email")
    ]
    assert addressed == [], f"these would publish an address on every mirror: {addressed}"


def test_the_window_itself_is_named() -> None:
    """The four files the window is made of, asserted by name as well.

    The walk above would pass if `ui/web` were emptied; this says the window
    exists at all, so a refactor that moves these has to say where they went.
    """
    for name in ("index.html", "app.js", "styles.css", "strings.js"):
        path = SOURCE / "ui" / "web" / name
        assert path.is_file(), f"the window has lost {name}"
        assert _is_declared(path.relative_to(SOURCE), _declared()), f"{name} would not ship"
