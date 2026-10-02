"""What the window can ask for when it wants to acquire music."""

import logging
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from diglibrary.application.api import LibraryApi, _inside
from diglibrary.connectors.models import (
    SearchAvailability,
    SearchFile,
    SearchProgress,
    SearchQueueState,
    Transfer,
    TransferState,
)
from diglibrary.connectors.slskd.exceptions import SlskdMalformedResponseError
from diglibrary.providers.models import (
    ProviderHealth,
    ProviderOperationResult,
    ProviderStatus,
)
from diglibrary.providers.soulseek import SoulseekFolder


class FakeProvider:
    """Answer like the Soulseek provider, without a network under it."""

    def __init__(self, status: ProviderStatus = ProviderStatus.READY) -> None:
        """Create a provider that reports one health and remembers what it was asked."""
        self._status = status
        self.requested: list[tuple[str, tuple[str, ...]]] = []
        self.destination = "/somewhere/downloads"
        self.forgotten: list[str] = []
        self.searches: list[str] = []
        self.folder_error: Exception | None = None
        self.health_error: Exception | None = None
        # How many of the files asked for this service will actually take. `None`
        # means all of them, which is the ordinary case.
        self.takes: int | None = None
        self.cleared = 0
        # Whether the service reports this folder's transfers as finished.
        self.settled = False
        # The terminal state every transfer of this folder reached, when a test
        # wants one that is settled without having arrived.
        self.ended: TransferState | None = None
        # A service that calls a transfer `completed` while fewer bytes than it
        # promised actually moved: the file that reaches the disk is cut off
        # in the middle.
        self.short = False
        # What `cancel` was told, so a test can check the remove flag rather
        # than only that something happened.
        self.cancelled: list[tuple[str, str, bool]] = []
        # Held open so a test can watch a search that has not finished, which is
        # the state every tab is in for the first seconds of its life.
        self.release: threading.Event | None = None
        self.folder = SoulseekFolder(
            source="peer",
            directory="@@m\\Album",
            files=(
                SearchFile("@@m\\Album\\01.flac", 100, None, 300, 44100, 16),
                SearchFile("@@m\\Album\\02.flac", 200, None, 240, 44100, 16),
                SearchFile("@@m\\Album\\cover.jpg", 5, None, None),
            ),
            transfer_rate=900,
            queue_depth=0,
            availability=SearchAvailability.AVAILABLE,
            queue_state=SearchQueueState.AVAILABLE,
        )

    def health(self) -> ProviderHealth:
        # A provider that fails while being asked, for the one path the
        # application cannot name: the blanket `except` behind every state.
        if self.health_error is not None:
            raise self.health_error
        return ProviderHealth(self._status, "said so")

    def search(self, request) -> ProviderOperationResult:
        self.searches.append(request.target)
        watch = request.parameters.get("watch")
        if self.release is not None:
            while not self.release.wait(0.01):
                if watch is not None and watch.abandoned():
                    raise RuntimeError("The search was closed before it was read.")
        if watch is not None:
            watch.observed(SearchProgress(sources=1, files=3, finished=True))
        return ProviderOperationResult(
            True, {"folders": (self.folder,), "request_id": f"remote-{request.target}"}, "1 folder"
        )

    def forget(self, request_id: str) -> None:
        self.forgotten.append(request_id)

    def folder_contents(self, source: str, directory: str) -> tuple[SearchFile, ...]:
        if self.folder_error is not None:
            raise self.folder_error
        # What a search never shows: the rest of the album, and the two files
        # that say where the rip came from.
        return (
            *self.folder.files,
            SearchFile(f"{directory}\\cover.jpg", 5, None, None),
            SearchFile(f"{directory}\\rip.log", 2, None, None),
        )

    def download(self, request) -> ProviderOperationResult:
        files = tuple(file.name for file in request.parameters["files"])
        self.requested.append((request.target, files))
        # A transfer per file the service took. Answering `()` while reporting
        # success would be a fixture contradicting itself, and would let a
        # caller drop this number unnoticed. `takes` is how a test says the
        # service accepted fewer files than it was asked for.
        taken = files if self.takes is None else files[: self.takes]
        transfers = tuple(
            Transfer(
                str(index),
                request.target,
                name,
                TransferState.QUEUED,
                0,
                0,
                "2001-02-04T01:00:00",
            )
            for index, name in enumerate(taken, start=1)
        )
        return ProviderOperationResult(
            True, {"transfers": transfers}, f"{len(transfers)} of {len(files)} files"
        )

    def progress(self, source: str | None = None) -> tuple[Transfer, ...]:
        whose = source or "peer"
        moving = TransferState.COMPLETED if self.settled else TransferState.IN_PROGRESS
        # What every transfer of this folder ended as. A test sets it to say
        # that nothing actually arrived, which is not the same as nothing being
        # in flight any more.
        ended = self.ended or TransferState.COMPLETED
        second = ended if self.ended else moving
        transfers = (
            Transfer("1", whose, "@@m\\Album\\01.flac", ended, 100, 100, "2001-02-03T01:00:00"),
            Transfer(
                "2",
                whose,
                "@@m\\Album\\02.flac",
                second,
                200,
                # A completed transfer has moved everything it promised: 50 of
                # 200 under `completed` is a combination slskd does not
                # produce, and a fixture must not state it. Half-arrived is
                # what `in_progress`, `failed` and `cancelled` are for.
                200 if second is TransferState.COMPLETED and not self.short else 50,
                "2001-02-03T01:00:01",
            ),
        )
        if source is not None:
            return transfers
        # Everything means every source, which is what the Transfers screen
        # asks. This one was asked for later, so it belongs above the others.
        return (
            *transfers,
            Transfer(
                "3",
                "other",
                "@@m\\Outro\\03.flac",
                TransferState.QUEUED,
                400,
                0,
                "2001-02-03T02:00:00",
            ),
        )

    def cancel(self, source: str, identifiers, remove: bool = False) -> int:
        self.cancelled.extend((source, identifier, remove) for identifier in identifiers)
        return len(tuple(identifiers))

    def clear_finished(self) -> None:
        self.cleared += 1

    def download_directory(self) -> str | None:
        return self.destination


def test_a_build_without_a_provider_says_so_instead_of_failing() -> None:
    """Acquisition absent is ordinary, and the window has to be able to explain it."""
    api = _api(provider=None)

    assert api.acquisition_state() == {"available": False, "reason": "no_provider"}
    assert not api.acquisition_search("anything")["ok"]
    assert not api.acquisition_download("s1", "f0")["ok"]


