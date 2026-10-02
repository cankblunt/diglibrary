"""Identifying an album by what its audio is, when its names say nothing usable."""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from diglibrary.application.contracts import MetadataSourceId, MetadataSources, ReleaseMetadata
from diglibrary.library.audio_key import audio_key
from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.library.models import AlbumUnit
from diglibrary.metadata.acoustid import AcousticMatch, AcoustIdClient
from diglibrary.metadata.service import MetadataService

DEFAULT_SAMPLE_SIZE = 3
"""How many tracks of an album are fingerprinted before it is asked about.

One track usually names many release groups, and three tracks agreeing narrow
that to one or two. Fewer than three leaves the intersection wide; more spends
lookups at three a second to confirm what three already said.
"""

DEFAULT_GROUP_LIMIT = 2
"""How many release groups are browsed for pressings. Each one costs a request."""


class Fingerprinter(Protocol):
    """Produce one file's acoustic fingerprint, or nothing when it cannot."""

    def fingerprint(self, path: object) -> AudioFingerprint | None:
        """Return the fingerprint of the audio at ``path``."""


class FingerprintStore(Protocol):
    """Remember a fingerprint against the audio it was computed from.

    Keyed by content signature rather than by path, which is what makes it
    survive the rename that organizing performs — and by the audio's own key as
    well, because a signature is a declared shape and two different recordings
    can share one.
    """

    def fingerprint_for(
        self, content_signature: str, audio_key: str | None = None
    ) -> AudioFingerprint | None:
        """Return a stored fingerprint, or ``None``."""

    def remember_fingerprint(
        self, content_signature: str, audio_key: str | None, value: AudioFingerprint
    ) -> None:
        """Store a fingerprint against the audio it belongs to."""


@dataclass(frozen=True, slots=True)
class _OneFile:
    """One file put to the service by itself, in the shape the helpers read.

    `_fingerprint` and `_remember_answer` were written against the scanner's
    `AudioFileFacts` and need exactly two of its fields. Naming that here beats
    passing the whole scanned unit for one file.
    """

    path: Path
    content_signature: str


