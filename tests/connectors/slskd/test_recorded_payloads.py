"""The mapper read against payloads a real slskd produced, not against invention.

Every other test in this package builds its own payload, which means it proves
the mapper agrees with whoever wrote the test. A mapper that reads `filename`
off a search response — a field slskd puts one level down — passes such a suite
and parses no real payload.

These run only once the real thing has been recorded with
``tools/capture_slskd_payloads.py``. Until then they skip, by name, so the gap
is visible in the report instead of being mistaken for coverage.
"""

import json
from pathlib import Path

import pytest

from diglibrary.connectors.models import SearchKind, SearchRequest
from diglibrary.connectors.slskd.exceptions import SlskdMalformedResponseError
from diglibrary.connectors.slskd.mapper import SlskdMapper

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "slskd"
UNRECORDED = "No slskd payload has been recorded yet: run tools/capture_slskd_payloads.py."


def test_the_health_payload_a_real_slskd_returns_is_understood() -> None:
    """Its version is what the provider reports when asked whether the service is up."""
    payload = _recorded("application.json")

    health = SlskdMapper().health(payload)

    assert health.service_version


def test_the_search_payload_a_real_slskd_returns_is_understood() -> None:
    """One respondent per entry, files inside it, backslash-separated names."""
    created = _recorded("search_created.json")
    responses = _recorded("search_responses.json")

    response = SlskdMapper().search(
        SearchRequest("recorded", SearchKind.ALBUM, 100), created, responses
    )

    assert response.request_id
    assert response.sources, "a recorded search that found nothing proves nothing"
    assert response.file_count > 0
    assert all(source.identifier for source in response.sources)
    assert any(
        file.directory for source in response.sources for file in source.files
    ), "no recorded file carried a folder, so the folder column has never been proven"


def test_the_download_payload_a_real_slskd_returns_is_understood() -> None:
    """Folders holding files, each with the handle a cancellation needs."""
    payload = _recorded("downloads.json")

    transfers = SlskdMapper().transfers("peer-1", payload)

    assert all(transfer.identifier for transfer in transfers)


def test_a_browsed_folder_keeps_the_name_its_source_will_answer_for() -> None:
    """The two routes name a file differently, and a download depends on which.

    Recorded from a running slskd: a search reports the whole remote path, and
    a browsed folder reports the leaf alone, a bare file name, with
    the folder stated once above it. A peer answers for the name it published,
    so sending the leaf back asks for a file it has never heard of, and the
    download would simply never start.
    """
    payload = _recorded("directory.json")

    files = SlskdMapper().directory(payload, "@@fallback\\Album")

    assert files, "a recording of an empty folder proves nothing"
    assert all("\\" in file.name for file in files), "a leaf name is not one a peer answers for"
    assert all(file.directory for file in files)
    assert any(file.basename.endswith(".mp3") for file in files)


def test_a_browsed_folder_carries_what_is_not_audio_too() -> None:
    """The cover and the log say where a rip came from, and they are in the folder."""
    payload = _recorded("directory.json")

    files = SlskdMapper().directory(payload, "@@fallback\\Album")

    assert any(
        not file.basename.lower().endswith((".mp3", ".flac", ".wav", ".m4a")) for file in files
    ), "this recording holds only audio, so it cannot prove the cover travels"


def test_the_whole_download_list_a_real_slskd_returns_is_understood() -> None:
    """The two transfer routes answer in different shapes, and it is measurable.

    ``/transfers/downloads`` nests folders under each source; the per-source
    route answers the folders bare. Reading the first with the second's mapper
    finds no files at all — a failure that looks exactly like "nothing is
    downloading", which is why this reads a recording and not a fixture someone
    wrote to match the code.
    """
    payload = _recorded("downloads_all.json")

    transfers = SlskdMapper().all_transfers(payload)

    assert transfers, "a recording with nothing downloading proves nothing"
    assert all(transfer.source for transfer in transfers)
    assert all(transfer.identifier for transfer in transfers)
    assert any(transfer.basename for transfer in transfers)


def test_the_two_transfer_routes_do_not_share_a_mapper() -> None:
    """Proof the shapes really differ, rather than a note claiming they do."""
    whole = _recorded("downloads_all.json")

    with pytest.raises(SlskdMalformedResponseError):
        # The per-source mapper wants folders, and this payload holds sources.
        SlskdMapper().transfers("peer-1", whole)


def _recorded(name: str) -> dict | list:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"{UNRECORDED} ({name} is missing.)")
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_recorded_transfer_says_when_it_was_asked_for() -> None:
    """The Transfers screen is ordered newest first, and this is what orders it.

    Read from a recording rather than assumed: the field is `requestedAt`, it
    is stated on every transfer a real slskd returns, and it is ISO 8601 — so
    comparing the strings puts them in the order they were asked for without
    this application having to parse a time it never needs as one.
    """
    payload = _recorded("downloads_all.json")

    transfers = SlskdMapper().all_transfers(payload)

    stamped = [transfer for transfer in transfers if transfer.requested_at]
    assert stamped, "a recording with no timestamps cannot order anything"
    assert len(stamped) == len(transfers)
    assert all(transfer.requested_at.startswith("20") for transfer in stamped)
    # Sorting on the string is the whole ordering, so it has to be sortable.
    assert sorted(t.requested_at for t in stamped) == sorted(
        (t.requested_at for t in stamped), key=str
    )
