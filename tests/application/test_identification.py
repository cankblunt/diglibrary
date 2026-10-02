"""Unit tests for the identification workflow, with every source faked."""

import logging
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

from mutagen.flac import FLAC, Picture

from diglibrary.application.artwork import ArtworkPolicy, ArtworkService
from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataQuery,
    MetadataSourceId,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.application.identification import Decision, IdentificationWorkflow
from diglibrary.library.artwork import Artwork, FilesystemArtworkStore
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.matching import AlbumMatcher
from diglibrary.library.models import AlbumUnit
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangePlanner, OperationKind
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.metadata.service import MetadataService

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


class FakeSource:
    """Answer searches from a fixed list, and detail requests from another."""

    def __init__(
        self,
        summaries: tuple[ReleaseMetadata, ...] = (),
        details: dict[str, ReleaseMetadata] | None = None,
    ) -> None:
        self._summaries = summaries
        self._details = details or {}
        self.searches: list[MetadataQuery] = []
        self.detail_requests: list[str] = []

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        """Record the query and return the configured summaries."""
        self.searches.append(query)
        return self._summaries

    def get_release(self, release_id: str) -> ReleaseMetadata:
        """Record the request and return the configured full release."""
        self.detail_requests.append(release_id)
        return self._details[release_id]


def test_a_confident_match_is_decided_automatically(tmp_path: Path) -> None:
    """Durations agreeing on every track clear the 0.90 threshold for an automatic decision."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    source = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900))},
    )

    outcome = _workflow(source).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.confidence >= 0.90
    assert outcome.plan is not None and outcome.plan.is_applicable
    assert source.detail_requests == ["r1"], "a summary without tracks must be fetched in full"


def test_a_weak_match_is_sent_to_review_with_the_reason(tmp_path: Path) -> None:
    """Durations that do not agree keep an album away from an unattended write.

    The reason reported is the blocker rather than the score, because "no
    release track matches these files" tells a person far more than a number.
    """
    unit = _album(tmp_path, "marina do acordeao - forro")
    source = FakeSource(
        summaries=(_summary("r1", "Something Else"),),
        details={"r1": _release("r1", "Something Else", (12_000, 47_000))},
    )

    outcome = _workflow(source).identify(unit)

    assert outcome.decision is Decision.REVIEW
    assert outcome.plan is not None and not outcome.plan.is_applicable
    # Counted and never named, so a large folder prints no file names into the
    # dialog: the table below it draws every file on its own row.
    assert "no file" in outcome.reason and ".flac" not in outcome.reason
    assert outcome.confidence < 0.90


def test_an_ambiguous_layout_is_identified_and_never_planned(tmp_path: Path) -> None:
    """With an ambiguous layout the application asks instead of guessing.

    Everything else about this album is certain — the same files, the same
    release, the same confident score that decides an ordinary album
    unattended. What is not certain is which folder is the album, and no score
    answers that, so there is nothing here to approve.
    """
    unit = replace(
        _album(tmp_path, "marina do acordeao - forro"),
        ambiguous_layout="This folder holds audio of its own beside a disc folder.",
    )
    source = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900))},
    )

    outcome = _workflow(source).identify(unit)

    assert outcome.decision is Decision.REVIEW
    assert outcome.confidence >= 0.90, "the album is still identified, and says so"
    assert outcome.plan is None, "nothing to approve, so nothing can be written"
    assert outcome.partial_plan is None
    assert outcome.reason.startswith("This folder holds audio of its own")


def test_a_second_source_is_not_consulted_once_the_answer_is_certain(tmp_path: Path) -> None:
    """A rate-limited second source is not queried once the first answer is certain."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    primary = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900))},
    )
    secondary = FakeSource(summaries=(_summary("mb1", "Forró"),))

    outcome = _workflow(primary, secondary).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.sources_consulted == (MetadataSources.DISCOGS,)
    assert secondary.searches == []


