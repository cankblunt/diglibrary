"""Mapping from slskd JSON payload shapes to immutable connector models."""

from collections.abc import Mapping
from typing import Any

from diglibrary.connectors.models import (
    ConnectorHealth,
    ConnectorHealthState,
    SearchAvailability,
    SearchFile,
    SearchQueueState,
    SearchRequest,
    SearchResponse,
    SearchSource,
    Transfer,
    TransferState,
)
from diglibrary.connectors.slskd.exceptions import SlskdMalformedResponseError


class SlskdMapper:
    """Map documented slskd transport payloads without applying DigLibrary policy.

    Purpose:
        Isolates service JSON shape knowledge from HTTP execution and connector operations.
    Responsibilities:
        Validates required transport fields and constructs immutable connector models.
    Architectural boundaries:
        Does not perform requests, read configuration, know providers, or rank candidates.
    Dependencies:
        Depends only on connector models and connector exceptions.
    Expected collaborators:
        ``SlskdClient`` supplies JSON payloads and ``SlskdConnector`` invokes mapping.
    Constraints:
        Missing required transport fields raise an explicit malformed-response exception.
    """

    def health(self, payload: Mapping[str, Any]) -> ConnectorHealth:
        """Map a successful application payload to a ready health value.

        A running slskd reports its version as an object — ``current``, ``full``,
        ``latest`` and whether an update is waiting — not as a string. A
        mapper that reads only a string never finds the version, so both shapes
        are read.
        """
        return ConnectorHealth(
            state=ConnectorHealthState.READY,
            service_version=_version(payload.get("version")),
        )

    def destination(self, payload: Mapping[str, Any]) -> str | None:
        """Read where slskd puts a finished download, from its own settings."""
        directories = payload.get("directories")
        if isinstance(directories, Mapping):
            downloads = directories.get("downloads")
            if isinstance(downloads, str) and downloads:
                return downloads
        return None

    def search(
        self,
        request: SearchRequest,
        created: Mapping[str, Any],
        responses_payload: object,
    ) -> SearchResponse:
        """Map a created search and its response collection to immutable transport models."""
        search_id = _string(created, "id")
        return SearchResponse(
            request_id=search_id,
            request=request,
            sources=tuple(self._source(item) for item in _response_items(responses_payload)),
        )

    def _source(self, payload: object) -> SearchSource:
        """Map one respondent and the files it offered.

        slskd answers a search with one object per user, and the files live in a
        ``files`` array inside it — the name of a file is never a field of the
        response itself.
        """
        if not isinstance(payload, Mapping):
            raise SlskdMalformedResponseError("slskd search response entry must be an object.")
        return SearchSource(
            identifier=_string(payload, "username"),
            transfer_rate=_optional_int(payload, "uploadSpeed"),
            queue_depth=_optional_int(payload, "queueLength"),
            availability=_availability(payload.get("hasFreeUploadSlot")),
            queue_state=_queue_state(payload.get("queueLength")),
            files=tuple(self._file(item) for item in _files(payload)),
        )

    def _file(self, payload: object) -> SearchFile:
        if not isinstance(payload, Mapping):
            raise SlskdMalformedResponseError("slskd search response file must be an object.")
        return SearchFile(
            name=_first_string(payload, "filename", "fileName"),
            size_bytes=_optional_int(payload, "size"),
            bitrate_kbps=_first_optional_int(payload, "bitRate", "bitrate"),
            duration_seconds=_first_optional_int(payload, "length", "duration"),
            sample_rate_hz=_first_optional_int(payload, "sampleRate", "samplerate"),
            bit_depth=_first_optional_int(payload, "bitDepth", "bitdepth"),
            variable_bitrate=_optional_bool(payload, "isVariableBitRate"),
        )

    def transfers(self, source: str, payload: object) -> tuple[Transfer, ...]:
        """Map one source's download list, which slskd nests under its folders.

        slskd answers with ``directories``, each holding the ``files`` it is
        moving. The folder is already part of every file's name, so the nesting
        is flattened away here and the folder stays readable from the name.
        """
        transfers: list[Transfer] = []
        for directory in _directories(payload):
            for item in _files(directory):
                transfers.append(self._transfer(source, item))
        return tuple(transfers)

    def directory(self, payload: object, directory: str) -> tuple[SearchFile, ...]:
        """Map one browsed folder, restoring the names the peer will answer for.

        **The two routes name a file differently, and this is the trap.** A
        search reports the whole remote path — ``@@share\\Artist\\Album\\01.flac``
        — while a browsed folder reports the leaf alone, ``01.flac``, with the
        folder stated once above it. A download asks for the name the peer
        published, so a leaf sent back as a request is a file the peer has never
        heard of. The folder is put back in front of every name here, which is
        the only place that knows both halves.
        """
        files: list[SearchFile] = []
        for entry in _entries(payload):
            if not isinstance(entry, Mapping):
                raise SlskdMalformedResponseError("slskd directory entry must be an object.")
            folder = entry.get("name")
            base = folder if isinstance(folder, str) and folder else directory
            for item in _files(entry):
                if not isinstance(item, Mapping):
                    raise SlskdMalformedResponseError("slskd directory file must be an object.")
                name = _first_string(item, "filename", "fileName")
                files.append(
                    SearchFile(
                        name=name if "\\" in name else f"{base}\\{name}",
                        size_bytes=_optional_int(item, "size"),
                        bitrate_kbps=_first_optional_int(item, "bitRate", "bitrate"),
                        duration_seconds=_first_optional_int(item, "length", "duration"),
                        sample_rate_hz=_first_optional_int(item, "sampleRate", "samplerate"),
                        bit_depth=_first_optional_int(item, "bitDepth", "bitdepth"),
                        variable_bitrate=_optional_bool(item, "isVariableBitRate"),
                    )
                )
        return tuple(files)

    def all_transfers(self, payload: object) -> tuple[Transfer, ...]:
        """Map the whole download list, which nests folders under each source.

        The two routes answer differently:
        ``/transfers/downloads`` gives an array of ``{username, directories}``,
        while ``/transfers/downloads/{user}`` gives one bare ``{directories}``
        and the caller already knows whose it is. Reading the first with the
        second's mapper finds no files at all, so it has its own.
        """
        transfers: list[Transfer] = []
        for entry in _entries(payload):
            if not isinstance(entry, Mapping):
                raise SlskdMalformedResponseError("slskd transfer entry must be an object.")
            transfers.extend(self.transfers(_string(entry, "username"), entry))
        return tuple(transfers)

    def _transfer(self, source: str, payload: object) -> Transfer:
        if not isinstance(payload, Mapping):
            raise SlskdMalformedResponseError("slskd transfer entry must be an object.")
        return Transfer(
            identifier=_string(payload, "id"),
            source=source,
            name=_first_string(payload, "filename", "fileName"),
            state=_transfer_state(payload.get("state")),
            size_bytes=_optional_int(payload, "size"),
            transferred_bytes=_optional_int(payload, "bytesTransferred"),
            # In a running instance's payloads `requestedAt` is always there,
            # `enqueuedAt` follows it by about a second, and `startedAt` is
            # absent until a peer actually begins sending. Asked-for is the
            # order a person reads this list in, so it is the one taken.
            requested_at=_optional_string(payload, "requestedAt", "enqueuedAt"),
            average_speed=_optional_float(payload, "averageSpeed"),
            # Kept as the service worded it. slskd states a duration here and
            # this application has no use for it as a number — it puts it on a
            # row. Parsing it would be inventing precision to throw away.
            remaining_time=_optional_string(payload, "remainingTime"),
            # Often absent, because a peer states it only while the
            # transfer is actually queued with it. Read where it appears and
            # absent everywhere else, which the screen shows as nothing at all
            # rather than as zero — position zero would mean "next".
            place_in_queue=_optional_int(payload, "placeInQueue"),
        )


