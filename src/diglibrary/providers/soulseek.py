"""The acquisition provider that turns a Soulseek share into folders and fetches one."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from diglibrary.connectors.contracts import SearchConnector, SearchWatch, TransferConnector
from diglibrary.connectors.models import (
    ConnectorHealth,
    ConnectorHealthState,
    SearchAvailability,
    SearchFile,
    SearchQueueState,
    SearchResponse,
    Transfer,
    TransferFile,
    TransferRequest,
)
from diglibrary.providers.capabilities import ProviderCapabilities
from diglibrary.providers.models import (
    ProviderHealth,
    ProviderMetadata,
    ProviderOperationResult,
    ProviderRequest,
    ProviderStatus,
)
from diglibrary.providers.types import ProviderId

SOULSEEK_PROVIDER_ID = ProviderId("soulseek")
"""The identifier this provider registers under, and the only one it answers to."""

DEFAULT_CAPABILITIES = frozenset(
    {
        ProviderCapabilities.SEARCH_ALBUM,
        ProviderCapabilities.SEARCH_TRACK,
        ProviderCapabilities.DOWNLOAD_ALBUM,
        ProviderCapabilities.DOWNLOAD_TRACK,
        ProviderCapabilities.FLAC,
        ProviderCapabilities.MP3,
    }
)
"""What this provider declares it can do, which is what the manager will let it do."""


@dataclass(frozen=True, slots=True)
class SoulseekFolder:
    """Purpose: present one source's folder as the thing a person chooses.

    Responsibilities: pair a source with one of its folders, the files inside
    it, and the facts a chooser reads across a row — how fast the source is,
    whether it has a slot free, how deep its queue runs. Boundaries: it ranks
    nothing, downloads nothing, and does not decide that a folder is an album.
    Dependencies: connector-neutral search models only. Collaborators:
    ``SoulseekProvider`` and the window that draws the results. Constraints: the
    files keep the names the source published, because those names are what the
    source will answer for.
    """

    source: str
    directory: str
    files: tuple[SearchFile, ...]
    transfer_rate: int | None
    queue_depth: int | None
    availability: SearchAvailability
    queue_state: SearchQueueState

    @property
    def file_count(self) -> int:
        """Return how many files this folder holds."""
        return len(self.files)

    @property
    def total_size_bytes(self) -> int:
        """Return the size of everything in the folder, counting what is known."""
        return sum(file.size_bytes or 0 for file in self.files)

    @property
    def name(self) -> str:
        """Return the folder as a person reads it: the path below the share root.

        The last segment alone is not a folder name: a path such as
        ``Artist - Album (1965)\\A-I\\00_AudioLib`` ends in ``00_AudioLib``,
        which says nothing, while the album's name is two segments above it.
        Soulseek clients show the whole path below the share for this reason.

        The share root itself is dropped. slskd reports it as an opaque token —
        ``@@abcde`` — that identifies nothing to anybody, and a Soulseek client
        does not show it either. A share whose first segment is not one of those
        tokens keeps every segment, because then it is a folder someone named.
        """
        if not self.directory:
            return ""
        parts = self.directory.split("\\")
        if len(parts) > 1 and parts[0].startswith("@@"):
            parts = parts[1:]
        return "/".join(part for part in parts if part)

    def transfer_files(self) -> tuple[TransferFile, ...]:
        """Return everything in this folder, named as the source named it.

        Every file travels, not only the six audio extensions the library reads:
        the cover, the log and the cue sheet are what say where a rip came from,
        and they are in the folder because they belong to it.
        """
        return tuple(TransferFile(file.name, file.size_bytes) for file in self.files)


class SoulseekProvider:
    """Purpose: acquire music through a slskd server the user runs.

    Responsibilities: search a Soulseek network through an injected connector,
    group what answers into the folders a person actually chooses between, and
    ask for one folder's files. Boundaries: it opens no socket, knows no HTTP or
    JSON, never names a concrete connector package, decides nothing about what a
    release is, and writes nothing to the library. Dependencies: the
    ``SearchConnector`` and ``TransferConnector`` contracts and a logger, all
    injected by the composition root. Collaborators: ``ProviderRegistry`` and
    ``ProviderManager``, which own its lifecycle. Constraints: a folder is the
    unit — a Soulseek share is browsed and downloaded by folder — and remote
    names pass through unaltered, because a peer answers for the name it
    published and for no other spelling of it.
    """

    def __init__(
        self,
        search_connector: SearchConnector,
        transfer_connector: TransferConnector,
        logger: logging.Logger,
        *,
        priority: int = 10,
        enabled: bool = True,
        version: str = "1.0.0",
        author: str = "DigLibrary",
    ) -> None:
        """Create the provider from already-assembled collaborators."""
        self._search = search_connector
        self._transfers = transfer_connector
        self._logger = logger
        self._metadata = ProviderMetadata(
            identifier=SOULSEEK_PROVIDER_ID,
            name="Soulseek",
            version=version,
            author=author,
            capabilities=DEFAULT_CAPABILITIES,
            priority=priority,
            enabled=enabled,
        )

    @property
    def metadata(self) -> ProviderMetadata:
        """Return immutable provider metadata configured by the composition root."""
        return self._metadata

    def initialize(self) -> ProviderHealth:
        """Initialize the connectors this provider was given, coordinating no peer.

        What the connector observed while starting up is passed on rather than
        discarded: it is the same observation `health` would make a moment later,
        and without it the framework would record `READY` for a server that
        reported there was no key.
        """
        return self._health_of(self._search.initialize())

    def shutdown(self) -> None:
        """Release what this provider owns, coordinating no peer provider."""
        self._search.shutdown()

    def health(self) -> ProviderHealth:
        """Report whether the slskd server can be used right now, and why not.

        **Every state it is told, and no default that describes one of them.**
        A fallback of `did not answer` for anything that is not ready or
        switched off would report a credential failure — or any state added
        later — as a server that is away. A state this translation does not
        know is reported as unknown with its own name in the message, which
        names no cause that was not observed.
        """
        return self._health_of(self._search.health())

    def _health_of(self, health: ConnectorHealth) -> ProviderHealth:
        """Say one connector observation in the framework's words.

        One translation with two callers — ``health`` and ``initialize`` —
        because a second copy of a mapping is a second answer that can disagree.
        """
        if health.state is ConnectorHealthState.DISABLED:
            return ProviderHealth(ProviderStatus.DISABLED, "The slskd connector is switched off.")
        if health.state is ConnectorHealthState.READY:
            version = f" (slskd {health.service_version})" if health.service_version else ""
            return ProviderHealth(ProviderStatus.READY, f"The slskd server answered{version}.")
        if health.state is ConnectorHealthState.NO_CREDENTIAL:
            return ProviderHealth(
                ProviderStatus.NO_CREDENTIAL,
                "No slskd API key has been given to this application.",
            )
        if health.state is ConnectorHealthState.CREDENTIAL_REJECTED:
            return ProviderHealth(
                ProviderStatus.CREDENTIAL_REJECTED,
                "The slskd server rejected the API key it was given.",
            )
        if health.state is ConnectorHealthState.UNAVAILABLE:
            return ProviderHealth(ProviderStatus.OFFLINE, "The slskd server did not answer.")
        return ProviderHealth(
            ProviderStatus.UNKNOWN, f"The slskd connector reported {health.state.value}."
        )

    def search(self, request: ProviderRequest) -> ProviderOperationResult:
        """Search the network and return what answered, grouped into folders.

        ``target`` is the query, unaltered. ``parameters['kind']`` chooses an
        album- or track-oriented search and defaults to album, because a folder
        is what is usually wanted.
        """
        query = request.target.strip()
        if not query:
            return ProviderOperationResult(False, {}, "A search needs something to look for.")
        kind = str(request.parameters.get("kind", "album")).lower()
        watch = request.parameters.get("watch")
        watching = watch if isinstance(watch, SearchWatch) else None
        response = (
            self._search.search_track(request.target, watching)
            if kind == "track"
            else self._search.search_album(request.target, watching)
        )
        folders = group_into_folders(response)
        self._logger.info(
            "Soulseek search completed.", extra={"operation": "provider.soulseek.search"}
        )
        return ProviderOperationResult(
            True,
            {
                "request_id": response.request_id,
                "folders": folders,
                "source_count": len(response.sources),
                "file_count": response.file_count,
            },
            f"{len(folders)} folders from {len(response.sources)} sources.",
        )

    def download(self, request: ProviderRequest) -> ProviderOperationResult:
        """Ask one source for a set of its files, which is normally a whole folder.

        ``target`` is the source. ``parameters['files']`` are the files to fetch,
        either ``TransferFile`` values or the folder they came from.
        """
        source = request.target.strip()
        if not source:
            return ProviderOperationResult(False, {}, "A download needs the source to ask.")
        files = _requested_files(request.parameters)
        if not files:
            return ProviderOperationResult(False, {}, "A download needs at least one file.")
        transfers = self._transfers.enqueue(TransferRequest(source, files))
        self._logger.info(
            "Soulseek download requested.", extra={"operation": "provider.soulseek.download"}
        )
        return ProviderOperationResult(
            True,
            {"transfers": transfers, "requested": len(files)},
            f"{len(transfers)} of {len(files)} files are on their way from {source}.",
        )

    def progress(self, source: str | None = None) -> tuple[Transfer, ...]:
        """Return where transfers have got to — one source's, or everything.

        Outside the lifecycle vocabulary on purpose: following a download is not
        one of the four operations the manager dispatches, and inventing a
        capability for it would say this provider does something it does not.

        Without a source it asks for the lot, because a screen showing what is
        downloading cannot name the sources in advance: asking is how it finds
        out which they are.
        """
        if source is None:
            return self._transfers.all_transfers()
        return self._transfers.transfers(source)

    def cancel(self, source: str, identifiers: Sequence[str], remove: bool = False) -> int:
        """Stop these transfers, and say how many stopped.

        Outside the lifecycle vocabulary for the reason ``progress`` is: acting
        on a transfer already asked for is not one of the four operations the
        manager dispatches.

        ``remove`` false leaves the transfer in the service's own list, which is
        what makes a stop readable afterwards — and what lets it be told apart
        from one that was never asked for.
        """
        stopped = 0
        for identifier in identifiers:
            self._transfers.cancel(source, identifier, remove)
            stopped += 1
        self._logger.info(
            "Soulseek transfers stopped.",
            extra={"operation": "provider.soulseek.cancel", "count": stopped},
        )
        return stopped

    def forget(self, request_id: str) -> None:
        """Drop one finished search from the service, so a closed tab leaves nothing."""
        self._search.forget(request_id)

    def folder_contents(self, source: str, directory: str) -> tuple[SearchFile, ...]:
        """Return everything in one folder, not only what the words matched.

        A search reports the files that answered the query, and a Soulseek
        folder is nearly always more than that — the rest of the album, the
        cover, the log. What the chooser is choosing is the folder, so the
        folder is what has to be showable.
        """
        return self._search.directory(source, directory)

    def clear_finished(self) -> None:
        """Drop every finished transfer from the service's list.

        A list, not a disk: what finished downloading is where the service put
        it, and clearing the screen never moves or removes a file.
        """
        self._transfers.clear_finished()

    def download_directory(self) -> str | None:
        """Return where a finished download lands, so the organiser can be pointed at it.

        The acquisition service owns that setting and is asked for it; a copy
        kept here would go stale as soon as the folder was changed there.
        """
        return self._transfers.destination()

    def resolve(self, request: ProviderRequest) -> ProviderOperationResult:
        """Report that a Soulseek share has no link to resolve."""
        return ProviderOperationResult(
            False, {}, "Soulseek has no addresses to resolve; search it by name."
        )

    def validate(self, request: ProviderRequest) -> ProviderOperationResult:
        """Report that what a file really is gets decided by measuring it, elsewhere.

        A Soulseek peer publishes a bitrate it was told; this project measures
        the audio instead, and that judgement belongs to the quality
        measurement, not to the provider that fetched the file.
        """
        return ProviderOperationResult(
            False, {}, "A downloaded file is judged by measuring it, not by asking its source."
        )


def group_into_folders(response: SearchResponse) -> tuple[SoulseekFolder, ...]:
    """Group everything one search found into a folder per source.

    A Soulseek share is browsed and downloaded by folder, and this is where a
    flat list of files becomes the rows a person reads: one line per folder,
    holding the files it contains. Files a source offered outside any folder
    are kept under an empty name rather than dropped, because they are still
    files it offered.
    """
    folders: dict[tuple[str, str], list[SearchFile]] = {}
    facts: dict[tuple[str, str], tuple[int | None, int | None, object, object]] = {}
    for source in response.sources:
        for file in source.files:
            key = (source.identifier, file.directory)
            folders.setdefault(key, []).append(file)
            facts[key] = (
                source.transfer_rate,
                source.queue_depth,
                source.availability,
                source.queue_state,
            )
    return tuple(
        SoulseekFolder(
            source=identifier,
            directory=directory,
            files=tuple(files),
            transfer_rate=facts[(identifier, directory)][0],
            queue_depth=facts[(identifier, directory)][1],
            availability=facts[(identifier, directory)][2],  # type: ignore[arg-type]
            queue_state=facts[(identifier, directory)][3],  # type: ignore[arg-type]
        )
        for (identifier, directory), files in folders.items()
    )


def _requested_files(parameters: Mapping[str, object]) -> tuple[TransferFile, ...]:
    """Read the files a caller asked for, accepting a folder as shorthand for its own."""
    folder = parameters.get("folder")
    if isinstance(folder, SoulseekFolder):
        return folder.transfer_files()
    files = parameters.get("files")
    if isinstance(files, SoulseekFolder):
        return files.transfer_files()
    if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
        return tuple(file for file in files if isinstance(file, TransferFile))
    return ()