def test_a_contradicting_source_costs_confidence_and_forces_review(tmp_path: Path) -> None:
    """A source whose best answer is a different album pushes the match to review."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    primary = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900), year=1955)},
    )
    secondary = FakeSource(
        summaries=(_summary("mb1", "Zabumba Do Zarolho"),),
        details={"mb1": _release("mb1", "Zabumba Do Zarolho", (400, 900), year=1955)},
    )

    outcome = _workflow(primary, secondary, consult_every_source=True).identify(unit)

    assert outcome.decision is Decision.REVIEW
    assert outcome.candidate is not None
    assert outcome.candidate.release.released_on == date(1955, 1, 1)
    assert "Contradicted by musicbrainz" in outcome.candidate.explanation


def test_another_pressing_year_is_not_a_contradiction(tmp_path: Path) -> None:
    """Two pressings one year apart stay an automatic decision.

    Both catalogues identified the same album by its durations; they merely
    surfaced different pressings. The album's own year comes from the master,
    so a pressing-year mismatch is noise, not a dispute.
    """
    unit = _album(tmp_path, "marina do acordeao - forro")
    primary = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900), year=1955)},
    )
    secondary = FakeSource(
        summaries=(_summary("mb1", "Forró"),),
        details={"mb1": _release("mb1", "Forró", (400, 900), year=1954)},
    )

    outcome = _workflow(primary, secondary, consult_every_source=True).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.candidate is not None
    assert outcome.candidate.release.released_on == date(1955, 1, 1)


def test_a_source_returning_nothing_leaves_the_album_unidentified(tmp_path: Path) -> None:
    """An album no source recognizes is left exactly as it is."""
    unit = _album(tmp_path, "completely unknown")

    outcome = _workflow(FakeSource()).identify(unit)

    assert outcome.decision is Decision.UNIDENTIFIED
    assert outcome.plan is None
    assert "No source returned" in outcome.reason


def test_a_failing_source_does_not_abort_identification(tmp_path: Path) -> None:
    """A network error is a missing answer, not a crash mid-library."""

    class BrokenSource:
        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            raise ConnectionError("the network is down")

    unit = _album(tmp_path, "marina do acordeao - forro")

    outcome = _workflow(BrokenSource()).identify(unit)

    assert outcome.decision is Decision.UNIDENTIFIED


def test_the_album_remembers_which_source_did_not_answer_its_identification(
    tmp_path: Path,
) -> None:
    """The outcome names the source that failed during its own search.

    Without it, a source that failed and a source never asked look identical
    from the dialog. The record is per album, so the dialog can say it for
    that album instead of raising a notice over the whole library.
    """

    class BrokenSource:
        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            raise ConnectionError("the network is down")

    unit = _album(tmp_path, "marina do acordeao - forro")
    release = ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="mb1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(
            TrackMetadata(title="A", position=1, duration_ms=180_000),
            TrackMetadata(title="B", position=2, duration_ms=181_000),
        ),
    )
    good = FakeSource(summaries=(release,), details={"mb1": release})

    outcome = _workflow(BrokenSource(), good).identify(unit)

    assert outcome.candidate is not None, "the healthy source still identifies the album"
    missed = (outcome.verification or {}).get("missed")
    assert missed == {"discogs": "unreachable"}, missed

    # And a run whose sources all answered says nothing — absence of the key is
    # the ordinary case, not an empty dict on every album.
    healthy = _workflow(good).identify(unit)
    assert "missed" not in (healthy.verification or {})


def test_a_blocked_plan_is_reviewed_even_when_confidence_is_high(tmp_path: Path) -> None:
    """Confidence never overrides a blocker; a wrong rename is unrecoverable."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    ambiguous = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(
            TrackMetadata(title="A", position=1, duration_ms=400),
            TrackMetadata(title="B", position=2, duration_ms=401),
        ),
    )
    source = FakeSource(summaries=(ambiguous,), details={"r1": ambiguous})

    outcome = _workflow(source).identify(unit)

    assert outcome.decision is Decision.REVIEW
    assert outcome.plan is not None and not outcome.plan.is_applicable


def test_duration_proof_from_a_later_source_beats_a_text_guess_from_the_first(
    tmp_path: Path,
) -> None:
    """A release proven by durations beats a text guess from an earlier source.

    The first source returns only entries without durations (a text guess,
    capped at 0.55) while the second has the album with every length
    agreeing. The second answer must win, and automatically.
    """
    unit = _album(tmp_path, "marina do acordeao - forro")
    durationless = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(
            TrackMetadata(title="Um", position=1, position_on_medium=1),
            TrackMetadata(title="Dois", position=2, position_on_medium=2),
        ),
        released_on=date(1955, 1, 1),
    )
    discogs = FakeSource(summaries=(_summary("r1", "Forró"),), details={"r1": durationless})
    musicbrainz = FakeSource(summaries=(_musicbrainz_release("mb1", (400, 900)),))

    outcome = _workflow(discogs, musicbrainz).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.candidate is not None
    assert outcome.candidate.release.source is MetadataSources.MUSICBRAINZ
    assert outcome.sources_consulted == (MetadataSources.DISCOGS, MetadataSources.MUSICBRAINZ)