def test_an_unreachable_server_is_reported_rather_than_raised() -> None:
    """The slskd server may simply be switched off, which is not a crash."""
    api = _api(provider=FakeProvider(ProviderStatus.OFFLINE))

    state = api.acquisition_state()

    assert state["available"] is False
    assert state["reason"] == "offline"


def test_a_key_problem_is_not_reported_as_a_server_problem() -> None:
    """A missing or rejected key is not reported as an unreachable server.

    A server that is up and has no API key is answering. Collapsing every
    failure into `unreachable` makes the window describe a server that has
    gone away. The provider's own reason is handed over unread, so the screen
    can say which of the two things needs fixing.
    """
    absent = _api(provider=FakeProvider(ProviderStatus.NO_CREDENTIAL)).acquisition_state()
    refused = _api(provider=FakeProvider(ProviderStatus.CREDENTIAL_REJECTED)).acquisition_state()

    assert absent["reason"] == "no_credential"
    assert refused["reason"] == "credential_rejected"
    assert absent["available"] is False and refused["available"] is False


def test_a_provider_that_fails_unexpectedly_is_not_diagnosed_as_offline() -> None:
    """What reaches the blanket `except` is unexpected, and is reported as that.

    Answering `unreachable` would be a claim about the network with nothing
    measured behind it. The failure's own text travels instead, and the
    window's fallback quotes it rather than picking a sentence.
    """
    provider = FakeProvider()
    provider.health_error = SlskdMalformedResponseError("slskd returned invalid JSON.")

    state = _api(provider=provider).acquisition_state()

    assert state["available"] is False
    assert state["reason"] == "error"
    assert "invalid JSON" in str(state["detail"])


def test_a_search_publishes_folders_the_window_can_draw() -> None:
    """One row per folder, with the columns a Soulseek window shows."""
    api = _api()

    opened = api.acquisition_search("query")
    _settle(api, opened["id"])
    folders = api.acquisition_results(opened["id"])["folders"]

    assert opened["id"] == "s1"
    assert len(folders) == 1
    row = folders[0]
    assert (row["source"], row["files"], row["free"]) == ("peer", 3, True)
    assert row["folder"] == "Album"
    assert [track["name"] for track in row["tracks"]] == ["01.flac", "02.flac", "cover.jpg"]
    assert row["tracks"][0]["attributes"] == "16/44.1kHz, 5m0s"
    assert row["tracks"][2]["attributes"] == ""


def test_the_artists_name_in_the_folder_outranks_a_faster_stranger() -> None:
    """A folder named after the artist ranks above a compilation track that matches.

    A rank list rather than a weighted sum, so a fast peer can never outweigh
    the artist's name being in the folder.
    """
    from diglibrary.application.api import _relevance, _terms

    terms = _terms("nilo tarquinio")
    album = SoulseekFolder(
        source="slow",
        directory="@@m\\Music\\Nilo Tarquinio\\Zabumbeia",
        files=tuple(
            SearchFile(
                f"@@m\\Music\\Nilo Tarquinio\\Zabumbeia\\{n}.flac", 100, None, 200, 44100, 16
            )
            for n in range(1, 12)
        ),
        transfer_rate=12,
        queue_depth=0,
        availability=SearchAvailability.AVAILABLE,
        queue_state=SearchQueueState.AVAILABLE,
    )
    stray = SoulseekFolder(
        source="fast",
        directory="@@m\\Collections\\Assorted Dance Floor",
        files=(SearchFile("@@m\\Collections\\Assorted Dance Floor\\07 Nilo.mp3", 100, 320, 200),),
        transfer_rate=5_000_000,
        queue_depth=0,
        availability=SearchAvailability.AVAILABLE,
        queue_state=SearchQueueState.AVAILABLE,
    )

    assert _relevance(album, terms) > _relevance(stray, terms)


def test_a_lossless_copy_outranks_a_lossy_one_of_the_same_album() -> None:
    """Between two copies equally worth having, the audio decides."""
    from diglibrary.application.api import _relevance, _terms

    terms = _terms("nilo tarquinio")

    def copy(extension: str, bitrate: int | None, depth: int | None) -> SoulseekFolder:
        return SoulseekFolder(
            source="peer",
            directory="@@m\\Nilo Tarquinio\\Zabumbeia",
            files=tuple(
                SearchFile(
                    f"@@m\\Nilo Tarquinio\\Zabumbeia\\{n}{extension}",
                    100,
                    bitrate,
                    200,
                    None,
                    depth,
                )
                for n in range(1, 6)
            ),
            transfer_rate=900,
            queue_depth=0,
            availability=SearchAvailability.AVAILABLE,
            queue_state=SearchQueueState.AVAILABLE,
        )

    assert _relevance(copy(".flac", None, 16), terms) > _relevance(copy(".mp3", 320, None), terms)


def test_downloading_a_folder_asks_for_everything_in_it() -> None:
    """The folder is the usual unit, and the cover travels with the audio."""
    api = _api()
    opened = api.acquisition_search("query")
    _settle(api, opened["id"])

    result = api.acquisition_download(opened["id"], "f0")

    assert result["ok"] and result["requested"] == 3
    assert api._acquisition.requested[0][1] == (
        "@@m\\Album\\01.flac",
        "@@m\\Album\\02.flac",
        "@@m\\Album\\cover.jpg",
    )


def test_a_folder_already_asked_for_says_so_on_the_next_search() -> None:
    """The row carries the `requested` flag the window paints its mark from.

    The flag crosses the bridge by name. If the two sides disagree on the
    name, the mark never appears and nothing fails, so the name is asserted
    here.
    """
    api = _api()
    first = api.acquisition_search("query")
    _settle(api, first["id"])

    assert api.acquisition_results(first["id"])["folders"][0]["requested"] is False

    api._store.record_download(
        source="peer",
        directory="@@m\\Album",
        folder="Album",
        files=3,
        size_bytes=305,
        whole_folder=True,
    )
    after = api.acquisition_search("query")
    _settle(api, after["id"])

    assert api.acquisition_results(after["id"])["folders"][0]["requested"] is True


def test_one_named_track_travels_alone() -> None:
    """A single named track is requested alone; nothing else in the folder follows it."""
    api = _api()
    opened = api.acquisition_search("query")
    _settle(api, opened["id"])

    result = api.acquisition_download(opened["id"], "f0", ["02.flac"])

    assert result["requested"] == 1
    assert api._acquisition.requested[0][1] == ("@@m\\Album\\02.flac",)


def test_a_result_that_is_no_longer_on_screen_is_refused_clearly() -> None:
    """A stale identifier must not silently fetch the wrong folder."""
    api = _api()

    result = api.acquisition_download("s1", "f0")

    assert not result["ok"]
    assert "search again" in str(result["error"])


