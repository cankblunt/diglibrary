"""What a Soulseek share becomes once somebody has to choose from it."""

import logging

import pytest

from diglibrary.connectors.contracts import SearchWatch
from diglibrary.connectors.models import (
    ConnectorHealth,
    ConnectorHealthState,
    SearchAvailability,
    SearchFile,
    SearchKind,
    SearchQueueState,
    SearchRequest,
    SearchResponse,
    SearchSource,
    Transfer,
    TransferFile,
    TransferRequest,
    TransferState,
)
from diglibrary.providers.capabilities import ProviderCapabilities
from diglibrary.providers.manager import ProviderManager
from diglibrary.providers.models import ProviderRequest, ProviderStatus
from diglibrary.providers.registry import ProviderRegistry
from diglibrary.providers.soulseek import (
    SOULSEEK_PROVIDER_ID,
    SoulseekProvider,
    group_into_folders,
)


class FakeConnector:
    """Answer like a connector would, without a service behind it."""

    def __init__(
        self,
        response: SearchResponse | None = None,
        health: ConnectorHealth | None = None,
    ) -> None:
        """Create a fake that returns one prepared answer per operation."""
        self._response = response
        self._health = health or ConnectorHealth(ConnectorHealthState.READY, "0.25.1")
        self.requests: list[TransferRequest] = []
        self.queries: list[tuple[str, SearchKind]] = []
        self.watches: list[SearchWatch | None] = []
        self.forgotten: list[str] = []
        self.everything: tuple[Transfer, ...] = ()
        self.initialized = False

    def initialize(self) -> ConnectorHealth:
        self.initialized = True
        return self._health

    def shutdown(self) -> None:
        self.initialized = False

    def health(self) -> ConnectorHealth:
        return self._health

    def search_album(self, query: str, watch: SearchWatch | None = None) -> SearchResponse:
        self.queries.append((query, SearchKind.ALBUM))
        self.watches.append(watch)
        return self._response or _empty_response()

    def search_track(self, query: str, watch: SearchWatch | None = None) -> SearchResponse:
        self.queries.append((query, SearchKind.TRACK))
        self.watches.append(watch)
        return self._response or _empty_response()

    def forget(self, request_id: str) -> None:
        self.forgotten.append(request_id)

    def enqueue(self, request: TransferRequest) -> tuple[Transfer, ...]:
        self.requests.append(request)
        return tuple(
            Transfer(str(index), request.source, file.name, TransferState.QUEUED, file.size_bytes)
            for index, file in enumerate(request.files)
        )

    def transfers(self, source: str) -> tuple[Transfer, ...]:
        return ()

    def all_transfers(self) -> tuple[Transfer, ...]:
        return self.everything

    def cancel(self, source: str, identifier: str, remove: bool = False) -> None:
        return None

    destination_path: str | None = None

    def destination(self) -> str | None:
        return self.destination_path


def test_a_search_becomes_one_row_per_folder() -> None:
    """The window draws a folder with its files under it, so that is what comes back."""
    connector = FakeConnector(_response_with_two_folders())
    provider = _provider(connector)

    result = provider.search(ProviderRequest("Kettle Parade Harbour Ledger"))

    folders = {folder.directory: folder for folder in result.values["folders"]}
    assert result.successful
    assert set(folders) == {"@@m\\Kettle Parade - Harbour Ledger\\Music", "@@m\\Loose"}
    album = folders["@@m\\Kettle Parade - Harbour Ledger\\Music"]
    assert album.file_count == 3
    # The path below the share, not the last segment: this fixture's last
    # segment is "Music", and the album's name is the one above it. The share
    # root — an opaque `@@m` that identifies nothing to anybody — is dropped.
    assert album.name == "Kettle Parade - Harbour Ledger/Music"
    assert album.total_size_bytes == 34771000 + 27650700 + 255489
    assert album.availability is SearchAvailability.AVAILABLE
    assert album.transfer_rate == 900


def test_two_sources_offering_the_same_folder_stay_two_rows() -> None:
    """The same album from two peers is two choices, and one may be far faster."""
    response = SearchResponse(
        request_id="s",
        request=SearchRequest("q", SearchKind.ALBUM, 100),
        sources=(
            _source("fast", 9000, files=("@@a\\Album\\01.flac",)),
            _source("slow", 12, files=("@@a\\Album\\01.flac",)),
        ),
    )

    folders = group_into_folders(response)

    assert len(folders) == 2
    assert {folder.source for folder in folders} == {"fast", "slow"}
    assert {folder.directory for folder in folders} == {"@@a\\Album"}