def test_a_confident_but_unalignable_candidate_does_not_stop_the_search() -> None:
    """A high score whose files cannot be paired is not an answer.

    The first source's pressing scores a perfect confidence, but two of its
    published lengths collide and the files cannot be told apart. The next
    source's pressing pairs cleanly, so the search must go on to consult it.
    """
    unit = _synthetic_unit((180_000, 183_000, 900_000))
    colliding = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        # X and Y both sit within tolerance of both short files, no local
        # title resembles them, and the release lists its tracks in another
        # order, so neither the positional reading nor a title can save it.
        tracks=(
            TrackMetadata(title="Z", position=1, position_on_medium=1, duration_ms=900_000),
            TrackMetadata(title="X", position=2, position_on_medium=2, duration_ms=181_000),
            TrackMetadata(title="Y", position=3, position_on_medium=3, duration_ms=182_000),
        ),
    )
    clean = ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="mb1",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(
            TrackMetadata(title="Alpha", position=1, position_on_medium=1, duration_ms=180_000),
            TrackMetadata(title="Beta", position=2, position_on_medium=2, duration_ms=183_000),
            TrackMetadata(title="Gamma", position=3, position_on_medium=3, duration_ms=900_000),
        ),
        release_group_id="rg1",
    )
    discogs = FakeSource(summaries=(colliding,))
    musicbrainz = FakeSource(summaries=(clean,))

    class EmptyTagStore:
        def read(self, path: Path) -> dict[str, tuple[str, ...]]:
            return {}

        def write(self, path: Path, tags: object) -> None:
            raise AssertionError("identification must never write")

    workflow = IdentificationWorkflow(
        metadata=MetadataService(
            (
                (MetadataSources.DISCOGS, discogs),  # type: ignore[arg-type]
                (MetadataSources.MUSICBRAINZ, musicbrainz),  # type: ignore[arg-type]
            )
        ),
        matcher=AlbumMatcher(),
        planner=ChangePlanner(NamingPolicy(), EmptyTagStore(), FilesystemArtworkStore()),
        tag_store=EmptyTagStore(),
        logger=logging.getLogger("test.identification"),
    )

    outcome = workflow.identify(unit)

    assert musicbrainz.searches, "the blocked first answer did not stop the search"
    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.candidate is not None
    assert outcome.candidate.release.source is MetadataSources.MUSICBRAINZ


def _synthetic_unit(durations: tuple[int, ...]) -> AlbumUnit:
    """An in-memory unit whose durations the fixtures cannot provide."""
    from datetime import UTC, datetime

    from diglibrary.library.audio import AudioProperties
    from diglibrary.library.models import AudioFileFacts

    files = tuple(
        AudioFileFacts(
            path=Path(f"/music/artist - album/{index:02d}. Local {index}.flac"),
            content_signature=f"sig-{index}",
            file_size_bytes=1_000,
            modified_at=datetime(2001, 2, 3, tzinfo=UTC),
            properties=AudioProperties(
                codec="flac", duration_ms=duration, sample_rate=44_100, channels=2
            ),
        )
        for index, duration in enumerate(durations, start=1)
    )
    return AlbumUnit(
        folder_path=Path("/music/artist - album"),
        unit_signature="unit",
        audio_files=files,
    )


def test_a_weak_guess_from_another_source_cannot_veto_a_proven_match(tmp_path: Path) -> None:
    """Only an answer that could itself have won may contest the winner.

    One source proves the album on every duration, while the other's best is
    a low-confidence guess for some other record. The guess is an absence,
    not a dispute, and the album stays automatic.
    """
    unit = _album(tmp_path, "marina do acordeao - forro")
    wrong_guess = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Northern Carousel",
        artists=(ArtistMetadata(name="Someone Else"),),
        tracks=(
            TrackMetadata(title="A", position=1, position_on_medium=1, duration_ms=50_000),
            TrackMetadata(title="B", position=2, position_on_medium=2, duration_ms=60_000),
        ),
    )
    discogs = FakeSource(summaries=(wrong_guess,))
    musicbrainz = FakeSource(summaries=(_musicbrainz_release("mb1", (400, 900)),))

    outcome = _workflow(discogs, musicbrainz, consult_every_source=True).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.candidate is not None
    assert "Contradicted" not in outcome.candidate.explanation