def test_two_searches_hold_their_own_rows_side_by_side() -> None:
    """Each tab keeps its own results, and its own identifiers for them.

    With global identifiers, ``f0`` means whatever the most recent search put
    there, and a download started from the older tab fetches an album from
    the newer one, with nothing on screen showing it.
    """
    api = _api()
    first = api.acquisition_search("zabumbeia")["id"]
    second = api.acquisition_search("nilo tarquinio")["id"]
    _settle(api, first)
    _settle(api, second)

    assert first != second
    assert api.acquisition_results(first)["query"] == "zabumbeia"
    assert api.acquisition_results(second)["query"] == "nilo tarquinio"
    # Both tabs still resolve their own ``f0`` to a folder of their own.
    assert api.acquisition_download(first, "f0")["ok"]
    assert api.acquisition_download(second, "f0")["ok"]


def test_closing_a_finished_search_drops_it_here_and_at_the_service() -> None:
    """A closed search is also deleted at the service, so searches do not accumulate there."""
    api = _api()
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)

    assert api.acquisition_close(opened)["ok"]

    assert api._acquisition.forgotten == ["remote-query"]
    assert api.acquisition_results(opened)["closed"] is True
    assert not api.acquisition_download(opened, "f0")["ok"]


def test_closing_a_running_search_abandons_it_without_reporting_a_failure() -> None:
    """Closing a tab is an intention carried out, not an error to explain."""
    api = _api()
    api._acquisition.release = threading.Event()
    opened = api.acquisition_search("query")["id"]
    while not api._acquisition.searches:
        time.sleep(0.01)

    api.acquisition_close(opened)

    # The provider abandoned it, and no tab is left holding an error nobody
    # suffered — the tab is simply gone.
    for _ in range(100):
        if not _any_thread_alive(api):
            break
        time.sleep(0.02)
    assert api.acquisition_results(opened) == {"searching": False, "closed": True, "folders": []}


def test_a_running_search_shows_what_it_has_heard_so_far() -> None:
    """Several seconds of silence is what the count is there to replace."""
    api = _api()
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)

    running = api.acquisition_results(opened)

    assert (running["sources"], running["files"]) == (1, 3)


def test_reading_a_folder_replaces_the_query_shadow_with_the_folder() -> None:
    """A search answers with what matched the words; the album is the rest of it.

    And the row has to keep its contents afterwards, or a download started from
    it would still ask for the handful of names the query happened to match.
    """
    api = _api()
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)

    read = api.acquisition_folder(opened, "f0")

    assert read["ok"] and read["files"] == 5
    assert [track["name"] for track in read["tracks"]][-2:] == ["cover.jpg", "rip.log"]
    downloaded = api.acquisition_download(opened, "f0")
    assert downloaded["requested"] == 5


def test_a_source_that_has_gone_offline_is_an_outcome_and_not_a_crash() -> None:
    """Peers come and go, and the row keeps what the search already found."""
    api = _api()
    api._acquisition.folder_error = RuntimeError("appears to be offline")
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)

    read = api.acquisition_folder(opened, "f0")

    assert not read["ok"]
    assert read["offline"] is True
    # The row is untouched, so the three files the search did find still download.
    assert api.acquisition_download(opened, "f0")["requested"] == 3


def test_progress_is_grouped_by_the_folder_it_belongs_to() -> None:
    """The window draws a folder with its files under it, downloading included."""
    api = _api()

    folders = api.acquisition_transfers("peer")["folders"]

    assert len(folders) == 1
    assert folders[0]["folder"] == "Album"
    assert (folders[0]["size"], folders[0]["moved"]) == (300, 150)
    assert folders[0]["done"] is False


def test_a_folder_lists_its_tenth_track_tenth_and_not_second() -> None:
    """Sorted as text, `010` slots between `01` and `02`.

    A space orders before a digit, so a text sort draws the tenth track
    second, directly under track one. The names here carry the number in the
    middle, `Artist - Album - NN - Title`, not at the front. The order inside
    a folder is the album's, the album's order lives in those numbers, and
    numbers are compared as numbers.
    """
    api = _api()
    numbers = ("02", "010", "01", "03")
    api._acquisition.progress = lambda source=None: tuple(
        Transfer(
            str(index),
            "peer",
            f"@@m\\Album\\Nilo Tarquinio - Zabumbeia Geral - {number} - Título.flac",
            TransferState.IN_PROGRESS,
            100,
            0,
            "2001-02-03T01:00:00",
        )
        for index, number in enumerate(numbers, start=1)
    )

    folders = api.acquisition_transfers("peer")["folders"]

    drawn = [str(file["name"]) for file in folders[0]["files"]]
    assert drawn == [
        f"Nilo Tarquinio - Zabumbeia Geral - {number} - Título.flac"
        for number in ("01", "02", "03", "010")
    ]


def test_a_download_is_remembered_in_the_history_beside_the_renames() -> None:
    """Two things happen to a folder, and History answers for both.

    It arrived, and then this application renamed it. One list, newest first,
    each row carrying which kind it is so the screen knows what to offer.
    """
    api = _api()
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)

    api.acquisition_download(opened, "f0")

    history = api.state()["history"]
    assert len(history) == 1
    row = history[0]
    assert row["kind"] == "download"
    assert (row["source"], row["files"]) == ("peer", 3)
    assert row["folder"] == "Album"
    assert row["whole_folder"] is True
    assert row["size"] == 305


def test_taking_one_track_is_remembered_as_less_than_the_folder() -> None:
    """ "Everything in this folder" and "this one song" are different events."""
    api = _api()
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)

    api.acquisition_download(opened, "f0", ["02.flac"])

    row = api.state()["history"][0]
    assert row["files"] == 1
    assert row["whole_folder"] is False


def test_a_refused_download_is_not_written_into_the_history() -> None:
    """The history says what happened, and nothing happened."""
    api = _api()

    api.acquisition_download("s1", "f0")

    assert api.state()["history"] == []


def test_opening_a_download_asks_the_service_where_it_puts_them(tmp_path: Path) -> None:
    """A copy of that setting kept here would drift when the service's setting changes."""
    opened_paths: list[str] = []
    api = _api(opener=opened_paths.append)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    api._acquisition.destination = str(landing)
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    identifier = api.state()["history"][0]["download_id"]

    result = api.open_download(identifier)

    assert result["ok"] and result["arrived"] is True
    assert opened_paths == [str(landing / "Album")]


def test_opening_a_download_that_has_not_arrived_opens_where_it_will(tmp_path: Path) -> None:
    """Opening nothing is worse than opening the folder it is coming to."""
    opened_paths: list[str] = []
    api = _api(opener=opened_paths.append)
    landing = tmp_path / "downloads"
    landing.mkdir()
    api._acquisition.destination = str(landing)
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    result = api.open_download(api.state()["history"][0]["download_id"])

    assert result["ok"] and result["arrived"] is False
    assert opened_paths == [str(landing)]