def test_a_file_outside_any_folder_is_kept_rather_than_dropped() -> None:
    """It is still a file the source offered, and dropping it would hide it."""
    response = SearchResponse(
        request_id="s",
        request=SearchRequest("q", SearchKind.ALBUM, 100),
        sources=(_source("peer", 10, files=("loose.mp3",)),),
    )

    folders = group_into_folders(response)

    assert folders[0].directory == ""
    assert folders[0].files[0].name == "loose.mp3"


def test_downloading_a_folder_asks_for_everything_in_it_including_the_cover() -> None:
    """A folder is asked for with every file in it, not only what the library can read."""
    connector = FakeConnector(_response_with_two_folders())
    provider = _provider(connector)
    folder = next(
        item
        for item in provider.search(ProviderRequest("q")).values["folders"]
        if item.name.endswith("/Music")
    )

    result = provider.download(ProviderRequest(folder.source, {"folder": folder}))

    asked = connector.requests[0]
    assert asked.source == "peer"
    assert [file.name for file in asked.files] == [
        "@@m\\Kettle Parade - Harbour Ledger\\Music\\01-01. Slow window turns.flac",
        "@@m\\Kettle Parade - Harbour Ledger\\Music\\01-02. Salt on the sill.flac",
        "@@m\\Kettle Parade - Harbour Ledger\\Music\\cover.jpg",
    ]
    assert result.successful
    assert len(result.values["transfers"]) == 3


def test_the_names_a_peer_published_are_passed_back_unaltered() -> None:
    """A peer answers for the name it offered and for no other spelling of it."""
    connector = FakeConnector()
    provider = _provider(connector)

    provider.download(
        ProviderRequest(
            "peer", {"files": (TransferFile("@@x\\Fólder (2001)\\01 — Träck.flac", 5),)}
        )
    )

    assert connector.requests[0].files[0].name == "@@x\\Fólder (2001)\\01 — Träck.flac"


def test_a_track_search_is_asked_for_when_it_is_asked_for() -> None:
    """The default is an album, because a folder is what is usually wanted."""
    connector = FakeConnector()
    provider = _provider(connector)

    provider.search(ProviderRequest("one song", {"kind": "track"}))
    provider.search(ProviderRequest("one album"))

    assert connector.queries == [("one song", SearchKind.TRACK), ("one album", SearchKind.ALBUM)]


def test_an_empty_query_never_reaches_the_network() -> None:
    """Asking a whole network for nothing is a caller's mistake."""
    connector = FakeConnector()

    result = _provider(connector).search(ProviderRequest("   "))

    assert not result.successful
    assert connector.queries == []


def test_a_download_without_files_is_refused() -> None:
    """An empty request is not something to ask a peer about."""
    connector = FakeConnector()

    result = _provider(connector).download(ProviderRequest("peer", {"files": ()}))

    assert not result.successful
    assert connector.requests == []


def test_health_speaks_the_framework_vocabulary() -> None:
    """A switched-off connector is disabled; an unreachable server is offline."""
    ready = _provider(FakeConnector()).health()
    disabled = _provider(FakeConnector(health=ConnectorHealth(ConnectorHealthState.DISABLED)))
    unreachable = _provider(FakeConnector(health=ConnectorHealth(ConnectorHealthState.UNAVAILABLE)))

    assert ready.status is ProviderStatus.READY
    assert "0.25.1" in ready.message
    assert disabled.health().status is ProviderStatus.DISABLED
    assert unreachable.health().status is ProviderStatus.OFFLINE


def test_the_provider_is_what_the_framework_expects_and_the_manager_will_dispatch() -> None:
    """Registration, capability-checked dispatch, and lifecycle all through the manager."""
    connector = FakeConnector(_response_with_two_folders())
    provider = _provider(connector)
    registry = ProviderRegistry()
    registry.register(provider)
    manager = ProviderManager(registry, logging.getLogger("test.soulseek"))

    manager.initialize()
    result = manager.invoke(
        SOULSEEK_PROVIDER_ID,
        ProviderCapabilities.SEARCH_ALBUM,
        "search",
        ProviderRequest("query"),
    )

    assert connector.initialized
    assert result.successful
    assert manager.state(SOULSEEK_PROVIDER_ID).status is ProviderStatus.READY


def test_what_a_file_really_is_gets_decided_by_measuring_it() -> None:
    """A peer publishes a bitrate it was told; this project believes the audio."""
    result = _provider(FakeConnector()).validate(ProviderRequest("anything"))

    assert not result.successful
    assert "measuring" in result.explanation


def _provider(connector: FakeConnector) -> SoulseekProvider:
    return SoulseekProvider(connector, connector, logging.getLogger("test.soulseek"))


def _empty_response() -> SearchResponse:
    return SearchResponse("s", SearchRequest("q", SearchKind.ALBUM, 100), ())