def test_a_search_returning_nothing_walks_the_query_ladder(tmp_path: Path) -> None:
    """A contaminated title costs extra queries, not the album.

    The album name carries a second part after a dash, the structured search
    returns nothing for the whole of it, and the truncated title finds the
    release.
    """
    unit = _album(tmp_path, "VA - Farol Zonzo - Emissora Do Quintal (2015)")

    class LadderSource(FakeSource):
        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            self.searches.append(query)
            if query.album == "Farol Zonzo":
                return self._summaries
            return ()

    source = LadderSource(
        summaries=(_summary("r1", "Farol Zonzo"),),
        details={"r1": _release("r1", "Farol Zonzo", (400, 900))},
    )

    outcome = _workflow(source).identify(unit)

    assert outcome.decision is not Decision.UNIDENTIFIED
    assert len(source.searches) > 1, "the first query failed and a fallback ran"
    assert any(query.album == "Farol Zonzo" for query in source.searches)


def test_a_compilation_never_searches_for_various_as_an_artist(tmp_path: Path) -> None:
    """ "Various" is a library convention, not a name a catalogue indexes."""
    unit = _album(tmp_path, "VA - Distant Lanterns (1995)")
    source = FakeSource(
        summaries=(_summary("r1", "Distant Lanterns"),),
        details={"r1": _release("r1", "Distant Lanterns", (400, 900))},
    )

    _workflow(source).identify(unit)

    assert source.searches, "the source was queried"
    assert all(query.artist != "Various" for query in source.searches)


class FakeArtwork:
    """Record which MusicBrainz release the archive was asked about."""

    def __init__(self, enabled: bool = True) -> None:
        self.requests: list[tuple[str | None, str | None]] = []
        self.folders: list[Path | None] = []
        self.enabled = enabled

    def fetch(
        self,
        release_id: str | None = None,
        release_group_id: str | None = None,
        tracks: tuple[Path, ...] = (),
        folder: Path | None = None,
    ) -> Artwork:
        """Record the request and return nothing, which the planner accepts."""
        self.requests.append((release_id, release_group_id))
        self.folders.append(folder)
        return Artwork()


def test_a_discogs_win_resolves_a_musicbrainz_release_for_its_art(tmp_path: Path) -> None:
    """The archive is keyed by MBID, so one is found for the winner."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    discogs = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900))},
    )
    musicbrainz = FakeSource(
        summaries=(_musicbrainz_release("mb1", (400, 900)),),
    )
    artwork = FakeArtwork()

    outcome = _workflow(discogs, musicbrainz, artwork=artwork).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert outcome.candidate is not None
    assert outcome.candidate.release.source is MetadataSources.DISCOGS, "the winner is unchanged"
    assert artwork.requests == [("mb1", "rg1")]


def test_the_barcode_addresses_the_archive_before_any_text_search(tmp_path: Path) -> None:
    """A barcode names a pressing exactly, so it is tried first and settles it."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    discogs = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900), barcode="0012345678905")},
    )
    musicbrainz = FakeSource(summaries=(_musicbrainz_release("mb1", (400, 900)),))

    _workflow(discogs, musicbrainz, artwork=FakeArtwork()).identify(unit)

    assert musicbrainz.searches[0] == MetadataQuery(barcode="0012345678905")
    assert len(musicbrainz.searches) == 1, "a settled barcode needs no text search"


def test_the_archive_is_not_addressed_when_no_musicbrainz_release_matches(
    tmp_path: Path,
) -> None:
    """Durations decide here too: an unproven MBID would fetch another album's cover.

    The service is still asked, with no release to look up, because the album's
    own embedded picture remains a candidate for the folder cover.
    """
    unit = _album(tmp_path, "marina do acordeao - forro")
    discogs = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900))},
    )
    musicbrainz = FakeSource(summaries=(_musicbrainz_release("mb1", (12_000, 47_000)),))
    artwork = FakeArtwork()

    outcome = _workflow(discogs, musicbrainz, artwork=artwork).identify(unit)

    assert outcome.decision is Decision.AUTOMATIC
    assert artwork.requests == [(None, None)]


