"""The iTunes Search API as a verification witness, and nothing more.

A streaming catalogue may *corroborate* an identification — track count,
durations, year, title — and its answer raises or lowers confidence, but no
value it returns is ever written, cached, or stored. ``verify`` returns
comparison verdicts, never data: the dictionaries it produces contain booleans
and counts computed here, which are this project's own observations. The
comparison result is kept, not the data compared.

``recognised`` is the one thing here that hands back what the catalogue
published, and it exists so that it can be **read** on screen. Reading is
neither writing nor persisting: what it returns is fetched at the moment the
fold is opened, drawn beside the local files, and dropped when the dialog
closes. Nothing of it reaches a tag, a file name, `metadata_releases`, or any
cache — this module's transport has none — and no caller may offer a control
that adopts it. Textual fields only: no artwork and no preview is ever
requested, which keeps this outside the service's terms on promotional content.

The documented limit is roughly twenty calls a minute, enforced here, which is
why verification is a per-album act and never a bulk sweep.
"""

import json
import locale
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from diglibrary.application.contracts import ReleaseMetadata
from diglibrary.metadata.normalization import fold_accents
from diglibrary.metadata.rate_limit import RateLimiter

_SEARCH_URL = "https://itunes.apple.com/search"
_LOOKUP_URL = "https://itunes.apple.com/lookup"
_REQUESTS_PER_SECOND = 20 / 60
_DEFAULT_STORE = "US"
"""The store this service answers from when nobody says which.

Each store is a different catalogue, not a different language: a record one
country's store holds may be absent from another's. A witness asked in the wrong
store answers that it does not know a record it does hold, which is a false
negative, so the default is used only when the platform names no country.
"""
_DURATION_TOLERANCE_MS = 6_000
_TITLE_FLOOR = 0.72
"""How similar an album title must be before a result counts as the album.

Deliberately forgiving: iTunes decorates titles with "(Deluxe Version)" and
similar, and a witness that cannot find the album is silence, not testimony.
"""

Transport = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class WitnessTrack:
    """One track as the witness publishes it, carried to the screen and dropped."""

    position: int
    name: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class WitnessRecord:
    """The record a witness recognised, for reading beside the local files.

    Purpose: show what an independent catalogue holds for an album the
    licensed sources failed on. Boundaries: nothing here is written, cached or
    stored, and nothing built from it may adopt it — it is read, and whatever
    is wanted from it is typed by hand. Constraints: textual fields only,
    never artwork and never a preview.
    """

    title: str
    artist: str
    year: int | None
    track_count: int | None
    url: str | None
    tracks: tuple[WitnessTrack, ...]


