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
from diglibrary.metadata.acoustid import AcousticMatch, AcoustIdClient, AcoustIdUnansweredError
from diglibrary.metadata.service import MetadataService

DEFAULT_SAMPLE_SIZE = 3
"""How many tracks of an album are fingerprinted before it is asked about.

One track usually names many release groups, and three tracks agreeing narrow
that to one or two. Fewer than three leaves the intersection wide; more spends
lookups at three a second to confirm what three already said.
"""

DEFAULT_GROUP_LIMIT = 2
"""How many release groups are browsed for pressings. Each one costs a request."""

UNFINGERPRINTED = "unfingerprinted"
"""No sampled file produced a fingerprint, so the service was never asked."""

UNREACHABLE = "unreachable"
"""A fingerprint was produced and the service gave no answer to any lookup."""

FAILED = "failed"
"""The question could not be put, and nothing observed says which half failed."""

NAMED_RECORDINGS_READ = 3
"""How many of a fingerprint's recordings are looked up to name it. Each is a request."""

FOREIGN = "foreign"
"""The file's recording is on none of the releases its neighbours named."""

SAME_ALBUM = "same"
"""The file's recording is on a release most of its neighbours share."""

UNRECOGNISED = "unrecognised"
"""The service was asked about the file and holds no recording for it."""

NO_ALBUM_TO_COMPARE = "no_album"
"""The file was recognised and its neighbours do not settle what the album is."""


def belonging(own: Sequence[str] | None, neighbours: Sequence[Sequence[str] | None]) -> str | None:
    """Return whether one file's recording belongs with its neighbours' album.

    ``own`` and each neighbour are the release groups a lookup named, ``None``
    where no lookup was recorded. The answer is ``None`` while the file itself
    was not asked about — nothing has been heard, so nothing is said.

    The album is the groups more than half of the recognised neighbours share,
    and at least two of them must be recognised: one neighbour names many
    releases and proves no album. ``FOREIGN`` is kept for a recording that
    shares no group with *any* neighbour, so a file between the two — off the
    album's groups and on some neighbour's — is ``NO_ALBUM_TO_COMPARE`` rather than
    accused.
    """
    if own is None:
        return None
    if not own:
        return UNRECOGNISED
    recognised = [set(groups) for groups in neighbours if groups]
    if len(recognised) < 2:
        return NO_ALBUM_TO_COMPARE
    named = Counter(group for groups in recognised for group in groups)
    album = {group for group, count in named.items() if count * 2 > len(recognised)}
    if not album:
        return NO_ALBUM_TO_COMPARE
    if album & set(own):
        return SAME_ALBUM
    return NO_ALBUM_TO_COMPARE if set(named) & set(own) else FOREIGN