class AcousticIdentification:
    """Purpose: turn an album's audio into the releases it might be.

    Responsibilities: fingerprint a sample of an album's tracks, ask AcoustID
    what they are, find the release group its tracks agree on, and return that
    group's pressings. Boundaries: it decides nothing and writes no metadata —
    what comes back are candidates, scored and chosen exactly like any other.
    Dependencies: an injected fingerprinter, AcoustID client, and metadata
    service. Collaborators: the identification workflow, which asks only when
    an album did not settle. Constraints: every failure is an empty answer,
    because acoustic identification never blocks organizing.
    """

    def __init__(
        self,
        fingerprinter: Fingerprinter,
        acoustid: AcoustIdClient,
        metadata: MetadataService,
        logger: logging.Logger,
        store: FingerprintStore | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        group_limit: int = DEFAULT_GROUP_LIMIT,
        source: MetadataSourceId = MetadataSources.MUSICBRAINZ,
        audio_key_of: Callable[[Path], str | None] = audio_key,
    ) -> None:
        """Create the acoustic path over already-assembled infrastructure."""
        self._fingerprinter = fingerprinter
        self._acoustid = acoustid
        self._metadata = metadata
        self._logger = logger
        self._store = store
        self._sample_size = sample_size
        self._group_limit = group_limit
        self._source = source
        self._audio_key_of = audio_key_of
        self._keys: dict[object, str | None] = {}

    @property
    def is_available(self) -> bool:
        """Return whether the audio can be asked at all, so the window can say why not."""
        return self._acoustid.is_configured

    def releases_for(self, unit: AlbumUnit) -> tuple[ReleaseMetadata, ...]:
        """Return the pressings this album's own audio points at."""
        if not self._acoustid.is_configured or not unit.audio_files:
            return ()
        groups = self._groups_named_by(unit)
        if not groups:
            return ()
        releases: list[ReleaseMetadata] = []
        for group_id in groups:
            releases.extend(self._metadata.releases_in_group(self._source, group_id))
        self._logger.info(
            "The audio named an album.",
            extra={
                "operation": "acoustic.identified",
                "groups": len(groups),
                "releases": len(releases),
            },
        )
        return tuple(releases)

    def _groups_named_by(self, unit: AlbumUnit) -> tuple[str, ...]:
        """Return the release groups this album's sampled tracks agree on.

        Agreement is the whole instrument: a single track can name many release
        groups, and the groups every sampled track shares are few. A group named
        by more of the sample is a better answer than one named by less, and
        only the best are browsed because each browse is a request.
        """
        named: Counter[str] = Counter()
        answered = 0
        for file in self._sample(unit.audio_files):
            fingerprint = self._fingerprint(file)
            if fingerprint is None:
                continue
            matches = self._acoustid.lookup(fingerprint)
            # Written down whichever way it went. An answer that is consumed
            # and dropped leaves nothing a screen could show about what the
            # service said, and costs a second request to learn again.
            self._remember_answer(file, matches[0] if matches else None)
            if not matches:
                continue
            answered += 1
            # One track's own best answer, not every answer it has: a fingerprint
            # that matches two recordings is one recording published twice, and
            # counting both would let a single track outvote its neighbours.
            named.update(dict.fromkeys(matches[0].release_group_ids, 1))
        if not named:
            # Asked and answered nothing, which is a different state from never
            # asked and has to say so. The service not knowing a record is an
            # ordinary outcome — a fingerprint may also be held with no
            # recording behind it — and an application that goes quiet there
            # reads as one that did not try.
            self._logger.info(
                "The audio was asked and the service does not know this album.",
                extra={"operation": "acoustic.unknown", "tracks": answered},
            )
            return ()
        best = max(named.values())
        agreed = [group for group, count in named.items() if count == best]
        if best < 2 and answered > 1:
            # Every sampled track named a different album, which is what a
            # folder of unrelated files looks like. Saying so is more useful
            # than browsing an arbitrary one of them.
            self._logger.info(
                "The audio did not agree with itself about this album.",
                extra={"operation": "acoustic.disagreed", "tracks": answered},
            )
            return ()
        return tuple(sorted(agreed)[: self._group_limit])

    def recognize(self, path: Path, content_signature: str) -> str | None:
        """Put one file to the service on request, and keep what came back.

        The identification path asks about an *album*, and only when its names
        failed, so most files are never asked about. This is the other entry:
        one file, on an explicit request, so the bench can show what the service
        knows about it.

        Returns the recording it named, or ``None`` for every other outcome. The
        answer is written down either way, and *that* is what the screens read —
        a return value cannot tell "asked and unknown" from "could not ask".
        """
        if not self._acoustid.is_configured:
            return None
        probe = _OneFile(path=path, content_signature=content_signature)
        fingerprint = self._fingerprint(probe)
        if fingerprint is None:
            return None
        try:
            matches = self._acoustid.lookup(fingerprint)
        except Exception:
            self._logger.exception(
                "One file could not be put to the service.",
                extra={"operation": "acoustic.lookup.failure"},
            )
            return None
        self._remember_answer(probe, matches[0] if matches else None)
        if matches and matches[0].recording_ids:
            return matches[0].recording_ids[0]
        return None

    def _remember_answer(self, file: object, match: AcousticMatch | None) -> None:
        """Keep what the service answered about this file, including nothing at all.

        A lookup costs a request against a service that allows about three a
        second, so its answer is worth exactly as much as the fingerprint it was
        drawn from — and the fingerprint was the only half being kept.

        Written against the audio, not merely the shape: an answer filed under a
        signature two recordings share is an answer one of them carries
        without it being about that recording.
        """
        signature = getattr(file, "content_signature", "")
        if self._store is None or not signature:
            return
        recording = match.recording_ids[0] if match and match.recording_ids else None
        self._store.remember_lookup(
            signature, self._audio_key(file), recording, match.score if match else None
        )

    def _sample(self, files: Sequence[object]) -> tuple[object, ...]:
        """Return tracks spread across the album rather than its first few.

        A compilation's opening tracks are the ones most likely to appear on
        other compilations; taking the first, a middle and the last asks about
        three different points of the record.
        """
        if len(files) <= self._sample_size:
            return tuple(files)
        last = len(files) - 1
        positions = {
            round(index * last / (self._sample_size - 1)) for index in range(self._sample_size)
        }
        return tuple(files[position] for position in sorted(positions))

    def _fingerprint(self, file: object) -> AudioFingerprint | None:
        """Return this file's fingerprint, from the store when it is known to be this file's.

        The audio is named first because naming it costs far less than
        fingerprinting the file afresh. A stored print is reused only when it
        was drawn from this same audio; a print stored without an audio key is
        recomputed rather than trusted, and claims that row when it proves to
        be the same.
        """
        signature = getattr(file, "content_signature", "")
        key = self._audio_key(file)
        if self._store is not None and signature:
            remembered = self._store.fingerprint_for(signature, key)
            if remembered is not None:
                return remembered
        computed = self._fingerprinter.fingerprint(getattr(file, "path", file))
        if computed is not None and self._store is not None and signature:
            self._store.remember_fingerprint(signature, key, computed)
        return computed

    def _audio_key(self, file: object) -> str | None:
        """Return the identity of this file's audio, or ``None`` when it cannot be read.

        Memoized per file because both halves of one visit ask for it — the
        print is looked up before the service is asked and the answer is written
        after — and reading the same 128 KB twice for one lookup is waste with
        no upside.
        """
        path = getattr(file, "path", file)
        if path in self._keys:
            return self._keys[path]
        key = self._audio_key_of(path) if isinstance(path, Path) else None
        self._keys[path] = key
        return key