def test_a_finished_download_joins_the_library_by_itself(tmp_path: Path) -> None:
    """A folder this application asked for joins the library without being pointed at."""
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    assert api.state()["downloads"] == 1
    assert [album["folder"] for album in api.state()["albums"]] == ["Album"]
    assert api.state()["history"][0]["collected_at"]


def test_collecting_says_what_joined_so_the_screen_can_hear_it(tmp_path: Path) -> None:
    """The window is told, rather than left to find out on its next refresh.

    Reading the album off the disk happens on a worker thread, so the window
    that asked has already read the library by the time it lands. Without this
    event the album is in the library and nothing on screen knows, so a
    finished download would appear only after a manual refresh.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    announced = [event for event in api.events() if event["type"] == "collected"]
    # And which album, not only how many. A collected download is an arrival
    # like a dropped folder, the window marks what arrives, and a count
    # cannot be marked.
    assert len(announced) == 1
    payload = announced[0]["payload"]
    assert payload["albums"] == 1
    assert payload["folder"] == "Album"
    assert payload["unit_ids"] == sorted(
        api._albums
    ), "the event names albums the window cannot find, so nothing arrives marked"


def test_collecting_says_so_even_when_nothing_had_finished(tmp_path: Path) -> None:
    """A window waiting on this event has to be released either way.

    The event is emitted in a ``finally``: an exception that swallowed it would
    leave the screen waiting for ever.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = False  # still moving, so nothing may be collected
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    announced = [event for event in api.events() if event["type"] == "collected"]
    assert announced == [
        {
            "type": "collected",
            "payload": {"albums": 0, "folder": "", "unit_ids": [], "same_audio": []},
        }
    ]


def test_a_download_that_only_stopped_is_not_read_as_one_that_arrived(tmp_path: Path) -> None:
    """Failed and cancelled are settled states, and neither of them is "here".

    Every transfer of the folder reached a terminal state. Reading that as
    finished would send the scanner at a folder nothing had written.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    api._acquisition.ended = TransferState.CANCELLED
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    assert api.state()["downloads"] == 0
    # And it is still waiting, because a cancelled download may yet be asked
    # for again — burning the record would put it beyond reach for good.
    assert api.state()["history"][0]["collected_at"] is None


def test_a_download_that_held_no_album_stays_waiting(tmp_path: Path) -> None:
    """Nothing read means nothing collected, and the record has to survive it.

    Marking it collected anyway takes it out of the waiting list permanently:
    no later attempt ever looks at it again, so the album can never arrive.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)  # a folder, and no audio in it
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    assert api.state()["downloads"] == 0
    assert api.state()["history"][0]["collected_at"] is None


def test_a_downloaded_album_keeps_its_place_when_organising_renames_it(tmp_path: Path) -> None:
    """The one thing this app does to a download is rename its folder.

    If the download row goes on naming the folder as it landed, the next
    start looks for the album under a name nothing has, and the album leaves
    the library silently. The row follows the rename.
    """
    from diglibrary.library.planner import OperationKind

    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    api.acquisition_collect()
    _collected(api)
    assert api.state()["downloads"] == 1

    # What an apply does: the folder on disk is renamed, and the plan says so.
    organised = landing / "Peer - Album (1999) [MP3]"
    (landing / "Album").rename(organised)

    rename = SimpleNamespace(
        kind=OperationKind.RENAME_FOLDER,
        target_path=landing / "Album",
        after_state={"path": str(organised)},
    )
    api._follow_the_rename(SimpleNamespace(operations=(rename,)))

    # A fresh window over the same database, which is what reopening the app is.
    api._downloaded.clear()
    api._albums.clear()
    api._restore_library()

    assert api.state()["downloads"] == 1
    assert [album["folder"] for album in api.state()["albums"]] == [organised.name]


