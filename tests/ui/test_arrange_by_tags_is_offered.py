"""Which albums the footer offers `Arrange by its own tags`, run rather than read.

The footer is the gesture's only home, so an album the rule leaves out has no
way to be arranged by its tags at all. That is how an album identified with
full confidence came to have none: the rule asked only for `review`, and a
confident identification that was applied and reverted came back `automatic`.

The function is extracted from `app.js` rather than copied, so a change to the
rule is a change to what this measures.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _function() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"^function offersArrangeByTags\(album\) \{\n.*?^\}\n", text, re.M | re.S)
    assert found, "offersArrangeByTags is no longer a top-level function in app.js"
    return found.group(0)


def _offered(albums: list[dict[str, object]]) -> list[bool]:
    harness = f"""
{_function()}
process.stdout.write(JSON.stringify({json.dumps(albums)}.map(offersArrangeByTags)));
"""
    run = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _album(decision: str, **flags: bool) -> dict[str, object]:
    return {"decision": decision, "written": False, "organized": False, **flags}


def test_a_confident_identification_not_yet_written_is_offered_it() -> None:
    """An automatic album whose files were never written, or were reverted."""
    assert _offered([_album("automatic")]) == [True]


def test_an_album_in_review_keeps_it() -> None:
    assert _offered([_album("review"), _album("review", organized=True)]) == [True, True]


def test_nothing_is_offered_once_the_files_are_written() -> None:
    written = [
        _album("automatic", written=True, organized=True),
        _album("automatic", organized=True),
        _album("review", written=True),
    ]
    assert _offered(written) == [False, False, False]


def test_an_album_no_catalogue_named_is_not_offered_it() -> None:
    assert _offered([_album("unidentified")]) == [False]
