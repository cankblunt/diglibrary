"""The Transfers row's own estimate, run rather than read.

A wrong estimate is arithmetic, and every shape in it is valid: no static guard
over `ui/web/` can tell `queue / rate` from `one file / rate`, exactly as none
could tell a comparator from its opposite (`test_shelf_order.py`, which this
follows). So this executes the function.

The numbers describe a folder coming down from one peer that sends one file at
a time, each starting only after the one before it ended, with no overlap
between them. That is why a folder's wait is its whole queue: the longest
estimate among the files *in progress* describes one file of it.

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

# A folder of several files from a peer that sends one at a time. `RATE` is
# the peer's average speed, which is the number slskd states and this window
# divides by.
FOLDER_BYTES = 93_500_000
RATE = 500_000


def _run_eta(node: dict[str, object], files: list[dict[str, object]]) -> str:
    """Ask the window's own `etaOf` what this row says, and read the answer."""
    source = SCRIPT.read_text(encoding="utf-8")
    wanted = ("etaOf", "durationSeconds", "shortDuration", "roughDuration")
    parts = []
    for name in wanted:
        found = re.search(rf"function {name}\([^)]*\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        parts.append(found.group(0))
    harness = "\n".join(
        [
            *parts,
            f"console.log(etaOf({json.dumps(node)}, {json.dumps(files)}));",
        ]
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return answer.stdout.strip()


def test_a_folder_is_done_when_its_queue_is_done_and_not_when_one_file_is() -> None:
    """The whole queue, because the peer sends it one file at a time.

    Nothing of the folder has arrived and the first file is moving. At this
    peer's own speed the folder is a little over three minutes out; the file in
    front of it is nineteen seconds out, and that is not the folder's wait.
    """
    node = {"still_coming": FOLDER_BYTES, "rate": RATE}
    files = [{"state": "in_progress", "eta": "00:00:19", "size": 9_000_000, "moved": 0}]

    said = _run_eta(node, files)

    assert said == "3m07s", "the folder's estimate is its whole queue at the rate it is arriving"


def test_a_folder_says_nothing_rather_than_a_number_it_cannot_stand_on() -> None:
    """No rate is no estimate.

    A transfer slskd has not averaged yet reports zero, and dividing by it gives
    infinity — which formats into a sentence with total confidence in it. The
    silence is deliberate: a number with no rate behind it is invented.
    """
    node = {"still_coming": FOLDER_BYTES, "rate": 0}
    files = [{"state": "queued", "size": 9_000_000, "moved": 0}]

    assert _run_eta(node, files) == "—"


def test_a_folder_with_nothing_left_is_not_given_a_wait() -> None:
    """Everything has arrived, so there is no time to state."""
    node = {"still_coming": 0, "rate": RATE}
    files = [{"state": "completed", "size": 9_000_000, "moved": 9_000_000}]

    assert _run_eta(node, files) == "—"


def test_one_file_keeps_the_estimate_the_service_wrote_for_it() -> None:
    """A file row is the one row slskd's own number is actually about.

    It has no `still_coming` of its own — the field belongs to the folders the
    other side of the bridge builds — and it goes on reading the service's word
    for itself rather than a division of it.
    """
    file = {"state": "in_progress", "eta": "00:09:00", "size": 9_000_000, "moved": 1_000}

    assert _run_eta(file, [file]) == "9m00s"


def test_the_longest_estimate_wins_and_it_is_compared_as_time() -> None:
    """`.sort()` with no comparator sorts strings, and these are `45s`, `9m00s`,
    `1h02m` — which sort to `1h02m`, `45s`, `9m00s`.

    A row with one file an hour out and one nine minutes out would say nine
    minutes. Kept as a test that runs, because the ordering is the part no shape reveals.
    """
    files = [
        {"state": "in_progress", "eta": "00:09:00", "size": 1, "moved": 0},
        {"state": "in_progress", "eta": "01:02:00", "size": 1, "moved": 0},
    ]

    assert _run_eta(files[0], files) == "1h02m"
