"""Metadata Engine implementation that coordinates sources through contracts."""

from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol, TypeVar, runtime_checkable

from diglibrary.application.contracts import (
    MetadataQuery,
    MetadataSearchResult,
    MetadataSourceId,
)
from diglibrary.metadata.models import ReleaseMetadata

_Answer = TypeVar("_Answer")


class MetadataSourceClient(Protocol):
    """Search one metadata source without knowing about other sources or Engines."""

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        """Return canonical releases from this source without ranking them."""


@runtime_checkable
class ReleaseDetailClient(Protocol):
    """Fetch one release in full, for a source that publishes more than its search does.

    This is an optional capability. A search result is often a summary — Discogs
    search, in particular, carries no tracklist — and identification needs track
    durations, so a source that can supply them is asked to.
    """

    def get_release(self, release_id: str) -> ReleaseMetadata:
        """Return one complete release by its identifier within this source."""


@runtime_checkable
class ReleaseGroupClient(Protocol):
    """Report when the album behind a pressing was first released.

    Optional, like detail retrieval: a source that cannot distinguish a reissue
    from an original simply does not offer this.
    """

    def first_release_date(self, group_id: str) -> date | None:
        """Return the album's first release date, or ``None`` when unknown."""


class MetadataService:
    """Purpose: collect unranked canonical metadata from every configured source.

    Responsibilities: query each registered source in the configured precedence
    order and group results by source. Boundaries: it does not select a release,
    score a match, or know which sources exist — the composition root supplies
    them. Dependencies: the source-client protocol only. Collaborators: the
    composition root, source clients, and the future album matcher. Constraints:
    the source set is open, so adding a source means registering a
    client here rather than editing this class. A failing source must not vanish
    silently from the result, so failures propagate to the caller.
    """

    def __init__(self, sources: Sequence[tuple[MetadataSourceId, MetadataSourceClient]]) -> None:
        """Create the Metadata Engine from an ordered sequence of identified sources."""
        if not sources:
            raise ValueError("At least one metadata source must be configured.")
        identifiers = [identifier for identifier, _ in sources]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Metadata sources must be registered under unique identifiers.")
        self._sources = tuple(sources)
        # How many times each source has answered, counted here because here
        # is where they answer. The workflow's search loop is only one of the
        # ways this class reaches a client: counted there, a source whose
        # search timed out and whose releases were then fetched by the acoustic
        # path would be reported as not having answered, by a run it decided.
        # A source that answered even once is not down.
        self._answers: dict[str, int] = {}

    @property
    def answers(self) -> dict[str, int]:
        """How many times each source has answered since the last `forget_answers`."""
        return dict(self._answers)

    def forget_answers(self) -> None:
        """Start a new run's count. Called where a run begins, never where one ends."""
        self._answers.clear()

    def _ask(
        self, source: MetadataSourceId, call: Callable[..., _Answer], *arguments: object
    ) -> _Answer:
        """Call one source's client, and count the answer if one comes back.

        The only place this class calls a client, so that the count cannot be
        missing from a method added later.
        `tests/architecture/test_metadata_service_counts.py` refuses a call on
        a client anywhere else in this file.

        A raise is not an answer and is not counted; it travels on to the caller,
        which is what decides whether the source is reported as out. A capability
        the client does not have never reaches here at all: the methods above
        answer `None` for that, and *cannot do this* is not *did not answer*.
        """
        answer = call(*arguments)
        self._answers[str(source)] = self._answers.get(str(source), 0) + 1
        return answer

    @property
    def source_order(self) -> tuple[MetadataSourceId, ...]:
        """Return the configured precedence order, highest precedence first."""
        return tuple(identifier for identifier, _ in self._sources)

    def fetch_release(self, source: MetadataSourceId, release_id: str) -> ReleaseMetadata | None:
        """Return one release in full, or ``None`` if that source cannot supply detail.

        A source is not required to implement detail retrieval, so an absent
        capability is a normal answer rather than an error.
        """
        for identifier, client in self._sources:
            if identifier == source:
                if not isinstance(client, ReleaseDetailClient):
                    return None
                return self._ask(source, client.get_release, release_id)
        return None

    def fetch_first_release_date(self, source: MetadataSourceId, group_id: str) -> date | None:
        """Return when the album behind a pressing first came out, if the source knows."""
        for identifier, client in self._sources:
            if identifier == source:
                if not isinstance(client, ReleaseGroupClient):
                    return None
                return self._ask(source, client.first_release_date, group_id)
        return None

    def resolve_group(self, source: MetadataSourceId, group_id: str) -> str | None:
        """Return the release a group or master stands for, if the source can say.

        A user copying a link from their browser usually copies the album's
        page rather than a pressing's, and only a pressing has a tracklist to
        align files against.
        """
        for identifier, client in self._sources:
            if identifier == source:
                resolver = getattr(client, "main_release_id", None)
                return self._ask(source, resolver, group_id) if callable(resolver) else None
        return None

    def releases_in_group(
        self, source: MetadataSourceId, group_id: str
    ) -> tuple[ReleaseMetadata, ...]:
        """Return the pressings of one release group, when the source can browse them.

        Asked of a source that cannot, the answer is nothing rather than an
        error — the same shape as ``resolve_group``, and the reason is the same:
        a capability one catalogue has is not a capability every client must
        implement.
        """
        for identifier, client in self._sources:
            if identifier == source:
                browse = getattr(client, "releases_in_group", None)
                return self._ask(source, browse, group_id) if callable(browse) else ()
        return ()

    def search_source(
        self, source: MetadataSourceId, query: MetadataQuery
    ) -> tuple[ReleaseMetadata, ...]:
        """Search exactly one source, leaving every other one untouched.

        Asking one source at a time is what lets a caller stop as soon as it has
        an unambiguous answer, which matters because these services are rate
        limited and MusicBrainz allows roughly one request a second.
        """
        for identifier, client in self._sources:
            if identifier == source:
                return self._ask(source, client.search_releases, query)
        return ()

    def search(self, query: MetadataQuery) -> tuple[MetadataSearchResult, ...]:
        """Collect source-separated results in the configured precedence order."""
        return tuple(
            MetadataSearchResult(
                source=identifier, releases=self._ask(identifier, client.search_releases, query)
            )
            for identifier, client in self._sources
        )