def test_a_collected_download_carries_the_album_it_became(tmp_path: Path) -> None:
    """So History can show the sleeve the Library shows, for the same album.

    The landing path answers while the folder is where it landed, and an apply
    renames it in place, so it follows that for free. What it does not survive
    is the organised folder being moved by hand, which is why the collect
    also writes down what the album *is*; see
    `test_the_collect_writes_down_what_the_album_is_not_only_where_it_landed`.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    api.acquisition_collect()
    _collected(api)

    entry = next(e for e in api.state()["history"] if e["kind"] == "download")

    assert entry["unit_id"] == api.state()["albums"][0]["unit_id"]
    # And exactly one row per download, not one per identification attempt: an
    # album re-identified twice would otherwise appear in History twice.
    assert len([e for e in api.state()["history"] if e["kind"] == "download"]) == 1


def test_a_download_still_arriving_is_left_alone(tmp_path: Path) -> None:
    """Half an album is not an album, and the service is asked before the disk."""
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = False  # the service says it is still moving
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    assert api.state()["downloads"] == 0


def test_a_scan_does_not_sweep_away_what_arrived_by_download(tmp_path: Path) -> None:
    """Pointing at one folder is not an instruction about another one."""
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    api.acquisition_collect()
    _collected(api)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    api.scan(str(elsewhere))
    for _ in range(100):
        if not api.state()["scanning"]:
            break
        time.sleep(0.02)

    assert api.state()["downloads"] == 1


def test_clearing_downloads_takes_them_off_the_screen_and_leaves_the_disk(tmp_path: Path) -> None:
    """Clearing acknowledges the downloads on screen; it deletes nothing."""
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    track = landing / "Album" / "01.mp3"
    _fake_track(track)
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    api.acquisition_collect()
    _collected(api)

    result = api.acquisition_clear_downloads()

    assert result["ok"] and result["cleared"] == 1
    assert api.state()["downloads"] == 0
    assert api.state()["albums"] == []
    assert track.is_file(), "clearing the screen must never reach the disk"
    # History still answers for it, because it did arrive.
    assert api.state()["history"][0]["kind"] == "download"


def _fake_track(path: Path) -> None:
    """Write something the scanner will read as one audio file."""
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 4096)


def _collected(api: LibraryApi) -> None:
    for _ in range(200):
        job = api._collect_job
        if job is not None and not job.is_alive():
            return
        time.sleep(0.02)


def test_clearing_finished_transfers_is_passed_on() -> None:
    """It clears the list the screen draws, and never a file on disk."""
    api = _api()

    assert api.acquisition_clear()["ok"]

    assert api._acquisition.cleared == 1


def test_transfers_without_a_source_answer_for_every_source() -> None:
    """The Transfers screen cannot name the sources: asking is how it learns them."""
    api = _api()

    folders = api.acquisition_transfers()["folders"]

    assert {folder["source"] for folder in folders} == {"peer", "other"}
    assert {folder["folder"] for folder in folders} == {"Album", "Outro"}


def test_the_organiser_is_pointed_where_the_service_says_it_downloads(tmp_path: Path) -> None:
    """Asking the service beats keeping a copy of a setting it owns."""
    api = _api()
    landing = tmp_path / "downloads"
    landing.mkdir()
    api._acquisition.destination = str(landing)

    assert api.acquisition_organize()["ok"]
    assert api._root == str(landing)


def test_organising_without_a_stated_destination_says_why(tmp_path: Path) -> None:
    """A service that will not say where it downloads cannot be organised blindly."""
    api = _api()
    api._acquisition.destination = None

    result = api.acquisition_organize()

    assert not result["ok"]
    assert "did not say" in str(result["error"])


def _settle(api: LibraryApi, search_id: str) -> None:
    for _ in range(50):
        if not api.acquisition_results(search_id)["searching"]:
            return
        time.sleep(0.02)
    raise AssertionError("the search never finished")


def _any_thread_alive(api: LibraryApi) -> bool:
    return any(
        search.job is not None and search.job.is_alive() for search in api._searches.values()
    )


class _NoCatalogue:
    """A catalogue that is never asked, because these tests never identify anything."""

    def search_releases(self, query: object) -> tuple[()]:
        return ()


def _api(
    provider: object | None = "default",
    tmp_path: Path | None = None,
    opener: object | None = None,
    spotify: object | None = None,
) -> LibraryApi:
    """Build the window's API with only the parts acquisition actually touches."""
    import tempfile

    from diglibrary.application.api import _Pipeline
    from diglibrary.application.contracts import MetadataSources
    from diglibrary.application.identification import IdentificationWorkflow
    from diglibrary.database.connection import Database
    from diglibrary.database.library_store import LibraryStore
    from diglibrary.library.artwork import FilesystemArtworkStore
    from diglibrary.library.audio import MutagenAudioProbe
    from diglibrary.library.executor import ChangeExecutor
    from diglibrary.library.matching import AlbumMatcher
    from diglibrary.library.naming import NamingPolicy
    from diglibrary.library.planner import ChangePlanner
    from diglibrary.library.scanner import LibraryScanner
    from diglibrary.library.tags import MutagenTagStore
    from diglibrary.metadata.service import MetadataService

    root = Path(tmp_path or tempfile.mkdtemp())
    logger = logging.getLogger("test.acquisition")
    database = Database(root / "library.sqlite3", logger)
    database.initialize()
    tag_store = MutagenTagStore()
    artwork_store = FilesystemArtworkStore()

    def pipeline(settings: dict[str, object]) -> _Pipeline:
        planner = ChangePlanner(NamingPolicy(), tag_store, artwork_store)
        return _Pipeline(
            scanner=LibraryScanner(MutagenAudioProbe(), logger),
            workflow=IdentificationWorkflow(
                # Never reached: acquisition asks no catalogue anything, and the
                # one folder these scan is empty.
                metadata=MetadataService(((MetadataSources.DISCOGS, _NoCatalogue()),)),  # type: ignore[arg-type]
                matcher=AlbumMatcher(),
                planner=planner,
                tag_store=tag_store,
                logger=logger,
                threshold=0.9,
            ),
            executor=ChangeExecutor(tag_store, logger, artwork_store, root / "backup"),
            threshold=0.9,
            planner=planner,
            artwork=None,
            tag_store=tag_store,
        )

    return LibraryApi(
        pipeline_factory=pipeline,
        store=LibraryStore(database, logger),
        artwork_store=artwork_store,
        logger=logger,
        cover_cache=root / "covers",
        path_opener=opener,  # type: ignore[arg-type]
        acquisition=FakeProvider() if provider == "default" else provider,
        spotify=spotify,  # type: ignore[arg-type]
    )


def test_a_pasted_spotify_album_link_searches_soulseek_for_its_words() -> None:
    """A pasted Spotify album link is searched for as words.

    What must reach the network is the words the link resolves to, never the
    URL, which Soulseek would search for literally and answer with nothing.
    Written against `acquisition_search` rather than against the pointer: a
    pointer that resolves correctly proves nothing if the search still sends
    the URL.
    """
    api = _api(spotify=_FakeSpotify("Lantern (Deluxe)", "Vera Quillon"))

    opened = api.acquisition_search("https://open.spotify.com/album/0aB1cD2eF3gH4iJ5kL6mN7")
    _settle(api, opened["id"])

    assert opened["ok"] is True
    assert (
        opened["query"] == "Vera Quillon Lantern (Deluxe)"
    ), "the tab is labelled with the search, not with the gesture"
    assert opened["seeded_from"] == "spotify"
    assert api._acquisition.searches == [
        "Vera Quillon Lantern (Deluxe)"
    ], "and the network was asked the words, never the address"


def test_a_pasted_spotify_track_link_searches_for_the_album_it_is_on() -> None:
    """A track link searches for the album the track is on."""
    api = _api(spotify=_FakeSpotify("Lantern", "Vera Quillon"))

    opened = api.acquisition_search("https://open.spotify.com/track/7nM6lK5jI4hG3fE2dC1bA0")
    _settle(api, opened["id"])

    assert api._acquisition.searches == ["Vera Quillon Lantern"]


def test_a_spotify_link_this_build_cannot_read_is_reported_not_searched() -> None:
    """A build with no pointer must not send `open.spotify.com/...` to Soulseek."""
    api = _api(spotify=None)

    opened = api.acquisition_search("https://open.spotify.com/album/0aB1")

    assert opened["ok"] is False
    assert api._acquisition.searches == []


def test_ordinary_words_still_reach_the_network_untouched() -> None:
    """Plain words are searched for exactly as typed."""
    api = _api(spotify=_FakeSpotify("unused", "unused"))

    opened = api.acquisition_search("nilo tarquinio")
    _settle(api, opened["id"])

    assert opened["query"] == "nilo tarquinio"
    assert "seeded_from" not in opened
    assert api._acquisition.searches == ["nilo tarquinio"]


class _FakeSpotify:
    """Answer with fixed words, without going near Spotify."""

    def __init__(self, album: str, artist: str | None) -> None:
        self._album = album
        self._artist = artist

    def words_for(self, link: object) -> object:
        """Return the words this link would have resolved to."""
        from diglibrary.metadata.spotify import PointedWords

        return PointedWords(album=self._album, artist=self._artist, exact=True)