class AudioNotAskedError(Exception):
    """An album's audio could not be put to the service at all.

    Distinct from an empty answer, which says the service was asked and does
    not hold the album. ``state`` is which half failed — ``UNFINGERPRINTED`` or
    ``UNREACHABLE`` — so a screen can name the one that was observed, and
    ``FAILED`` when neither was, so it names none.
    """

    def __init__(self, state: str) -> None:
        """Carry which half of the question could not be asked."""
        super().__init__(state)
        self.state = state


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
    an album did not settle. Constraints: an empty answer means the service was
    asked and does not hold the album; an album whose audio could not be asked
    at all raises ``AudioNotAskedError``, which the workflow reads as a state and
    never as a reason to stop, because acoustic identification never blocks
    organizing.
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
        """Return the pressings this album's own audio points at.

        Raises ``AudioNotAskedError`` when no sampled file could be put to the
        service, so that outcome is not read as the service not knowing.
        """
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
        # Lookups the service completed, with or without a match. Zero of them
        # means nothing was asked, which is not the service knowing nothing.
        asked = 0
        unreached = 0
        for file in self._sample(unit.audio_files):
            fingerprint = self._fingerprint(file)
            if fingerprint is None:
                continue
            try:
                matches = self._acoustid.lookup(fingerprint)
            except AcoustIdUnansweredError:
                # Not written down: the store's row would say this audio was
                # asked about and is unknown, and it was never answered.
                unreached += 1
                continue
            asked += 1
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
        if not asked:
            state = UNREACHABLE if unreached else UNFINGERPRINTED
            self._logger.warning(
                "The audio of this album could not be put to the service.",
                extra={"operation": "acoustic.not_asked", "state": state},
            )
            raise AudioNotAskedError(state)
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

        Returns the recording it named, or ``None`` when the service answered
        and holds none; that answer is written down either way. A file that
        could not be asked about raises ``AudioNotAskedError`` and nothing is
        written, because the store's row would read as asked and unknown.
        """
        if not self._acoustid.is_configured:
            return None
        probe = _OneFile(path=path, content_signature=content_signature)
        fingerprint = self._fingerprint(probe)
        if fingerprint is None:
            raise AudioNotAskedError(UNFINGERPRINTED)
        try:
            matches = self._acoustid.lookup(fingerprint)
        except AcoustIdUnansweredError:
            raise AudioNotAskedError(UNREACHABLE) from None
        except Exception:
            self._logger.exception(
                "One file could not be put to the service.",
                extra={"operation": "acoustic.lookup.failure"},
            )
            raise AudioNotAskedError(FAILED) from None
        self._remember_answer(probe, matches[0] if matches else None)
        if matches and matches[0].recording_ids:
            return matches[0].recording_ids[0]
        return None

    def hear_every_file(self, files: Sequence[object]) -> int:
        """Put every one of these files to the service, and return how many were answered.

        The identification path samples three tracks to find the album. This
        asks about each file, which is what comparing one of them with its
        neighbours takes, and it is made on request for one album: each file is
        a request. Raises ``AudioNotAskedError`` when not one could be asked.
        """
        asked = 0
        state = ""
        for file in files:
            try:
                self.recognize(getattr(file, "path", file), getattr(file, "content_signature", ""))
            except AudioNotAskedError as not_asked:
                state = state or not_asked.state
                continue
            asked += 1
        if not asked:
            raise AudioNotAskedError(state or UNFINGERPRINTED)
        return asked

    def name_of(
        self, recording_ids: Sequence[str], duration_ms: int | None
    ) -> tuple[str, str] | None:
        """Return the title and credit of the recording a file was named as.

        A fingerprint is linked to several recordings and not every link is
        right: a remix twice the length can sit first. So each is looked up, up
        to ``NAMED_RECORDINGS_READ``, and the one whose published length is
        nearest the file's own is the one named. Each lookup is a request.
        """
        best: tuple[float, str, str] | None = None
        for recording_id in list(recording_ids)[:NAMED_RECORDINGS_READ]:
            try:
                recording = self._metadata.recording(self._source, recording_id)
            except Exception:
                self._logger.exception(
                    "A recording could not be looked up by its identifier.",
                    extra={"operation": "acoustic.recording.failure"},
                )
                continue
            if recording is None:
                continue
            apart = (
                abs(recording.duration_ms - duration_ms)
                if recording.duration_ms and duration_ms
                else float("inf")
            )
            credit = recording.artist_credit or ", ".join(
                artist.name for artist in recording.artists
            )
            if best is None or apart < best[0]:
                best = (apart, recording.title, credit)
        return (best[1], best[2]) if best else None

    def remember_name(self, file: object, title: str, artist: str) -> None:
        """Keep what the catalogue calls this file's recording, beside its answer."""
        signature = getattr(file, "content_signature", "")
        if self._store is not None and signature:
            self._store.remember_recording_name(signature, self._audio_key(file), title, artist)

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
            signature,
            self._audio_key(file),
            recording,
            match.score if match else None,
            release_group_ids=match.release_group_ids if match else (),
            recording_ids=match.recording_ids if match else (),
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