def test_a_musicbrainz_win_needs_no_bridge(tmp_path: Path) -> None:
    """When the winner is already from MusicBrainz, its own identifier is the answer."""
    unit = _album(tmp_path, "marina do acordeao - forro")
    musicbrainz = FakeSource(summaries=(_musicbrainz_release("mb1", (400, 900)),))
    artwork = FakeArtwork()

    workflow = IdentificationWorkflow(
        metadata=MetadataService(((MetadataSources.MUSICBRAINZ, musicbrainz),)),  # type: ignore[arg-type]
        matcher=AlbumMatcher(duration_tolerance_ms=100, ordered_tolerance_ms=100),
        planner=ChangePlanner(NamingPolicy(), MutagenTagStore(), FilesystemArtworkStore()),
        tag_store=MutagenTagStore(),
        logger=logging.getLogger("test.identification"),
        artwork=artwork,
    )
    workflow.identify(unit)

    assert artwork.requests == [("mb1", "rg1")]
    assert len(musicbrainz.searches) == 1


def _workflow(
    primary: object,
    secondary: object | None = None,
    consult_every_source: bool = False,
    artwork: object | None = None,
) -> IdentificationWorkflow:
    sources: list[tuple[MetadataSourceId, object]] = [(MetadataSources.DISCOGS, primary)]
    if secondary is not None:
        sources.append((MetadataSources.MUSICBRAINZ, secondary))
    return IdentificationWorkflow(
        metadata=MetadataService(tuple(sources)),  # type: ignore[arg-type]
        matcher=AlbumMatcher(duration_tolerance_ms=100, ordered_tolerance_ms=100),
        planner=ChangePlanner(NamingPolicy(), MutagenTagStore(), FilesystemArtworkStore()),
        tag_store=MutagenTagStore(),
        logger=logging.getLogger("test.identification"),
        consult_every_source=consult_every_source,
        artwork=artwork,  # type: ignore[arg-type]
    )


def _musicbrainz_release(identifier: str, durations: tuple[int, ...]) -> ReleaseMetadata:
    """A MusicBrainz search result, which unlike Discogs already carries its tracks."""
    return ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id=identifier,
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=tuple(
            TrackMetadata(
                title=f"Faixa {index}",
                position=index,
                position_on_medium=index,
                duration_ms=duration,
            )
            for index, duration in enumerate(durations, start=1)
        ),
        release_group_id="rg1",
    )


def _album(root: Path, folder_name: str) -> AlbumUnit:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    units = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test")).scan(root)
    return units[0]


def _summary(identifier: str, title: str) -> ReleaseMetadata:
    """A search result, which for Discogs carries no tracklist at all."""
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=identifier,
        title=title,
        artists=(),
    )


def _release(
    identifier: str,
    title: str,
    durations: tuple[int, ...],
    year: int | None = None,
    barcode: str | None = None,
) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=identifier,
        title=title,
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=tuple(
            TrackMetadata(
                title=f"Faixa {index}",
                position=index,
                position_on_medium=index,
                duration_ms=duration,
            )
            for index, duration in enumerate(durations, start=1)
        ),
        released_on=date(year, 1, 1) if year else None,
        barcode=barcode,
    )


def test_a_release_that_pairs_every_file_outranks_a_more_confident_one(tmp_path: Path) -> None:
    """A release that names every file wins over one that scores higher.

    Two catalogues can index one album differently: one joins two songs in a
    single track, the other lists them apart. The release whose track count
    equals the file count scores higher, yet it leaves a file paired with
    nothing. The other release names every file and reports the track that
    has no file.
    """
    unit = _album(tmp_path, "marina do acordeao - forro")
    # Discogs: every file pairs, and one track has no file.
    discogs = FakeSource(
        summaries=(_summary("d1", "Forró"),),
        details={"d1": _release("d1", "Forró", (400, 900, 700))},
    )
    # MusicBrainz: the track count matches the file count, which scores better,
    # but one of the files ends up with no track at all.
    musicbrainz = FakeSource(summaries=(_musicbrainz_release("mb1", (400, 5_000)),))

    outcome = _workflow(discogs, musicbrainz).identify(unit)

    assert outcome.candidate is not None
    assert outcome.candidate.release.source == MetadataSources.DISCOGS
    assert outcome.candidate.confidence < 1.0, "and it wins without being the more confident one"
    assert not outcome.alignment.unmatched_files, "no file may be left unnamed"
    assert len(outcome.alignment.unmatched_tracks) == 1, "the missing track is still reported"