def test_organizing_downloads_says_which_folder_and_why_it_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused request names the folder and is written to the log.

    When `scan` refuses, a toast with no folder in it and nothing in the log
    makes a request that did nothing look like one that worked.
    """
    api = _api()
    # A folder that is really there, because where slskd downloads is now
    # checked before anything else: a path this machine does not have is its own
    # refusal, and not the one being tested here.
    downloads = Path(tempfile.mkdtemp())
    api._acquisition.destination = str(downloads)
    # A collect in flight, which is the state this gesture is most likely to
    # meet: it is pressed about downloads that have just finished.
    api._collect_job = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]

    with caplog.at_level(logging.INFO, logger="test.acquisition"):
        answer = api.acquisition_organize()

    assert answer["ok"] is False
    assert str(downloads) in str(
        answer["error"]
    ), "the sentence has to name the folder that was not read"
    said = [r for r in caplog.records if getattr(r, "operation", "") == "api.acquisition.organize"]
    assert len(said) == 1
    assert said[0].folder == str(downloads)
    assert said[0].started is False, "and says it did not start"


def test_a_download_that_has_not_landed_yet_opens_nothing(tmp_path: Path) -> None:
    """`str(Path(""))` is `"."`, and `Path(".").is_dir()` is True.

    A guard written as `if str(path) and path.is_dir()` assumes an empty string
    stays empty through `Path`. It does not: a download whose landing has not
    been written down, which is every one not yet collected, becomes this
    process's own directory, and `Open folder` on its transfer opens the
    folder the application runs in.
    """
    # The download folder exists and the album's own folder does not, which is
    # what "has not arrived yet" looks like on disk.
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True)
    api = _api(tmp_path=tmp_path, opener=lambda path: opened.append(path))
    api._acquisition.destination = str(downloads)
    opened: list[str] = []
    api._store.record_download("peer", "@@m\\Album", "Album", 2, 300, True)

    # The Transfers context menu's `Open folder`, the only caller of
    # `_recorded_landing`, which is where the empty string arrives.
    answer = api.acquisition_open_transfer("@@m\\Album", "peer")

    assert answer["ok"] is True
    assert Path.cwd() not in [Path(path) for path in opened], (
        "a download with no landing written down opened the folder this "
        "application is running in"
    )
    assert opened == [str(downloads)], (
        "with nothing written down about where it landed, the folder it will "
        "appear in is the honest answer"
    )


def test_a_relative_download_folder_never_reaches_the_platform() -> None:
    """A relative download folder is refused by every reader of that setting.

    slskd answers with the folder it writes to, which may be relative to its
    own working directory. A relative path resolves against this process's
    directory, so opening it would open the wrong folder. Every reader of the
    setting comes through one function, and each of them refuses.
    """
    api = _api(opener=lambda path: None)
    api._acquisition.destination = "downloads"
    recorded = api._store.record_download("peer", "@@m\\Album", "Album", 2, 300, True)

    for answer in (
        api.open_downloads(),
        api.open_download(recorded),
        api.acquisition_organize(),
    ):
        assert answer["ok"] is False
        assert "relative" in str(answer["error"]), answer


def test_collecting_claims_only_what_is_inside_the_folder_it_collected(
    tmp_path: Path,
) -> None:
    """A collect claims an album only if it is inside the collected folder.

    A set difference over a map that other threads write to answers "appeared
    while the collect was working", which is not the question. A collect that
    begins while the library is being restored would claim every album the
    restore put on the shelf, and `Clear downloads` would then remove them.

    Asked as a rule about the album (is it inside the folder that was
    collected?), no concurrency can make the answer wrong.
    """
    landed = tmp_path / "downloads" / "Album"
    (landed / "CD1").mkdir(parents=True)
    mine = tmp_path / "library" / "An album I scanned myself"
    mine.mkdir(parents=True)

    assert _inside(landed / "CD1", landed), "a disc folder belongs to the album above it"
    assert _inside(landed, landed), "and the folder itself is inside itself"
    assert not _inside(mine, landed), (
        "an album that merely appeared while the collect was working is not a " "download"
    )


def test_an_album_that_finished_joins_the_library_without_being_watched() -> None:
    """A finished album joins the library whichever screen is in front.

    If only the Transfers screen asks for the collect, a download that
    finishes while another tab is open stays on disk, uncollected. The watch
    thread asks for it.

    Written against the watcher rather than against `acquisition_collect`,
    because what is being tested is that the collect gets asked for.
    """
    api = _api()
    asked: list[str] = []
    api.acquisition_collect = lambda: asked.append("collect") or {"ok": True}  # type: ignore[assignment]
    # One turn of the loop, then the thread is told to stop.
    threading.Timer(0.05, api._closing.set).start()
    api._closing.wait(0)

    watcher = threading.Thread(target=api.acquisition_watch_sleep, daemon=True)
    original = _sleep_seconds(0.01)
    try:
        watcher.start()
        watcher.join(timeout=5)
    finally:
        _sleep_seconds(original)

    assert not watcher.is_alive(), "the watch did not stop when it was told to"
    assert asked, "the watch thread never asked for the collect"


def _sleep_seconds(value: float) -> float:
    """Set how long the sleep watch waits between turns, returning the old value."""
    from diglibrary.application import api as module

    was = module._SLEEP_WATCH_SECONDS
    module._SLEEP_WATCH_SECONDS = value
    return was


def test_a_collect_waits_for_the_library_to_finish_arriving() -> None:
    """A collect refuses while the library is being restored.

    `acquisition_collect` refuses while a scan is running, because two passes
    would fill one map. The restore fills the same map and is not
    `self._job`, so it is checked separately.
    """
    api = _api()
    api._restore_job = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]

    answer = api.acquisition_collect()

    assert answer["busy"] is True, "a collect must not run on top of the restore"
    assert api._collect_job is None, "and nothing was started"


def test_a_scan_refuses_while_downloads_are_being_brought_in() -> None:
    """The guard between scan and collect holds in both directions.

    `acquisition_collect` refuses while a scan is running: two identify
    passes rebind `self._transcoded`, which a later re-plan reads to decide
    whether a folder is named `[FLAC]` or `[Lossy]`. The reverse is a scan
    requested while downloads are being brought in, and it is refused too.
    """
    api = _api()
    api._collect_job = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]

    assert api.scan("/tmp")["ok"] is False


def test_transfers_are_newest_first() -> None:
    """The thing just asked for is the thing being watched, so it is on top."""
    api = _api()

    folders = api.acquisition_transfers()["folders"]

    assert [folder["folder"] for folder in folders] == ["Outro", "Album"]
    assert folders[0]["at"] > folders[1]["at"]


def test_a_transfer_the_service_did_not_time_goes_last_not_first() -> None:
    """An empty string is not a time, and sorting it as one puts it on top.

    That is the one place it does not belong: a folder nothing is known about
    would push the newest transfer down the screen.
    """
    api = _api()
    api._acquisition.progress = lambda source=None: (
        Transfer("1", "peer", "@@m\\Untimed\\01.flac", TransferState.QUEUED, 100, 0, None),
        Transfer("2", "peer", "@@m\\Timed\\01.flac", TransferState.QUEUED, 100, 0, "2001-01-01"),
    )

    folders = api.acquisition_transfers()["folders"]

    assert [folder["folder"] for folder in folders] == ["Timed", "Untimed"]


def test_a_transfer_carries_what_is_needed_to_act_on_it() -> None:
    """Stopping needs the service's handle; resuming needs the source's own name."""
    api = _api()

    folder = api.acquisition_transfers()["folders"][0]

    track = folder["files"][0]
    assert track["id"]
    assert track["remote"].startswith("@@m\\")
    assert track["name"] == track["remote"].rsplit("\\", 1)[-1]


def test_pausing_keeps_the_row_and_cancelling_takes_it_away() -> None:
    """The same act on this network; what differs is what is left behind.

    Soulseek has no pause. Stopping keeps the partial file either way, so what
    makes one reversible is that the row survives to be resumed from.
    """
    api = _api()

    assert api.acquisition_stop("peer", ["1", "2"], remove=False)["stopped"] == 2
    assert api.acquisition_stop("peer", ["3"], remove=True)["stopped"] == 1

    assert api._acquisition.cancelled == [
        ("peer", "1", False),
        ("peer", "2", False),
        ("peer", "3", True),
    ]


def test_resuming_asks_for_the_same_file_from_the_same_source() -> None:
    """There is nothing else to ask: the service kept the bytes, not the transfer."""
    api = _api()

    result = api.acquisition_resume("peer", ["@@m\\Album\\01.flac", "@@m\\Album\\02.flac"])

    assert result["requested"] == 2
    assert api._acquisition.requested == [("peer", ("@@m\\Album\\01.flac", "@@m\\Album\\02.flac"))]


def test_resuming_nothing_is_refused_rather_than_asked_for() -> None:
    """An empty ask would look like a download starting and start nothing."""
    api = _api()

    assert api.acquisition_resume("peer", [])["ok"] is False
    assert api._acquisition.requested == []


def test_what_a_sleeping_machine_broke_is_asked_for_again() -> None:
    """Transfers that failed while the machine slept are requested again.

    The partial files are still on disk, so asking again carries on from them.
    """
    api = _api()
    api._acquisition.ended = TransferState.FAILED
    api._acquisition.settled = True

    api._resume_what_stopped()

    asked = dict(api._acquisition.requested)
    assert set(asked["peer"]) == {"@@m\\Album\\01.flac", "@@m\\Album\\02.flac"}
    announced = [event for event in api.events() if event["type"] == "resumed"]
    assert announced[-1]["payload"]["files"] == 2


def test_something_cancelled_on_purpose_is_not_asked_for_again() -> None:
    """Resuming it would overrule an explicit cancellation."""
    api = _api()
    api._acquisition.ended = TransferState.CANCELLED
    api._acquisition.settled = True

    api._resume_what_stopped()

    assert api._acquisition.requested == []
    assert [event for event in api.events() if event["type"] == "resumed"] == []


def test_transfers_are_gathered_under_whoever_is_sending_them() -> None:
    """Who a download is coming from is the first thing about it on this network.

    One peer's queue moves as one, a peer going offline takes everything of its
    own with it, and every action worth offering is naturally that peer's.
    """
    api = _api()

    users = api.acquisition_transfers()["users"]

    assert [user["source"] for user in users] == ["other", "peer"]
    assert [len(user["folders"]) for user in users] == [1, 1]
    assert users[0]["at"] > users[1]["at"]
    peer = users[1]
    assert peer["files"] == 2
    assert peer["size"] == 300


def test_a_transfer_carries_the_speed_the_estimate_and_the_queue_place() -> None:
    """Passed on as the service measured them; none of these is computed here.

    This application is not the one moving the bytes, and a second opinion about
    how fast they are moving would only ever be a worse one.
    """
    api = _api()
    api._acquisition.progress = lambda source=None: (
        Transfer(
            "1",
            "peer",
            "@@m\\Album\\01.flac",
            TransferState.IN_PROGRESS,
            100,
            50,
            "2001-02-03T01:00:00",
            average_speed=374_700.0,
            remaining_time="00:07:09",
            place_in_queue=17,
        ),
    )

    track = api.acquisition_transfers()["folders"][0]["files"][0]

    assert track["rate"] == 374_700.0
    assert track["eta"] == "00:07:09"
    assert track["place"] == 17


def test_a_queue_place_the_peer_did_not_state_is_absent_not_zero() -> None:
    """Absent is the ordinary case, and position zero would mean "next"."""
    api = _api()

    track = api.acquisition_transfers()["folders"][0]["files"][0]

    assert track["place"] is None


def test_opening_a_transfer_reveals_where_it_is_landing(tmp_path: Path) -> None:
    """A folder still arriving may not exist yet, and that is still an answer."""
    opened: list[str] = []
    api = _api(tmp_path=tmp_path, opener=opened.append)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    api._acquisition.destination = str(landing)

    arrived = api.acquisition_open_transfer("@@m\\Album")
    pending = api.acquisition_open_transfer("@@m\\NotHereYet")

    assert arrived["arrived"] is True
    assert pending["arrived"] is False
    # The one that has not arrived opens the folder it will appear in, which is
    # what "where is this going" actually asks.
    assert opened == [str(landing / "Album"), str(landing)]


def test_open_folder_on_a_transfer_opens_the_album_and_not_the_folder_above_it(
    tmp_path: Path,
) -> None:
    """Guessing the leaf is right only until the album is organised, and it is.

    Organising renames the folder, which is what this application is for, so the
    guess then misses and the downloads folder opens instead. The recorded
    landing is kept true across that rename, which makes it the only thing
    here that knows where the album is.
    """
    opened: list[str] = []
    api = _api(tmp_path=tmp_path, opener=opened.append)
    landing = tmp_path / "downloads"
    organised = landing / "Peer - Album (1999) [MP3]"
    organised.mkdir(parents=True)
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    # What an apply leaves behind: the download record naming the new folder.
    api._store.mark_download_collected(1, str(organised))

    result = api.acquisition_open_transfer("@@m\\Album", "peer")

    assert result["arrived"] is True
    assert opened == [str(organised)]


def test_the_window_is_told_what_the_service_took_not_what_was_asked() -> None:
    """The answer carries how many files the service took, not only how many were asked.

    A service may accept fewer files than the request named. The provider
    reports the number it took; if this route drops it, the window announces
    a whole folder on its way while one file arrives.
    """
    api = _api()
    api._acquisition.takes = 1
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)
    read = api.acquisition_folder(opened, "f0")
    assert read["files"] == 5, "the whole folder is on screen, which is the premise"

    answer = api.acquisition_download(opened, "f0")

    assert answer["ok"] is True, "the request itself did not fail, which is the trap"
    assert answer["requested"] == 5
    assert answer["queued"] == 1, "what the service actually took has to reach the window"


def test_a_service_that_takes_everything_says_so_plainly() -> None:
    """The ordinary case still reads as complete, or the warning means nothing."""
    api = _api()
    opened = api.acquisition_search("query")["id"]
    _settle(api, opened)
    api.acquisition_folder(opened, "f0")

    answer = api.acquisition_download(opened, "f0")

    assert (answer["requested"], answer["queued"]) == (5, 5)


def test_a_download_that_never_arrived_does_not_say_it_downloaded(tmp_path: Path) -> None:
    """A History row says `Downloaded` only for a download that arrived.

    A request to a peer that went away leaves a row with no folder on disk
    and no transfers at the service. The row's state is derived from the
    disk and the database, so it cannot claim an arrival that did not happen.
    """
    from diglibrary.application.api import _download_arrival

    root = tmp_path / "downloads"
    root.mkdir()
    asked = {"directory": "@@peer\\Music\\Vera Quillon\\Lantern Sessions 001"}

    # Nothing on disk under the download root: nothing arrived.
    assert _download_arrival(asked, root) == "absent"

    # The folder is there but the collect has not run: it is on its way in.
    (root / "Lantern Sessions 001").mkdir()
    assert _download_arrival(asked, root) == "waiting"

    # And the two states the database answers for on its own.
    assert _download_arrival({**asked, "collected_at": "2001-02-03"}, root) == "collected"
    assert _download_arrival({**asked, "cleared_at": "2001-02-04"}, root) == "cleared"


def test_nothing_is_guessed_when_the_service_cannot_say_where_downloads_land(
    tmp_path: Path,
) -> None:
    """A guess dressed as a fact is what this whole answer exists to remove, so
    an unreachable service produces the neutral answer rather than `absent`."""
    from diglibrary.application.api import _download_arrival

    assert _download_arrival({"directory": "@@peer\\X"}, None) == "waiting"


def test_the_collect_writes_down_what_the_album_is_not_only_where_it_landed(
    tmp_path: Path,
) -> None:
    """The whole path, from arriving to the row surviving the folder being moved.

    Not the store on its own: this goes through the collect the watch thread
    calls, so what is asserted is that the anchor is written by the code that
    actually runs when a download finishes — a direct call to the store would
    prove the column works and nothing about whether anyone fills it.

    The move at the end is one this application never performs and never
    records: the organised folder is moved by hand. A History row that knows
    only where the download landed loses its album at that point.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    api.acquisition_collect()
    _collected(api)

    unit_id = api.state()["albums"][0]["unit_id"]
    stored = api._store.unit_by_id(unit_id)
    assert stored is not None
    signature = stored.unit_signature
    recorded = next(r for r in api._store.download_history() if r["collected_at"])
    assert recorded["landed_path"] == str(landing / "Album"), "where it landed, as before"
    assert recorded["unit_signature"] == signature, "and what it turned out to be"

    # The album is organised, then moved elsewhere by hand. Only the first
    # half is something this application did.
    api._store.relocate_unit(str(landing / "Album"), str(tmp_path / "Music" / "Peer - Album"))

    entry = next(e for e in api.state()["history"] if e["kind"] == "download")
    assert entry["unit_id"] == unit_id, "the row still knows the record it brought in"


