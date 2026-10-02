"""Asking AcoustID what recording a fingerprint is.

What comes back is an identifier, not metadata: a MusicBrainz recording id and
the release groups it belongs to. The written values still come from a value
source — this only says *which* release to look at, which is the whole reason
it can settle an album whose folder and tags say nothing usable.
"""

import logging
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.transport import JsonHttpClient, MetadataRequestError

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
REQUESTS_PER_SECOND = 3.0
"""The documented limit: "Do not make more than 3 requests per second"."""

DEFAULT_KEY_VARIABLE = "DIGLIBRARY_ACOUSTID_KEY"
"""The environment variable that holds the user's own application key.

The service expects one key per application; this project cannot ship one,
because an open-source desktop application cannot keep it confidential. So each
user registers their own, exactly as they do for Discogs, and this project never
holds a copy.
"""

REQUESTED_META = "recordingids releasegroupids"
"""Identifiers only, never text.

A recording id is what pairs a file with a track; a release group id is what
points at the album. Neither is metadata this project would write, so nothing
here can become a value by accident.
"""


@dataclass(frozen=True, slots=True)
class AcousticMatch:
    """Purpose: carry what AcoustID says one fingerprint is.

    Responsibilities: hold the service's confidence and the MusicBrainz
    identifiers it maps to. Boundaries: it names nothing — no title, artist, or
    year travels in it. Dependencies: none. Collaborators: the acoustic
    identification workflow, which turns a recording into a release through a
    value source. Constraints: ``score`` is the service's own, between zero and
    one, and is never rewritten into this project's confidence scale.
    """

    score: float
    recording_ids: tuple[str, ...] = ()
    release_group_ids: tuple[str, ...] = field(default=())


class AcoustIdClient:
    """Purpose: turn a fingerprint into the recordings AcoustID maps it to.

    Responsibilities: build the lookup, spend the request through the shared
    cache, limiter, and retry policy, and read identifiers out of the answer.
    Boundaries: it fingerprints nothing, chooses nothing, and writes nothing —
    ranking candidates is the Decision Engine's and only the caller decides what
    an answer means. Dependencies: an injected JSON client and the credential
    resolver. Collaborators: the identification workflow. Constraints: the key
    is the user's own and is never logged; a service that cannot be reached is
    an empty answer, because acoustic identification never blocks organizing.
    """

    def __init__(
        self,
        http_client: JsonHttpClient,
        credentials: EnvironmentCredentials,
        logger: logging.Logger,
        key_variable: str = DEFAULT_KEY_VARIABLE,
    ) -> None:
        """Create a client over one configured key variable."""
        self._http = http_client
        self._credentials = credentials
        self._logger = logger
        self._key_variable = key_variable

    @property
    def is_configured(self) -> bool:
        """Return whether a key is present, so the window can say why it is off."""
        return self._credentials.optional(self._key_variable) is not None

    def lookup(self, fingerprint: AudioFingerprint) -> tuple[AcousticMatch, ...]:
        """Return what the service maps this fingerprint to, best score first."""
        key = self._credentials.optional(self._key_variable)
        if key is None:
            self._logger.info(
                "Acoustic identification is off: no key is configured.",
                extra={"operation": "acoustid.unconfigured"},
            )
            return ()
        query = urllib.parse.urlencode(
            {
                "client": key,
                "duration": fingerprint.duration_seconds,
                "fingerprint": fingerprint.fingerprint,
                "meta": REQUESTED_META,
            }
        )
        try:
            payload = self._http.get(f"{LOOKUP_URL}?{query}", {"Accept": "application/json"})
        except MetadataRequestError:
            # Named without the URL on purpose: the key rides in the query
            # string, and a log line is the easiest place for it to escape.
            self._logger.warning(
                "AcoustID could not be reached.",
                extra={"operation": "acoustid.failure"},
            )
            return ()
        if payload.get("status") != "ok":
            self._logger.warning(
                "AcoustID refused the lookup.",
                extra={"operation": "acoustid.refused"},
            )
            return ()
        matches = [_match(result) for result in _sequence(payload.get("results"))]
        return tuple(sorted(matches, key=lambda match: match.score, reverse=True))


def _match(result: Mapping[str, object]) -> AcousticMatch:
    recordings = _sequence(result.get("recordings"))
    return AcousticMatch(
        score=float(result.get("score") or 0.0),
        recording_ids=tuple(
            str(recording["id"]) for recording in recordings if recording.get("id")
        ),
        release_group_ids=tuple(
            dict.fromkeys(
                str(group["id"])
                for recording in recordings
                for group in _sequence(recording.get("releasegroups"))
                if group.get("id")
            )
        ),
    )


def _sequence(value: object) -> Sequence[Mapping[str, object]]:
    """Return a list of objects, whatever shape absence arrived in."""
    if not isinstance(value, list):
        return ()
    return [item for item in value if isinstance(item, dict)]