class _SilentAudio:
    """An acoustic engine that is there, is asked, and knows nothing."""

    is_available = True

    def __init__(self) -> None:
        self.asked = 0

    def releases_for(self, unit: AlbumUnit) -> tuple[ReleaseMetadata, ...]:
        """Answer the way the service answers about a recording it does not hold."""
        self.asked += 1
        return ()


def test_what_the_audio_answered_leaves_the_workflow(tmp_path: Path) -> None:
    """What the audio answered is carried in the outcome.

    An outcome with `acoustic=""` is rendered by the window as the audio never
    having been fingerprinted. The audio is asked only when the names failed,
    so dropping its answer makes that sentence false for exactly the albums
    it was asked about.

    The positive answer depends on it too: `_persist_outcome` writes
    `method='acoustic'` only when the outcome says `named`.
    """
    unit = _album(tmp_path, "válter brumaldo - varandela lunática")
    audio = _SilentAudio()
    # A source that answers with the wrong record rather than with nothing.
    # That is the case that reaches `_outcome`: an empty answer returns
    # earlier, through another path.
    wrong = _release("r-wrong", "Bruma della Sera", (240_000,))
    workflow = _workflow(
        FakeSource(summaries=(_summary("r-wrong", "Bruma della Sera"),), details={"r-wrong": wrong})
    )
    workflow._acoustic = audio  # type: ignore[attr-defined]

    outcome = workflow.identify(unit)

    assert audio.asked == 1, "the names found nothing, so the audio has to be asked"
    assert (
        outcome.acoustic == "unknown"
    ), "the answer has to leave the workflow, or the screen says it was never asked"