def _urllib_transport(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "DigLibrary/0.1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


class WitnessThrottledError(RuntimeError):
    """The service refused because it has been asked too much, not too little.

    Its own class because the two failures need different answers: a witness
    that cannot be reached is worth trying again next time, and a witness that
    is throttling must be *left alone* or the block extends itself: the service
    answers `429`, and then `403` for a while afterwards even to a slow,
    well-behaved caller.
    """


def store_country(reader: Callable[[], str | None] | None = None) -> str:
    """Which iTunes store to ask — the one for the country the system is set to.

    Asked of the platform rather than of the environment. An application
    launched from the Finder inherits none of the shell's variables, so `LANG`
    may be unset and `locale.getlocale()` may answer `('C', 'UTF-8')`, which is
    no country at all, while the platform's own locale names one. So macOS is
    asked first, the standard library second, and `US` is the last resort.
    """
    if reader is not None:
        return (reader() or _DEFAULT_STORE).upper()
    try:
        import Foundation

        country = Foundation.NSLocale.currentLocale().objectForKey_(Foundation.NSLocaleCountryCode)
        if country:
            return str(country).upper()
    except Exception:
        pass
    tag = locale.getlocale()[0] or ""
    _, _, region = tag.partition("_")
    region = region.split(".")[0]
    return region.upper() if len(region) == 2 else _DEFAULT_STORE


class ITunesVerifier:
    """Purpose: ask one independent catalogue whether an identification holds.

    Responsibilities: find the album on iTunes, compare its track count,
    durations, and year against the identified release and the local audio,
    and report agreement or silence. Boundaries: nothing it reads is returned,
    stored, or written — only verdicts leave this class.
    Dependencies: an injected transport and the shared rate limiter.
    Collaborators: the identification workflow, for an album that did not
    settle on its own. Constraints: every failure is silence rather than an
    error, because a witness that cannot be reached must not block a library.
    """

    def __init__(
        self,
        logger: logging.Logger,
        transport: Transport = _urllib_transport,
        limiter: RateLimiter | None = None,
        country: str | None = None,
    ) -> None:
        """Create a verifier over an injected transport, rate-limited by default.

        ``country`` is the store to ask, and it defaults to the system's own
        country rather than to the service's default of `US` — see
        ``store_country``.
        """
        self._logger = logger
        self._transport = transport
        self._limiter = limiter if limiter is not None else RateLimiter(_REQUESTS_PER_SECOND)
        self._country = (country or store_country()).upper()

    def verify(
        self,
        release: ReleaseMetadata,
        local_durations_ms: Sequence[int],
        local_titles: Sequence[str] = (),
    ) -> dict[str, object] | None:
        """Return this witness's verdicts, or ``None`` when it has nothing to say.

        The durations compared are the *local audio's*, on purpose: agreement
        then means an independent catalogue corroborates what the files
        themselves are, not merely that two databases copied each other.

        ``local_titles`` are compared the same way and are the stronger
        evidence: a name agreeing is a statement about the record, while two
        lengths agreeing is often a coincidence of duration. Only the count
        leaves this class — the names iTunes published are compared and
        dropped, never returned and never stored.
        """
        artist = " ".join(a.name for a in release.artists)
        collection = self._find_album(artist, release.title)
        if collection is None:
            return None
        verdicts: dict[str, object] = {"witness": "itunes", "found": True}
        track_count = collection.get("trackCount")
        if isinstance(track_count, int):
            verdicts["track_count_agrees"] = track_count == len(local_durations_ms)
            verdicts["track_count"] = track_count
        year = self._year(collection)
        release_year = (
            (release.original_released_on or release.released_on).year
            if release.original_released_on or release.released_on
            else None
        )
        if year is not None and release_year is not None:
            verdicts["year_agrees"] = abs(year - release_year) <= 1
        # The page it read, so a verdict can be checked rather than believed.
        # An address is not catalogue data: a witness that contributed to a
        # decision is attributed with a link, and a verdict with no page to
        # look at would have to be taken on trust.
        page = collection.get("collectionViewUrl")
        if isinstance(page, str) and page.startswith("https://"):
            verdicts["url"] = page
        tracks = self._tracks(collection.get("collectionId"))
        durations = tuple(duration for duration, _ in tracks)
        if durations:
            agreeing = _agreeing(local_durations_ms, durations)
            verdicts["durations_compared"] = len(durations)
            verdicts["durations_agreeing"] = agreeing
            verdicts["durations_agree"] = len(durations) == len(
                local_durations_ms
            ) and agreeing >= max(1, round(0.8 * len(durations)))
        names = tuple(name for _, name in tracks if name)
        if names and local_titles:
            matched = _agreeing_names(local_titles, names)
            verdicts["titles_compared"] = len(names)
            verdicts["titles_agreeing"] = matched
        return verdicts

    def recognised(self, release: ReleaseMetadata) -> "WitnessRecord | None":
        """Return what this witness publishes about the album, for reading.

        The one method here that hands a caller the catalogue's own words, and
        it is for the screen: an album whose sources failed while an
        independent catalogue knows the record must not be met with silence,
        and a count alone does not show *what* was recognised.

        It is a live lookup on purpose. The words may not be kept, so there is
        nothing stored for this to read. Two requests, made when the fold is
        opened and not before.

        ``None`` means the witness does not know this record, which is an answer
        and not a failure.
        """
        artist = " ".join(a.name for a in release.artists)
        collection = self._find_album(artist, release.title)
        if collection is None:
            return None
        page = collection.get("collectionViewUrl")
        count = collection.get("trackCount")
        return WitnessRecord(
            title=str(collection.get("collectionName", "")),
            artist=str(collection.get("artistName", "")),
            year=self._year(collection),
            track_count=count if isinstance(count, int) else None,
            url=page if isinstance(page, str) and page.startswith("https://") else None,
            tracks=tuple(
                WitnessTrack(position=index, name=name, duration_ms=duration)
                for index, (duration, name) in enumerate(
                    self._tracks(collection.get("collectionId")), start=1
                )
            ),
        )

    def _find_album(self, artist: str, album: str) -> Mapping[str, Any] | None:
        term = fold_accents(f"{artist} {album}").strip()
        if not term:
            return None
        payload = self._get(
            _SEARCH_URL, {"term": term, "entity": "album", "media": "music", "limit": "8"}
        )
        if payload is None:
            return None
        results = payload.get("results")
        if not isinstance(results, list):
            return None
        best: tuple[float, Mapping[str, Any]] | None = None
        for result in results:
            if not isinstance(result, dict):
                continue
            name = str(result.get("collectionName", ""))
            similarity = _similarity(album, name)
            if similarity >= _TITLE_FLOOR and (best is None or similarity > best[0]):
                best = (similarity, result)
        return best[1] if best else None

    def _tracks(self, collection_id: object) -> tuple[tuple[int, str], ...]:
        """Return each track's length and name, for comparison and nothing else.

        Both are read in one request rather than two: the lookup already answers
        with the whole tracklist, and asking twice would spend a second slot of
        a witness limited to twenty calls a minute. Neither value is returned to
        a caller by ``verify`` — only the counts computed from them are.
        """
        if not collection_id:
            return ()
        payload = self._get(_LOOKUP_URL, {"id": str(collection_id), "entity": "song"})
        if payload is None:
            return ()
        results = payload.get("results")
        if not isinstance(results, list):
            return ()
        return tuple(
            (int(item["trackTimeMillis"]), str(item.get("trackName") or ""))
            for item in results
            if isinstance(item, dict)
            and item.get("wrapperType") == "track"
            and isinstance(item.get("trackTimeMillis"), int)
        )

    @staticmethod
    def _year(collection: Mapping[str, Any]) -> int | None:
        raw = str(collection.get("releaseDate", ""))[:4]
        return int(raw) if raw.isdigit() else None

    def _get(self, base: str, parameters: Mapping[str, str]) -> Mapping[str, Any] | None:
        self._limiter.acquire()
        # The store goes on every request. Both of them: the search finds the
        # album and the lookup reads its tracklist, and a lookup in another shop
        # answers about another catalogue's copy of the record.
        url = f"{base}?{urllib.parse.urlencode({**parameters, 'country': self._country})}"
        try:
            return self._transport(url)
        except urllib.error.HTTPError as error:
            # Told apart from every other failure, because the answer is
            # different: being throttled means stop asking, and asking anyway is
            # what turns a minute of `429` into a stretch of `403`. The caller
            # stops for this album and the next one.
            if error.code in (403, 429):
                self._logger.warning(
                    "The verification witness is throttling; it will be left alone.",
                    extra={"operation": "verification.itunes.throttled", "status": error.code},
                )
                raise WitnessThrottledError(str(error)) from error
            self._logger.warning(
                "The verification witness could not be consulted.",
                extra={"operation": "verification.itunes.failure", "status": error.code},
            )
            return None
        except Exception as error:
            self._logger.warning(
                "The verification witness could not be consulted.",
                extra={"operation": "verification.itunes.failure", "why": type(error).__name__},
            )
            return None


def _agreeing(local: Sequence[int], remote: Sequence[int]) -> int:
    remaining = sorted(remote)
    agreeing = 0
    for duration in sorted(local):
        for index, candidate in enumerate(remaining):
            if abs(candidate - duration) <= _DURATION_TOLERANCE_MS:
                remaining.pop(index)
                agreeing += 1
                break
    return agreeing


_NAME_FLOOR = 0.8
"""How similar two track names must be to count as the same song.

The matcher's own bar for a title (`TITLE_MATCH_THRESHOLD`), repeated here as a
number rather than imported: `metadata/` does not depend on `library/`, and the
boundary is worth more than the duplication.
"""


def _agreeing_names(local: Sequence[str], remote: Sequence[str]) -> int:
    """Count how many local titles this witness also publishes, each used once."""
    remaining = [name for name in remote if name.strip()]
    agreeing = 0
    for title in local:
        if not title.strip():
            continue
        scored = sorted(
            ((_similarity(title, name), index) for index, name in enumerate(remaining)),
            reverse=True,
        )
        if scored and scored[0][0] >= _NAME_FLOOR:
            remaining.pop(scored[0][1])
            agreeing += 1
    return agreeing


def _similarity(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    first = fold_accents(left).casefold().strip()
    second = fold_accents(right).casefold().strip()
    if not first or not second:
        return 0.0
    if first in second or second in first:
        return 1.0
    return SequenceMatcher(None, first, second).ratio()
