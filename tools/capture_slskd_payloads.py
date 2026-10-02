"""Record what a running slskd actually answers, so the tests can stop guessing.

A mapper written from somebody else's client code can agree with a fixture
written from the same reading and still parse no real payload. This records
real payloads: point it at a running slskd, give it something to search for,
and it writes them under ``tests/fixtures/slskd/``. ``test_recorded_payloads.py``
runs the mapper against them, so a shape that changes is a failing test.

It only reads. It creates a search and never queues a transfer.

Nothing recorded says what was searched, who answered, what they share or
when: before anything is written, every account name, search text, folder,
file name, identifier, size, length and statistic is replaced, and every
timestamp is moved to an invented day with the intervals kept. The keys, the
types, the separators and the file extensions are what the tests read, and
they are left as recorded.

    .venv/bin/python tools/capture_slskd_payloads.py "some album"
    .venv/bin/python tools/capture_slskd_payloads.py --scrub-recorded

The second form asks the network nothing: it passes the files already recorded
through the same replacement, which is how a rule added later reaches them.
"""

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diglibrary.connectors.models import SearchKind, SearchRequest
from diglibrary.connectors.slskd.client import (
    SlskdClient,
    UrllibSlskdTransport,
)
from diglibrary.connectors.slskd.configuration import (
    SlskdAuthenticationMode,
    SlskdConfiguration,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "slskd"


def main(argv: list[str]) -> int:
    """Record one search and the current download list from the configured slskd."""
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "--scrub-recorded":
        return _scrub_recorded()
    query = argv[1]
    base_url = os.environ.get("DIGLIBRARY_SLSKD_URL", "http://127.0.0.1:5030")
    variable = "DIGLIBRARY_SLSKD_API_KEY"
    if not os.environ.get(variable):
        print(f"{variable} is not set.")
        return 2

    configuration = SlskdConfiguration(
        enabled=True,
        base_url=base_url,
        timeout_seconds=30.0,
        retry_limit=2,
        authentication_mode=SlskdAuthenticationMode.API_KEY,
        api_key_environment_variable=variable,
        search_settle_seconds=float(os.environ.get("DIGLIBRARY_SLSKD_SETTLE", "20")),
    )
    logger = logging.getLogger("capture")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = SlskdClient(configuration, UrllibSlskdTransport(), logger, os.environ.get)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    recorded: dict[str, object] = {"application.json": client.health_payload()}

    created, responses = client.search_payloads(SearchRequest(query, SearchKind.ALBUM, 100))
    recorded["search_created.json"] = created
    recorded["search_responses.json"] = responses
    print(f"{_file_count(responses)} files recorded.")

    # A browsed folder names its files differently from a search: the leaf
    # alone, with the folder stated once above it. A download asks for the
    # name the peer published, so both shapes are recorded. Peers go offline,
    # so this tries several.
    for source, folder in _respondents(responses):
        try:
            recorded["directory.json"] = client.directory_payload(source, folder)
        except Exception:
            continue
        break
    else:
        print("No respondent was still online to read a folder from.")

    # The two transfer routes answer in different shapes, and one mapper cannot
    # read both: the whole list nests folders under each source, while a single
    # source's answers bare folders. Both are recorded so both are proven.
    try:
        everything = client.all_downloads_payload()
    except Exception as error:
        print(f"No download list at all ({error}).")
    else:
        recorded["downloads_all.json"] = everything

        # The per-source list is recorded for a source that has transfers, read
        # out of the whole list above. The first peer that answered the search
        # would usually give an empty list, and an empty fixture proves nothing.
        for source in _sources_with_downloads(everything):
            recorded["downloads.json"] = client.downloads_payload(source)
            break
        else:
            print("Nothing is downloading, so the per-source list was left as it was.")

    _write_scrubbed(recorded)
    return 0


def _scrub_recorded() -> int:
    """Pass the files already recorded through the replacement again."""
    recorded = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.json"))
    }
    if not recorded:
        print(f"Nothing is recorded under {FIXTURES}.")
        return 1
    _write_scrubbed(recorded)
    return 0


def _write_scrubbed(recorded: dict[str, object]) -> None:
    """Write every payload through one scrubber, so a name is replaced the same way in all."""
    scrubber = Scrubber(_timestamps(list(recorded.values())))
    for name in sorted(recorded):
        _write(name, scrubber.scrub(recorded[name]))