def test_replanning_offline_still_offers_the_album_its_own_picture(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """`offline` stops the archive being asked, not the disk being read.

    `adopt_release(offline=True)` is the restoration path every organized
    album goes through when the window opens, so it must not fetch art from
    the network. Two sources of art never touch the network and stay
    available: the album's own embedded picture, offered as the folder cover,
    and a picture already in the folder, offered to the tracks.

    The whole test runs against an archive client that raises if it is called.
    """
    folder = tmp_path / "Aurelio"
    folder.mkdir(parents=True)
    picture = Picture()
    picture.type, picture.mime, picture.depth = 3, "image/jpeg", 24
    picture.width = picture.height = 600
    picture.data = jpeg(600, 600)
    # Named as the release names its tracks: both files are one tone copied
    # twice, so their lengths are equal to the millisecond and the pairing is
    # settled by the names or by nothing at all. This test is about the picture;
    # an album whose files nothing can tell apart is a different question, and
    # `_pairs_a_length_reads_the_same_either_way` is where it is asked.
    for name in ("01. Faixa 1.flac", "02. Faixa 2.flac"):
        shutil.copy(FIXTURES / "tone.flac", folder / name)
        audio = FLAC(folder / name)
        audio.add_picture(picture)
        audio.save()
    assert not [path for path in folder.iterdir() if path.suffix.lower() == ".jpg"]

    class NoArchive:
        def cover_art(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("planning offline must not reach the archive")

    artwork = ArtworkService(
        NoArchive(),  # type: ignore[arg-type]
        tmp_path / "staging",
        ArtworkPolicy(),
        logging.getLogger("test.identification"),
        FilesystemArtworkStore(),
    )
    unit = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test")).scan(folder)[0]
    release = _musicbrainz_release("r1", tuple(unit.track_durations_ms or (400, 400)))
    workflow = _workflow(FakeSource(), artwork=artwork)

    outcome = workflow.adopt_release(unit, MetadataSources.MUSICBRAINZ, release, offline=True)

    assert outcome.plan is not None
    written = [
        operation
        for operation in outcome.plan.operations
        if operation.kind is OperationKind.WRITE_IMAGE
    ]
    assert written, (
        "the folder holds no picture, every track holds one, and nothing here "
        "needs the network — so the cover has to reach the folder"
    )


def test_a_source_that_goes_dark_is_remembered_and_named() -> None:
    """A source that cannot be used is classified, so the failure can be shown.

    A search that raises is caught and treated as having found nothing, and
    identification carries on with the sources that answer. If that happens
    silently, a missing credential degrades every identification with
    nothing on screen saying that a catalogue was not asked.

    Carrying on without a source is right. Carrying on silently is what this
    forbids.
    """
    from diglibrary.application.identification import _why_unavailable
    from diglibrary.metadata.transport import CredentialError

    # The three states, and never the exception's own text: a failure raised
    # while a request was being signed is the one place a key could reach a
    # screen (`config.credentials`).
    assert _why_unavailable(CredentialError("Required environment variable is not set: X")) == (
        "no_key"
    )

    refused = RuntimeError("nope")
    refused.status = 401
    assert _why_unavailable(refused) == "refused"

    forbidden = RuntimeError("nope")
    forbidden.status = 403
    assert _why_unavailable(forbidden) == "refused"

    # Anything not classified is *unreachable*, and only that: status codes
    # that mean a refusal are told apart, so a server that answered is not
    # reported as one that could not be reached.
    assert _why_unavailable(RuntimeError("something new")) == "unreachable"

    secret = CredentialError("token=abc123secret")
    assert "abc123secret" not in _why_unavailable(secret)


def test_a_pairing_made_by_hand_does_not_take_the_partial_apply_away(tmp_path: Path) -> None:
    """Pairing one file by hand must not remove the partial plan.

    An album has one file more than the release has tracks. Pairing a file by
    hand sends the album through `refine` instead of `identify`; if `refine`
    builds its outcome without a partial plan, the same files, release and
    alignment lose the partial apply with nothing on screen explaining why.

    Both paths ask one function, so the alignment decides and the route to it
    does not.
    """
    folder = tmp_path / "Tavinho - Faróis Dos Becos"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "01 - Uma.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "02 - Outra.flac")
    shutil.copy(FIXTURES / "tone.wav", folder / "03 - Bonus.wav")
    unit = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test")).scan(tmp_path)[0]
    # A release for two of the three files.
    release = _release("r1", "Faróis Dos Becos", unit.track_durations_ms[:2])
    workflow = _workflow(FakeSource())

    scanned = workflow.adopt_release(unit, MetadataSources.DISCOGS, release, offline=True)
    assert scanned.plan is not None and not scanned.plan.is_applicable
    assert scanned.partial_plan is not None, "the scan offers what did pair"

    paired = workflow.refine(
        unit,
        scanned.candidate,
        track_files={unit.audio_files[0].content_signature: 1},
        offline=True,
    )

    assert paired.alignment is not None and paired.alignment.is_partial
    assert paired.partial_plan is not None, "a manual pairing must not take the offer away"
    assert paired.partial_plan.is_applicable


def test_a_partial_album_still_gets_the_cover_it_already_carries(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A partial alignment still plans the album's cover.

    When all but one file pair, the album's identity, and therefore its
    sleeve, is not in doubt; only the extra track is. The artwork gate in
    `refine` must accept `is_usable or is_partial`, as the gate in `_outcome`
    does, or no art is read and the folder gets no `cover.jpg`. Nothing here
    touches the network: the picture is the album's own.
    """
    folder = tmp_path / "Tavinho - Faróis Dos Becos"
    folder.mkdir(parents=True)
    picture = Picture()
    picture.type, picture.mime, picture.depth = 3, "image/jpeg", 24
    picture.width = picture.height = 600
    picture.data = jpeg(600, 600)
    for name, fixture in (("01 - Uma.flac", "tone.flac"), ("02 - Outra.flac", "tone-long.flac")):
        shutil.copy(FIXTURES / fixture, folder / name)
        audio = FLAC(folder / name)
        audio.add_picture(picture)
        audio.save()
    # The bonus, and a different recording on purpose: two copies of one fixture
    # share a content signature, and a signature is a shape rather than a
    # recording, so the duplicate never reaches the alignment at all.
    shutil.copy(FIXTURES / "tone.wav", folder / "03 - Bonus.wav")
    assert not [path for path in folder.iterdir() if path.suffix.lower() == ".jpg"]

    class NoArchive:
        def cover_art(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("the album's own picture costs no request")

    artwork = ArtworkService(
        NoArchive(),  # type: ignore[arg-type]
        tmp_path / "staging",
        ArtworkPolicy(),
        logging.getLogger("test.identification"),
        FilesystemArtworkStore(),
    )
    unit = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test")).scan(tmp_path)[0]
    # Two tracks for three files: the third is a bonus the release does not list.
    release = _release("r1", "Faróis Dos Becos", unit.track_durations_ms[:2])
    workflow = _workflow(FakeSource(), artwork=artwork)

    scanned = workflow.adopt_release(unit, MetadataSources.DISCOGS, release, offline=True)
    paired = workflow.refine(
        unit,
        scanned.candidate,
        track_files={unit.audio_files[0].content_signature: 1},
        offline=True,
    )

    assert paired.partial_plan is not None
    written = [
        operation
        for operation in paired.partial_plan.operations
        if operation.kind is OperationKind.WRITE_IMAGE
    ]
    assert written, "an album whose identity is settled gets its sleeve on the folder"


def test_a_rung_that_answers_with_nothing_convincing_is_a_rung_that_answered_nothing(
    tmp_path: Path,
) -> None:
    """A rung whose answers are all refused does not end the walk.

    If the walk stops at the first rung that returns anything, a loose rung
    returning only other records ends it before the rung that finds the
    album. A later rung can only contribute if it runs.
    """
    unit = _album(tmp_path, "Marina do Acordeão - Forró (2015)")
    # Another record: the names meet nowhere and the lengths do not agree
    # either, which is what the veto is for.
    wrong = _release("wrong", "Somebody Else's Compilation", (12_345, 45_678))
    right = _release("right", "Forró", (400, 900))

    class TwoRungSource(FakeSource):
        """A rung that answers with a record the names refuse, then the album."""

        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            self.searches.append(query)
            if query.text:
                return (_summary("right", "Forró"),)
            return (_summary("wrong", "Somebody Else's Compilation"),)

    source = TwoRungSource(details={"wrong": wrong, "right": right})

    outcome = _workflow(source).identify(unit)

    assert len(source.searches) > 1, "the walk stopped on a rung that convinced nobody"
    assert outcome.candidate is not None
    assert (
        outcome.candidate.release.source_release_id == "right"
    ), "a later rung's answer must be able to win, or walking on buys nothing"


def test_a_rung_that_convinces_ends_the_walk(tmp_path: Path) -> None:
    """The cost of walking on is paid only by albums no rung has convinced.

    Walking every rung of every album would add searches to each one of a
    library scan, against a rate-limited catalogue. A rung whose best answer
    survives the names is the answer, and the walk ends there.
    """
    unit = _album(tmp_path, "Marina do Acordeão - Forró (2015)")
    source = FakeSource(
        summaries=(_summary("r1", "Forró"),),
        details={"r1": _release("r1", "Forró", (400, 900))},
    )

    outcome = _workflow(source).identify(unit)

    assert outcome.decision is not Decision.UNIDENTIFIED
    assert len(source.searches) == 1, "a convincing rung is the last one"


def test_an_answer_nobody_could_check_does_not_end_the_search(tmp_path: Path) -> None:
    """A release with no tracklist cannot end the walk.

    A rung stops the walk when something it brought back convinces. The veto
    decides that, and the veto needs names on both sides to compare, so a
    release that publishes no tracklist at all can never be refused by it.
    Such a release, scored on album-level text alone, must not count as a
    convincing answer.
    """
    unit = _album(tmp_path, "Marina do Acordeão - Forró (2015)")
    right = _release("right", "Forró", (400, 900))

    class BoxSetThenTheAlbum(FakeSource):
        """A rung that answers with a release nothing can be checked against."""

        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            self.searches.append(query)
            if query.text:
                return (_summary("right", "Forró"),)
            return (_summary("boxset", "Jubilee - 13 Volumes Box Set"),)

    source = BoxSetThenTheAlbum(
        details={
            # No tracks, and none to fetch.
            "boxset": ReleaseMetadata(
                source=MetadataSources.DISCOGS,
                source_release_id="boxset",
                title="Jubilee - 13 Volumes Box Set",
                artists=(ArtistMetadata(name="Earl Whitcombe"),),
                tracks=(),
            ),
            "right": right,
        }
    )

    outcome = _workflow(source).identify(unit)

    assert len(source.searches) > 1, (
        "a guess with no per-track evidence stopped the search, and the rung "
        "that finds the album never ran"
    )
    assert outcome.candidate is not None
    assert outcome.candidate.release.source_release_id == "right"
