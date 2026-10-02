"""The public pages state how many tests there are. The figure must be true.

`verify.html` prints commands with the answer written beside each, under the
sentence that nothing on it asks to be believed. `index.html` carries the same
figure. One fact stated in two places drifts apart when only one of them is
written again, and neither page fails when that happens.

A number on a page can be read by a test, so this runs the command the page
prints, compares the answer with what is printed beside it, and asserts that the
two pages agree. It counts itself: adding a test makes both pages false until
they are written again, and the failure names the number and the file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

from export_public import PRIVATE  # noqa: E402

VERIFY = PROJECT / "docs" / "verify.html"
INDEX = PROJECT / "docs" / "index.html"
LOSSLESS = PROJECT / "docs" / "lossless.html"


def _stated(text: str, pattern: str) -> int:
    """Read one figure off a page, refusing to guess when it is not there."""
    found = re.search(pattern, text)
    assert found, f"the page no longer states this figure the way this guard reads it: {pattern}"
    return int(found.group(1).replace(",", "").replace(".", ""))


def _tests_this_repository_has() -> int:
    """Ask pytest, the way the page tells a reader to."""
    answer = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=PROJECT,
        check=True,
    )
    found = re.search(r"(\d+) tests? collected", answer.stdout)
    assert found, f"pytest did not say how many it collected:\n{answer.stdout[-2000:]}"
    return int(found.group(1))


def test_the_verify_page_states_the_number_its_own_command_answers() -> None:
    """The page that asks to be checked, checked."""
    page = VERIFY.read_text(encoding="utf-8")

    tests = _tests_this_repository_has()

    assert (
        _stated(page, r"pytest -q\s*#\s*([\d,]+) tests") == tests
    ), f"docs/verify.html must say `# {tests:,} tests, offline`"


def test_the_front_page_states_the_same_number() -> None:
    """Two places stating one fact are two places for it to go stale."""
    page = INDEX.read_text(encoding="utf-8")

    tests = _tests_this_repository_has()

    assert (
        _stated(page, r"<b>([\d,]+)</b><span>tests, offline") == tests
    ), f"docs/index.html must say <b>{tests:,}</b> tests"


def test_every_picture_and_local_link_on_the_public_pages_is_there() -> None:
    """A renamed file shows a reader a broken frame, and nothing raises.

    Only what this repository serves is asked about. An absolute URL is
    somebody else's to keep, and `#install` is a place on the page itself.
    """
    missing: list[str] = []
    for page in (INDEX, VERIFY, LOSSLESS):
        text = page.read_text(encoding="utf-8")
        targets = re.findall(r'(?:src|href)="([^"]+)"', text)
        for target in targets:
            if target.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            if not (page.parent / target.split("#")[0].split("?")[0]).exists():
                missing.append(f"{page.name} → {target}")

    assert missing == [], f"these point at nothing: {missing}"


def test_no_public_page_sends_a_reader_to_something_that_is_not_published() -> None:
    """A link into a path that is left out of the published tree is a dead link.

    The absolute links are the ones the test above cannot follow, so the path
    each of them names inside this repository is compared with what is
    published.
    """
    inside = re.compile(
        r"https://(?:github\.com|raw\.githubusercontent\.com)/cankblunt/diglibrary/"
        r"(?:(?:blob|tree|raw)/)?[^/\"\s)]+/([^\"\s)#]+)"
    )
    dead: list[str] = []
    for page in (INDEX, VERIFY, LOSSLESS, PROJECT / "README.md", PROJECT / "LEIAME.md"):
        for path in inside.findall(page.read_text(encoding="utf-8")):
            if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PRIVATE):
                dead.append(f"{page.name} → {path}")

    assert dead == [], f"these name something that is not published: {dead}"


def test_the_landing_page_shows_the_install_where_it_is_typed() -> None:
    """The recording, when there is one, sits inside the block that prints the commands.

    `install.gif` is checked against the version it shows and against the route
    both front pages give, and none of that reaches the page it is on. This
    asserts the picture is where somebody is about to type, not among the
    screenshots two sections down.
    """
    page = INDEX.read_text(encoding="utf-8")
    if not (PROJECT / "docs" / "images" / "install.gif").exists():
        assert "install.gif" not in page, "the page shows a recording that is not here"
        return

    install = re.search(r'<div class="wrap install" id="install">([\s\S]+?)</div>', page)
    assert install, "the install block is no longer written the way this guard reads it"

    assert "images/install.gif" in install.group(1), (
        "the recording of the install left the block that prints the commands; "
        "it is the one place on this page where somebody is about to type them"
    )
    assert re.search(r'<img src="images/install\.gif" alt="[^"]{40,}"', page), (
        "the recording carries no useful alt text, so the one illustration of the "
        "step somebody has to perform says nothing to a screen reader"
    )