def _source(identifier: str, rate: int, files: tuple[str, ...]) -> SearchSource:
    return SearchSource(
        identifier=identifier,
        transfer_rate=rate,
        queue_depth=0,
        availability=SearchAvailability.AVAILABLE,
        queue_state=SearchQueueState.AVAILABLE,
        files=tuple(SearchFile(name, None, None, None) for name in files),
    )


def _response_with_two_folders() -> SearchResponse:
    album = "@@m\\Kettle Parade - Harbour Ledger\\Music"
    return SearchResponse(
        request_id="search-1",
        request=SearchRequest("q", SearchKind.ALBUM, 100),
        sources=(
            SearchSource(
                identifier="peer",
                transfer_rate=900,
                queue_depth=0,
                availability=SearchAvailability.AVAILABLE,
                queue_state=SearchQueueState.AVAILABLE,
                files=(
                    SearchFile(
                        f"{album}\\01-01. Slow window turns.flac", 34771000, 1004, 309, 44100, 16
                    ),
                    SearchFile(
                        f"{album}\\01-02. Salt on the sill.flac", 27650700, 1004, 247, 44100, 16
                    ),
                    SearchFile(f"{album}\\cover.jpg", 255489, None, None),
                    SearchFile("@@m\\Loose\\01.mp3", 5, 320, 200),
                ),
            ),
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])


def test_one_track_can_be_fetched_without_its_folder() -> None:
    """A single song can be wanted, not the album it sits in.

    The folder is the usual unit, not the only one: a caller that names files
    gets exactly those, and nothing else in the folder travels with them.
    """
    connector = FakeConnector(_response_with_two_folders())
    provider = _provider(connector)
    folder = next(
        item
        for item in provider.search(ProviderRequest("q")).values["folders"]
        if item.name.endswith("/Music")
    )
    one = folder.files[1]

    result = provider.download(
        ProviderRequest(folder.source, {"files": (TransferFile(one.name, one.size_bytes),)})
    )

    assert [file.name for file in connector.requests[0].files] == [one.name]
    assert len(result.values["transfers"]) == 1
    assert folder.file_count == 3


def test_the_organiser_is_pointed_where_the_service_actually_drops_files() -> None:
    """Asking the service beats keeping a copy of a setting it owns."""
    connector = FakeConnector()
    connector.destination_path = "/Users/someone/Documents/Soulseek Downloads"

    assert _provider(connector).download_directory() == connector.destination_path


def test_every_state_the_connector_can_report_gets_its_own_answer() -> None:
    """No state falls through to `did not answer` for want of being named.

    A translation that ends in that default reports a credential failure as a
    server that is away, and does the same to any state added later. Written by
    walking the vocabulary: only unavailability may be reported as offline.
    """
    for state in ConnectorHealthState:
        health = _provider(FakeConnector(health=ConnectorHealth(state))).health()

        assert health.message, f"{state.value} is translated without a word about it"
        if state is ConnectorHealthState.UNAVAILABLE:
            assert health.status is ProviderStatus.OFFLINE
        else:
            assert (
                health.status is not ProviderStatus.OFFLINE
            ), f"{state.value} is reported as a server that did not answer"
        if state is not ConnectorHealthState.READY:
            assert health.status is not ProviderStatus.READY


def test_a_missing_key_and_a_refused_key_are_two_different_answers() -> None:
    """Two states because they are two gestures: give a key, or correct one."""
    absent = _provider(
        FakeConnector(health=ConnectorHealth(ConnectorHealthState.NO_CREDENTIAL))
    ).health()
    refused = _provider(
        FakeConnector(health=ConnectorHealth(ConnectorHealthState.CREDENTIAL_REJECTED))
    ).health()

    assert absent.status is ProviderStatus.NO_CREDENTIAL
    assert refused.status is ProviderStatus.CREDENTIAL_REJECTED
    assert absent.message != refused.message


def test_starting_up_records_what_it_observed_and_not_merely_that_it_did_not_raise() -> None:
    """A provider that cannot be used is not filed as ready to use.

    Recording `READY` for anything whose `initialize` returns without raising
    is correct only while every failure is an exception. A missing key is a
    *state*, so a service that has just answered that there is no key would be
    recorded as ready; this is what an installation with no credentials does.
    """
    keyless = _provider(FakeConnector(health=ConnectorHealth(ConnectorHealthState.NO_CREDENTIAL)))

    observed = keyless.initialize()

    assert observed is not None, "initialization reports nothing, so the framework must guess"
    assert observed.status is ProviderStatus.NO_CREDENTIAL
    # And the same words as `health`, because there is one translation.
    assert observed == keyless.health()