def _response_items(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        responses = payload.get("responses", [])
        if isinstance(responses, list):
            return responses
    raise SlskdMalformedResponseError("slskd search responses must be an array.")


def _string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SlskdMalformedResponseError(f"slskd response is missing required field {key!r}.")
    return value


def _files(payload: object) -> list[object]:
    """Return the files one respondent or folder carries, accepting none at all."""
    if not isinstance(payload, Mapping):
        raise SlskdMalformedResponseError("slskd response entry must be an object.")
    value = payload.get("files", [])
    if not isinstance(value, list):
        raise SlskdMalformedResponseError("slskd search response files must be an array.")
    return value


def _first_string(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    raise SlskdMalformedResponseError("slskd search response is missing a file name.")


def _optional_string(payload: Mapping[str, Any], *keys: str) -> str | None:
    """Return the first of these the payload states, or nothing if it states none."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_float(payload: Mapping[str, Any], key: str) -> float | None:
    value = payload.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _optional_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _first_optional_int(payload: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        found = _optional_int(payload, key)
        if found is not None:
            return found
    return None


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _availability(value: object) -> SearchAvailability:
    if value is True:
        return SearchAvailability.AVAILABLE
    if value is False:
        return SearchAvailability.UNAVAILABLE
    return SearchAvailability.UNKNOWN


def _queue_state(value: object) -> SearchQueueState:
    if isinstance(value, int) and not isinstance(value, bool):
        return SearchQueueState.QUEUED if value > 0 else SearchQueueState.AVAILABLE
    return SearchQueueState.UNKNOWN


def _entries(payload: object) -> list[object]:
    """Return the per-source entries of the whole-service download list."""
    if isinstance(payload, list):
        return payload
    raise SlskdMalformedResponseError("slskd download list must be an array of sources.")


def _directories(payload: object) -> list[object]:
    """Return the folders slskd is moving files from for one source."""
    if isinstance(payload, list):
        if any(isinstance(entry, Mapping) and "directories" in entry for entry in payload):
            # The whole-service list, handed to the per-source mapper. Read that
            # way it finds no files at all and says nothing, which is
            # indistinguishable from "nothing is downloading". Use
            # ``all_transfers``.
            raise SlskdMalformedResponseError(
                "This payload lists sources, not folders; its folders sit under each source."
            )
        return payload
    if isinstance(payload, Mapping):
        directories = payload.get("directories", [])
        if isinstance(directories, list):
            return directories
    raise SlskdMalformedResponseError("slskd transfer payload must carry directories.")


_TRANSFER_STATES: tuple[tuple[str, TransferState], ...] = (
    ("Succeeded", TransferState.COMPLETED),
    ("Cancelled", TransferState.CANCELLED),
    ("Aborted", TransferState.CANCELLED),
    ("Errored", TransferState.FAILED),
    ("TimedOut", TransferState.FAILED),
    ("Rejected", TransferState.FAILED),
    ("InProgress", TransferState.IN_PROGRESS),
    ("Queued", TransferState.QUEUED),
    ("Requested", TransferState.QUEUED),
    ("Initializing", TransferState.QUEUED),
)
"""How slskd words a transfer state, most specific first.

Its states are compound — ``Completed, Succeeded`` and ``Completed, Errored``
share a first word and mean opposite things — so the outcome is read before the
stage. A wording this list does not cover becomes ``UNKNOWN`` rather than being
guessed at.
"""


def _transfer_state(value: object) -> TransferState:
    if not isinstance(value, str):
        return TransferState.UNKNOWN
    for wording, state in _TRANSFER_STATES:
        if wording in value:
            return state
    return TransferState.UNKNOWN


def _version(value: object) -> str | None:
    """Read the service version, whichever shape this slskd states it in."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        for key in ("current", "full"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
    return None
