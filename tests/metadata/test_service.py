"""Unit tests for metadata service orchestration and matching contracts."""

import pytest

from diglibrary.metadata.matching import MetadataMatcher
from diglibrary.metadata.models import (
    MetadataQuery,
    MetadataSourceId,
    MetadataSources,
    ReleaseMetadata,
)
from diglibrary.metadata.service import MetadataService


class FakeSourceClient:
    """Return fixed releases through the source-client structural contract."""

    def __init__(self, releases: tuple[ReleaseMetadata, ...]) -> None:
        self._releases = releases

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        """Return fixed unranked releases."""
        return self._releases


def test_metadata_service_preserves_configured_source_precedence_without_selection() -> None:
    """The service groups unranked results in the precedence order it was given."""
    discogs_release = _release(MetadataSources.DISCOGS, "discogs-id")
    musicbrainz_release = _release(MetadataSources.MUSICBRAINZ, "musicbrainz-id")
    service = MetadataService(
        (
            (MetadataSources.DISCOGS, FakeSourceClient((discogs_release,))),
            (MetadataSources.MUSICBRAINZ, FakeSourceClient((musicbrainz_release,))),
        )
    )

    results = service.search(MetadataQuery(album="Album"))

    assert [result.source for result in results] == [
        MetadataSources.DISCOGS,
        MetadataSources.MUSICBRAINZ,
    ]
    assert results[0].releases == (discogs_release,)
    assert results[1].releases == (musicbrainz_release,)


def test_a_new_metadata_source_needs_no_change_to_the_engine() -> None:
    """Registering a source is the whole cost of adding one."""
    bandcamp_release = _release(MetadataSources.BANDCAMP, "bandcamp-id")
    official_site = MetadataSourceId("official_artist_site")
    service = MetadataService(
        (
            (MetadataSources.DISCOGS, FakeSourceClient(())),
            (MetadataSources.BANDCAMP, FakeSourceClient((bandcamp_release,))),
            (official_site, FakeSourceClient(())),
        )
    )

    results = service.search(MetadataQuery(album="Album"))

    assert service.source_order == (
        MetadataSources.DISCOGS,
        MetadataSources.BANDCAMP,
        official_site,
    )
    assert results[1].releases == (bandcamp_release,)


def test_metadata_service_rejects_an_unusable_source_configuration() -> None:
    """A duplicated or empty source set is a composition error, not a silent default."""
    with pytest.raises(ValueError, match="At least one metadata source"):
        MetadataService(())
    with pytest.raises(ValueError, match="unique identifiers"):
        MetadataService(
            (
                (MetadataSources.DISCOGS, FakeSourceClient(())),
                (MetadataSources.DISCOGS, FakeSourceClient(())),
            )
        )


def test_metadata_matcher_is_an_interface_only() -> None:
    """Phase 2 defines matching boundaries without introducing a selection algorithm."""
    assert MetadataMatcher.__dict__.get("_is_protocol") is True
    assert "match" in MetadataMatcher.__dict__


def _release(source: MetadataSourceId, identifier: str) -> ReleaseMetadata:
    return ReleaseMetadata(source=source, source_release_id=identifier, title="Album", artists=())


class BrowsingSourceClient(FakeSourceClient):
    """A source that can also list the pressings of a release group."""

    def releases_in_group(self, group_id: str) -> tuple[ReleaseMetadata, ...]:
        """Return the fixed releases, recording which group was asked for."""
        self.asked = group_id
        return self._releases


def test_a_source_that_cannot_browse_a_group_answers_nothing_rather_than_raising() -> None:
    """Browsing is a capability one catalogue has, not one every client must implement.

    Discogs resolves a master and MusicBrainz browses a group; neither has to
    grow the other's method for the acoustic path to ask.
    """
    pressing = _release(MetadataSources.MUSICBRAINZ, "pressing")
    browsing = BrowsingSourceClient((pressing,))
    service = MetadataService(
        (
            (MetadataSources.DISCOGS, FakeSourceClient(())),
            (MetadataSources.MUSICBRAINZ, browsing),
        )
    )

    assert service.releases_in_group(MetadataSources.DISCOGS, "group") == ()
    assert service.releases_in_group(MetadataSources.MUSICBRAINZ, "group") == (pressing,)
    assert browsing.asked == "group"


class WholeSourceClient(FakeSourceClient):
    """A source that answers through every door this Engine has."""

    def get_release(self, release_id: str) -> ReleaseMetadata:
        """Return one release in full."""
        return self._releases[0]

    def first_release_date(self, group_id: str):
        """Return when the album behind a pressing first came out."""
        return None

    def main_release_id(self, group_id: str) -> str:
        """Resolve a group to the pressing it stands for."""
        return "pressing"

    def releases_in_group(self, group_id: str) -> tuple[ReleaseMetadata, ...]:
        """List the pressings of a group."""
        return self._releases


def test_every_door_of_the_engine_counts_the_answer_it_got() -> None:
    """An answer is counted where the client is called, whichever door asked.

    The count of a source's answers decides whether the window says a run was
    decided without that source. Kept beside the search loop alone, which is one
    of the six ways this Engine reaches a client, it reports a source as absent
    from a run that a timed-out search and a successful acoustic lookup settled
    with that very source's releases.

    So the question is not *does searching count* but *does every door count*,
    and it is written by walking them all.
    """
    client = WholeSourceClient((_release(MetadataSources.MUSICBRAINZ, "r1"),))
    service = MetadataService(((MetadataSources.MUSICBRAINZ, client),))
    source = MetadataSources.MUSICBRAINZ

    doors = (
        lambda: service.search(MetadataQuery(album="Album")),
        lambda: service.search_source(source, MetadataQuery(album="Album")),
        lambda: service.fetch_release(source, "r1"),
        lambda: service.fetch_first_release_date(source, "g1"),
        lambda: service.resolve_group(source, "g1"),
        lambda: service.releases_in_group(source, "g1"),
    )
    for door in doors:
        door()

    assert service.answers == {str(source): len(doors)}, (
        "an answer that arrives through a door nobody counted is a source the "
        "window will call absent from a run it took part in"
    )

    service.forget_answers()

    assert service.answers == {}, "a run counts its own answers and no older ones"


def test_a_capability_the_client_lacks_is_not_an_answer() -> None:
    """*Cannot do this* and *did not answer* are different facts about a source.

    A client with no detail method has not been consulted by being asked for
    detail, and counting that would make an unreachable service look present.
    """
    service = MetadataService(((MetadataSources.DISCOGS, FakeSourceClient(())),))

    assert service.fetch_release(MetadataSources.DISCOGS, "r1") is None
    assert service.fetch_first_release_date(MetadataSources.DISCOGS, "g1") is None
    assert service.resolve_group(MetadataSources.DISCOGS, "g1") is None
    assert service.releases_in_group(MetadataSources.DISCOGS, "g1") == ()
    assert service.answers == {}, "nothing was asked of the source, so nothing answered"

    assert service.search_source(MetadataSources.MUSICBRAINZ, MetadataQuery(album="A")) == ()
    assert service.answers == {}, "and a source that is not registered answers nothing either"


def test_a_source_that_raises_is_not_counted_as_having_answered() -> None:
    """The raise is what makes a source reportable as out; counting it would hide that."""

    class FailingClient(FakeSourceClient):
        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            raise TimeoutError("The read operation timed out")

    service = MetadataService(((MetadataSources.MUSICBRAINZ, FailingClient(())),))

    with pytest.raises(TimeoutError):
        service.search_source(MetadataSources.MUSICBRAINZ, MetadataQuery(album="Album"))

    assert service.answers == {}