def test_a_download_missing_files_does_not_join_the_library(tmp_path: Path) -> None:
    """A folder is finished only when every requested file arrived.

    A test of `any(state == "completed")` declares an album finished as soon
    as one file made it, and the collect then brings in a folder with tracks
    missing.

    Here the first file arrives and the second fails.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    # Everything has stopped, and one of the two stopped without arriving.
    api._acquisition.ended = TransferState.FAILED
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    assert api.state()["albums"] == [], "a folder that arrived in part is not an album"
    assert not api.state()["history"][0]["collected_at"], "and it is still waiting, not finished"


def test_a_file_that_stopped_short_of_its_own_size_is_not_arrived(tmp_path: Path) -> None:
    """Completed is not whole, and the byte count is the one that knows.

    A transfer can be reported `completed` for a file cut off mid-write. A
    truncated file is expensive to admit: `content_signature` is a digest
    that includes the sample count, so half a song has a valid signature for
    a recording that does not exist, and every measurement keyed to it is
    orphaned when the rest arrives.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    api._acquisition.short = True
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")

    api.acquisition_collect()
    _collected(api)

    assert api.state()["albums"] == [], "every transfer said completed and one was not whole"
    assert _folder_named("@@m\\Album", api)[
        "stopped_short"
    ], "and the screen is told, so it cannot draw the folder as finished"