def _respondents(payload: object) -> list[tuple[str, str]]:
    """Return each respondent paired with a folder it offered."""
    items = payload.get("responses", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    found = []
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("files"):
            continue
        name = entry["files"][0].get("filename", "")
        if isinstance(entry.get("username"), str) and "\\" in name:
            found.append((entry["username"], name.rsplit("\\", 1)[0]))
    return found


def _sources_with_downloads(payload: object) -> list[str]:
    """Return the names slskd is currently moving files from."""
    if not isinstance(payload, list):
        return []
    return [
        entry["username"]
        for entry in payload
        if isinstance(entry, dict) and isinstance(entry.get("username"), str)
    ]


def _write(name: str, payload: object) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")


ACCOUNT_KEYS = frozenset({"username", "peer", "branchRoot", "parent"})
ACCOUNT_LIST_KEYS = frozenset({"children"})
TEXT_KEYS = frozenset({"searchText"})
PATH_KEYS = frozenset({"filename", "directory", "name", "folder"})
IDENTIFIER_KEYS = frozenset({"id"})
TOKEN_KEYS = frozenset({"token"})
# Sizes and lengths together are a fingerprint of one particular rip.
MEASURE_KEYS = frozenset({"size", "bytesTransferred", "length"})
# How much an account shares, which describes the account.
STATISTIC_KEYS = frozenset({"directoryCount", "fileCount", "directories", "files", "uploadCount"})
STATISTICS_UNDER = frozenset({"statistics", "shares"})

TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
INVENTED_START = datetime(2001, 2, 3, 4, 5, 6)
LEADING_NUMBER = re.compile(r"^(\d{1,3})\b")
FOLDER_IMAGES = frozenset({"cover", "folder", "front"})


def _timestamps(node: Any) -> list[datetime]:
    """Every moment a payload states, so the earliest can become the invented start."""
    if isinstance(node, dict):
        return [moment for value in node.values() for moment in _timestamps(value)]
    if isinstance(node, list):
        return [moment for item in node for moment in _timestamps(item)]
    if isinstance(node, str) and (found := TIMESTAMP.match(node)):
        return [datetime.fromisoformat(found.group(1))]
    return []


class Scrubber:
    """Replaces what a payload says about people and records, and keeps its shape.

    One instance serves a whole recording: the same account, folder or size is
    given the same replacement wherever it appears, so the payloads still agree
    with each other the way the recorded ones did.
    """

    def __init__(self, moments: list[datetime]) -> None:
        self._earliest = min(moments, default=INVENTED_START)
        self._replaced: dict[tuple[str, Any], Any] = {}

    def scrub(self, node: Any, key: str = "", under: str = "") -> Any:
        if isinstance(node, dict):
            scrubbed = {name: self.scrub(value, name, key) for name, value in node.items()}
            return _consistent(scrubbed)
        if isinstance(node, list):
            if key in ACCOUNT_LIST_KEYS:
                return [self._account(item) if isinstance(item, str) else item for item in node]
            return [self.scrub(item, key, under) for item in node]
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, str):
            return self._text(node, key)
        if isinstance(node, int):
            if key in STATISTIC_KEYS and under in STATISTICS_UNDER:
                return 0
            if key in TOKEN_KEYS:
                return self._stable("token", node, lambda count: 1000 + count)
            if key in MEASURE_KEYS and node > 0:
                return self._measure(key, node)
        return node

    def _text(self, value: str, key: str) -> str:
        if key in ACCOUNT_KEYS:
            return self._account(value)
        if key in TEXT_KEYS:
            return "invented query"
        if key in IDENTIFIER_KEYS:
            return self._stable(
                "id", value, lambda count: str(uuid.uuid5(uuid.NAMESPACE_URL, f"recorded/{count}"))
            )
        if found := TIMESTAMP.match(value):
            moved = INVENTED_START + (datetime.fromisoformat(found.group(1)) - self._earliest)
            return moved.isoformat() + (found.group(2) or "") + (found.group(3) or "")
        if key in PATH_KEYS:
            return self._path(value)
        return value

    def _account(self, value: str) -> str:
        return self._stable("account", value, lambda count: f"peer-{count}")

    def _path(self, value: str) -> str:
        parts = value.split("\\")
        *folders, leaf = parts
        named = []
        for folder in folders:
            if folder.startswith("@@"):
                named.append(self._stable("share", folder, lambda count: f"@@share{count}"))
            elif folder:
                named.append(self._stable("folder", folder, lambda count: f"Folder {count}"))
            else:
                named.append(folder)
        return "\\".join([*named, self._leaf(leaf, alone=not folders)])

    def _leaf(self, leaf: str, *, alone: bool) -> str:
        stem, dot, extension = leaf.rpartition(".")
        if not dot or len(extension) > 5 or " " in extension:
            # No extension: a folder stated on its own.
            return self._stable("folder", leaf, lambda count: f"Folder {count}") if leaf else leaf
        if stem.casefold() in FOLDER_IMAGES:
            return leaf
        number = LEADING_NUMBER.match(stem)
        if number:
            return f"{number.group(1)} - Track {number.group(1)}.{extension}"
        return self._stable("file", leaf, lambda count: f"File {count}.{extension}")

    def _measure(self, key: str, value: int) -> int:
        if key == "length":
            return self._stable("length", value, lambda count: 180 + count)
        return self._stable("size", value, lambda count: 4_000_000 + 4096 * count)

    def _stable(self, kind: str, value: Any, invent: Any) -> Any:
        known = self._replaced.get((kind, value))
        if known is None:
            count = sum(1 for seen, _ in self._replaced if seen == kind) + 1
            known = self._replaced[(kind, value)] = invent(count)
        return known


def _consistent(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep a transfer's own arithmetic true after its sizes were replaced."""
    size, moved = entry.get("size"), entry.get("bytesTransferred")
    if isinstance(size, int) and isinstance(moved, int) and "bytesRemaining" in entry:
        moved = min(moved, size)
        entry["bytesTransferred"] = moved
        entry["bytesRemaining"] = size - moved
        if "percentComplete" in entry and size:
            whole = isinstance(entry["percentComplete"], int)
            percent = 100 * moved / size
            entry["percentComplete"] = round(percent) if whole else percent
    return entry


def _file_count(payload: object) -> int:
    if isinstance(payload, dict):
        payload = payload.get("responses", [])
    if not isinstance(payload, list):
        return 0
    return sum(len(item.get("files", [])) for item in payload if isinstance(item, dict))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