def test_a_download_that_will_never_be_whole_can_be_taken_as_it_is(tmp_path: Path) -> None:
    """A peer that went away leaves a folder that will never finish.

    Refusing it for ever is not this application's decision, so the Transfers
    menu offers `Take it as it is` and the collect obeys. What joins is the
    files that arrived.
    """
    api = _api(tmp_path=tmp_path)
    landing = tmp_path / "downloads"
    (landing / "Album").mkdir(parents=True)
    _fake_track(landing / "Album" / "01.mp3")
    api._acquisition.destination = str(landing)
    api._acquisition.settled = True
    api._acquisition.ended = TransferState.FAILED
    search = api.acquisition_search("query")["id"]
    _settle(api, search)
    api.acquisition_download(search, "f0")
    api.acquisition_collect()
    _collected(api)
    assert api.state()["albums"] == []

    # Named, not indexed: the payload is newest first and the other peer's
    # folder was asked for later, so position 0 is not this album.
    directory = _folder_named("@@m\\Album", api)["directory"]
    assert api.accept_incomplete_download(directory)["ok"]
    api.acquisition_collect()
    _collected(api)

    assert [album["folder"] for album in api.state()["albums"]] == ["Album"]
    assert api.state()["history"][0]["collected_at"]


def _folder_named(directory: str, api: LibraryApi) -> dict[str, object]:
    """One folder out of the Transfers payload, by the name its source published."""
    folders = api.acquisition_transfers()["folders"]
    assert isinstance(folders, list)
    return next(folder for folder in folders if folder["directory"] == directory)
