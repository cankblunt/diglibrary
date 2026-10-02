"""End-to-end tests for the window's API, with every source faked and no window."""

import base64
import hashlib
import logging
import shutil
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.flac import FLAC, Picture

from diglibrary.application.api import (
    _WRITES_FILES,
    LibraryApi,
    _evidence_order,
    _is_witness_page,
    _parse_group_link,
    _parse_release_link,
    _Pipeline,
    _user_spelling_offer,
    _witness_backing_user_titles,
    _witness_knows_better,
)
from diglibrary.application.artwork import ArtworkPolicy, ArtworkService
from diglibrary.application.bench import QualityBench
from diglibrary.application.composition import naming_from_settings
from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataQuery,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.application.identification import Decision, IdentificationWorkflow
from diglibrary.config import credentials
from diglibrary.connectors.models import Transfer, TransferState
from diglibrary.database.connection import Database
from diglibrary.database.library_store import LibraryStore, StoredQuality, word_for
from diglibrary.library.artwork import Artwork, FilesystemArtworkStore, ImageKind, StagedImage
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.audio_key import audio_key
from diglibrary.library.executor import ChangeExecutor, ExecutionState
from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.library.matching import AlbumMatcher
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.naming import DEFAULT_TRANSCODED_LABEL, NamingPolicy
from diglibrary.library.planner import ChangeOperation, ChangePlanner, OperationKind
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.metadata.service import MetadataService
from diglibrary.quality.analysis import PROBE_FREQUENCIES
from diglibrary.quality.verdict import REFERENCE_WALLS, Encoding

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"
ORGANIZED = "Marina do Acordeão - Forró (1955) [FLAC]"


class FakeSource:
    """Answer searches from fixed summaries and detail requests from a map."""

    def __init__(
        self,
        summaries: tuple[ReleaseMetadata, ...] = (),
        details: dict[str, ReleaseMetadata] | None = None,
    ) -> None:
        self._summaries = summaries
        self._details = details or {}
        self.searches: list[MetadataQuery] = []

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        """Record the query and return the configured summaries."""
        self.searches.append(query)
        return self._summaries

    def get_release(self, release_id: str) -> ReleaseMetadata:
        """Return the configured full release."""
        return self._details[release_id]


def _release(identifier: str, durations: tuple[int, ...], title: str = "Forró") -> ReleaseMetadata:
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
        released_on=date(1955, 3, 1),
    )


def _library(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    folder = library / "marina do acordeao - forro"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    return library


def _api(
    tmp_path: Path,
    source: FakeSource,
    witness: object | None = None,
    spectrograms: object | None = None,
    verifier: object | None = None,
    spotify: object | None = None,
) -> LibraryApi:
    """Build the window's API over a temporary database.

    ``witness`` registers a second source as MusicBrainz. The cover lookup
    addresses the archive *through MusicBrainz*, so without a witness that
    call cannot happen here at all.
    """
    logger = logging.getLogger("test.api")
    database = Database(tmp_path / "app" / "library.sqlite3", logger)
    database.initialize()
    store = LibraryStore(database, logger)
    tag_store = MutagenTagStore()
    artwork_store = FilesystemArtworkStore()

    def pipeline(settings: dict[str, object]) -> _Pipeline:
        # The real rule, not a copy of it: a factory that read `naming_style`
        # and nothing else would let a composed arrangement be wired correctly
        # in production and prove nothing here.
        naming = naming_from_settings(
            NamingPolicy.from_style("spotiflac"), settings, logging.getLogger("test.naming")
        )
        matcher = AlbumMatcher(duration_tolerance_ms=100, ordered_tolerance_ms=100)
        planner = ChangePlanner(naming, tag_store, artwork_store)
        # The archive client is never reached: the only method this exercises
        # is the local extraction of embedded artwork, which touches no network.
        artwork = ArtworkService(
            client=None,  # type: ignore[arg-type]
            staging_directory=tmp_path / "app" / "staging",
            policy=ArtworkPolicy(),
            logger=logger,
            store=artwork_store,
        )
        workflow = IdentificationWorkflow(
            metadata=MetadataService(  # type: ignore[arg-type]
                ((MetadataSources.DISCOGS, source),)
                if witness is None
                else ((MetadataSources.DISCOGS, source), (MetadataSources.MUSICBRAINZ, witness))
            ),
            matcher=matcher,
            planner=planner,
            tag_store=tag_store,
            logger=logger,
            threshold=float(settings.get("threshold", 0.9)),
            # Wired, because the cover lookup is where restoring the Library
            # could reach the network, and a factory that leaves it out cannot
            # see that happen.
            artwork=artwork,
            verifier=verifier,
        )
        executor = ChangeExecutor(tag_store, logger, artwork_store, tmp_path / "app" / "backup")
        scanner = LibraryScanner(MutagenAudioProbe(), logger)
        return _Pipeline(
            scanner=scanner,
            workflow=workflow,
            executor=executor,
            threshold=float(settings.get("threshold", 0.9)),
            planner=planner,
            artwork=artwork,
            tag_store=tag_store,
            # No survey: the bench reads what is on record, and measuring is a
            # separate gesture that ffmpeg has to be present for.
            bench=QualityBench(scanner, store, logger),
        )

    return LibraryApi(
        pipeline_factory=pipeline,
        store=store,
        artwork_store=artwork_store,
        logger=logger,
        folder_picker=lambda: str(tmp_path / "library"),
        cover_cache=tmp_path / "app" / "covers",
        # Absent unless a test composes one, which is how the "no ffmpeg"
        # answer is exercised rather than assumed.
        spectrograms=spectrograms,  # type: ignore[arg-type]
        spotify=spotify,  # type: ignore[arg-type]
    )


class _AliveJob:
    """A run that has not finished, for the checks that must stand aside for one."""

    def is_alive(self) -> bool:
        return True


def _finish(job: object, what: str) -> None:
    """Wait for a worker thread, and fail by name if it did not finish.

    `join(timeout=...)` returns whether or not the thread ended, so a job that
    ran long would leave every later assertion reading a half-built album, and
    the failure would surface somewhere else entirely. A slow run must not
    arrive disguised as a bug in the thing being tested.
    """
    job.join(timeout=30)  # type: ignore[attr-defined]
    assert not job.is_alive(), f"the {what} had not finished after 30s"  # type: ignore[attr-defined]


def _finish_reads(api: LibraryApi) -> None:
    """Wait for every folder read, which runs on its own worker."""
    for read in list(api._reads):
        _finish(read, "folder read")


def _scan_and_wait(api: LibraryApi, library: Path) -> None:
    assert api.scan(str(library))["ok"]
    _finish(api._job, "scan")


def _search_and_wait(api: LibraryApi, unit_id: int, **hints: str) -> list[dict[str, object]]:
    """Run a corrected search to completion; it reports through events."""
    assert api.search_again(unit_id, **hints)["ok"]
    _finish(api._search_job, "search")
    searched = [event for event in api.events() if event["type"] == "searched"]
    return list(searched[-1]["payload"].get("candidates", []))


def _snapshot(root: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_file():
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            entries.append((relative, "<dir>"))
    return entries


RIGHT = _release("r2", (400, 900))
"""The release the two fixture files actually are."""


def test_a_folder_put_on_the_bench_survives_and_reports_what_is_known(tmp_path: Path) -> None:
    """The bench is state that outlives a session.

    Measurements already on record are shown without the disk being walked
    again.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())

    assert api.bench_add(str(library))["ok"]
    _finish(api._bench_job, "bench walk")

    state = api.bench_state()
    assert [root["name"] for root in state["roots"]] == ["library"]
    assert [album["folder"] for album in state["albums"]] == ["marina do acordeao - forro"]
    # Nothing was decoded, so nothing is claimed about the audio.
    assert state["albums"][0]["analyzed"] == 0
    assert state["albums"][0]["confidence"] == "weak"
    # And ffmpeg is absent here, which the window is told rather than left to
    # discover by pressing something that does nothing.
    assert state["can_measure"] is False


def test_the_bench_says_what_stopped_it_rather_than_going_quiet(tmp_path: Path) -> None:
    """A folder that is not there is an answer, not a silence."""
    api = _api(tmp_path, FakeSource())

    answer = api.bench_add(str(tmp_path / "nowhere"))

    assert answer["ok"] is False
    assert "Not a folder" in str(answer["error"])


def test_taking_a_folder_off_the_bench_leaves_the_disk_alone(tmp_path: Path) -> None:
    """Removing a root is a screen gesture: the folders and files stay put."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    assert api.bench_add(str(library))["ok"]
    _finish(api._bench_job, "bench walk")
    root_id = api.bench_state()["roots"][0]["id"]

    assert api.bench_remove(root_id)["ok"]

    assert api.bench_state()["albums"] == []
    assert (library / "marina do acordeao - forro").is_dir()


def test_an_album_can_be_sent_to_the_bench_from_the_library(tmp_path: Path) -> None:
    """Sending one album from the Library puts that album on the bench alone.

    Its own folder is what goes on, not the shelf around it — otherwise sending
    one album would drag in every album beside it.
    """
    library = _library(tmp_path)
    # A second album beside it, so "alone" is something this can actually see.
    other = library / "another album"
    other.mkdir()
    shutil.copy(FIXTURES / "tone.flac", other / "ccc.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    album = next(
        entry for entry in api.state()["albums"] if entry["folder"] == "marina do acordeao - forro"
    )

    answer = api.send_to_bench(album["unit_id"])

    assert answer["ok"]
    assert [entry["folder"] for entry in api.bench_state()["albums"]] == [
        "marina do acordeao - forro"
    ]


def test_a_scan_reports_progress_and_a_dashboard(tmp_path: Path) -> None:
    """The window's whole world: events while scanning, then grouped state."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    _scan_and_wait(api, library)

    kinds = [event["type"] for event in api.events()]
    assert "scanned" in kinds and "identified" in kinds and "finished" in kinds
    state = api.state()
    assert len(state["albums"]) == 1
    album = state["albums"][0]
    assert album["decision"] == "automatic"
    assert album["applicable"] is True
    assert album["applied"] is False


def test_the_batch_writes_only_to_the_marked_albums(tmp_path: Path) -> None:
    """The batch writes to the marked albums and to no other automatic album.

    Marking is how the window says *these* everywhere else, so the one gesture
    that writes to files must not be the exception: an unmarked album is
    neither counted nor organized.
    """
    library = _library(tmp_path)
    # A second album, so the marks name one of two rather than being the only
    # thing on the shelf. Its own audio, because two folders holding the same
    # files are one album — a unit is keyed by what it sounds like.
    second = library / "another album"
    second.mkdir()
    shutil.copy(FIXTURES / "tone.flac", second / "aaa.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    albums = api.state()["albums"]
    sure = [album["unit_id"] for album in albums if album["decision"] == "automatic"]
    other = [album["unit_id"] for album in albums if album["decision"] != "automatic"]
    assert len(sure) == 1 and other, "this test needs one sure album and one that is not"

    # Marked: an album this batch has no business touching. Filtering by
    # identity, not merely by the list being empty.
    assert api.apply_automatic(other)["applied"] == 0, "the batch wrote to an unmarked album"
    assert all(not album["applied"] for album in api.state()["albums"])

    result = api.apply_automatic(sure)

    assert result["applied"] == 1
    applied = {album["unit_id"]: album["applied"] for album in api.state()["albums"]}
    assert applied[sure[0]] is True


def test_the_batch_never_writes_to_an_album_whose_folder_is_gone(tmp_path: Path) -> None:
    """An applicable plan against a folder that is not there is not applicable.

    A folder moved or renamed after the plan was made must not be counted by
    the batch, nor reported as a failed write.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    only = [album for album in api.state()["albums"] if album["decision"] == "automatic"]
    assert only, "this test needs an album the app is sure about"
    shutil.rmtree(only[0]["folder_path"])

    result = api.apply_automatic([only[0]["unit_id"]])

    assert result["applied"] == 0
    assert result["failures"] == [], "a folder that is not there is not a failed write"


def test_the_batch_gesture_applies_and_history_reverts_exactly(tmp_path: Path) -> None:
    """One gesture applies, one gesture undoes, byte for byte."""
    library = _library(tmp_path)
    before = _snapshot(library)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    result = api.apply_automatic()

    assert result["ok"] and result["applied"] == 1 and result["failures"] == []
    # The gesture returns its own certificate — the audio provably intact and
    # every write read back and confirmed.
    assert result["certificate"] == {
        "audio_checked": 1,
        "audio_intact": 1,
        "writes_checked": 1,
        "writes_verified": 1,
    }
    assert result["run_id"]
    assert (
        library / ORGANIZED / "01. Primeira.flac"
    ).exists() is False  # renamed by release titles
    assert (library / ORGANIZED).is_dir()
    history = api.state()["history"]
    assert len(history) == 1 and history[0]["state"] == "applied"

    reverted = api.revert(history[0]["plan_id"])

    assert reverted["ok"] is True
    assert _snapshot(library) == before
    assert api.state()["history"][0]["state"] == "reverted"


def test_the_trail_survives_a_restart(tmp_path: Path) -> None:
    """Reverting must not depend on the process that applied."""
    library = _library(tmp_path)
    before = _snapshot(library)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()
    plan_id = api.state()["history"][0]["plan_id"]

    reopened = _api(tmp_path, source)

    assert reopened.revert(plan_id)["ok"] is True
    assert _snapshot(library) == before


def test_review_can_reject_search_again_and_adopt(tmp_path: Path) -> None:
    """The three review powers, driven exactly as the window would."""
    library = _library(tmp_path)
    wrong = _release("r1", (12_000, 47_000), title="Something Else")
    right = _release("r2", (400, 900))
    source = FakeSource(
        summaries=(wrong,),
        details={"r1": wrong, "r2": right},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api.state()["albums"][0]
    assert state["decision"] == "review"
    unit_id = state["unit_id"]

    # The source starts answering with both once the user corrects the search.
    source._summaries = (wrong, right)
    candidates = _search_and_wait(api, unit_id, album="Forró")
    assert len(candidates) == 2
    best = candidates[0]
    assert best["release_id"] == "r2"

    adopted = api.adopt(unit_id, best["source"], best["release_id"])
    assert adopted["ok"] and adopted["album"]["applicable"]

    approved = api.approve(unit_id)
    assert approved["ok"], approved
    assert (library / ORGANIZED).is_dir()


def test_reject_leaves_the_files_alone(tmp_path: Path) -> None:
    """Rejecting is a decision about metadata, never about the audio."""
    library = _library(tmp_path)
    before = _snapshot(library)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.reject(unit_id)["ok"]

    assert _snapshot(library) == before
    assert api.state()["albums"][0]["rejected"] is True
    assert api.apply_automatic()["applied"] == 0, "a rejected album is never batch-applied"


def test_settings_rebuild_the_pipeline_and_survive_a_restart(tmp_path: Path) -> None:
    """The window's settings persist in the database and change real behavior."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    saved = api.set_settings({"naming_style": "minimal", "threshold": 0.95})
    assert saved["ok"]

    _scan_and_wait(api, library)
    api.apply_automatic()
    assert (library / "Marina do Acordeão - Forró").is_dir(), "the minimal style named it"

    reopened = _api(tmp_path, source)
    assert reopened.settings()["naming_style"] == "minimal"
    assert reopened.settings()["threshold"] == 0.95


def test_an_excluded_subfolder_is_never_read(tmp_path: Path) -> None:
    """Exclusion is absence — the subtree is pruned during the walk."""
    library = _library(tmp_path)
    shared = library / "Soulseek_shared" / "someone - album"
    shared.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", shared / "01.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    assert api.scan(str(library), excluded=["Soulseek_shared"])["ok"]
    _finish(api._job, "scan")

    state = api.state()
    assert len(state["albums"]) == 1, "the excluded subtree contributed nothing"
    listed = api.subfolders(str(library))
    assert {"name": "Soulseek_shared", "excluded": True} in listed[
        "folders"
    ], "the exclusion is remembered per root"


def test_the_scan_declares_its_reach(tmp_path: Path) -> None:
    """The reach panel states what the scan may touch, in plain numbers."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    reach = api.state()["reach"]
    assert reach["folders"] == 1
    assert reach["files"] == 2
    assert ".flac" in reach["extensions"]
    assert "write_tags" in reach["writes"]


def test_holding_pulls_an_automatic_album_back_into_review(tmp_path: Path) -> None:
    """Holding is not rejecting — the answer may be right."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    held = api.hold(unit_id)

    assert held["ok"] and held["album"]["decision"] == "review"
    assert api.apply_automatic()["applied"] == 0, "a held album is not applied by the batch"


def test_a_correction_reshapes_the_plan_and_is_remembered(tmp_path: Path) -> None:
    """The user outranks the catalogue, and the record says so."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    corrected = api.correct(
        unit_id, folder_name="Marina - Forró [mine]", track_titles={"1": "Tral de Quinde"}
    )

    assert corrected["ok"]
    operations = corrected["album"]["operations"]
    folder_renames = [op for op in operations if op["kind"] == "rename_folder"]
    assert folder_renames and folder_renames[0]["after"]["path"].endswith("Marina - Forró [mine]")
    assert any(
        "Tral de Quinde" in str(op["after"]) for op in operations
    ), "the corrected title reaches the tags and the names"
    stored = corrected["album"]["corrections"]
    assert {
        "field": "folder_name",
        "track_position": None,
        "value": "Marina - Forró [mine]",
        "replaced": "Marina do Acordeão - Forró (1955) [FLAC]",
        # A folder name answers to no release, so it is stored against none:
        # only a pairing names a position on a particular tracklist.
        "release_key": None,
    } in stored, "a folder correction records the name it overruled"
    assert any(
        entry["field"] == "track_title"
        and entry["value"] == "Tral de Quinde"
        and entry["replaced"] == "Faixa 1"
        for entry in stored
    ), "a correction records what it replaced"


def test_scanning_one_album_organizes_it_and_the_root_follows_its_name(tmp_path: Path) -> None:
    """Pointing the app at one album is a normal gesture, and it may rename it.

    The folder that moved is the folder the window calls the root, so everything
    that reads that path afterwards — the header, "Open folder", the next scan —
    has to move with it.
    """
    album = tmp_path / "marina do acordeao - forro"
    album.mkdir()
    shutil.copy(FIXTURES / "tone.flac", album / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "bbb.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, album)

    albums = api.state()["albums"]
    assert len(albums) == 1, "one album, not one album per track"
    assert albums[0]["tracks"] == 2
    assert api.approve(albums[0]["unit_id"])["ok"]

    organized = tmp_path / "Marina do Acordeão - Forró (1955) [FLAC]"
    assert organized.is_dir()
    assert api.state()["root"] == str(organized)


def test_a_second_approve_of_a_plan_that_just_landed_writes_nothing_and_is_not_an_error(
    tmp_path: Path,
) -> None:
    """Two presses of Apply are one apply.

    Two `approve` calls for one album can arrive together; the second waits on
    the executor's lock and would then rehearse a plan the first has just
    carried out, reporting every file of a freshly organized album as gone.
    Sequential here is the same order: the second request is the one that runs
    after the first has let go.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    organized = _snapshot(library)

    again = api.approve(unit_id)

    assert again == {"ok": True, "error": None, "partial": False}
    assert _snapshot(library) == organized, "nothing was written the second time"


def test_an_empty_plan_is_not_taken_for_one_already_applied(tmp_path: Path) -> None:
    """A plan of no operations has an empty trail before it runs, not only after.

    The repeated-apply guard compares the trail's length with the plan's, and
    zero equals zero: an album already exactly as it should be, holding a plan
    id, was answered `ok` and never marked organized.
    """
    first = tmp_path / "first"
    library = _library(first)
    release = _release("r1", (400, 900))
    api = _api(first, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    assert api.approve(api.state()["albums"][0]["unit_id"])["ok"]
    second = tmp_path / "second"
    shutil.copytree(library, second / "library")
    api = _api(second, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, second / "library")
    unit_id = api.state()["albums"][0]["unit_id"]
    state = api._albums[unit_id]
    assert state.outcome.plan is not None and not state.outcome.plan.operations
    state.plan_id = api._store.record_plan(unit_id, None, ())

    assert api.approve(unit_id)["ok"]

    assert state.written and state.applied, "the album already right was not marked organized"


def test_a_hand_renamed_album_survives_a_scan_of_the_whole_library(tmp_path: Path) -> None:
    """What this app already organized is left alone, however it looks now.

    A folder renamed by hand after an apply must survive a scan of the whole
    library: the second scan must not plan the catalogue's name over the typed
    one, where one "Apply automatic" would take it.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    organized_folder = next(path for path in library.iterdir() if path.is_dir())
    by_hand = organized_folder.rename(library / "Marina — my own way")

    _scan_and_wait(api, library)

    album = api.state()["albums"][0]
    assert album["organized"] is True
    assert album["folder"] == "Marina — my own way"
    assert album["cover_pending"] is False, "leaving it alone includes not writing a cover"
    before = sorted(path.name for path in by_hand.iterdir())
    assert api.apply_automatic()["applied"] == 0, "an organized album is never in a batch"
    assert by_hand.is_dir(), "the scan must not have touched the folder"
    assert sorted(path.name for path in by_hand.iterdir()) == before

    resumed = api.plan_anyway(album["unit_id"])

    assert resumed["ok"] and resumed["album"]["organized"] is False
    renames = [
        operation
        for operation in resumed["album"]["operations"]
        if operation["kind"] == "rename_folder"
    ]
    assert renames, "asked by name, it plans the album again"


def test_an_album_follows_its_files_even_when_its_folder_keeps_its_name(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The files move even when the folder does not, and memory has to move with them.

    Everything that reaches for this album by path — its embedded cover, the
    bench, the tag read behind `Plan this album again` — asks the copy in
    memory. If that copy followed only a folder rename, an album already named
    correctly would go on naming files the apply had just replaced, and every
    such read would fail about files that are sitting right there.
    """
    library = tmp_path / "Marina do Acordeão - Forró (1955) [FLAC]"
    library.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", library / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", library / "bbb.flac")
    for track in sorted(library.glob("*.flac")):
        _embed(track, jpeg(640, 640))
    (library / "seed.jpg").unlink()
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.approve(unit_id)["ok"]

    assert library.is_dir(), "this album's folder was already named correctly"
    assert sorted(path.name for path in library.glob("*.flac")) == [
        "01. Faixa 1.flac",
        "02. Faixa 2.flac",
    ], "and its files were renamed"
    held = api._albums[unit_id].unit
    missing = [file.path for file in held.audio_files if not file.path.is_file()]
    assert missing == [], "so the album in memory must name the files that now exist"


def test_an_album_planned_again_and_found_right_is_still_organized(tmp_path: Path) -> None:
    """Answering a question perfectly must not cost the album its own word.

    A plan that comes back empty means there is nothing to change. The card
    must go on saying `Organized` rather than `Auto`, and keep `Plan this album
    again` in its context menu, which is offered to what is organized or
    applied.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.state()["albums"][0]["organized"] is True

    resumed = api.plan_anyway(unit_id)

    assert resumed["ok"]
    assert resumed["album"]["operations"] == [], "the album is already exactly as it should be"
    assert resumed["album"]["organized"] is True, "so it is still an organized album"


def test_cancel_takes_the_gesture_back_and_leaves_typed_words_alone(tmp_path: Path) -> None:
    """`Cancel` undoes the gesture and nothing else.

    A typed name is recorded against the album and outranks the catalogue; it
    is not part of what a re-plan replaced, so taking the re-plan back does not
    take it away. It is out of sight while the album is back to what it was,
    and it is there again the moment the album is planned again.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.plan_anyway(unit_id)["ok"]
    assert api.correct(unit_id, folder_name="A Name Typed By Hand")["ok"]

    cancelled = api.cancel(unit_id)

    assert cancelled["ok"]
    assert cancelled["kept_words"] is True, (
        "the screen has to say the typed word is kept, because this is the one "
        "moment it leaves the screen"
    )
    with api._store._database.connect() as connection:
        kept = connection.execute(
            "SELECT value FROM manual_corrections WHERE album_unit_id = ? "
            "AND field = 'folder_name'",
            (unit_id,),
        ).fetchall()
    assert [row[0] for row in kept] == ["A Name Typed By Hand"], "the word is still recorded"

    again = api.plan_anyway(unit_id)

    assert again["ok"]
    assert "A Name Typed By Hand" in str(
        again["album"]["operations"]
    ), "and it is what the album is planned as the next time it is asked"


def test_a_cancel_that_left_nothing_behind_says_nothing_extra(tmp_path: Path) -> None:
    """The sentence is offered where it is true and nowhere else.

    A correction typed **before** the gesture is already inside the reading being
    put back, so it never leaves the screen and needs no explaining. Saying it
    anyway would be a notice about nothing.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.plan_anyway(unit_id)["ok"]

    assert api.cancel(unit_id)["kept_words"] is False, "nothing was typed after the gesture"

    # And the other half: a word typed **before** the gesture is inside the
    # reading being put back, so it never leaves the screen either.
    assert api.correct(unit_id, folder_name="Typed First")["ok"]
    assert api.scan_selected([unit_id])["ok"]
    _finish(api._job, "scan")

    assert (
        api.cancel(unit_id)["kept_words"] is False
    ), "a word already inside the reading being put back does not leave the screen"


def test_rejecting_an_organized_album_is_refused_and_says_what_fits(tmp_path: Path) -> None:
    """`skipped` over an organized album is the screen contradicting the disk.

    Two readers translate that word as *rejected*, so the card would say so about
    files that are organized — and `ids_in_state("organized")` would stop
    protecting the album, which is the promise that a name typed by hand
    survives a scan of the whole library. The refusal names the two gestures
    that do fit.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.state()["albums"][0]["organized"] is True
    assert api.plan_anyway(unit_id)["ok"], "the dialog opened by mistake"

    refused = api.reject(unit_id)

    assert refused["ok"] is False
    assert "Cancel" in refused["error"] and "Revert" in refused["error"]
    assert api._albums[unit_id].rejected is False
    with api._store._database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM album_units WHERE id = ?", (unit_id,)).fetchone()[
                0
            ]
            == "organized"
        ), "the album keeps the word that makes a scan leave it alone"
    assert unit_id in api._store.ids_in_state(
        "organized"
    ), "and it is still in the set a scan leaves alone"


def test_rejecting_is_refused_while_this_apps_names_are_on_disk(tmp_path: Path) -> None:
    """The guard reads the word the database holds, not the flag in memory.

    `state.organized` is cleared the moment a re-plan has something to write,
    which is the normal case — so on an organized album wearing a fresh
    proposal the flag is already False. What must be asked is the word on
    record: it survives the re-plan, and it is the word `ids_in_state` reads to
    leave a finished album alone.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.plan_anyway(unit_id)["ok"]
    assert api.correct(unit_id, folder_name="Something To Write")["ok"]
    assert api._albums[unit_id].organized is False, "the flag is gone, by design"
    with api._store._database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM album_units WHERE id = ?", (unit_id,)).fetchone()[
                0
            ]
            == "identified"
        ), "and so is the row, which is why neither of them can be the guard"
    assert api.album(unit_id)["album"]["organized_on_record"] is True, "the disk still is"

    refused = api.reject(unit_id)

    assert refused["ok"] is False
    assert "Cancel" in refused["error"] and "Revert" in refused["error"]
    assert api._albums[unit_id].rejected is False
    with api._store._database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM album_units WHERE id = ?", (unit_id,)).fetchone()[
                0
            ]
            != "skipped"
        ), "the organized album is not dropped out of the run"


def test_rejecting_an_album_in_review_is_untouched(tmp_path: Path) -> None:
    """The refusal is about finished albums and nothing else.

    On an album in review `Reject` means what it has always meant: this release
    is not this record, the files are left alone, the album drops out of the run.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api._albums[unit_id].organized is False

    assert api.reject(unit_id)["ok"] is True

    assert api._albums[unit_id].rejected is True
    with api._store._database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM album_units WHERE id = ?", (unit_id,)).fetchone()[
                0
            ]
            == "skipped"
        )


def test_arranging_by_tags_does_not_unlearn_that_it_was_looked_up(tmp_path: Path) -> None:
    """Arranging by tags keeps the fact that a catalogue answered.

    `looked_at` answers *has a catalogue spoken about this album*. The
    arrangement asks nobody anything, so setting the flag from it would make an
    identified album come out of the gesture with a card saying `not scanned`.

    The other case is kept by the test below: an album with no identification
    on record is exactly what `not scanned` means.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo - vesperal")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api._store.identification_for(unit_id) is not None, "a catalogue answered"

    arranged = api.arrange_by_tags(unit_id)

    assert arranged["ok"], arranged.get("error")
    assert (
        arranged["album"]["looked_at"] is True
    ), "the album is one this app has looked up, whatever this proposal came from"


def test_cancelling_a_replan_gives_back_the_album_that_was_there(tmp_path: Path) -> None:
    """A finished album re-planned by mistake can be put back as it was.

    `Approve & apply` writes to the files, and `Reject` records the
    identification as wrong and drops the album to `skipped` — so neither leads
    back to *finished*. `Cancel` does, and what it puts back is the album, not
    a screen: the word on the row, the word in the database, and the rows the
    gesture wrote.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.state()["albums"][0]["organized"] is True
    resumed = api.plan_anyway(unit_id)
    assert resumed["ok"]
    assert resumed["album"]["can_cancel"] is True, "the way back is offered where it exists"
    planned_identification = api._albums[unit_id].identification_id

    cancelled = api.cancel(unit_id)

    assert cancelled["ok"]
    assert cancelled["album"]["organized"] is True, "the album that was there is back"
    assert cancelled["album"]["can_cancel"] is False, "and there is nothing left to take back"
    assert api._albums[unit_id].planned_again is False, "its names are not open for typing again"
    with api._store._database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM album_units WHERE id = ?", (unit_id,)).fetchone()[
                0
            ]
            == "organized"
        ), "and the row keeps the word the badge reads"
        assert (
            connection.execute(
                "SELECT state FROM identifications WHERE id = ?", (planned_identification,)
            ).fetchone()[0]
            == "superseded"
        ), "the withdrawn attempt is not what this album is any more"
    # The plan is the other row a gesture writes, and this album is already
    # exactly as it should be — re-planning it produces nothing to record, which
    # is the best answer there is. What happens to a plan that *was* written is
    # in `test_cancelling_a_rescan_puts_the_reading_it_replaced_back`.
    assert api._albums[unit_id].plan_id is None


def test_a_cancelled_replan_does_not_come_back_at_the_next_start(tmp_path: Path) -> None:
    """A withdrawal that only the window knows about is a withdrawal for one session.

    `identification_for` reads the newest identification that is not
    `superseded`, so an attempt left as `proposed` would be what the album *is*
    the next time the Library is read back.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    stood = api._store.identification_for(unit_id)
    assert api.plan_anyway(unit_id)["ok"]
    assert api._store.identification_for(unit_id)["identification_id"] != stood["identification_id"]

    assert api.cancel(unit_id)["ok"]

    assert api._store.identification_for(unit_id)["identification_id"] == (
        stood["identification_id"]
    ), "what the album is, is what it was before the gesture"


def test_an_album_with_nothing_to_take_back_is_told_so(tmp_path: Path) -> None:
    """The way back is offered where it exists, and says so where it does not.

    A control that is always there and does nothing most of the time is
    refused: the way back is drawn only where there is something to take back.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.album(unit_id)["album"]["can_cancel"] is False

    refused = api.cancel(unit_id)
    assert refused["ok"] is False
    assert "nothing to take back" in refused["error"]


def test_an_applied_album_is_reverted_rather_than_cancelled(tmp_path: Path) -> None:
    """Once a gesture has reached the files, the way back is the one that undoes files.

    `Cancel` puts a proposal back and touches nothing on disk. Offering it over
    an album that has been written would be offering a promise it cannot keep,
    and `Revert` is the gesture that can.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.plan_anyway(unit_id)["ok"]

    assert api.approve(unit_id)["ok"], "the second plan was approved instead of taken back"

    assert api.album(unit_id)["album"]["can_cancel"] is False
    refused = api.cancel(unit_id)
    assert refused["ok"] is False
    assert "nothing to take back" in refused["error"]


def test_cancelling_a_rescan_puts_the_reading_it_replaced_back(tmp_path: Path) -> None:
    """Cancelling a re-scan returns the album to the reading of the scan before.

    A re-scan builds a **new** `_AlbumState` and remembers it over the old one,
    so the reading it replaced is gone from the map the moment it lands — which
    is why the snapshot is taken before the thread starts and kept outside the
    album it is of.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    first = api._albums[unit_id]
    assert first.looked_at is True, "there is a reading to go back to"

    assert api.scan_selected([unit_id])["ok"]
    _finish(api._job, "scan")

    assert api._albums[unit_id] is not first, "the re-scan replaced the album in the map"
    assert api.album(unit_id)["album"]["can_cancel"] is True
    replaced_plan = api._albums[unit_id].plan_id
    cancelled = api.cancel(unit_id)
    assert cancelled["ok"]
    assert cancelled["gesture"] == "scanned_again"
    assert (
        api._albums[unit_id].identification_id == first.identification_id
    ), "the album on screen is the one the re-scan replaced"
    assert replaced_plan not in (None, first.plan_id), "the re-scan wrote a plan of its own"
    with api._store._database.connect() as connection:
        assert (
            connection.execute(
                "SELECT state FROM change_plans WHERE id = ?", (replaced_plan,)
            ).fetchone()[0]
            == "discarded"
        ), "and it is not left waiting to be applied over an album that was taken back"


def test_scanning_an_organized_album_leaves_it_exactly_as_it_is(tmp_path: Path) -> None:
    """Why `Scan this album` is not offered to one, and `Plan this album again` is.

    Scanning an organized album changes nothing at all, while planning again
    is the only gesture that gives the album a plan. That is what makes a
    rename made by hand survive a scan of the whole library, and it is why
    only one of the two appears on an organized album.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.state()["albums"][0]["organized"] is True

    assert api.scan_selected([unit_id])["ok"]
    _finish(api._job, "scan")

    scanned = api.album(unit_id)["album"]
    assert scanned["organized"] is True, "a scan does not release an organized album"
    assert (
        scanned["operations"] == [] and scanned["applicable"] is False
    ), "and it hands it no plan, which is what keeps it out of every batch"
    assert api.plan_anyway(unit_id)["ok"], "the gesture that does reach it is the other one"


def test_the_row_keeps_the_word_a_re_plan_did_not_take(tmp_path: Path) -> None:
    """A re-plan that changes nothing leaves `organized` on the row as well.

    Every path that re-plans one album by hand ends in `_persist_outcome`. If
    that wrote `identified` or `needs_review` over the row, planning an
    organized album again and being told *nothing would change* would demote
    it for the next start, while the screen still said `Organized`.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api._store.unit_by_id(unit_id).state == "organized"

    resumed = api.plan_anyway(unit_id)

    assert resumed["ok"] and resumed["album"]["operations"] == []
    assert resumed["album"]["organized"] is True, "the screen kept the word"
    assert (
        api._store.unit_by_id(unit_id).state == "organized"
    ), "and so must the row, or the next start reads an album waiting for review"


def test_withdrawing_an_edit_gives_the_album_its_word_back(tmp_path: Path) -> None:
    """An edit that is withdrawn must not cost the album its word.

    A typed name gives an organized album something to write, so it stops
    being one that needs nothing — and putting the catalogue's name back leaves
    a plan that changes nothing again. The album must then be `Organized`
    again, not stay out of it over an edit that no longer exists.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    assert api.state()["albums"][0]["organized"] is True

    edited = api.correct(unit_id, "Marina — my own way")
    assert edited["ok"] and edited["album"]["organized"] is False

    withdrawn = api.correct(unit_id, "")

    assert withdrawn["ok"]
    assert withdrawn["album"]["operations"] == [], "the plan changes nothing again"
    assert withdrawn["album"]["organized"] is True, "so the album is organized again"
    assert api._store.unit_by_id(unit_id).state == "organized"


def test_a_run_says_what_it_read_and_left_exactly_as_it_was(tmp_path: Path) -> None:
    """A run over an organized album reports that it left the album alone.

    The mark is on every card, so an organized album can be marked and
    scanned. The run reads the folder, matches it and hands it no plan, and
    the ending must not count it among the albums it identified.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    api.events()

    assert api.scan_selected([unit_id])["ok"]
    _finish(api._job, "scan")

    finished = [event for event in api.events() if event["type"] == "finished"]
    assert finished, "a run that says nothing at the end is the silence this is about"
    assert (
        finished[-1]["payload"]["left_alone"] == 1
    ), "the run has to say it left the album exactly as it was"


def test_a_title_typed_on_an_organized_album_keeps_its_file_and_is_applied(
    tmp_path: Path,
) -> None:
    """Typing a title that resembles no file must leave that file on its track.

    An organized album's files carry the names the catalogue published. A title
    typed over one of them is the name to write, and if it were also the name
    the files are compared with, the file it was typed for would answer to no
    track: the plan would hold nothing and `Approve & apply` would be refused.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    assert api.plan_anyway(unit_id)["ok"]

    answered = api.correct(unit_id, track_titles={"1": "A Title Typed Now"})

    album = answered["album"]
    assert album["blockers"] == [], "the typed title took the file away from its track"
    assert any(
        operation["kind"] == "rename_file" for operation in album["operations"]
    ), "there is nothing to approve after typing a title"
    assert api.approve(unit_id)["ok"]
    names = sorted(path.name for folder in library.iterdir() for path in folder.iterdir())
    assert names == ["01. A Title Typed Now.flac", "02. Faixa 2.flac"]


def test_planning_again_goes_back_to_the_release_chosen_by_hand(tmp_path: Path) -> None:
    """The album opens as it was applied, on the release that was applied.

    A search answers with the release that ranks first, and here that is not
    the one in use: another was adopted by hand before approving. Planning
    again by searching would propose renaming the finished album to the release
    that had been turned down. Asked in the same session and after a restart,
    because the two read the album from different places.
    """
    library = _library(tmp_path)
    first = _release("r1", (400, 900))
    chosen = _release("r2", (400, 900), title="A Second Pressing")
    source = FakeSource(summaries=(first,), details={"r1": first, "r2": chosen})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.adopt(unit_id, "discogs", "r2")["ok"]
    assert api.approve(unit_id)["ok"]
    asked = len(source.searches)

    again = api.plan_anyway(unit_id)

    assert again["ok"]
    assert again["album"]["release_id"] == "r2", "it went back to the release the search prefers"
    assert again["album"]["operations"] == [], "and nothing is proposed against what was applied"
    assert len(source.searches) == asked, "no catalogue is asked by this gesture"
    assert api.cancel(unit_id)["ok"]

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    later = reopened.plan_anyway(unit_id)

    assert later["ok"]
    assert later["album"]["release_id"] == "r2"
    assert later["album"]["operations"] == []
    assert len(source.searches) == asked


def test_planning_again_opens_the_names_while_no_catalogue_answers(tmp_path: Path) -> None:
    """A catalogue that is down does not close a finished album's names.

    The release is on record and so is everything typed over it, so the editor
    has what it needs with nobody answering: the tracks are drawn, the typed
    title is in its field, and the album goes on being organized.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    source = FakeSource(summaries=(release,), details={"r1": release})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.correct(unit_id, track_titles={"1": "A Title Typed Now"})["ok"]
    assert api.approve(unit_id)["ok"]

    def unreachable(query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        raise OSError("the catalogue is not answering")

    source.search_releases = unreachable  # type: ignore[method-assign]
    source.get_release = unreachable  # type: ignore[method-assign,assignment]

    again = api.plan_anyway(unit_id)

    assert again["ok"], again.get("error")
    album = again["album"]
    assert album["release_id"] == "r1"
    assert len(album["release_tracks"]) == 2, "the editor has no tracks to draw"
    assert album["asleep"] is False, "the names are still closed"
    assert album["organized"] is True and album["operations"] == []
    assert [
        entry["value"] for entry in album["corrections"] if entry["field"] == "track_title"
    ] == ["A Title Typed Now"]


def test_an_organized_album_nobody_answers_for_stays_organized_on_its_row(
    tmp_path: Path,
) -> None:
    """Asking and hearing nothing is not a reason to forget the album is done.

    With no release on record this gesture asks a catalogue, and a catalogue
    may answer nothing. The album's row is what the next start reads: written
    as `discovered` there, an album whose files are in place would come back as
    one never organized, in reach of the next scan.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    source = FakeSource(summaries=(release,), details={"r1": release})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    # Nothing stands on record for it, and nothing answers when asked.
    with api._store._database.connect(write=True) as connection:
        connection.execute(
            "UPDATE identifications SET state = 'superseded' WHERE album_unit_id = ?", (unit_id,)
        )
    source._summaries = ()

    again = api.plan_anyway(unit_id)

    assert again["ok"], again.get("error")
    assert again["album"]["release_id"] is None, "this is the case with nobody answering"
    with api._store._database.connect() as connection:
        row = connection.execute(
            "SELECT state FROM album_units WHERE id = ?", (unit_id,)
        ).fetchone()
    assert row[0] == "organized", "the row forgot an album whose files are in place"

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    assert reopened.state()["albums"][0]["organized"] is True


def test_a_gesture_that_says_nothing_about_the_folder_leaves_its_name_standing(
    tmp_path: Path,
) -> None:
    """Confirming a pairing or keeping one spelling does not withdraw a folder name.

    Called the way the window calls them: positionally, with nothing in the
    folder's place. Only the field emptied withdraws the name, and that is an
    empty string.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    source = FakeSource(summaries=(release,), details={"r1": release})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.correct(unit_id, "A Name Typed By Hand")["ok"]
    signature = str(_row_for(_opened(api, unit_id), 2)["signature"])

    def typed_folder() -> list[str]:
        with api._store._database.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT value FROM manual_corrections WHERE album_unit_id = ? "
                    "AND field = 'folder_name'",
                    (unit_id,),
                )
            ]

    paired = api.correct(unit_id, None, None, None, {"2": signature})

    assert paired["ok"], paired.get("error")
    assert typed_folder() == ["A Name Typed By Hand"], "confirming a pairing withdrew it"
    assert "A Name Typed By Hand" in str(paired["album"]["operations"])

    spelled = api.correct(unit_id, None, {"1": "A Spelling Kept"})

    assert spelled["ok"]
    assert typed_folder() == ["A Name Typed By Hand"], "keeping a spelling withdrew it"

    assert api.correct(unit_id, "")["ok"]
    assert typed_folder() == [], "the field emptied is still how the name is withdrawn"


def test_an_organized_album_still_takes_a_typed_name(tmp_path: Path) -> None:
    """An answer of "nothing would change" must leave something to act on.

    `Plan this album again` on an organized album comes back with an empty
    plan, which is the right answer. The restore marks an organized album
    applied as well, so a correction must not be refused with "already
    applied; revert it first", or the gesture has no point.

    The album takes the typed name, re-plans around it, and stops calling
    itself organized the moment there is something to write — which is what
    puts `Approve & apply` back on screen. Nothing is written here.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    organized = api.state()["albums"][0]
    assert organized["organized"] is True and organized["unit_id"] == unit_id

    resumed = api.plan_anyway(unit_id)
    assert resumed["ok"] and resumed["album"]["operations"] == []
    assert resumed["album"]["release_tracks"], "the names are what the claim is checked against"

    answered = api.correct(unit_id, "Marina — my own way")

    assert answered["ok"], "a typed name for an organized album is not a refusal"
    album = answered["album"]
    assert any(
        operation["kind"] == "rename_folder" for operation in album["operations"]
    ), "and it is planned, so there is something to approve"
    assert album["organized"] is False, "an album with something to write needs something"
    assert album["applied"] is False, "so `Approve & apply` is offered and not disabled"
    assert api.approve(unit_id)["ok"], "and it writes exactly what the table showed"
    assert (library / "Marina — my own way").is_dir()


def test_an_album_that_is_only_a_row_can_still_be_pointed_at_its_folder(tmp_path: Path) -> None:
    """The one gesture a marked album has must work for a marked album.

    The row of an album whose folder does not answer is kept, and its card is
    marked `not where it was`. Such an album is never read back into memory —
    that is the whole reason it is marked — so a way out that asks memory
    first answers "Unknown album" or "That album is not on screen", and an
    album renamed by hand in the file manager is drawn on the shelf and
    unreachable by every gesture that could repair it.

    The row carries everything this needs: where the album was, and which audio
    it is.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    scanned = library / "marina do acordeao - forro"

    # Renamed by hand, to a name this app's own search structurally cannot find,
    # and forgotten by the window — which is the state a restart leaves it in.
    renamed = library / "Marina do Acordeão - Forró (1955) [FLAC]"
    scanned.rename(renamed)
    with api._albums_lock:
        api._albums.pop(unit_id)
    assert api.album(unit_id)["ok"] is False, "its card cannot be opened, which is the point"

    answered = api.relocate_album(unit_id, str(renamed))

    assert answered["ok"], answered.get("error")
    assert answered["album"]["folder"] == renamed.name
    assert answered["album"]["folder_missing"] is False
    assert api.album(unit_id)["ok"], "and the album is back the way every other one is"


def test_a_folder_holding_another_record_is_refused_from_the_row_too(tmp_path: Path) -> None:
    """The audio decides, whether or not the album is in memory.

    The refusal is what keeps `Relocate…` safe: pointing at the wrong folder
    would attach this album's identification, plan and corrections to music they
    are not about. The signature is read off the copy in memory, and off the
    row when there is no copy — the same test, from the same fact.
    """
    library = _library(tmp_path)
    other = library / "another record"
    other.mkdir()
    shutil.copy(FIXTURES / "tone-long.flac", other / "only.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = next(
        album["unit_id"]
        for album in api.state()["albums"]
        if album["folder"] == "marina do acordeao - forro"
    )
    with api._albums_lock:
        api._albums.pop(unit_id)

    answered = api.relocate_album(unit_id, str(other))

    assert answered["ok"] is False
    assert "different album" in str(answered["error"])


def test_a_correction_outlives_the_scan_that_made_it(tmp_path: Path) -> None:
    """A correction overrules the catalogue, and it does not expire.

    A correction that is stored and then read by nothing lets the next scan
    plan the catalogue's name over the typed one.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.correct(unit_id, folder_name="Marina - Forró [mine]")["ok"]

    _scan_and_wait(api, library)

    album = _opened(api, api.state()["albums"][0]["unit_id"])
    assert album["planned_folder"] == "Marina - Forró [mine]"
    assert [entry["value"] for entry in album["corrections"]] == ["Marina - Forró [mine]"]


def test_submitting_the_form_unchanged_corrects_nothing(tmp_path: Path) -> None:
    """The fields open filled, so only what differs is a correction.

    Otherwise opening the form and pressing the button would freeze this app's
    own proposal as the user's decision, and every later re-identification
    would be overruled by a name nobody chose.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    album = api.state()["albums"][0]
    unit_id = album["unit_id"]
    opened = api.album(unit_id)["album"]

    resubmitted = api.correct(
        unit_id,
        folder_name=opened["planned_folder"],
        track_titles={str(track["position"]): track["title"] for track in opened["release_tracks"]},
    )

    assert resubmitted["ok"]
    assert resubmitted["album"]["corrections"] == []
    assert resubmitted["album"]["planned_folder"] == opened["planned_folder"]


def test_a_correction_survives_being_resubmitted(tmp_path: Path) -> None:
    """Reopening a corrected album and saving again must keep the correction.

    The release in memory carries the corrected title by then, so comparing the
    field against it would read as "same as the catalogue" and withdraw the very
    correction being resubmitted. What it is compared against is what it
    replaced.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    api.correct(unit_id, folder_name="Marina - Forró [mine]", track_titles={"1": "Tral de Quinde"})

    again = api.album(unit_id)["album"]
    resubmitted = api.correct(
        unit_id,
        folder_name=again["planned_folder"],
        track_titles={str(track["position"]): track["title"] for track in again["release_tracks"]},
    )

    stored = resubmitted["album"]["corrections"]
    assert any(entry["value"] == "Marina - Forró [mine]" for entry in stored)
    assert any(entry["value"] == "Tral de Quinde" for entry in stored)


def test_a_pasted_link_identifies_the_album(tmp_path: Path) -> None:
    """A release the user found themselves, given by its public URL."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(),
        details={"10203040": _release("10203040", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    adopted = api.adopt_link(unit_id, "https://www.discogs.com/release/10203040-Whatever")

    assert adopted["ok"]
    assert adopted["album"]["release_id"] == "10203040"
    assert adopted["album"]["url"] == "https://www.discogs.com/release/10203040"

    nonsense = api.adopt_link(unit_id, "https://example.com/not-a-release")
    assert not nonsense["ok"]


def test_a_pasted_spotify_link_searches_the_catalogues_and_adopts_nothing(
    tmp_path: Path,
) -> None:
    """A Spotify link is searched for, never adopted.

    A Spotify link cannot *be* an identification — Spotify is not a catalogue
    this application may quote, and no value of its own may be written
    anywhere. So the same field that adopts a Discogs release instead
    *searches* Discogs and MusicBrainz with the words Spotify supplied, and
    what comes back are candidates that still have to be chosen from.

    Asserted on the query the catalogue actually received, because a pointer
    that resolves while nothing is searched would pass otherwise.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source, spotify=_FakeSpotify("Lantern", "Nora Velling"))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    source.searches.clear()

    answered = api.adopt_link(unit_id, "https://open.spotify.com/album/0AbCdEfGhIjKlMnOpQrStU")
    _finish(api._search_job, "catalogue search")

    assert answered["ok"] is True
    assert answered["searching"] is True, "it searches; it does not adopt"
    assert "album" not in answered, (
        "nothing was identified by this gesture — and `album` at the top level "
        "of an answer from this API means the identified album everywhere else"
    )
    assert answered["words"]["artist"] == "Nora Velling"
    assert answered["words"]["album"] == "Lantern"
    asked = [(query.artist, query.album) for query in source.searches]
    assert (
        "Nora Velling",
        "Lantern",
    ) in asked, "the catalogue was asked in Spotify's words, which is the whole gesture"


def test_a_spotify_link_the_pointer_refuses_is_reported_in_the_dialog(tmp_path: Path) -> None:
    """A track link without the user's own key has no reading, and the field must say so."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource(), spotify=_RefusingSpotify("A track link needs your own key."))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    answered = api.adopt_link(unit_id, "https://open.spotify.com/track/4uLU")

    assert answered["ok"] is False
    assert answered["error"] == "A track link needs your own key."


class _FakeSpotify:
    """Return fixed words for any link, without reaching Spotify."""

    def __init__(self, album: str, artist: str | None) -> None:
        self._album = album
        self._artist = artist

    def words_for(self, link: object) -> object:
        """Return the words this link resolves to."""
        from diglibrary.metadata.spotify import PointedWords

        return PointedWords(album=self._album, artist=self._artist, exact=True)


class _RefusingSpotify:
    """Refuse every link with one sentence, the way a real refusal reads."""

    def __init__(self, message: str) -> None:
        self._message = message

    def words_for(self, link: object) -> object:
        """Raise the refusal this pointer was built to give."""
        from diglibrary.metadata.spotify import SpotifyError

        raise SpotifyError(self._message)


def test_every_identification_carries_its_source_url(tmp_path: Path) -> None:
    """A Discogs or MusicBrainz release is addressable, so link to it."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    album = api.state()["albums"][0]

    assert album["url"] == "https://www.discogs.com/release/r1"
    # And which catalogue that link leads to, in the same row. The Library's
    # right-click menu is built from this summary and names the source in the
    # label, so without this it offered "View on undefined".
    assert album["source"] == "discogs"


def _embed(track: Path, image: bytes) -> None:
    """Put a picture inside an audio file, the way a rip usually carries one."""
    FilesystemArtworkStore().embed(track, _written(track.parent / "seed.jpg", image))


def _written(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _sleeve(folder: Path, image: bytes) -> Path:
    """Leave a picture beside the files and none inside them.

    That is the arrangement in which a plan carries `embed_image` for every
    track: the folder has a sleeve, no file carries one, so the album's own
    picture is what goes in. Padded past a FLAC's own padding block, because a
    handful of bytes is absorbed and proves nothing about a size that moved.
    """
    return _written(folder / "front.jpg", image + b"\x00" * 40_000)


def test_a_coverless_album_gets_its_own_embedded_picture(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The picture is already inside the album's own files.

    An album in review with no folder cover, whose files carry an embedded
    picture, gets that picture as its cover: art must not reach a folder only
    inside an applied identification.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _embed(folder / "aaa.flac", jpeg(640, 640))
    (folder / "seed.jpg").unlink()
    source = FakeSource(summaries=(), details={})
    api = _api(tmp_path, source)

    _scan_and_wait(api, library)
    finished = next(event for event in api.events() if event["type"] == "finished")

    assert finished["payload"]["covers"] == 1
    assert api.state()["albums"][0]["cover_pending"] is True
    assert not (folder / "cover.jpg").exists(), "nothing is written before the gesture"

    result = api.apply_automatic()

    assert result["covers"] == 1
    assert (folder / "cover.jpg").is_file(), "the album's own picture reached its folder"
    # Which album, not just how many: a count alone does not say where the
    # cover went.
    assert result["cover_albums"] == [folder.name]


def test_a_folder_that_already_holds_an_image_is_left_alone(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """Embedded art fills an absence; it does not compete with an existing picture."""
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _embed(folder / "aaa.flac", jpeg(640, 640))
    existing = _written(folder / "seed.jpg", jpeg(90, 90))
    existing.rename(folder / "front.jpg")
    api = _api(tmp_path, FakeSource(summaries=(), details={}))

    _scan_and_wait(api, library)

    assert api.state()["albums"][0]["cover_pending"] is False
    assert api.apply_automatic()["covers"] == 0


def test_an_album_without_any_embedded_picture_plans_no_cover(tmp_path: Path) -> None:
    """No picture in the files is a normal outcome, not a failure."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource(summaries=(), details={}))

    _scan_and_wait(api, library)

    assert api.state()["albums"][0]["cover_pending"] is False
    assert api.apply_automatic()["covers"] == 0


def test_a_written_cover_is_reverted_without_disturbing_the_album(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """Every write stays reversible, and undoing a cover is not undoing an album."""
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _embed(folder / "aaa.flac", jpeg(640, 640))
    (folder / "seed.jpg").unlink()
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    _scan_and_wait(api, library)
    api.apply_automatic()

    plan_id = api.state()["history"][0]["plan_id"]
    reverted = api.revert(plan_id)

    assert reverted["ok"]
    assert not (folder / "cover.jpg").exists()
    assert api.state()["albums"][0]["applied"] is False


def test_a_corrected_search_does_not_grade_its_own_answers(tmp_path: Path) -> None:
    """What is typed finds; what the album is judges.

    Scoring every result on its similarity to the typed words would let an
    unrelated record outrank the release that was just found by hand.
    """
    library = _library(tmp_path)
    decoy = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="decoy",
        title="Orvo Orvo Orvo",
        artists=(ArtistMetadata(name="Drusa Velling"),),
        tracks=(TrackMetadata(title="Orvo Orvo Orvo", position=1, duration_ms=200_000),),
        released_on=date(1988, 1, 1),
    )
    source = FakeSource(summaries=(RIGHT, decoy), details={"r2": RIGHT, "decoy": decoy})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    candidates = _search_and_wait(api, unit_id, album="Orvo Orvo Orvo")

    by_id = {candidate["release_id"]: candidate for candidate in candidates}
    assert (
        by_id["r2"]["confidence"] > by_id["decoy"]["confidence"]
    ), "the album's own release must outrank a decoy that merely matches the typed words"


def test_a_partial_match_may_be_applied_on_request(tmp_path: Path) -> None:
    """Most of an album matching is a real answer when the user says so."""
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    extra = folder / "zzz-bonus.flac"
    shutil.copyfile(folder / "aaa.flac", extra)
    api = _api(tmp_path, FakeSource(summaries=(RIGHT,), details={"r2": RIGHT}))

    _scan_and_wait(api, library)
    album = _opened(api, api.state()["albums"][0]["unit_id"])

    assert album["applicable"] is False, "an incomplete match is never applied unattended"
    assert album["partial_applicable"] is True
    assert album["partial_matched"] == len(RIGHT.tracks)
    # The files it leaves alone are rows, not a sentence: the table gives
    # each one a row of its own, and that is the only place they are named.
    # This asserts the row, because a guard that stopped checking is how a
    # fact leaves a screen.
    left = [row for row in album["evidence"] if row["status"] == "unmatched_file"]
    assert left, "the files being left untouched are not on this screen at all"
    assert not any(".flac" in text for text in album["partial_warnings"])

    approved = api.approve(album["unit_id"], partial=True)

    assert approved["ok"], approved
    assert not extra.exists(), "the album folder itself was renamed"
    # It travelled with its folder, as every companion does, and kept the one
    # thing a partial plan promises: its own name.
    assert (library / ORGANIZED / "zzz-bonus.flac").is_file()
    assert (library / ORGANIZED / "01. Faixa 1.flac").is_file(), "what matched was renamed"


def test_a_partial_apply_is_never_automatic(tmp_path: Path) -> None:
    """The batch gesture ignores partial plans entirely."""
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    shutil.copyfile(folder / "aaa.flac", folder / "zzz-bonus.flac")
    api = _api(tmp_path, FakeSource(summaries=(RIGHT,), details={"r2": RIGHT}))

    _scan_and_wait(api, library)

    assert api.apply_automatic()["applied"] == 0


def test_an_album_that_fails_to_identify_is_still_shown(tmp_path: Path) -> None:
    """A scanned folder is a folder the window shows, whatever went wrong."""
    library = _library(tmp_path)

    class Exploding(FakeSource):
        def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
            raise RuntimeError("the catalogue is on fire")

    api = _api(tmp_path, Exploding(summaries=(), details={}))
    _scan_and_wait(api, library)

    albums = api.state()["albums"]
    assert len(albums) == 1, "the album must not vanish between the count and the grid"
    assert albums[0]["decision"] == "unidentified"


def test_one_gesture_reverts_the_whole_run(tmp_path: Path) -> None:
    """What one click applied, one deliberate click undoes — exactly."""
    library = _library(tmp_path)
    before = _snapshot(library)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    result = api.apply_automatic()
    assert (library / ORGANIZED).is_dir()

    undone = api.revert_run(result["run_id"])

    assert undone["ok"] and undone["reverted"] == 1
    assert _snapshot(library) == before, "the run came back byte for byte"


def test_the_certificate_screams_when_a_write_is_tampered_with(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The proof must catch a lie, or it is decoration.

    The plan claims a folder cover was written; the file is replaced behind
    the executor's back before certification. A certificate that stays green
    here would be worthless.
    """
    build_jpeg = jpeg
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _embed(folder / "aaa.flac", build_jpeg(640, 640))
    (folder / "seed.jpg").unlink()
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    _scan_and_wait(api, library)

    state = next(iter(api._albums.values()))
    original_apply = api._pipeline.executor.apply

    def sabotaged(plan, witness=None):
        result = original_apply(plan, witness=witness)
        for operation in plan.operations:
            # Overwrite what was just written, as a crash or another tool might.
            operation.target_path.write_bytes(build_jpeg(2, 2))
        return result

    api._pipeline.executor.apply = sabotaged
    api.apply_automatic()

    assert state.cover_applied
    proofs = api._store.proofs(state.cover_plan_id)
    assert proofs["writes"]["ok"] is False, "the tampered write must be called out"


def test_a_pasted_discogs_master_link_is_followed_to_a_release() -> None:
    """The link a browser hands you is the album's page, not a pressing's.

    Searching Discogs and clicking the album lands on its master, which has no
    tracklist to align files against — so the master is followed to the edition
    Discogs itself calls the main one, rather than refused.
    """
    assert _parse_release_link("https://www.discogs.com/master/100200-Velame-Velame") is None

    group = _parse_group_link("https://www.discogs.com/master/100200-Velame-Velame")

    assert group == (MetadataSources.DISCOGS, "100200")


def test_a_localized_discogs_link_is_recognized() -> None:
    """Discogs serves localized paths, and a browser may hand one over."""
    parsed = _parse_release_link("https://www.discogs.com/pt_BR/master/100200-Velame")
    assert parsed is None

    assert _parse_group_link("https://www.discogs.com/pt_BR/master/100200-Velame") == (
        MetadataSources.DISCOGS,
        "100200",
    )
    assert _parse_release_link("https://www.discogs.com/pt_BR/release/300400-Velame") == (
        MetadataSources.DISCOGS,
        "300400",
    )


def test_a_link_that_is_neither_is_refused_with_guidance() -> None:
    """A refusal has to say what would have worked."""
    assert _parse_release_link("https://example.com/whatever") is None
    assert _parse_group_link("https://example.com/whatever") is None


def test_evidence_reads_in_release_order_with_leftover_files_last() -> None:
    """Track 10 above track 01 makes both lists impossible to follow.

    Rows must interleave: a file paired to track 7 belongs between the unfiled
    tracks 6 and 8, not in a separate block. Files the release has no track
    for close the list, under their own names.
    """
    rows = [
        {"status": "unmatched_track", "track": "Ten", "position": "10", "file": None},
        {"status": "paired", "track": "Seven", "position": 7, "disc": 1, "file": "b.flac"},
        {"status": "unmatched_file", "track": None, "position": None, "file": "stray.flac"},
        {"status": "unmatched_track", "track": "One", "position": "1", "file": None},
        {"status": "paired", "track": "Two", "position": 2, "disc": 1, "file": "a.flac"},
        {"status": "unmatched_track", "track": "Eight", "position": "8", "file": None},
    ]

    ordered = sorted(rows, key=_evidence_order)

    assert [row["track"] for row in ordered] == ["One", "Two", "Seven", "Eight", "Ten", None]
    assert ordered[-1]["file"] == "stray.flac"


def test_evidence_order_respects_discs() -> None:
    """Disc 2 track 1 comes after disc 1 track 10, not before it."""
    rows = [
        {"status": "paired", "track": "D2T1", "position": 1, "disc": 2, "file": "x.flac"},
        {"status": "paired", "track": "D1T10", "position": 10, "disc": 1, "file": "y.flac"},
        {"status": "unmatched_track", "track": "D2T5", "position": "2-5", "file": None},
    ]

    ordered = sorted(rows, key=_evidence_order)

    assert [row["track"] for row in ordered] == ["D1T10", "D2T1", "D2T5"]


def test_one_albums_verdict_never_names_a_track_in_another_album(tmp_path: Path) -> None:
    """A verdict is about an album, and the same audio can sit in two.

    A track that measures lossless in one album can sit, byte for byte, in a
    compilation that really is a transcode and whose verdict marks every one of
    its files. Identity is the audio, so one global set of signatures would
    carry one album's verdict into the other album's name.
    """
    library = tmp_path / "library"
    compilation = library / "compilation"
    honest = library / "honest album"
    compilation.mkdir(parents=True)
    honest.mkdir(parents=True)
    for folder in (compilation, honest):
        shutil.copy(FIXTURES / "tone.flac", folder / "shared.flac")
    # Two albums, one track in common.
    shutil.copy(FIXTURES / "tone-long.flac", compilation / "another.flac")
    shutil.copy(FIXTURES / "tone.wav", honest / "other.wav")

    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    units = api._pipeline.scanner.scan(library)
    by_name = {unit.folder_path.name: unit for unit in units}
    assert (
        by_name["compilation"].unit_signature != by_name["honest album"].unit_signature
    ), "two different albums"
    api._pipeline = replace(api._pipeline, quality=_FakeSurvey({"compilation"}))

    measured = api._measure_for_naming(units)

    shared = next(
        file.content_signature
        for file in by_name["honest album"].audio_files
        if file.path.name == "shared.flac"
    )
    assert shared in measured[by_name["compilation"].unit_signature]
    assert measured[by_name["honest album"].unit_signature] == frozenset()
    assert api._transcoded_for(by_name["honest album"]) == frozenset()


class _FakeSurvey:
    """A survey that calls the named albums transcodes and measures nothing else."""

    def __init__(self, transcoded_folders: set[str]) -> None:
        self._transcoded = transcoded_folders

    def measure(self, units, on_album=None):
        return tuple(
            SimpleNamespace(
                folder_path=unit.folder_path,
                verdict=SimpleNamespace(
                    encoding=(
                        Encoding.TRANSCODED
                        if unit.folder_path.name in self._transcoded
                        else Encoding.LOSSLESS
                    )
                ),
            )
            for unit in units
        )

    def known(self, units):
        return {}


def test_a_stated_verdict_outranks_the_measurement(tmp_path: Path) -> None:
    """Measuring is evidence, not authority.

    The rule convicts only the unmistakable and still gets it wrong sometimes,
    in both directions. Everywhere else in this app the user's word wins — a
    pasted link, a corrected name, a typed title — and a spectrum cannot know
    what was heard.
    """
    library = tmp_path / "library"
    compilation = library / "compilation"
    honest = library / "honest album"
    compilation.mkdir(parents=True)
    honest.mkdir(parents=True)
    for folder in (compilation, honest):
        shutil.copy(FIXTURES / "tone.flac", folder / "shared.flac")
    shutil.copy(FIXTURES / "tone-long.flac", compilation / "another.flac")
    shutil.copy(FIXTURES / "tone.wav", honest / "other.wav")

    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    units = api._pipeline.scanner.scan(library)
    by_name = {unit.folder_path.name: unit for unit in units}
    api._pipeline = replace(api._pipeline, quality=_FakeSurvey({"compilation"}))
    fake = by_name["compilation"]
    clean = by_name["honest album"]

    api._transcoded = api._measure_for_naming(units)
    assert api._transcoded_for(fake), "measured a transcode to begin with"

    # The album is called honest, and it stops being named for the measurement.
    assert api.set_quality_verdict(fake.unit_signature, "honest")["ok"]
    api._transcoded = api._measure_for_naming(units)
    assert api._transcoded_for(fake) == frozenset()

    # One track of it is called a fake; the rest stay honest.
    marked = fake.audio_files[0].content_signature
    assert api.set_quality_verdict(fake.unit_signature, "transcoded", marked)["ok"]
    api._transcoded = api._measure_for_naming(units)
    assert api._transcoded_for(fake) == frozenset({marked})

    # And the same audio in the other album is untouched by any of it.
    assert api._transcoded_for(clean) == frozenset()

    # Withdrawing gives the measurement its voice back.
    assert api.set_quality_verdict(fake.unit_signature, "clear")["ok"]
    assert api.set_quality_verdict(fake.unit_signature, "clear", marked)["ok"]
    api._transcoded = api._measure_for_naming(units)
    assert len(api._transcoded_for(fake)) == len(fake.audio_files)


def test_an_album_can_be_called_a_fake_the_measurement_cleared(tmp_path: Path) -> None:
    """The other direction: an album the rule misses can be called a fake by hand."""
    library = tmp_path / "library"
    album = library / "dark master"
    album.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", album / "01.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "02.flac")

    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    units = api._pipeline.scanner.scan(library)
    api._pipeline = replace(api._pipeline, quality=_FakeSurvey(set()))
    unit = units[0]

    api._transcoded = api._measure_for_naming(units)
    assert api._transcoded_for(unit) == frozenset(), "the measurement found nothing"

    assert api.set_quality_verdict(unit.unit_signature, "transcoded")["ok"]
    api._transcoded = api._measure_for_naming(units)

    assert api._transcoded_for(unit) == frozenset(
        file.content_signature for file in unit.audio_files
    )


def test_an_unknown_verdict_is_refused(tmp_path: Path) -> None:
    """A typo in the bridge must not silently record something meaningless."""
    api = _api(tmp_path, FakeSource(summaries=(), details={}))

    response = api.set_quality_verdict("signature", "maybe")

    assert response["ok"] is False


def test_a_correction_keeps_the_pairing_the_plan_is_shown_against(tmp_path: Path) -> None:
    """Correcting the folder name keeps the pairing of files to tracks.

    `refine` computes the pairing and plans from it. If it then drops the
    pairing, the plan comes back saying `no file here` beside every track of an
    album whose files the same dialog has just reported as paired: the `now on
    disk` column reads exactly that field.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    before = api.album(unit_id)["album"]["evidence"]
    paired_before = [row for row in before if row["status"] == "paired"]

    corrected = api.correct(unit_id, folder_name="Marina - Forró [mine]")

    assert corrected["ok"]
    paired_after = [row for row in corrected["album"]["evidence"] if row["status"] == "paired"]
    assert paired_before, "the album must be paired before the correction, or this proves nothing"
    assert [row["file"] for row in paired_after] == [row["file"] for row in paired_before]


def test_dropped_folders_are_added_to_what_is_already_there(tmp_path: Path) -> None:
    """Dropping says "these as well"; only pointing at a root says "this instead".

    A scan replaces what a scan found, because a root is an instruction about
    the whole library. A folder dropped on the window is not one, so an album
    already on screen has to survive the next drop.

    **The dropped folder is a different record.** An album here *is* its
    audio, so a folder holding the same two files as the library's own album
    would carry the same signature, and the drop would re-anchor the album
    already on screen instead of adding one.
    """
    library = _library(tmp_path)
    other = tmp_path / "dropped" / "nilo varga - ágata turva"
    other.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", other / "aaa.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 1

    assert api.adopt_folders([str(other)])["started"] == 1
    _finish_reads(api)

    folders = sorted(str(album["folder"]) for album in api.state()["albums"])
    assert folders == ["marina do acordeao - forro", "nilo varga - ágata turva"]
    announced = [event for event in api.events() if event["type"] == "adopted"]
    assert announced[-1]["payload"]["albums"] == 1


def test_several_dropped_folders_land_together(tmp_path: Path) -> None:
    """Several folders dropped at once is the ordinary case."""
    dropped = []
    for name in ("lena quirino - ébano", "os tapiris - voltou chorando"):
        folder = tmp_path / "dropped" / name
        folder.mkdir(parents=True)
        shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
        shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
        dropped.append(str(folder))
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    assert api.adopt_folders(dropped)["started"] == 2
    _finish_reads(api)

    assert len(api.state()["albums"]) == 2


def test_dropping_an_album_the_library_already_holds_says_so(tmp_path: Path) -> None:
    """Found-and-already-here is not the same answer as nothing-here-is-an-album.

    `adopted` counts albums that are *new*, and an album already on screen
    makes it zero — the same number a folder with no album in it produces. One
    number for two outcomes leaves the window unable to tell them apart, so
    the payload carries `known` as well.
    """
    folder = tmp_path / "dropped" / "quarteto rufo - vesperal"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    assert api.adopt_folders([str(folder)])["started"] == 1
    _finish_reads(api)
    first = [event for event in api.events() if event["type"] == "adopted"][-1]["payload"]
    assert (first["albums"], first["known"]) == (1, 0)

    assert api.adopt_folders([str(folder)])["started"] == 1
    _finish_reads(api)
    again = [event for event in api.events() if event["type"] == "adopted"][-1]["payload"]
    assert again["albums"] == 0, "the same album is not new the second time"
    assert again["known"] == 1, "and the window must be able to say it was found"
    # Said, and nothing else — named, and neither marked nor floated.
    assert again["known_albums"] == [folder.name]
    assert again["unit_ids"] == [], "an album dropped again from where it lives is not an arrival"
    assert again["same_audio"] == []


def test_opening_a_folder_again_names_its_album_the_same_way_a_drop_does(tmp_path: Path) -> None:
    """Opening a folder and dropping it are one gesture, and answer alike.

    A rule applied to the drop alone would leave the folder opened again
    still marked.
    """
    folder = tmp_path / "music" / "trio norte - mare alta"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    assert api.open_folder(str(folder))["ok"]
    _finish_reads(api)
    first = [event for event in api.events() if event["type"] == "opened"][-1]["payload"]
    assert len(first["albums"]) == 1 and first["known_albums"] == []

    assert api.open_folder(str(folder))["ok"]
    _finish_reads(api)
    again = [event for event in api.events() if event["type"] == "opened"][-1]["payload"]
    assert again["albums"] == [], "an album opened again from where it lives was marked"
    assert again["known_albums"] == [folder.name]


def _two_copies(tmp_path: Path) -> tuple[LibraryApi, Path, Path]:
    """An album on the shelf, and a second copy of its audio in a folder of its own."""
    folder = tmp_path / "music" / "trio norte - mare alta"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    assert api.adopt_folders([str(folder)])["started"] == 1
    _finish_reads(api)
    copy = tmp_path / "downloads" / "Trio Norte - Maré Alta (1980) [FLAC]"
    shutil.copytree(folder, copy)
    api.events()
    return api, folder, copy


def test_a_second_copy_dropped_is_a_new_album_and_says_which_one_it_repeats(
    tmp_path: Path,
) -> None:
    """A second copy of an album is an album of its own, and is announced as one.

    Asking only whether the *audio* is on record would count the copy as the
    album already here and announce that the kept copy had *moved* to the new
    folder, which it has not: that row is still sitting in its folder.
    """
    api, folder, copy = _two_copies(tmp_path)

    assert api.adopt_folders([str(copy)])["started"] == 1
    _finish_reads(api)
    payload = [event for event in api.events() if event["type"] == "adopted"][-1]["payload"]

    assert (payload["albums"], payload["known"]) == (1, 0), payload
    assert payload["moved"] == [], "the kept copy was said to have moved"
    assert [(entry["album"], entry["twin"]) for entry in payload["same_audio"]] == [
        (copy.name, folder.name)
    ]
    folders = {album["folder_path"] for album in api.state()["albums"]}
    assert {str(folder), str(copy)} <= folders, "both copies are kept"


def test_a_second_copy_found_by_a_scan_is_said_once(tmp_path: Path) -> None:
    """Every door says it: the scan and the download (which is a scan) as well."""
    api, folder, copy = _two_copies(tmp_path)

    _scan_and_wait(api, copy.parent)
    finished = [event for event in api.events() if event["type"] == "finished"][-1]["payload"]
    assert [entry["twin"] for entry in finished["same_audio"]] == [folder.name]

    _scan_and_wait(api, copy.parent)
    again = [event for event in api.events() if event["type"] == "finished"][-1]["payload"]
    assert again["same_audio"] == [], "a pair already on the shelf is not announced again"


def test_relocating_onto_a_folder_another_album_holds_is_refused_and_offers_to_let_go(
    tmp_path: Path,
) -> None:
    """A relocation onto a folder another album holds is refused.

    The database refuses the move, because the folder is another row's.
    Answering `ok` anyway would draw the card at the new folder and leave the
    row naming the old one. The refusal hands the window what it needs to
    offer ✕.
    """
    api, folder, copy = _two_copies(tmp_path)
    assert api.adopt_folders([str(copy)])["started"] == 1
    _finish_reads(api)
    second = next(
        album["unit_id"] for album in api.state()["albums"] if album["folder_path"] == str(copy)
    )
    shutil.rmtree(copy)

    answer = api.relocate_album(second, str(folder))

    assert answer["ok"] is False
    assert answer["second_copy"]["unit_id"] == second
    assert api._store.unit_by_id(second).folder_path == copy, "the row was moved anyway"


def test_a_drop_that_carried_no_folder_still_answers(tmp_path: Path) -> None:
    """The window starts waiting when something lands on it, so it must be released.

    Nothing is emitted from the worker in this case because there is no worker;
    the answer is given where the decision is made, or the screen waits for one
    that was never coming.
    """
    source = FakeSource(summaries=(), details={})
    api = _api(tmp_path, source)

    assert api.adopt_folders([])["ok"]

    announced = [event for event in api.events() if event["type"] == "adopted"]
    assert announced == [{"type": "adopted", "payload": {"albums": 0, "folders": 0}}]


def test_a_dropped_path_that_is_not_a_folder_is_refused_by_name(tmp_path: Path) -> None:
    """Refusing says which one, because a drop can carry several."""
    source = FakeSource(summaries=(), details={})
    api = _api(tmp_path, source)

    result = api.adopt_folders([str(tmp_path / "gone")])

    assert result["ok"] is False
    assert "gone" in str(result["error"])


def test_an_album_can_be_cleared_from_the_library_without_touching_disk(tmp_path: Path) -> None:
    """Clearing says the album has been seen, and nothing more than that.

    History still answers for the album, every file stays where it is, and a
    scan that reaches the folder again brings it back — this hides a row, it
    does not remember a decision.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])
    folder = library / "marina do acordeao - forro"

    assert api.forget_album(unit_id)["ok"]

    assert api.state()["albums"] == []
    assert folder.is_dir()
    assert sorted(p.name for p in folder.iterdir()) == ["aaa.flac", "bbb.flac"]
    # And it comes back, because nothing was remembered about it.
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 1


def test_opening_an_album_opens_its_own_folder(tmp_path: Path) -> None:
    """Read from the album, not built from the root and a name.

    An organised album lives under the name this application wrote, and one
    scanned from its own folder may *be* the root.
    """
    opened: list[str] = []
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    api._path_opener = opened.append
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    assert api.open_album(unit_id)["ok"]

    assert opened == [str(library / "marina do acordeao - forro")]


def test_opening_an_album_whose_folder_has_gone_says_so(tmp_path: Path) -> None:
    """Naming the path, because the answer to "where is it" is that it is not there."""
    import shutil as _shutil

    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    api._path_opener = lambda path: None
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])
    _shutil.rmtree(library / "marina do acordeao - forro")

    result = api.open_album(unit_id)

    assert result["ok"] is False
    assert "marina do acordeao - forro" in str(result["error"])


# --- the Library holds what it has been shown -------------------------------


def _reopen(api: LibraryApi) -> LibraryApi:
    """Do to one API what closing and reopening the window does to the app.

    The database is the only thing that crosses, which is the whole point: a
    restart is where "it was saved" and "it was only on screen" stop looking
    alike.
    """
    api._albums.clear()
    api._downloaded.clear()
    api._restore_library()
    return api


def test_a_scanned_album_is_still_there_after_a_restart(tmp_path: Path) -> None:
    """Scanned albums stay on the shelf across a restart, as downloaded ones do."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 1

    albums = _reopen(api).state()["albums"]

    assert [album["folder"] for album in albums] == ["marina do acordeao - forro"]
    assert albums[0]["origin"] == "scanned"
    # Restored with what was decided about it, not as a bare folder: the title
    # comes from the release cached at scan time.
    assert albums[0]["title"] == "Forró"


class Landmine:
    """A source that counts what it was asked, and answers nothing useful.

    Raising would prove nothing: every one of these calls sits inside a handler
    that logs the failure and carries on, so a refusal is caught and the suite
    stays green while the promise is broken. A fixture the code under test is
    designed to swallow is not a test.

    Counting cannot be swallowed.
    """

    def __init__(self) -> None:
        self.asked = 0

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        self.asked += 1
        return ()

    def get_release(self, release_id: str) -> ReleaseMetadata:
        self.asked += 1
        raise LookupError("nothing here")

    def first_release_date(self, group_id: str) -> date | None:
        self.asked += 1
        return None


def test_restoring_the_library_asks_no_source_anything(tmp_path: Path) -> None:
    """Opening the window is not the moment to spend a rate limit.

    The cached release is what a restored album is matched against, so a source
    consulted here would be a promise with one exception per album — which is
    how a fast start becomes a minute of waiting on a catalogue.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    _scan_and_wait(_api(tmp_path, source), library)

    # Reopening the app over the same database, with every source counting
    # instead of answering. The restore runs in the constructor, on its thread.
    landmine = Landmine()
    archive = Landmine()
    reopened = _api(tmp_path, landmine, witness=archive)  # type: ignore[arg-type]
    _finish(reopened._restore_job, "restore")

    assert landmine.asked == 0, "restoring the Library must reach no source"
    # And the one behind the cover lookup, which is the door that was open.
    assert archive.asked == 0, "restoring the Library must not address the art archive"
    albums = reopened.state()["albums"]
    assert [album["title"] for album in albums] == ["Forró"]
    assert albums[0]["decision"] == "automatic"


def test_an_album_whose_folder_has_gone_leaves_the_library(tmp_path: Path) -> None:
    """An album scanned in a transit folder and filed afterwards leaves the shelf.

    A folder that is no longer where it was recorded is the normal end of that
    journey, not a fault to report — marking every one of them would fill the
    screen with notices about albums that simply arrived where they belong.
    """
    import shutil as _shutil

    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    _shutil.rmtree(library / "marina do acordeao - forro")

    assert _reopen(api).state()["albums"] == []


def test_clearing_an_album_from_the_library_outlives_the_window(tmp_path: Path) -> None:
    """The ✕ survives a restart."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    assert api.forget_album(unit_id)["ok"]

    assert _reopen(api).state()["albums"] == []
    # Cleared is not deleted, and never was: the folder is untouched, and
    # scanning it again brings the row back.
    assert (library / "marina do acordeao - forro").is_dir()
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 1


def test_state_says_while_the_library_is_still_arriving(tmp_path: Path) -> None:
    """The window asks once, so it must be told there is more coming.

    Without this the boot call gets a legitimate empty answer and treats it as
    the final one: for the short interval the restore takes, the Library would
    open empty.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    _scan_and_wait(_api(tmp_path, source), library)

    reopened = _api(tmp_path, source)
    # Whatever this call catches, it must never be "nothing, and nothing more
    # is coming": either the albums are already there, or it says they are on
    # their way.
    caught = reopened.state()
    assert caught["albums"] or caught["restoring"]

    _finish(reopened._restore_job, "restore")
    settled = reopened.state()
    assert settled["restoring"] is False
    assert len(settled["albums"]) == 1


def test_the_library_is_on_screen_before_a_single_file_is_read(tmp_path: Path) -> None:
    """The shelf comes from the database's columns, not from the disk.

    Reading every album back from disk costs orders of magnitude more than
    drawing the same shelf from columns. The work is not slow; the first sight
    of the window does not need it.

    Asserted before the restore thread is allowed to finish, which is the only
    moment that can tell the two apart.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    _scan_and_wait(_api(tmp_path, source), library)

    reopened = _api(tmp_path, source)
    drawn = reopened.state()["albums"]
    _finish(reopened._restore_job, "restore")

    assert len(drawn) == 1, "the album is on the shelf before anything has read it"
    assert drawn[0]["title"] == "Forró", "and it is named, from the column that holds the name"
    assert drawn[0]["artist"] == "Marina do Acordeão"
    assert drawn[0]["settled"] is False, "while saying it has not been read back yet"
    assert drawn[0]["match_total"] is None, (
        "and refusing to state a pairing nobody has measured — the one thing a "
        "column cannot hold"
    )
    assert drawn[0]["match_files_unpaired"] is None
    assert (
        drawn[0]["match_tracks_unpaired"] is None
    ), "the two open halves are as unmeasured as the total they add up to"


def test_an_album_only_on_the_shelf_is_read_back_when_it_is_opened(tmp_path: Path) -> None:
    """Reading an album back is spent on the album that is actually opened.

    Fails against a build that answers `Unknown album` for anything the restore
    has not reached yet — which, with the shelf drawn first, is every album for
    the first seconds and the organized ones for much longer.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    _scan_and_wait(_api(tmp_path, source), library)

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    unit_id = int(reopened.state()["albums"][0]["unit_id"])
    # Put it back the way the window meets it: a row, and nothing read yet.
    with reopened._albums_lock:
        reopened._shelf[unit_id] = dict(reopened._store.shelf()[0])
        reopened._albums.pop(unit_id, None)

    opened = reopened.album(unit_id)

    assert opened["ok"] is True, "opening a card must not depend on the restore having reached it"
    assert opened["album"]["settled"] is True, "and what comes back is the album read off the disk"
    assert unit_id not in reopened._shelf, "the row steps aside for the album it stood in for"


def test_taking_an_album_off_the_shelf_reaches_the_row_it_is_drawn_from(
    tmp_path: Path,
) -> None:
    """The ✕ on a card that has not been read back yet.

    Both halves are "on screen", so a gesture that removed only one of them
    would take the card away and let it redraw itself from the row nobody
    touched.
    """
    library = _library(tmp_path)
    source = FakeSource(details={"r1": _release("r1", (400, 900))})
    _scan_and_wait(_api(tmp_path, source), library)

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    unit_id = int(reopened.state()["albums"][0]["unit_id"])
    with reopened._albums_lock:
        reopened._shelf[unit_id] = dict(reopened._store.shelf()[0])
        reopened._albums.pop(unit_id, None)

    assert reopened.forget_album(unit_id)["ok"] is True
    assert reopened.state()["albums"] == []


def test_the_restore_stops_saying_it_is_running_the_moment_it_reports(
    tmp_path: Path,
) -> None:
    """`Reading your library…` must not outlive the reading.

    Two answers mean "still restoring" and they disagree for as long as it
    takes a thread to stop existing: the `restored` event, and a snapshot asking
    whether the thread is alive. The window clears the banner on the event and
    immediately asks for a fresh snapshot — which would find the thread alive,
    turn the flag back on, and leave it on, because the one event that could
    turn it off has already been drained.

    Asserted at the only moment that can catch it — the instant the event is
    reported, with the thread still running — which is what makes it fail
    against a `state()` that only asks whether the thread is alive.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    _scan_and_wait(_api(tmp_path, source), library)

    reopened = _api(tmp_path, source)
    reopened._restore_reported.wait(timeout=30)
    said = reopened.state()["restoring"]
    _finish(reopened._restore_job, "restore")

    assert said is False, (
        "the restore has reported and the window would be told, in the same "
        "breath, that it is still going"
    )


def test_a_restore_that_cannot_read_its_rows_still_reports(tmp_path: Path) -> None:
    """A screen waiting on an event an exception swallowed never updates.

    A per-album failure is caught; the read of the rows themselves is every
    album at once, and it happens before the only line that tells the window
    anything.
    """
    library = _library(tmp_path)
    source = FakeSource()
    _scan_and_wait(_api(tmp_path, source), library)

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    reopened._store.library_units = _raising  # type: ignore[method-assign]
    reopened._restore_reported.clear()
    # The failure still surfaces — it is the thread's to log, and swallowing it
    # would be the other half of this defect. What must not depend on it is the
    # window being told.
    with pytest.raises(RuntimeError):
        reopened._restore_library()

    assert reopened._restore_reported.is_set()
    assert any(event["type"] == "restored" for event in reopened.events())


def _raising() -> None:
    raise RuntimeError("the database is not readable")


def test_an_organized_album_says_what_it_is_without_being_planned_again(
    tmp_path: Path,
) -> None:
    """An organized album is protected by having no plan, not by having no name.

    The database holds what the record is from the moment it is organized, so
    the dialog can say so. The answer comes back; the plan does not, because
    the plan is what would put it back inside every batch gesture.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()

    # Scanning again leaves an organized album exactly as it is.
    _scan_and_wait(api, library)
    album = api.state()["albums"][0]

    assert album["organized"] is True
    assert album["title"] == "Forró"
    assert album["artist"] == "Marina do Acordeão"
    # And it stays out of reach of the one-gesture batch.
    assert album["applicable"] is False
    assert album["decision"] != "automatic"


def test_a_restored_organized_album_is_not_swept_into_the_batch(tmp_path: Path) -> None:
    """A restored organized album does not come back as `automatic`.

    A restore that rebuilds a full outcome would return an organized album as
    `automatic` with full confidence, and one press of "Apply N automatic"
    would reach an album this app had already finished with.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()

    album = _reopen(api).state()["albums"][0]

    assert album["organized"] is True
    assert album["applicable"] is False
    assert album["decision"] != "automatic"
    # Still named, because being protected and being anonymous are not the
    # same thing.
    assert album["title"] == "Forró"


def test_scanning_one_folder_leaves_the_rest_of_the_library_alone(tmp_path: Path) -> None:
    """The Library holds what it has been shown, so a scan may not sweep the shelf.

    Scanning one chosen album must leave every other album where it is.
    """
    library = _library(tmp_path)
    other = tmp_path / "elsewhere" / "another album"
    other.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", other / "aaa.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 1

    _scan_and_wait(api, other.parent)

    folders = sorted(str(album["folder"]) for album in api.state()["albums"])
    assert folders == ["another album", "marina do acordeao - forro"]

    # And scanning the same folder twice updates the album rather than doubling it.
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 2


def test_opening_a_folder_shows_its_albums_without_looking_any_up(tmp_path: Path) -> None:
    """Choosing a folder shows its albums, and asks no catalogue anything."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    albums = api.state()["albums"]
    assert len(albums) == 1
    assert albums[0]["looked_at"] is False
    # Nothing was asked of any catalogue: reading the disk is the cheap half,
    # and identifying is the half that is chosen.
    assert source.searches == []
    # And it cannot be swept into a batch before it has been looked up.
    assert albums[0]["applicable"] is False

    opened = [event for event in api.events() if event["type"] == "opened"]
    assert opened and opened[-1]["payload"]["albums"] == [albums[0]["unit_id"]]


def test_scan_looks_up_only_the_marked_albums(tmp_path: Path) -> None:
    """Marking replaces the exclusions dialog, one album at a time."""
    library = _library(tmp_path)
    second = library / "another album"
    second.mkdir()
    shutil.copy(FIXTURES / "tone.flac", second / "aaa.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    api.open_folder(str(library))
    _finish_reads(api)
    by_folder = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}
    assert len(by_folder) == 2

    assert api.scan_selected([by_folder["another album"]])["ok"]
    _finish(api._job, "scan")

    looked = {album["folder"]: album["looked_at"] for album in api.state()["albums"]}
    assert looked["another album"] is True
    # The unmarked one is untouched, and still on screen.
    assert looked["marina do acordeao - forro"] is False


def test_a_marked_album_that_moved_is_followed_rather_than_dropped(tmp_path: Path) -> None:
    """A scan follows a marked album that moved, as planning again does.

    Re-reading only the recorded folder makes a moved album raise
    `NotADirectoryError` inside the scanner, be logged, and leave the run —
    while the run goes on to report success about the albums that had not
    moved.
    """
    library = _library(tmp_path)
    moving = library / "box" / "another album"
    moving.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    api.open_folder(str(library))
    _finish_reads(api)
    marked = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}
    # Moved, keeping its name, into a folder this app has been shown — which is
    # the only kind of place the search looks, and never the whole disk.
    shutil.move(str(moving), str(library / "another album"))

    assert api.scan_selected([marked["another album"]])["ok"]
    _finish(api._job, "scan")

    albums = {album["unit_id"]: album for album in api.state()["albums"]}
    followed = albums[marked["another album"]]
    assert followed["looked_at"] is True, "the marked album was dropped from the run in silence"
    assert followed["folder_missing"] is False
    assert followed["folder_path"] == str(library / "another album")


def test_a_renamed_folder_is_found_by_what_is_inside_it(tmp_path: Path) -> None:
    """The folder's name is the user's, and the audio in it is what identifies it.

    Looking for the album's folder *under its own name* finds a move and
    structurally cannot find a rename.

    So the files inside are what it searches by. Their sizes come free with the
    listing, and the signature still decides on the one folder that is read.
    """
    library = _library(tmp_path)
    vanishing = library / "another album"
    vanishing.mkdir()
    shutil.copy(FIXTURES / "tone.flac", vanishing / "aaa.flac")
    api = _api(tmp_path, FakeSource())
    api.open_folder(str(library))
    _finish_reads(api)
    marked = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}
    renamed = library / "a name nothing recorded"
    vanishing.rename(renamed)

    assert api.scan_selected([marked["another album"]])["ok"]
    _finish(api._job, "scan")

    found = {album["unit_id"]: album for album in api.state()["albums"]}[marked["another album"]]
    assert found["folder_missing"] is False, "the album is right there, under a chosen name"
    assert found["folder_path"] == str(renamed)


def test_what_the_apply_wrote_is_what_the_database_says_the_files_weigh(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The sizes the search compares are recorded again after the apply changes them.

    Embedding a cover adds bytes to a track. A recorded weight taken by the
    scan *before* that would describe a file that no longer exists in that
    form, and the search for a renamed album could never match again.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _sleeve(folder, jpeg(640, 640))
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    weighed = api._store.audio_sizes(unit_id, folder)

    api.apply_automatic()

    organized = library / ORGANIZED
    assert organized.is_dir(), "the apply renamed the folder, which is the ordinary case"
    on_disk = tuple(sorted(track.stat().st_size for track in organized.glob("*.flac")))
    assert on_disk != weighed, "the picture went into the files, so the bytes moved"
    assert api._store.audio_sizes(unit_id, organized) == on_disk


def test_an_album_renamed_after_its_cover_was_embedded_is_still_found(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """An album organized with a cover and then renamed by hand is still found.

    The first search looks for the folder under the name it was recorded with,
    which has just changed. The second weighs the audio — and the apply has
    embedded the cover into the tracks, so sizes still holding what the files
    weighed before would never equal what is on disk, and the album would sit
    on the shelf reading *not where it was* until pointed at by hand.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _sleeve(folder, jpeg(640, 640))
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()
    unit_id = api.state()["albums"][0]["unit_id"]
    organized = library / ORGANIZED

    # Renamed in the file manager, in the same session.
    renamed = library / "Marina do Acordeão - Forró (renamed by hand)"
    organized.rename(renamed)

    assert api.library_present()["ok"]

    stored = api._store.unit_by_id(unit_id)
    assert stored is not None and stored.folder_path == renamed


def test_an_album_whose_discs_sit_in_folders_is_found_by_what_it_weighs(
    tmp_path: Path,
) -> None:
    """A two-disc album is weighed through its disc folders.

    Reading what an album weighs from the rows whose *parent* is the album's
    own folder finds nothing for a `CD1`/`CD2` album — every file is one level
    down — and the search would give up before it started.
    """
    library = tmp_path / "library"
    album = library / "an album on two discs"
    (album / "CD1").mkdir(parents=True)
    (album / "CD2").mkdir()
    shutil.copy(FIXTURES / "tone.flac", album / "CD1" / "1-01.flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "CD2" / "2-01.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit_id = next(
        album_row["unit_id"]
        for album_row in api.state()["albums"]
        if album_row["folder"] == album.name
    )

    renamed = library / "the name it was given"
    album.rename(renamed)

    assert api.library_present()["ok"]

    stored = api._store.unit_by_id(unit_id)
    assert stored is not None and stored.folder_path == renamed


def test_the_name_a_file_used_to_have_is_not_added_to_what_the_album_weighs(
    tmp_path: Path,
) -> None:
    """A row is a path a file was once seen at, and that is on purpose.

    So renaming a track — in the file manager, or by a plan a later plan
    renamed over — leaves the old row standing. Adding both would describe an
    album twice its own size, which matches nothing, ever.

    Nothing is deleted to fix it. What an album weighs is what it weighed **when
    it was last seen**, which is a question the rows can already answer.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    (folder / "aaa.flac").rename(folder / "renamed in the finder.flac")
    _scan_and_wait(api, library)
    rows = api._store.audio_sizes(unit_id, folder)
    on_disk = tuple(sorted(track.stat().st_size for track in folder.glob("*.flac")))
    assert rows == on_disk, "the name it used to have is not a second track"

    renamed = library / "the name it was given"
    folder.rename(renamed)

    assert api.library_present()["ok"]

    stored = api._store.unit_by_id(unit_id)
    assert stored is not None and stored.folder_path == renamed


def _stops_at(api: LibraryApi, kind: OperationKind) -> None:
    """Make the real executor fail on the first operation of one kind.

    Everything before it is performed for real, on real files, and the trail
    that comes back is the trail that really landed — which is the only way to
    ask what the caller does with a plan that stopped partway.
    """
    executor = api._pipeline.executor
    perform = executor._perform

    def refuse(operation: ChangeOperation) -> object:
        if operation.kind is kind:
            raise OSError(f"the disk refused this {operation.kind}")
        return perform(operation)

    executor._perform = refuse  # type: ignore[method-assign]


def test_a_plan_that_stopped_partway_leaves_the_rows_naming_the_files_that_are_there(
    tmp_path: Path,
) -> None:
    """A plan whose file renames landed and whose folder rename did not.

    Everything that follows an apply — the recorded paths, the album the window
    holds, what the files weigh — must run for a plan that *stopped* too.
    Otherwise every row the plan had already moved names a file that is no
    longer there, with nothing said.

    A dead row is not an untidy row. Two names over one content signature is
    exactly what `ambiguous_signatures` is looking for, so the library would
    begin to withhold measurements from itself. The sizes happen to survive,
    because renaming a file does not change what it weighs; the paths do not.
    """
    import sqlite3

    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    _stops_at(api, OperationKind.RENAME_FOLDER)

    api.apply_automatic()

    assert folder.is_dir(), "the folder rename is the one that was refused"
    assert sorted(track.name for track in folder.glob("*.flac")) == [
        "01. Faixa 1.flac",
        "02. Faixa 2.flac",
    ], "the renames before it did land, which is what leaves the rows behind"
    connection = sqlite3.connect(tmp_path / "app" / "library.sqlite3")
    try:
        registered = sorted(
            Path(row[0]).name for row in connection.execute("SELECT path FROM audio_files")
        )
    finally:
        connection.close()
    assert registered == [
        "01. Faixa 1.flac",
        "02. Faixa 2.flac",
    ], "the rows still name the files as they were before the plan ran"


def test_a_plan_that_stopped_after_moving_the_folder_does_not_lose_the_album(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The other order, and the worse one: the folder went and nothing followed it.

    `album_units.folder_path` would name a folder that no longer exists, so the
    Library would search for an album that is exactly where this application
    itself put it.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _sleeve(folder, jpeg(640, 640))
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    # The cover goes in after the folder is renamed, so refusing it stops the
    # plan on the far side of the move.
    _stops_at(api, OperationKind.EMBED_IMAGE)

    api.apply_automatic()

    landed = library / ORGANIZED
    assert landed.is_dir() and not folder.exists(), "the folder did move"
    stored = api._store.unit_by_id(unit_id)
    assert stored is not None and stored.folder_path == landed


def test_a_reversal_that_stopped_partway_takes_the_recorded_paths_with_it(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The mirror: a reversal that stops partway reports it and follows the trail.

    `ExecutionError` is documented as raised *only for a refusal before any
    write*, because "a failure partway through is reported as a result — the
    trail matters more than the exception". A reversal that raised in the
    middle instead would leave everything that had already come back with no
    row following it: the folder at the name it had, and `album_units` still
    naming the organized one the reversal has just undone.

    The reversal rehearses first, which makes this rare; rare is not never.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _sleeve(folder, jpeg(640, 640))
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    api.apply_automatic()
    plan_id = api.state()["history"][0]["plan_id"]
    # A reversal runs most recent first, so refusing the file renames stops it
    # after the folder has already gone back to the name it had.
    executor = api._pipeline.executor
    undo = executor._undo

    def refuse(entry: object) -> object:
        if entry.operation.kind is OperationKind.RENAME_FILE:  # type: ignore[attr-defined]
            raise OSError("the disk refused this rename_file")
        return undo(entry)

    executor._undo = refuse  # type: ignore[method-assign]

    answered = api.revert(plan_id)

    assert not answered["ok"], "a reversal that stopped says so"
    assert folder.is_dir(), "the folder did come back before it stopped"
    stored = api._store.unit_by_id(unit_id)
    assert stored is not None and stored.folder_path == folder

    # And the three places that say whether it is organized agree with the
    # plan. Reporting the stop by returning early would skip every line after
    # it: `record_plan_outcome` writes `reverted` while the album's row, the
    # shelf row and `_AlbumState` all still say the album is organized — a
    # card wearing ✓ organized after an undo.
    assert stored.state == "discovered", "the row the next launch reads must not say organized"
    state = api._albums[unit_id]
    assert not state.applied and not state.written and not state.organized


def test_a_chosen_cover_leaves_the_weights_describing_the_files(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The second place that changes audio bytes, and it is a separate gesture.

    `chosen_cover_plan` reaches all three places a cover lives, so it puts a
    picture inside every track — through its own executor call, not through
    `_apply`. A rule that landed only in `_apply` would be applied by half.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    sleeve = _written(tmp_path / "sleeve.jpg", jpeg(700, 700) + b"\x00" * 40_000)
    api = _api(tmp_path, FakeSource())
    api._image_picker = lambda: str(sleeve)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    weighed = api._store.audio_sizes(unit_id, folder)

    assert api.choose_cover(unit_id)["ok"]
    assert api.write_chosen_cover(unit_id)["ok"]

    on_disk = tuple(sorted(track.stat().st_size for track in folder.glob("*.flac")))
    assert on_disk != weighed, "the sleeve went into the tracks"
    assert api._store.audio_sizes(unit_id, folder) == on_disk


def test_reverting_puts_the_weights_back_with_the_bytes(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """The reversal keeps the recorded weights true, as the apply does.

    A reversal takes the picture back out, so every weight the apply recorded
    stops describing the file — the same defect, pointing the other way, and
    reached by the gesture whose whole promise is that it puts things back.
    """
    library = _library(tmp_path)
    folder = library / "marina do acordeao - forro"
    _sleeve(folder, jpeg(640, 640))
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    api.apply_automatic()
    plan_id = api.state()["history"][0]["plan_id"]

    assert api.revert(plan_id)["ok"]

    assert folder.is_dir(), "the reversal put the folder's name back"
    on_disk = tuple(sorted(track.stat().st_size for track in folder.glob("*.flac")))
    assert api._store.audio_sizes(unit_id, folder) == on_disk


def test_an_album_nothing_can_find_says_so_instead_of_scanning_nothing(tmp_path: Path) -> None:
    """What the search still cannot reach has to be said, not guessed at.

    It looks only where this app has been shown, and never at the disk at
    large. An album carried off to a folder nothing has ever named is exactly
    that case, and the run has to carry what it could not answer for —
    `Relocate…` is what answers it, by being pointed at the folder.
    """
    library = _library(tmp_path)
    vanishing = library / "another album"
    vanishing.mkdir()
    shutil.copy(FIXTURES / "tone.flac", vanishing / "aaa.flac")
    api = _api(tmp_path, FakeSource())
    api.open_folder(str(library))
    _finish_reads(api)
    marked = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}
    # Out of every place this app has been shown, and renamed on the way.
    elsewhere = tmp_path / "somewhere nobody named" / "a name nothing recorded"
    elsewhere.parent.mkdir(parents=True)
    vanishing.rename(elsewhere)

    assert api.scan_selected([marked["another album"]])["ok"]
    _finish(api._job, "scan")

    lost = {album["unit_id"]: album for album in api.state()["albums"]}[marked["another album"]]
    assert lost["folder_missing"] is True, "a card kept claiming a folder that is not there"
    finished = [event for event in api.events() if event["type"] == "finished"]
    assert finished and finished[-1]["payload"]["misplaced"] == 1, (
        "the run reported only what it managed to do, which is how an album it "
        "never reached became invisible"
    )


def test_an_album_can_be_pointed_at_where_it_now_lives(tmp_path: Path) -> None:
    """Relocate is the answer to the rename the search cannot follow."""
    library = _library(tmp_path)
    moving = library / "another album"
    moving.mkdir()
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    api = _api(tmp_path, FakeSource())
    api.open_folder(str(library))
    _finish_reads(api)
    unit_id = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}[
        "another album"
    ]
    renamed = library / "the name it was given"
    moving.rename(renamed)

    result = api.relocate_album(unit_id, str(renamed))

    assert result["ok"], result.get("error")
    assert result["album"]["folder"] == "the name it was given"
    assert result["album"]["folder_missing"] is False
    assert result["moved"]["now"] == str(renamed)


def test_relocating_onto_a_different_album_is_refused_by_the_audio(tmp_path: Path) -> None:
    """The folder pointed at is a candidate; the audio is what decides.

    Adopting the wrong one would attach this album's identification, plan and
    corrections to a record they are not about — the same reasoning applied to
    the candidates the search itself turns up.
    """
    library = _library(tmp_path)
    moving = library / "another album"
    moving.mkdir()
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    api = _api(tmp_path, FakeSource())
    api.open_folder(str(library))
    _finish_reads(api)
    unit_id = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}[
        "another album"
    ]
    stranger = tmp_path / "somebody else's record"
    stranger.mkdir()
    shutil.copy(FIXTURES / "tone.flac", stranger / "one.flac")
    shutil.copy(FIXTURES / "tone.flac", stranger / "two.flac")

    result = api.relocate_album(unit_id, str(stranger))

    assert result["ok"] is False
    assert "different album" in str(result["error"])
    # And nothing moved: the album is still recorded where it was.
    assert api.state()["albums"]


def test_scanning_with_nothing_marked_refuses_and_says_so(tmp_path: Path) -> None:
    """Refusing in one sentence beats scanning a whole shelf nobody asked for."""
    source = FakeSource()
    api = _api(tmp_path, source)

    result = api.scan_selected([])

    assert result["ok"] is False
    assert "marked" in str(result["error"]).lower()


def test_organized_and_done_are_one_word(tmp_path: Path) -> None:
    """An album organized in this session and one organized earlier are one word.

    Two words for one fact make the count of organized albums wrong, make a
    rescan remember only this session, and make `Plan this album again` refuse
    on one word and work on the other.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()

    just_now = api.state()["albums"][0]
    assert just_now["organized"] is True

    # And after a restart, when it is the same fact told by a different session.
    later = _reopen(api).state()["albums"][0]
    assert later["organized"] is True

    # The gesture works on both.
    assert api.plan_anyway(int(later["unit_id"]))["ok"] is True


def test_a_dropped_folder_is_read_and_marked_but_not_looked_up(tmp_path: Path) -> None:
    """Dropping a folder reads it, as choosing a folder does.

    If dropping identified everything on the spot, the albums would arrive
    already done, marked for a Scan that had nothing left to do.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    assert api.adopt_folders([str(library / "marina do acordeao - forro")])["ok"]
    _finish_reads(api)

    albums = api.state()["albums"]
    assert len(albums) == 1
    assert albums[0]["looked_at"] is False
    assert source.searches == [], "dropping a folder must not spend a rate limit"

    adopted = [event for event in api.events() if event["type"] == "adopted"]
    assert adopted and adopted[-1]["payload"]["unit_ids"] == [albums[0]["unit_id"]]


def test_a_reverted_run_can_be_done_again_exactly(tmp_path: Path) -> None:
    """The undo of the undo, and nothing else.

    The proof that no catalogue is asked is a *count*, not a source that
    refuses: every metadata call in this application sits inside a handler that
    logs the failure and carries on, so a source that raises is caught by the
    code under test and the promise stays untested.
    """
    library = _library(tmp_path)
    before = _snapshot(library)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    run = api.apply_automatic()
    organized = _snapshot(library)
    assert (library / ORGANIZED).is_dir()
    assert api.revert_run(run["run_id"])["ok"]
    assert _snapshot(library) == before
    asked = len(source.searches)

    redone = api.redo_run(run["run_id"])

    assert redone["ok"] and redone["redone"] == 1
    assert _snapshot(library) == organized, "the run came back exactly as it was"
    assert len(source.searches) == asked, "redoing a run must ask no source anything"
    # And it is a run again: the two directions alternate without limit.
    assert api.revert_run(run["run_id"])["ok"]
    assert _snapshot(library) == before


def test_redoing_a_run_that_was_never_reverted_says_so(tmp_path: Path) -> None:
    """A refusal with a reason, not a silent nothing."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    run = api.apply_automatic()

    answer = api.redo_run(run["run_id"])

    assert answer["ok"] is False
    assert answer["redone"] == 0
    assert "nothing to do again" in " ".join(answer["failures"])


def test_redoing_a_plan_whose_folder_has_gone_refuses_with_the_reason(tmp_path: Path) -> None:
    """The disk moved under it, so it is refused — and the refusal names why.

    Redoing replays a stored plan rather than working one out, which is exactly
    why this case has to be answered for: the plan describes paths, and paths
    are the one thing this app does not own.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    run = api.apply_automatic()
    plan_id = api.state()["history"][0]["plan_id"]
    assert api.revert_run(run["run_id"])["ok"]
    # The folder was tidied up by hand in between.
    shutil.rmtree(library / "marina do acordeao - forro")
    (library / "marina do acordeao - forro").mkdir()

    answer = api.redo_plan(int(plan_id))

    assert answer["ok"] is False
    assert "again" in str(answer["error"]), "the refusal has to say a scan is the fix"


def test_an_album_whose_folder_moved_on_is_marked_and_not_taken_away(tmp_path: Path) -> None:
    """The check notices without a restart, and nothing leaves the shelf.

    An album filed away with the window open is noticed by this check, which
    the window polls on every arrival at the Library. Taking the row off the
    shelf would look, from the screen, the same as a deletion.

    Here the album cannot be found: the folder was moved *and* renamed, to a name
    and a place this app has never been shown. So it stays, marked.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    # Nothing has moved, so nothing goes. A check that removes rows on a quiet
    # library is worse than no check at all.
    assert api.library_present() == {
        "ok": True,
        "missing": [],
        "followed": [],
        "checked": 1,
    }
    assert len(api.state()["albums"]) == 1

    shutil.move(str(library / "marina do acordeao - forro"), str(tmp_path / "collection"))

    answer = api.library_present()

    assert answer["ok"] and answer["missing"] == [unit_id]
    shelf = api.state()["albums"]
    assert [album["unit_id"] for album in shelf] == [unit_id], "the card stays on the shelf"
    assert shelf[0]["folder_missing"] is True, "wearing the one fact that matters about it"
    # Nothing was deleted: the album is where it was put, and pointing at it —
    # or scanning there — brings the whole row back.
    assert (tmp_path / "collection" / "aaa.flac").is_file()


def test_an_organized_album_is_not_taken_for_a_folder_that_left(tmp_path: Path) -> None:
    """The trap this check walks straight into if it reads the wrong path.

    Organising *renames the folder*, and this check must answer about where the
    album is now. It asks the database row, which ``_follow_the_rename`` moves
    with the folder.

    `_follow_the_album` moves the unit in memory too, because `Open album` and
    `Send to bench` read that value. Both paths agree, and this check leaves an
    organized album alone.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()
    assert (library / ORGANIZED).is_dir()
    in_memory = next(iter(api._albums.values())).unit.folder_path
    assert in_memory.is_dir(), "the album in memory has to point at a folder that is there"

    answer = api.library_present()

    assert answer["missing"] == [], "an album that was organized has not gone anywhere"
    assert len(api.state()["albums"]) == 1


def test_one_folder_recorded_twice_does_not_end_the_whole_sweep(tmp_path: Path) -> None:
    """One row that raises must not end the walk over every album.

    `library_present` walks every album and follows the ones that moved. A
    `UNIQUE constraint failed` on one row would take the whole walk down — no
    album followed and none let go, and a red toast over an unchanged shelf.

    The case is one folder recorded under a composed row *and* a decomposed
    one, both matching the move.
    """
    library = tmp_path / "library"
    folder = library / "Os Tapiris - Travessão (1971) [FLAC]"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    stored = api._store.library_units()[0]
    decomposed = unicodedata.normalize("NFD", str(stored.folder_path))
    assert decomposed != str(stored.folder_path), "the two spellings really do differ"
    with api._store._database.connect() as connection:
        # A cleared row, spelled the other way — invisible on screen and
        # matched by every lookup that offers both spellings.
        connection.execute(
            "INSERT INTO album_units "
            "(folder_path, unit_signature, track_count, cleared_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (decomposed, stored.unit_signature, 2),
        )
    # A shelf this app has seen an album in, which is where it looks for one
    # that is no longer where it was recorded.
    shelf = tmp_path / "collection"
    other = shelf / "Something Else (1980) [FLAC]"
    other.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", other / "ccc.flac")
    _scan_and_wait(api, shelf)
    shutil.move(str(stored.folder_path), str(shelf / stored.folder_path.name))

    answer = api.library_present()

    assert answer["ok"], answer
    assert api.state()["albums"], "the album is where it was filed, so it stays on the shelf"


def _twin_albums(tmp_path: Path, shared: int) -> Path:
    """Two albums that hold `shared` of the same recordings, and one of their own."""
    library = tmp_path / "library"
    # A different recording per shared slot, so the shared tracks are shared with
    # the *other* album and not with each other.
    sources = ("tone.flac", "tone.aiff", "tone.mp3", "tone.wav")
    for folder in ("Velame (2004) [FLAC]", "Velame (Remasterizado - 2022) [FLAC]"):
        (library / folder).mkdir(parents=True)
        for index in range(shared):
            source = sources[index]
            shutil.copy(
                FIXTURES / source,
                library / folder / f"0{index + 1}. Shared{Path(source).suffix}",
            )
    # One track each that is theirs alone, so neither album is a copy of the other.
    shutil.copy(
        FIXTURES / "tone-long.flac", library / "Velame (2004) [FLAC]" / "09. Only Here.flac"
    )
    shutil.copy(
        FIXTURES / "tone.m4a",
        library / "Velame (Remasterizado - 2022) [FLAC]" / "09. Only There.m4a",
    )
    return library


def test_two_albums_that_share_audio_are_one_question(tmp_path: Path) -> None:
    """The pair is the question, not the track.

    An original and its remaster may hold several identical tracks between
    them and several of their own. Asked track by track that is one identical
    click per shared track; asked once it is the question that matters.
    """
    library = _twin_albums(tmp_path, shared=3)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)

    answer = api.shared_albums()

    assert answer["ok"]
    pairs = answer["pairs"]
    assert len(pairs) == 1, "two albums sharing three tracks is one question"
    assert len(pairs[0]["tracks"]) == 3
    assert pairs[0]["verdict"] is None, "and it is unanswered until somebody answers it"
    assert not pairs[0]["same_album"]


def test_one_album_holding_the_same_recording_twice_is_paired_with_itself(
    tmp_path: Path,
) -> None:
    """One folder may keep the instrumental and the regular version of a track.

    If they are the same recording, one of the two names is wrong — which is
    worth reporting, and would be dropped by a rule that only ever compared one
    album against another.
    """
    library = tmp_path / "library"
    folder = library / "Pale Kiln"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "Pale Kiln.flac")
    shutil.copy(FIXTURES / "tone.flac", folder / "Pale Kiln - Instrumental.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)

    pairs = api.shared_albums()["pairs"]

    assert len(pairs) == 1 and pairs[0]["same_album"] is True
    assert pairs[0]["first"]["album"] == pairs[0]["second"]["album"] == "Pale Kiln"


def test_a_verdict_about_a_pair_survives_the_album_being_organized(tmp_path: Path) -> None:
    """`They belong in both` is about the albums, and organising renames folders.

    Keyed by content signature and never by path, so filing an album away or
    letting the app rename it does not lose what was already decided — a
    decision keyed by folder would be worse than none.
    """
    library = _twin_albums(tmp_path, shared=2)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    pair = api.shared_albums()["pairs"][0]

    api.set_shared_verdict(pair["first"]["signature"], pair["second"]["signature"], "both")
    moved = tmp_path / "filed away"
    moved.mkdir()
    shutil.move(str(Path(pair["first"]["path"])), str(moved / "Renamed By Hand"))
    _scan_and_wait(api, moved)

    answered = [p for p in api.shared_albums()["pairs"] if p["verdict"] == "both"]
    assert answered, "the verdict is about the album, not about the name it had"
    assert Path(answered[0]["first"]["path"]).name == "Renamed By Hand"


def test_answering_a_pair_touches_no_file(tmp_path: Path) -> None:
    """Answering records the answer; the application moves and deletes nothing.

    What to do with the audio is up to the user; this only stops the app asking.
    """
    library = _twin_albums(tmp_path, shared=2)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    pair = api.shared_albums()["pairs"][0]
    before = _snapshot(library)

    api.set_shared_verdict(pair["first"]["signature"], pair["second"]["signature"], "handled")

    assert _snapshot(library) == before, "not one file may move, be renamed or disappear"
    assert api.shared_albums()["pairs"][0]["verdict"] == "handled"
    # And it can be taken back, which puts the pair on screen again.
    api.set_shared_verdict(pair["first"]["signature"], pair["second"]["signature"], "")
    assert api.shared_albums()["pairs"][0]["verdict"] is None


def test_answering_from_one_album_answers_for_the_other(tmp_path: Path) -> None:
    """The two albums of a pair are linked, and one answer covers both.

    The mark is on both albums because neither is the wrong one, and an answer
    given on either is an answer about the pair. Two marks that had to be cleared
    separately would be one question asked twice.
    """
    library = _twin_albums(tmp_path, shared=2)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    pair = api.shared_albums()["pairs"][0]
    assert pair["first"]["unit_id"] != pair["second"]["unit_id"]

    api.set_shared_verdict(pair["first"]["signature"], pair["second"]["signature"], "both")

    answered = api.shared_albums()["pairs"][0]
    assert answered["verdict"] == "both", "one answer, and it is about the pair"
    assert (
        answered["first"]["unit_id"] and answered["second"]["unit_id"]
    ), "both albums are still named, so the decision can be seen and taken back"


def test_both_folders_of_a_pair_can_be_opened(tmp_path: Path) -> None:
    """The pair opens as two folders in the file manager, not in a player."""
    library = _twin_albums(tmp_path, shared=2)
    api = _api(tmp_path, FakeSource())
    opened: list[str] = []
    api._path_opener = opened.append
    _scan_and_wait(api, library)
    pair = api.shared_albums()["pairs"][0]

    answer = api.open_folders([pair["first"]["path"], pair["second"]["path"]])

    assert answer["ok"], answer
    assert len(opened) == 2 and all(Path(path).is_dir() for path in opened)


def _compilation(identifier: str, credits: tuple[str, ...]) -> ReleaseMetadata:
    """A release that calls itself Various Artists and credits each track.

    The album's own artist is what makes it a compilation — that judgement
    belongs to the source — and a compilation is the only kind of album that
    writes a track's artist into its file name.
    """
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=identifier,
        title="Assorted Sides",
        artists=(ArtistMetadata(name="Various"),),
        tracks=tuple(
            TrackMetadata(
                title=f"Faixa {index}",
                position=index,
                position_on_medium=index,
                duration_ms=duration,
                artists=(ArtistMetadata(name=credit),),
            )
            for index, (duration, credit) in enumerate(
                zip((400, 900), credits, strict=True), start=1
            )
        ),
        released_on=date(1998, 3, 1),
    )


def _compilation_api(tmp_path: Path, credits: tuple[str, ...]) -> tuple[LibraryApi, int]:
    library = _library(tmp_path)
    release = _compilation("va1", credits)
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"va1": release}))
    _scan_and_wait(api, library)
    return api, api.state()["albums"][0]["unit_id"]


def _tags_written(album: dict[str, object], name: str) -> dict[str, list[str]]:
    """The tags one operation of this plan would write, by the file it renames."""
    for operation in album["operations"]:  # type: ignore[index]
        if operation["kind"] == "write_tags" and operation["target"].endswith(name):
            return dict(operation["after"]["tags"])
    raise AssertionError(f"this plan writes no tags to {name}")


def test_a_corrected_credit_names_the_file_whole_and_tags_it_split(tmp_path: Path) -> None:
    """One correction, two facts, and they are not the same string.

    A compilation credits track one to one artist and the correction names two,
    separated by a comma. The file name has to read exactly that, comma and
    all, because the name is what was typed. The `artist` tag has to hold the
    two names separately, because players group by artist and one value holding
    two names groups as neither.
    """
    api, unit_id = _compilation_api(tmp_path, ("Olga Hirt", "Elba Soriano"))

    corrected = api.correct(unit_id, track_artists={"1": "Franco Hirt, Olga Hirt"})

    assert corrected["ok"]
    album = corrected["album"]
    renames = [
        Path(str(operation["after"]["path"])).name
        for operation in album["operations"]
        if operation["kind"] == "rename_file"
    ]
    assert "01. Franco Hirt, Olga Hirt - Faixa 1.flac" in renames, renames
    assert _tags_written(album, "aaa.flac")["artist"] == ["Franco Hirt", "Olga Hirt"]
    # The other track is untouched, so the correction is about one track rather
    # than about the album.
    assert "02. Elba Soriano - Faixa 2.flac" in renames, renames
    assert _tags_written(album, "bbb.flac")["artist"] == ["Elba Soriano"]


def test_an_ampersand_in_a_credit_is_a_separator_too(tmp_path: Path) -> None:
    """`&` separates as `,` does, and that has a known cost.

    `Orvo, Zell & Timbu` reaches the tag as three values. No rule tells a
    separator from a name that contains one without knowing the band, and
    guessing is refused. The file name keeps the typed words whole, which is
    the half that stays right.
    """
    api, unit_id = _compilation_api(tmp_path, ("Olga Hirt", "Elba Soriano"))

    corrected = api.correct(unit_id, track_artists={"1": "Orvo, Zell & Timbu"})

    album = corrected["album"]
    assert any(
        Path(str(operation["after"]["path"])).name == "01. Orvo, Zell & Timbu - Faixa 1.flac"
        for operation in album["operations"]
        if operation["kind"] == "rename_file"
    ), "the file name is what was typed"
    assert _tags_written(album, "aaa.flac")["artist"] == ["Orvo", "Zell", "Timbu"]


def test_withdrawing_a_credit_gives_the_catalogue_back(tmp_path: Path) -> None:
    """An emptied field is a withdrawal, not a credit of nothing.

    The same rule the titles follow: submitting nothing restores what the source
    published rather than writing a blank into the name.
    """
    api, unit_id = _compilation_api(tmp_path, ("Olga Hirt", "Elba Soriano"))
    api.correct(unit_id, track_artists={"1": "Franco Hirt, Olga Hirt"})

    withdrawn = api.correct(unit_id, track_artists={"1": ""})

    assert withdrawn["ok"]
    album = withdrawn["album"]
    assert [entry for entry in album["corrections"] if entry["field"] == "track_artist"] == []
    assert any(
        Path(str(operation["after"]["path"])).name == "01. Olga Hirt - Faixa 1.flac"
        for operation in album["operations"]
        if operation["kind"] == "rename_file"
    ), "the catalogue's credit names the file again"
    assert _tags_written(album, "aaa.flac")["artist"] == ["Olga Hirt"]


def test_resubmitting_a_credit_unchanged_corrects_nothing(tmp_path: Path) -> None:
    """The field opens filled, so only a difference is a correction.

    What it is compared against is the credit as the *file name* writes it, which
    is not the artists joined by this app's own separator once one has been
    corrected — comparing against that would withdraw the very correction being
    resubmitted.
    """
    api, unit_id = _compilation_api(tmp_path, ("Olga Hirt", "Elba Soriano"))
    opened = api.album(unit_id)["album"]

    resubmitted = api.correct(
        unit_id,
        track_artists={
            str(track["position"]): track["artist"] for track in opened["release_tracks"]
        },
    )
    assert resubmitted["album"]["corrections"] == []

    api.correct(unit_id, track_artists={"1": "Franco Hirt, Olga Hirt"})
    again = api.album(unit_id)["album"]
    once_more = api.correct(
        unit_id,
        track_artists={
            str(track["position"]): track["artist"] for track in again["release_tracks"]
        },
    )

    stored = [
        entry for entry in once_more["album"]["corrections"] if entry["field"] == "track_artist"
    ]
    assert [entry["value"] for entry in stored] == ["Franco Hirt, Olga Hirt"]
    assert stored[0]["replaced"] == "Olga Hirt", "the record says what it overruled"


def test_only_a_compilation_offers_a_credit_to_type(tmp_path: Path) -> None:
    """The window is told which albums have a place for it.

    A track's artist reaches a file name on a compilation and nowhere else, so
    offering the field on a single artist's album would be a field whose value
    the name has nowhere to put. The fact is decided here rather than in the
    window, by the same rule that decides the name.
    """
    api, unit_id = _compilation_api(tmp_path, ("Olga Hirt", "Elba Soriano"))
    compilation = api.album(unit_id)["album"]

    assert compilation["is_various"] is True
    assert [track["artist"] for track in compilation["release_tracks"]] == [
        "Olga Hirt",
        "Elba Soriano",
    ]

    library = _library(tmp_path / "second")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    solo = _api(tmp_path / "second", source)
    _scan_and_wait(solo, library)
    alone = solo.album(solo.state()["albums"][0]["unit_id"])["album"]

    assert alone["is_various"] is False
    # The field is not offered, and the name proves why: nothing on this album
    # writes a track's artist into a file name.
    assert all(
        "Marina do Acordeão - Faixa" not in str(operation["after"].get("path", ""))
        for operation in alone["operations"]
        if operation["kind"] == "rename_file"
    )


def test_withdrawing_a_title_gives_the_catalogue_back(tmp_path: Path) -> None:
    """The same withdrawal, on the title field.

    If `refine` wrote the corrections into the candidate it returned, then
    after one correction the album *in memory* would be the correction.
    Emptying the field would take the row out of the database and leave the
    name on screen exactly as it was, and the next re-plan would correct an
    already corrected release.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    corrected = api.correct(unit_id, track_titles={"1": "Tral de Quinde"})
    assert any(
        Path(str(operation["after"]["path"])).name == "01. Tral de Quinde.flac"
        for operation in corrected["album"]["operations"]
        if operation["kind"] == "rename_file"
    ), "the correction reaches the name in the first place"

    withdrawn = api.correct(unit_id, track_titles={"1": ""})

    assert withdrawn["album"]["corrections"] == []
    assert [track["title"] for track in withdrawn["album"]["release_tracks"]] == [
        "Faixa 1",
        "Faixa 2",
    ], "the field opens filled with the catalogue's title again"
    assert any(
        Path(str(operation["after"]["path"])).name == "01. Faixa 1.flac"
        for operation in withdrawn["album"]["operations"]
        if operation["kind"] == "rename_file"
    ), "and so does the name the plan would write"


def _album_where_one_word_differs(tmp_path: Path) -> tuple[LibraryApi, int]:
    """A scanned album whose first file spells one word differently.

    `Brumal Tavo` in the file's tag against `Brumal Tago` in the catalogue: one
    word, one edit, and both are things a person types. That is the line the
    window marks and offers to keep.
    """
    library = _library(tmp_path)
    folder = next(library.iterdir())
    for name, title in (("aaa.flac", "Brumal Tavo"), ("bbb.flac", "Faixa 2")):
        audio = FLAC(folder / name)
        audio["title"] = [title]
        audio.save()
    catalogue = replace(
        _release("r1", (400, 900)),
        tracks=(
            TrackMetadata(title="Brumal Tago", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Faixa 2", position=2, position_on_medium=2, duration_ms=900),
        ),
    )
    api = _api(tmp_path, FakeSource(summaries=(catalogue,), details={"r1": catalogue}))
    _scan_and_wait(api, library)
    return api, int(str(api.state()["albums"][0]["unit_id"]))


def _first_line(album: dict[str, object]) -> dict[str, object]:
    return dict(album["release_tracks"][0])  # type: ignore[index,arg-type]


def test_a_kept_line_says_so_and_carries_the_way_back(tmp_path: Path) -> None:
    """A line kept from the file says so, and can be given back.

    `Keep yours` writes the file's spelling into the field. Without a trace of
    the decision, a kept line and a line the catalogue simply got right would
    draw identically, and the one gesture that overrides the catalogue could
    not be undone from the screen.
    """
    api, unit_id = _album_where_one_word_differs(tmp_path)

    offered = _first_line(api.album(unit_id)["album"])
    assert offered["yours"] == "Brumal Tavo", "the offer is made in the first place"
    assert offered["kept"] == "", "and nothing is decided yet"

    kept = api.correct(unit_id, track_titles={"1": "Brumal Tavo"})

    assert kept["ok"]
    line = _first_line(kept["album"])
    assert line["kept"] == "Brumal Tavo", "the aside says what stands"
    assert line["yours"] == "", "and stops making an offer already answered"
    assert line["title"] == "Brumal Tavo", "the field is filled with the kept word as before"
    assert kept["album"]["title_corrections"] == 1


def test_pressing_both_ways_in_turn_leaves_the_album_exactly_as_it_started(
    tmp_path: Path,
) -> None:
    """The round trip, which is why the per-line way back needs no question.

    `Use the catalogue's` is drawn only where withdrawing restores the very
    offer it came from, so this pair of presses is a closed loop: the kept
    spelling exists nowhere but the file it was read from, and the file is
    untouched.
    """
    api, unit_id = _album_where_one_word_differs(tmp_path)
    before = api.album(unit_id)["album"]

    assert api.correct(unit_id, track_titles={"1": "Brumal Tavo"})["ok"]
    back = api.withdraw_spelling(unit_id, 1)

    assert back["ok"]
    assert back["album"]["corrections"] == [], "the row is gone from the database"
    assert back["album"]["title_corrections"] == 0
    assert _first_line(back["album"]) == _first_line(
        before
    ), "and the line is the offer it was before it was answered"
    assert [
        operation["after"]["path"]
        for operation in back["album"]["operations"]
        if operation["kind"] == "rename_file"
    ] == [
        operation["after"]["path"]
        for operation in before["operations"]
        if operation["kind"] == "rename_file"
    ], "including the names the plan would write"


def test_both_ways_out_of_a_correction_leave_the_same_album(tmp_path: Path) -> None:
    """The explicit route and the emptied field are one door underneath.

    Withdrawing is also possible by sending a value equal to the catalogue's —
    the window still empties fields. Two routes reaching the same end by their
    own means can drift apart, so both end in `_replan_after_correction` and
    this test asserts that they agree.
    """
    api, unit_id = _album_where_one_word_differs(tmp_path)

    assert api.correct(unit_id, track_titles={"1": "Brumal Tavo"})["ok"]
    by_field = api.correct(unit_id, track_titles={"1": ""})["album"]
    assert api.correct(unit_id, track_titles={"1": "Brumal Tavo"})["ok"]
    by_route = api.withdraw_spelling(unit_id, 1)["album"]

    for key in ("release_tracks", "operations", "corrections", "applicable", "decision"):
        assert by_route[key] == by_field[key], key


def test_withdrawing_where_no_correction_stands_is_refused(tmp_path: Path) -> None:
    """A withdrawal that withdrew nothing must not answer `ok`.

    The window would redraw the same screen, the toast would say the catalogue
    names that track again, and the correction would still be in the database —
    a success message about a thing that did not happen.
    """
    api, unit_id = _album_where_one_word_differs(tmp_path)

    refused = api.withdraw_spelling(unit_id, 1)

    assert refused == {"ok": False, "error": "Nothing of yours stands on that line."}
    assert api.withdraw_title_corrections(unit_id) == {
        "ok": False,
        "error": "Nothing of yours stands on this album's titles.",
    }


def test_the_albums_own_way_out_states_the_number_before_it_writes(tmp_path: Path) -> None:
    """The second gesture asks first, and the number is the whole question.

    It undoes several decisions at once and reaches typed titles — which no
    file and no catalogue holds — so it is the one that stops to say how many.
    Nothing is written until the answer comes back.
    """
    api, unit_id = _album_where_one_word_differs(tmp_path)
    assert api.correct(unit_id, track_titles={"1": "Brumal Tavo", "2": "Segunda Faixa"})["ok"]

    asked = api.withdraw_title_corrections(unit_id)

    assert asked == {"ok": True, "confirm": "withdraw_titles", "count": 2}
    assert len(api.album(unit_id)["album"]["corrections"]) == 2, "and nothing was written"

    done = api.withdraw_title_corrections(unit_id, True)

    assert done["ok"] and done["dropped"] == 2
    assert done["album"]["corrections"] == []
    assert [track["title"] for track in done["album"]["release_tracks"]] == [
        "Brumal Tago",
        "Faixa 2",
    ], "the catalogue names both tracks again"
    assert done["album"]["title_corrections"] == 0


def test_the_albums_way_out_reaches_a_title_the_line_gesture_will_not_touch(
    tmp_path: Path,
) -> None:
    """A typed title gets no one-click button, and is not stranded either.

    The line gesture is drawn only where it restores an offer, so a title that
    was typed keeps its aside shut — one click must not be able to take words
    nothing on this screen could give back. The album's gesture is where those
    go, and it says how many first.
    """
    api, unit_id = _album_where_one_word_differs(tmp_path)
    typed = api.correct(unit_id, track_titles={"1": "Brumal Tavo de Estrada"})

    line = _first_line(typed["album"])
    assert line["title"] == "Brumal Tavo de Estrada"
    assert line["kept"] == "" and line["yours"] == "", "no per-line way out of a typed title"
    assert typed["album"]["title_corrections"] == 1, "but the album knows it is there"

    assert api.withdraw_title_corrections(unit_id) == {
        "ok": True,
        "confirm": "withdraw_titles",
        "count": 1,
    }
    dropped = api.withdraw_title_corrections(unit_id, True)
    assert dropped["dropped"] == 1
    assert (
        _first_line(dropped["album"])["yours"] == "Brumal Tavo"
    ), "and withdrawing it puts the album back where the offer is made again"


class _FakeSpectrograms:
    """Stand in for the renderer, counting what the API actually asks it for."""

    def __init__(self, tmp_path: Path, available: bool = True) -> None:
        self.folder = tmp_path / "spectrograms"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.renders: list[tuple[str, str, int]] = []
        self.rates: list[int | None] = []
        self._available = available

    def render(
        self,
        path: Path,
        signature: str,
        limit_bytes: int,
        sample_rate: int | None = None,
    ) -> object | None:
        """Record the request and hand back a picture, as the renderer would."""
        self.renders.append((path.name, signature, limit_bytes))
        self.rates.append(sample_rate)
        if not self._available:
            return None
        picture = self.folder / f"{signature}.png"
        picture.write_bytes(b"PNG-ish")
        return SimpleNamespace(
            path=picture, width=1280, height=480, top_hertz=22050.0, from_cache=False
        )

    def held_bytes(self) -> int:
        """What the folder holds, as the window is told."""
        return sum(entry.stat().st_size for entry in self.folder.glob("*.png"))

    def clear(self) -> int:
        """Delete the pictures and report the bytes."""
        freed = self.held_bytes()
        for entry in self.folder.glob("*.png"):
            entry.unlink()
        return freed


def _quality_api(tmp_path: Path, **kwargs: object) -> tuple[LibraryApi, Path]:
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source, **kwargs)  # type: ignore[arg-type]
    _scan_and_wait(api, library)
    return api, library / "marina do acordeao - forro"


def test_a_spectrogram_is_drawn_only_when_it_is_asked_for(tmp_path: Path) -> None:
    """Nothing is drawn by a scan, and the picture carries its own scale.

    No spectrogram is generated automatically — not by a scan, not by opening
    the album. So a scan that measured this library must have asked the
    renderer for nothing at all, and only the call that follows a press
    produces a picture.
    """
    spectrograms = _FakeSpectrograms(tmp_path)
    api, folder = _quality_api(tmp_path, spectrograms=spectrograms)

    assert spectrograms.renders == [], "a scan draws no pictures"

    drawn = api.track_spectrogram(str(folder), "aaa.flac")

    assert drawn["ok"]
    assert [name for name, _, _ in spectrograms.renders] == ["aaa.flac"]
    assert str(drawn["image"]).startswith("data:image/png;base64,")
    # What turns a frequency into a row, which is the whole reason the window can
    # mark the wall on the picture.
    assert drawn["top_hertz"] == 22050.0
    assert drawn["cache_bytes"] > 0
    # The reading beside the picture is the analyzer's, not the picture's.
    assert "cutoff" in drawn and "decay_db" in drawn and "ceiling_db" in drawn


def test_the_encoder_walls_the_picture_draws_are_the_ones_the_verdict_names(
    tmp_path: Path,
) -> None:
    """The rulers on the spectrogram and the rule that names a bitrate are one table.

    The picture draws a line where each quality habitually cuts, with the
    bitrate said. Written into the window they would be a second copy of
    `REFERENCE_WALLS`, and two copies are two answers once one of them moves —
    the picture would go on claiming 192 kbps at a frequency the verdict had
    stopped calling 192.

    Only the rungs this stream can hold: a line above a file's own Nyquist limit
    is a line drawn off the top of its picture.
    """
    api, folder = _quality_api(tmp_path, spectrograms=_FakeSpectrograms(tmp_path))

    drawn = api.track_spectrogram(str(folder), "aaa.flac")

    assert drawn["top_hertz"] == 22050.0
    assert drawn["encoder_walls"] == [
        {"hertz": hertz, "bitrate": bitrate} for hertz, bitrate in REFERENCE_WALLS
    ]
    assert all(wall["hertz"] < drawn["top_hertz"] for wall in drawn["encoder_walls"])


def test_the_renderer_is_told_the_rate_so_it_can_cap_what_it_draws(tmp_path: Path) -> None:
    """The ceiling on the drawing needs the rate, and only this side has it in hand.

    ffmpeg reports the rate *after* the render, so a renderer left to learn it
    there would have to draw the picture and then draw it again. The scan that
    just read this folder knows it, so it costs nothing to hand over — and if it
    stops being handed over, every high-rate picture silently goes back to
    spending half its height on frequencies no music reaches.
    """
    spectrograms = _FakeSpectrograms(tmp_path)
    api, folder = _quality_api(tmp_path, spectrograms=spectrograms)

    api.track_spectrogram(str(folder), "aaa.flac")

    assert spectrograms.rates == [44_100]


def test_the_configured_cache_ceiling_is_what_the_renderer_is_given(tmp_path: Path) -> None:
    """The limit is set in megabytes, and it has to reach the renderer as bytes."""
    spectrograms = _FakeSpectrograms(tmp_path)
    api, folder = _quality_api(tmp_path, spectrograms=spectrograms)

    api.set_settings({"spectrogram_cache_mb": 120})
    api.track_spectrogram(str(folder), "aaa.flac")

    assert spectrograms.renders[-1][2] == 120 * 1024 * 1024
    assert api.settings()["spectrogram_cache_mb"] == 120


def test_a_file_that_is_no_longer_there_is_named_in_the_refusal(tmp_path: Path) -> None:
    """The ordinary reason to be here is a file renamed since the screen was drawn."""
    api, folder = _quality_api(tmp_path, spectrograms=_FakeSpectrograms(tmp_path))

    refused = api.track_spectrogram(str(folder), "gone.flac")

    assert not refused["ok"]
    assert "gone.flac" in str(refused["error"])


def test_without_ffmpeg_the_window_is_told_so(tmp_path: Path) -> None:
    """ffmpeg is an external binary, so its absence is an answer and not a blank frame."""
    api, folder = _quality_api(tmp_path)

    refused = api.track_spectrogram(str(folder), "aaa.flac")

    assert not refused["ok"]
    assert "ffmpeg" in str(refused["error"])
    # And clearing a cache that was never composed is not an error either.
    assert api.clear_spectrograms() == {"ok": True, "freed": 0}


def test_clearing_the_cache_reports_what_came_back(tmp_path: Path) -> None:
    """Pictures only — the line `Clear downloaded` holds, for the same reason."""
    spectrograms = _FakeSpectrograms(tmp_path)
    api, folder = _quality_api(tmp_path, spectrograms=spectrograms)
    api.track_spectrogram(str(folder), "aaa.flac")
    assert spectrograms.held_bytes() > 0

    cleared = api.clear_spectrograms()

    assert cleared["ok"] and int(cleared["freed"]) > 0
    assert spectrograms.held_bytes() == 0
    assert (folder / "aaa.flac").is_file(), "no audio file is touched"


def test_the_window_is_given_the_rungs_the_wall_was_measured_on(tmp_path: Path) -> None:
    """The cutoff is one of the probes, so the window must be able to say so.

    `cutoff_hertz` is the lowest *probed* band that measured silent — a rung, not
    a frequency. The picture can go dark several hundred hertz below the rung
    that reports it, so a hairline at the number would look exact and be wrong.
    The rungs travel with the picture and the window draws the interval.
    """
    api, folder = _quality_api(tmp_path, spectrograms=_FakeSpectrograms(tmp_path))

    drawn = api.track_spectrogram(str(folder), "aaa.flac")

    assert drawn["ok"]
    assert list(drawn["probes"]) == list(PROBE_FREQUENCIES)
    # Named rather than only counted: three of these sit below 16 kHz, because
    # a 128 kbps wall is invisible from above it, and the window places its
    # interval against whichever pair the step fell between.
    assert list(drawn["probes"]) == [
        13_000,
        14_000,
        15_000,
        16_000,
        17_500,
        19_000,
        20_000,
        21_500,
    ]


def test_a_track_with_no_file_carries_the_files_nothing_claimed(tmp_path: Path) -> None:
    """The two halves of one failed pairing arrive together.

    `no file here` on a track and, rows below, the file itself as `left alone`
    reads as the application losing a file that is in the folder. So the
    track's own row carries what is loose, and the loose file's row says
    whether it is reachable from above, so the window need not say it twice.

    Offered, never asserted: the shortlist has no floor, because the pairing
    failed precisely by not clearing one. What it promises is "these are loose",
    which is a fact, rather than "this is the one", which would be a guess.
    """
    library = _library(tmp_path)
    # Two published tracks against the two files, with lengths that pair the
    # first and leave the second track claiming nothing.
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(
            TrackMetadata(title="Tral de Quinde", position=1, duration_ms=400),
            TrackMetadata(title="Um Nome Que Nada Diz", position=2, duration_ms=99_000),
        ),
        released_on=date(1955, 3, 1),
    )
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    album = _opened(api, api.state()["albums"][0]["unit_id"])

    loose_tracks = [row for row in album["evidence"] if row["status"] == "unmatched_track"]
    loose_files = [row for row in album["evidence"] if row["status"] == "unmatched_file"]
    assert loose_tracks and loose_files, "this album really does have a loose half of each"
    candidates = loose_tracks[0]["candidates"]
    assert [entry["file"] for entry in candidates] == [row["file"] for row in loose_files]
    entry = candidates[0]
    assert 0.0 <= entry["similarity"] <= 1.0
    assert "delta_ms" in entry and "file_ms" in entry
    # And the loose file knows it is reachable from the track's row, so the
    # window can stop repeating it at the bottom.
    assert all(row["offered_above"] is True for row in loose_files)


def test_a_file_the_release_has_no_track_for_is_still_said_once(tmp_path: Path) -> None:
    """More files than tracks: nothing is missing, so the file's own row is the only place.

    The suppression of the duplicate must not swallow the case it does not apply
    to — a file the release genuinely has no track for has no track's row to be
    mentioned on.
    """
    library = _library(tmp_path)
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(TrackMetadata(title="Tral de Quinde", position=1, duration_ms=400),),
        released_on=date(1955, 3, 1),
    )
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    album = _opened(api, api.state()["albums"][0]["unit_id"])

    loose_files = [row for row in album["evidence"] if row["status"] == "unmatched_file"]
    assert loose_files, "one file has no track on this release"
    assert all(row["offered_above"] is False for row in loose_files)
    assert [row for row in album["evidence"] if row["status"] == "unmatched_track"] == []


def test_the_restore_never_lands_on_an_album_the_scan_just_put_there(tmp_path: Path) -> None:
    """The restore gives way to an album a scan put in while it was reading.

    `_restore_unit` checks the map, then reads a whole folder off the disk, then
    writes. A check that far from its write is not a guard: the scan runs on
    another thread and can put that album in during the read, and the restore's
    answer is the *older* one — no candidate, no plan, `not looked at`. A
    freshly scanned album would lose the cover plan the scan had just built.

    The scanner here does what the race does — it puts the album in while the
    restore is reading — so the test fails if the second check is ever removed.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    _scan_and_wait(api, library)
    state = next(iter(api._albums.values()))
    unit_id, unit = state.unit_id, state.unit
    stored = next(row for row in api._store.library_units() if row.unit_id == unit_id)

    # As if the scan had just finished while the restore was reading the folder.
    api._albums.clear()
    real_scan = api._pipeline.scanner.scan

    def scan_and_race(root: Path, *args: object, **kwargs: object) -> object:
        found = real_scan(root, *args, **kwargs)
        api._albums[unit_id] = replace(state, looked_at=True)
        return found

    api._pipeline.scanner.scan = scan_and_race  # type: ignore[method-assign]

    assert api._restore_unit(stored) is False, "the restore must give way to the scan"
    survivor = api._albums[unit_id]
    assert survivor.looked_at is True, "what the scan put there is what stayed"
    assert survivor.unit is unit


def test_remembering_an_album_twice_keeps_the_first(tmp_path: Path) -> None:
    """The guard itself, said plainly: the second writer loses."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    _scan_and_wait(api, library)
    state = next(iter(api._albums.values()))

    assert api._remember_if_absent(state.unit_id, replace(state, rejected=True)) is False
    assert api._albums[state.unit_id].rejected is False
    api._albums.clear()
    assert api._remember_if_absent(state.unit_id, state) is True


def _twice_over(tmp_path: Path, first: tuple[str, ...], second: tuple[str, ...]) -> Path:
    """A folder holding two complete runs of numbered tracks."""
    library = tmp_path / "library"
    folder = library / "lauro e seus pardais"
    folder.mkdir(parents=True)
    store = MutagenTagStore()
    for group, titles in ((1974, first), (1977, second)):
        for number, title in enumerate(titles, start=1):
            path = folder / f"{group}-{number:02d}.flac"
            shutil.copy(FIXTURES / ("tone.flac" if number % 2 else "tone-long.flac"), path)
            store.write(
                path,
                {
                    "title": (title,),
                    "artist": ("Lauro e Seus Pardais",),
                    "album": ("Lauro e Seus Pardais",),
                    "tracknumber": (str(number),),
                    "date": (f"{group}-01-25",),
                },
            )
    return library


def test_a_folder_holding_two_albums_is_refused_with_what_was_measured(tmp_path: Path) -> None:
    """Two albums in one folder are refused, with the measurement as the reason.

    Two self-titled LPs in one folder: pairing the files of one against the
    adopted release orphans the files of the other, and the album is ambiguous
    with no sentence saying why. The folder is not split: the album arrives
    identified, with no plan to approve, carrying the measurement as its
    reason.
    """
    library = _twice_over(
        tmp_path,
        ("Pelma Runo", "Zib", "Ovo Tral Quinde", "Farlo Munte"),
        ("Drusa Vel Tiro", "412 Onzes", "Tral Quinde Novar", "Ubo Selta Rimo"),
    )
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Lauro E Seus Pardais",
        artists=(ArtistMetadata(name="Lauro E Seus Pardais"),),
        tracks=tuple(
            TrackMetadata(title=title, position=index, duration_ms=duration)
            for index, (title, duration) in enumerate(
                zip(
                    (
                        "Drusa Vel Tiro",
                        "412 Onzes",
                        "Tral Quinde Novar",
                        "Ubo Selta Rimo",
                    ),
                    (400, 900, 400, 900),
                    strict=True,
                ),
                start=1,
            )
        ),
        released_on=date(1977, 1, 25),
    )
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)

    album = api.state()["albums"][0]
    opened = api.album(album["unit_id"])["album"]

    assert album["decision"] == "review", "never applied unattended"
    assert opened["operations"] == [], "nothing is planned for a folder that is not one album"
    assert "2 different albums of 4 tracks" in opened["reason"]
    assert "on different songs" in opened["reason"]
    # And it is still identified, so what the app thinks it found can be seen.
    assert opened["release_id"] == "r1"


def test_an_ordinary_album_is_planned_as_before(tmp_path: Path) -> None:
    """The rule is silent for an ordinary album, and this is one of them."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    album = api.state()["albums"][0]
    opened = api.album(album["unit_id"])["album"]

    assert album["decision"] == "automatic"
    assert opened["operations"], "a folder the rule says nothing about is planned as always"


def test_a_witness_page_can_be_opened_and_a_look_alike_cannot() -> None:
    """The link this app offers has to open, or the attribution is decoration.

    The gate is deliberately not a browser, and a witness page is exactly the
    gesture it exists for: a witness that contributed to a decision is
    attributed with a link, so that link must not be refused as "not a
    catalogue release page".
    """
    assert _is_witness_page("https://music.apple.com/us/album/example/123") is True
    assert _is_witness_page("https://open.spotify.com/album/abc") is True
    # Host matched exactly, so nothing rides in on a substring, and plain http
    # is not a page this app opens.
    assert _is_witness_page("https://music.apple.com.evil.example/x") is False
    assert _is_witness_page("https://evil.example/?music.apple.com") is False
    assert _is_witness_page("http://music.apple.com/x") is False


def _opened(api: LibraryApi, unit_id: int) -> dict[str, object]:
    """The album as the dialog ends up showing it.

    Two answers, not one: the dialog is drawn from what is already known and
    the witness's line arrives as its own event. What the screen settles on is
    the second, so that is what a test about the dialog's contents reads.
    """
    return _after_the_witness(api, unit_id, api.album(unit_id)["album"])


def _after_the_witness(
    api: LibraryApi, unit_id: int, answered: dict[str, object]
) -> dict[str, object]:
    """Return the album once the witness has had its say, or as it is if none comes."""
    for _ in range(200):
        for event in api.events():
            if event["type"] == "album" and event["payload"]["album"]["unit_id"] == unit_id:
                return dict(event["payload"]["album"])
        with api._asking_lock:
            asking = unit_id in api._asking
        if not asking:
            return answered
        time.sleep(0.01)
    raise AssertionError("the witness never answered")


def _loose_file(album: dict[str, object]) -> dict[str, object]:
    return next(row for row in album["evidence"] if row["status"] == "unmatched_file")


def _row_for(album: dict[str, object], position: int) -> dict[str, object]:
    return next(row for row in album["evidence"] if row.get("track_position") == position)


def test_pairing_a_file_to_a_track_by_hand_settles_what_nothing_could(tmp_path: Path) -> None:
    """No threshold covers every case, so a file can be paired to a track by hand.

    A release may list one title twice: one file then scores the same against
    both, and a rescue that must beat its runner-up cannot choose between the
    same words. The file stays loose, both tracks say `no file here`, and only
    a person can settle it.

    Three things are asserted together because they are one decision. The pairing
    is stored by the audio's own signature, never by the file name the very next
    apply rewrites. It is stored against the release it answers to, because a
    position belongs to a tracklist. And the album stays in review however well
    it now closes: no batch reaches an album that was just decided by hand.
    """
    library = _library(tmp_path)
    # Track 2 is published five seconds from anything in the folder, so the
    # matcher pairs one file and leaves the other loose — the shape of a failure.
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),),
        details={"r1": _release("r1", (400, 5_000))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    before = _opened(api, unit_id)
    loose = _loose_file(before)
    assert loose["file"] == "bbb.flac"
    assert _row_for(before, 2)["status"] == "unmatched_track"

    paired = api.correct(unit_id, track_files={"2": str(loose["signature"])})

    assert paired["ok"]
    album = paired["album"]
    row = _row_for(album, 2)
    assert row["status"] == "paired" and row["file"] == "bbb.flac"
    assert row["by_hand"] is True, "the row says who decided, so it can be undone"
    assert album["decision"] == "review", "an album paired by hand never joins a batch"
    assert api.apply_automatic()["applied"] == 0
    stored = [entry for entry in album["corrections"] if entry["field"] == "track_file"]
    assert stored == [
        {
            "field": "track_file",
            "track_position": 2,
            "value": loose["signature"],
            "replaced": None,
            "release_key": "discogs:r1",
        }
    ]
    assert str(loose["signature"]) != "bbb.flac"
    assert any(
        operation["kind"] == "rename_file" and operation["target"].endswith("bbb.flac")
        for operation in album["operations"]
    ), "the paired file is now named after the track it was paired to"


def test_withdrawing_a_pairing_gives_the_question_back_to_the_matcher(tmp_path: Path) -> None:
    """A pairing is a correction, so taking it back restores the app's own answer.

    A correction that lives inside the album in memory cannot be withdrawn,
    because withdrawing it leaves the corrected album behind. What re-plans is
    read from the database every time.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),),
        details={"r1": _release("r1", (400, 5_000))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    signature = str(_loose_file(_opened(api, unit_id))["signature"])
    assert api.correct(unit_id, track_files={"2": signature})["ok"]

    withdrawn = api.correct(unit_id, track_files={"2": ""})

    assert withdrawn["ok"]
    album = withdrawn["album"]
    assert _row_for(album, 2)["status"] == "unmatched_track"
    assert _loose_file(album)["file"] == "bbb.flac"
    assert [entry for entry in album["corrections"] if entry["field"] == "track_file"] == []


def test_a_pairing_survives_the_file_being_renamed(tmp_path: Path) -> None:
    """The whole reason it is keyed by the audio and not by the path.

    Organizing renames every file of an album. A pairing bound to `bbb.flac`
    would be discarded by the application doing the very thing it was asked to
    do.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),),
        details={"r1": _release("r1", (400, 5_000))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    signature = str(_loose_file(_opened(api, unit_id))["signature"])
    assert api.correct(unit_id, track_files={"2": signature})["ok"]

    folder = library / "marina do acordeao - forro"
    (folder / "bbb.flac").rename(folder / "02 - Faixa 2.flac")
    _scan_and_wait(api, library)

    album = _opened(api, api.state()["albums"][0]["unit_id"])
    row = _row_for(album, 2)
    assert row["status"] == "paired" and row["by_hand"] is True
    assert row["file"] == "02 - Faixa 2.flac", "the same audio, under the name it was given"


def test_pairings_made_against_another_release_are_held_until_answered(tmp_path: Path) -> None:
    """A position belongs to a tracklist, so adopting another one asks.

    Neither applied nor deleted. Applying them would put the file on whatever
    track 2 happens to be over there; deleting them would spend work that came
    from comparing two pressings by hand. So they are named on screen and the
    user says which — and reusing is a claim that the two tracklists run alike.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),),
        details={"r1": _release("r1", (400, 5_000)), "r2": _release("r2", (400, 6_000))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    signature = str(_loose_file(_opened(api, unit_id))["signature"])
    assert api.correct(unit_id, track_files={"2": signature})["ok"]

    adopted = api.adopt(unit_id, "discogs", "r2")

    assert adopted["ok"]
    held = adopted["album"]["suspended_pairings"]
    assert held == [{"track_position": 2, "file": "bbb.flac", "added": False}]
    assert _row_for(adopted["album"], 2)["status"] == "unmatched_track", "held, not applied"

    reused = api.reuse_pairings(unit_id)

    assert reused["ok"] and reused["moved"] == 1
    assert reused["album"]["suspended_pairings"] == []
    assert _row_for(reused["album"], 2)["by_hand"] is True

    dropped = api.discard_pairings(unit_id)

    assert dropped["ok"] and dropped["dropped"] == 1
    assert _row_for(dropped["album"], 2)["status"] == "unmatched_track"
    assert [
        entry for entry in dropped["album"]["corrections"] if entry["field"] == "track_file"
    ] == []


def test_putting_a_file_at_a_place_the_catalogue_publishes_pairs_it(tmp_path: Path) -> None:
    """A file put at a place the catalogue publishes is paired with that track.

    Two files nothing had matched, two tracks nothing had matched: numbering
    the files with the places those tracks hold must close the album, not grow
    it by two tracks. Adopting *inserts* — each file would land beside the
    catalogue's track, which is pushed down still with no file — and what is
    meant is *this file is that track*.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),), details={"r1": _release("r1", (400, 5_000))}
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    signature = str(_loose_file(_opened(api, unit_id))["signature"])

    answer = api.adopt_extra_track(unit_id, signature, position=2)

    assert answer["ok"], answer.get("error")
    album = answer["album"]
    assert album["extra_positions"] == {}, "nothing was added; a track was claimed"
    assert album["match_files_unpaired"] == 0 and album["match_tracks_unpaired"] == 0
    assert _row_for(album, 2)["by_hand"] is True, "it is the user's pairing, and says so"
    fields = {entry["field"] for entry in album["corrections"]}
    assert "track_file" in fields and "extra_track" not in fields


def test_a_place_a_file_already_holds_still_inserts(tmp_path: Path) -> None:
    """The other half of the same rule, and the reason it is not one condition.

    On a release whose tracks all have files, putting one more at 1 is the user
    saying their song comes first. Pairing there would take track 1's file away
    and hand back the orphan they started with, so it inserts.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spare = next(
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )

    answer = api.adopt_extra_track(unit_id, spare, position=1)

    assert answer["ok"], answer.get("error")
    assert list(answer["album"]["extra_positions"].values()) == [
        1
    ], "the added track holds first place"
    assert answer["album"]["tracks"] == 3, "three files, three tracks"


def test_an_added_track_is_held_and_named_when_the_release_changes(tmp_path: Path) -> None:
    """An added track is held when the release changes, and the screen says so.

    A pairing is suspended when another release is adopted, and an added track
    must be too. Left on the release that was compared away from, it would be
    read by nothing and reported by nothing: the screen would go on saying two
    files were unmatched, with the answer sitting in the database and no line
    saying it was being held.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900)), "r2": _release("r2", (400, 950))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spare = next(
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )
    assert api.adopt_extra_track(unit_id, spare, position=3)["ok"]

    adopted = api.adopt(unit_id, "discogs", "r2")

    assert adopted["ok"], adopted.get("error")
    held = adopted["album"]["suspended_pairings"]
    assert [row["track_position"] for row in held] == [3], "the added track is held, not dropped"
    assert held[0]["added"] is True, "and it says which kind it is, not `pairing`"
    assert adopted["album"]["extra_positions"] == {}, "held means not applied here"

    reused = api.reuse_pairings(unit_id)

    assert reused["ok"] and reused["moved"] == 1, "and reusing reaches it"
    assert reused["album"]["suspended_pairings"] == []
    assert list(reused["album"]["extra_positions"].values()) == [3]

    dropped = api.discard_pairings(unit_id)

    assert dropped["ok"] and dropped["dropped"] == 1, "and so does discarding"
    assert dropped["album"]["extra_positions"] == {}


def test_a_pairing_the_album_cannot_keep_is_refused_rather_than_stored(tmp_path: Path) -> None:
    """A write that succeeds and then does nothing must be refused instead.

    A signature no file here holds, and a position this release does not publish,
    would both be stored happily by a column that only checks the field's name.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),),
        details={"r1": _release("r1", (400, 5_000))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    signature = str(_loose_file(_opened(api, unit_id))["signature"])

    assert api.correct(unit_id, track_files={"2": "not-a-signature-of-anything"}) == {
        "ok": False,
        "error": "That file is not one of this album's files.",
    }
    assert api.correct(unit_id, track_files={"99": signature}) == {
        "ok": False,
        "error": "That track is not on this release.",
    }
    assert [
        entry for entry in _opened(api, unit_id)["corrections"] if entry["field"] == "track_file"
    ] == []


def test_clearing_the_organized_albums_touches_no_file(tmp_path: Path) -> None:
    """The ✕ of a single card, applied to the shelf of organized albums.

    Only what this app organized: an album still waiting for a decision is
    exactly what a tidy-up button must not sweep away. Nothing is deleted and
    nothing moves — the folder is asserted to be still there afterwards,
    because the cost of getting this wrong is the music itself.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    organized = library / ORGANIZED
    assert organized.is_dir()

    cleared = api.clear_organized()

    assert cleared == {"ok": True, "cleared": 1}
    assert api.state()["albums"] == []
    assert organized.is_dir(), "clearing hides a row; it never touches the disk"
    # And it comes back, because this remembers nothing: scanning the folder
    # again is all it takes.
    _scan_and_wait(api, library)
    assert len(api.state()["albums"]) == 1


def test_an_album_still_waiting_survives_clear_organized(tmp_path: Path) -> None:
    """The button says `organized`, so it must reach nothing else."""
    library = _library(tmp_path)
    # Track 2 is five seconds from anything here, so this album lands in review.
    source = FakeSource(
        summaries=(_release("r1", (400, 5_000)),),
        details={"r1": _release("r1", (400, 5_000))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    cleared = api.clear_organized()

    assert cleared == {"ok": True, "cleared": 0}
    assert len(api.state()["albums"]) == 1


def test_the_old_word_is_rewritten_only_where_it_is_this_apps_token(tmp_path: Path) -> None:
    """Names already written with the old word are brought into line.

    The sweep is anchored on the brackets and on the codec that precedes the
    word: `Fake Paper Moons` is an album, and a sweep matching the bare word
    would rename it. Both cases are driven here, in one tree, so the rule
    cannot be loosened without this failing.

    It runs through the executor like every other change, so History holds the
    run and can undo it whole.
    """
    library = tmp_path / "collection"
    album = library / "VA - Crate Number Nine (1996) [FLAC, FLAC Fake]"
    album.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", album / "01 - Um [FLAC Fake].flac")
    shutil.copy(FIXTURES / "tone-long.flac", album / "02 - Dois.flac")
    innocent = library / "Moss Harbour - Fake Paper Moons (1995) [FLAC]"
    innocent.mkdir()
    shutil.copy(FIXTURES / "tone.flac", innocent / "01 - Fake Paper Moons.flac")
    api = _api(tmp_path, FakeSource())

    preview = api.old_label_preview(str(library))

    assert preview["ok"] and preview["count"] == 2
    assert not any("Moss Harbour" in str(row["from"]) for row in preview["renames"])

    done = api.rename_old_labels(str(library))

    assert done["ok"] and done["renamed"] == 2
    assert (library / "VA - Crate Number Nine (1996) [FLAC, Lossy]").is_dir()
    assert (
        library / "VA - Crate Number Nine (1996) [FLAC, Lossy]" / "01 - Um [Lossy].flac"
    ).is_file()
    assert (innocent / "01 - Fake Paper Moons.flac").is_file(), "a song is not a token"
    assert innocent.is_dir()

    # One gesture, one run — and reverting it puts every name back.
    reverted = api.revert_run(str(done["run_id"]))

    assert reverted["ok"]
    assert album.is_dir()
    assert (album / "01 - Um [FLAC Fake].flac").is_file()


def test_every_catalogue_says_where_it_stands_by_name(tmp_path: Path) -> None:
    """The dialog shows the position of all three sources, always.

    And the number is about **names**: the source in use says `using this`,
    the one that lost says how many of the album's titles it publishes anyway —
    which is what makes a wrong choice visible without opening a search — and
    both carry the link to the release they are talking about.

    A losing catalogue is not silence: this drives one that answered with a
    release of its own and asserts it is reported rather than dropped.
    """
    library = _library(tmp_path)
    discogs = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    other = FakeSource(
        summaries=(_release("mb1", (400, 900), title="Forró"),),
        details={"mb1": _release("mb1", (400, 900), title="Forró")},
    )
    api = _api(tmp_path, discogs, witness=other)
    _scan_and_wait(api, library)

    album = _opened(api, api.state()["albums"][0]["unit_id"])

    positions = {str(row["source"]): row for row in album["positions"]}
    # All three, always: two catalogues and the witness. A source that was not
    # asked says so — the second catalogue is consulted only when the first
    # fails to settle the album, and here it did settle it.
    assert set(positions) == {"discogs", "musicbrainz", "itunes"}
    assert positions["discogs"]["in_use"] is True
    assert positions["discogs"]["titles_compared"] == 2
    assert positions["discogs"]["url"], "the source in use is offered as a link"
    assert positions["musicbrainz"]["asked"] is False
    assert positions["itunes"]["asked"] is False, "the witness switch is off by default"


def test_an_album_filed_where_another_album_lives_stays_on_the_shelf(tmp_path: Path) -> None:
    """An album that merely moved stays listed.

    Filing an organized album into a collection folder is an ordinary last
    step, and taking the row away for it would empty the Library of finished
    albums.

    The places searched are the ones this app has been shown: the folder last
    scanned, and the parents of albums already recorded — so a collection is a
    place the moment one album of it has been seen. What the album keeps is
    everything that was never about the path.
    """
    library = _library(tmp_path)
    # A second album, so its parent is a folder this app knows about.
    collection = tmp_path / "collection"
    neighbour = collection / "another album"
    neighbour.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", neighbour / "x.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, tmp_path)
    unit_id = next(
        int(album["unit_id"])
        for album in api.state()["albums"]
        if "marina" in str(album["folder_path"])
    )
    folder = library / "marina do acordeao - forro"

    shutil.move(str(folder), str(collection / folder.name))

    answer = api.library_present()

    assert answer["ok"] and answer["missing"] == []
    assert unit_id in [int(album["unit_id"]) for album in api.state()["albums"]]
    assert _opened(api, unit_id)["release_id"] == "r1", "it kept what it was identified as"


def test_an_album_that_was_followed_is_named_so_the_window_can_redraw(
    tmp_path: Path,
) -> None:
    """What was followed is named, so the window can redraw its card.

    `library_present` is the one call that follows an album moved in the file
    manager. The window redraws only what this answer names; if it named only
    the albums it could *not* find, a sweep that followed everything would
    answer with an empty list, the window would take that for nothing to do,
    and `not where it was` would stay on screen over albums the database had
    just followed.
    """
    library = _library(tmp_path)
    collection = tmp_path / "collection"
    neighbour = collection / "another album"
    neighbour.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", neighbour / "x.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, tmp_path)
    unit_id = next(
        int(album["unit_id"])
        for album in api.state()["albums"]
        if "marina" in str(album["folder_path"])
    )
    folder = library / "marina do acordeao - forro"
    shutil.move(str(folder), str(collection / folder.name))

    answer = api.library_present()

    assert answer["missing"] == [], "it was found, so nothing is missing"
    assert answer["followed"] == [unit_id], "and the window is told which card is now wrong"


def test_an_album_that_was_followed_takes_its_tracks_with_it(
    tmp_path: Path, jpeg: "Callable[[int, int], bytes]"
) -> None:
    """Following the folder and leaving the files is half a follow.

    The cover is read out of the first track, by path, and so is every gesture
    that reaches a file rather than an album. Carrying the folder alone would
    bring an album moved in the file manager back with a folder that is right
    and file paths that are not, and its card would go blank until something
    scanned it again. The apply's path follows the files; this is the second
    path that must.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    sleeve = jpeg(900, 900)
    (folder / "cover.jpg").write_bytes(sleeve)
    collection = tmp_path / "collection"
    neighbour = collection / "another album"
    neighbour.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", neighbour / "x.flac")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, tmp_path)
    unit_id = next(
        int(album["unit_id"])
        for album in api.state()["albums"]
        if "marina" in str(album["folder_path"])
    )
    assert api.cover(unit_id), "it has a picture before it moves"

    shutil.move(str(folder), str(collection / folder.name))
    answer = api.library_present()

    assert answer["followed"] == [unit_id]
    held = api._albums[unit_id].unit
    assert held.folder_path == collection / folder.name
    assert all(
        file.path.parent == collection / folder.name for file in held.audio_files
    ), "the tracks moved with the folder they are in"
    assert api.cover(unit_id), "so the card is not blank at the place it was moved to"


def test_a_quiet_library_answers_the_same_shape_as_a_busy_one(tmp_path: Path) -> None:
    """Both returns of this call carry the same keys, or the window reads none.

    The early return taken while a run is alive must not answer under keys of
    its own. Nothing would crash: `response?.missing` is simply `undefined`
    there and the window reads it as an empty list, which is right only by
    accident. An early return is a second control flow, and it answers in the
    same words as the first.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    quiet = api.library_present()

    api._job = _AliveJob()
    busy = api.library_present()

    assert set(busy) == set(quiet), "the same question, answered in the same words"
    assert busy == {"ok": True, "missing": [], "followed": [], "checked": 0}


def test_a_different_album_of_the_same_name_is_not_adopted(tmp_path: Path) -> None:
    """The name finds the candidate; the audio decides.

    Two folders can be called the same thing, and adopting the wrong one would
    hand an identification, a plan and typed corrections to a record they were
    never about. So a folder whose audio is a different album is refused, and
    the album is marked as not found.
    """
    library = _library(tmp_path)
    collection = tmp_path / "collection"
    impostor = collection / "marina do acordeao - forro"
    impostor.mkdir(parents=True)
    # Same folder name, different audio: one file instead of two. Scanned too,
    # so its parent is a place this app knows and the impostor really is offered
    # as a candidate — otherwise this would pass without testing anything.
    shutil.copy(FIXTURES / "tone.flac", impostor / "something else.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, tmp_path)
    assert any(
        "collection" in str(album["folder_path"]) for album in api.state()["albums"]
    ), "the impostor's parent is a known place"
    ours = next(
        int(album["unit_id"])
        for album in api.state()["albums"]
        if "collection" not in str(album["folder_path"])
    )

    shutil.rmtree(library / "marina do acordeao - forro")

    answer = api.library_present()

    assert answer["missing"] == [ours], "the impostor was refused, so this one was not found"
    shelf = {int(album["unit_id"]): album for album in api.state()["albums"]}
    assert ours in shelf, "and an album that cannot be found is marked, never taken away"
    assert shelf[ours]["folder_missing"] is True
    assert (impostor / "something else.flac").is_file(), "and the other album is untouched"


class FakeWitness:
    """A streaming catalogue that knows one record, and says so in verdicts only."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def verify(self, release, durations, titles=()):
        """Record what it was asked about and answer as the real one does."""
        self.asked.append(release.title)
        return {
            "witness": "itunes",
            "found": True,
            "track_count": 2,
            "titles_compared": 2,
            "titles_agreeing": 2,
            "url": "https://music.apple.com/us/album/x/1",
        }


def test_the_witness_is_asked_in_the_albums_own_words_when_no_catalogue_knew_it(
    tmp_path: Path,
) -> None:
    """A failed album is worth a request to the witness.

    With no candidate there is no release to ask about, so the question is put in
    the album's own words — the artist and album its folder suggests — which is
    what a person would type. Nothing is identified by that: the witness answers
    with counts and a page, and the album stays exactly as unidentified as it
    was.
    """
    library = _library(tmp_path)
    silent = FakeSource()  # every source draws a blank
    witness = FakeWitness()
    api = _api(tmp_path, silent, verifier=witness)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    album = _opened(api, unit_id)

    assert witness.asked, "an album nothing identified is exactly when it is worth asking"
    assert album["release_id"] is None, "and it identifies nothing"
    itunes = next(row for row in album["positions"] if row["source"] == "itunes")
    assert itunes["asked"] is True
    assert itunes["titles_agreeing"] == 2
    assert itunes["url"], "with the page, so it can be looked at"


def test_the_witness_counts_the_album_once_and_the_medley_folds(tmp_path: Path) -> None:
    """The witness's count settles a fold, at one request per album.

    The fold is decided *during* the ranking of candidates, so the count has to
    be known by then. That must not become a request per candidate: the
    witness is rate-limited, and its cost is kept at one request per album.

    It does not, and the reason is what the count *is*: how many tracks the
    album has is a fact about the album and not about a pressing of it, so one
    answer serves every candidate, and it is asked in the album's own words.
    Only a tracklist with an entry indexed inside a track can need the count.
    """
    library = _library(tmp_path)

    def medley(identifier: str) -> object:
        release = _release(identifier, (400, 900, 900))
        return replace(
            release,
            tracks=(
                replace(release.tracks[0], published_position="A1"),
                replace(release.tracks[1], published_position="A2a"),
                replace(release.tracks[2], published_position="A2b"),
            ),
        )

    pressings = (medley("r1"), medley("r2"), medley("r3"))
    witness = FakeWitness()  # it counts two
    api = _api(
        tmp_path,
        FakeSource(summaries=pressings, details={"r1": pressings[0], "r2": pressings[1]}),
        verifier=witness,
    )

    _scan_and_wait(api, library)

    settled = api._album_snapshot()[0].outcome.candidate
    assert settled is not None
    assert len(settled.release.tracks) == 2, (
        "the witness counted two, two is the folded reading of a tracklist the "
        "shape rule cannot decide alone, and this copy holds both parts in one file"
    )
    assert settled.release.tracks[1].title == "Faixa 2 / Faixa 3"
    # `forro` is the album's own words — what its folder says — and that is
    # the *only* question this witness is ever put. The verification pass
    # reads this same memoized answer.
    assert witness.asked == ["forro"], (
        "three pressings of one album, one request — a witness's answer is a "
        "fact about the album, so asking again pays twice for one answer"
    )


def test_the_witness_is_asked_about_the_album_even_when_a_guess_was_adopted(
    tmp_path: Path,
) -> None:
    """A witness put the suspect's words is not an independent witness.

    When a guess is adopted at low confidence, asking the witness about *that*
    record lets it confirm the guess — a third source agreeing with nothing the
    files say — while the same service, asked in the album's own words, may
    publish the record complete.

    The album's-own-words question therefore stands behind `no candidate that
    convinced`, not behind `no candidate at all`: an album with a bad guess
    must not fall outside both.
    """
    library = _library(tmp_path)
    # The lengths agree, so this album settles and the identification pass never
    # spends a request — which puts the question on the *opening* path. The
    # title belongs to another record.
    stranger = _release("r1", (400, 900), title="The Harlow Assembly, Vol. 2")
    witness = FakeWitness()
    api = _api(
        tmp_path,
        FakeSource(summaries=(stranger,), details={"r1": stranger}),
        verifier=witness,
    )
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    _opened(api, unit_id)

    assert witness.asked, "an album a guess was adopted for is exactly when it is worth asking"
    assert "The Harlow Assembly, Vol. 2" not in witness.asked, (
        "the witness was put the adopted guess's words, which is what makes its "
        "agreement worthless and its silence a lie about the album"
    )
    assert set(witness.asked) == {"forro"}, "the question is the album's own words, always"


def test_a_tracklist_with_nothing_indexed_never_reaches_the_witness_for_a_count(
    tmp_path: Path,
) -> None:
    """The local test that keeps the request affordable, and it is free.

    A tracklist with no entry indexed inside a track never reaches the question
    at all — and an album that settles on its own is not put to the witness for
    anything else either.
    """
    library = _library(tmp_path)
    plain = _release("r1", (400, 900))
    witness = FakeWitness()
    api = _api(tmp_path, FakeSource(summaries=(plain,), details={"r1": plain}), verifier=witness)

    _scan_and_wait(api, library)

    assert witness.asked == [], "a tracklist with no fold on the table costs no request"


def test_every_dialog_the_app_answers_with_has_asked_the_witness(tmp_path: Path) -> None:
    """Every gesture that answers with a dialog has asked the witness.

    Opening an album asks it; re-planning, adopting and correcting answer with a
    dialog of their own and must ask too. The question belongs where the
    dialog's payload is built, which every one of those gestures passes through
    — and nothing that draws the grid does.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    witness = FakeWitness()
    api = _api(tmp_path, source, verifier=witness)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    corrected = api.correct(unit_id, track_titles={"1": "Tral de Quinde"})

    assert corrected["ok"]
    settled = _after_the_witness(api, unit_id, corrected["album"])
    itunes = next(row for row in settled["positions"] if row["source"] == "itunes")
    assert itunes["asked"] is True and itunes["url"]
    # And it is asked once, not once per gesture: the verdict is kept.
    api.correct(unit_id, track_titles={"1": "Tral de Quinde II"})
    assert len(witness.asked) == 1


def test_a_restored_album_still_knows_what_its_sources_said(tmp_path: Path) -> None:
    """A restored album's panel agrees with its header about the source in use.

    A restored album that rebuilt its outcome from the release alone, leaving
    the testimony in the database, would say `not asked` about the catalogue
    its own header names as in use.

    Two things prevent it, and both are asserted: the record is read back with
    the album, and the source in use answers for itself from the candidate, so
    the panel cannot contradict the header.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])
    before = _opened(api, unit_id)
    discogs = next(row for row in before["positions"] if row["source"] == "discogs")
    assert discogs["in_use"] is True

    # A fresh window over the same database: nothing is asked of any source.
    reopened = _api(tmp_path, FakeSource())
    reopened._restore_library()
    restored_id = int(reopened.state()["albums"][0]["unit_id"])
    album = _opened(reopened, restored_id)

    assert album["source"] == "discogs", "it is still using the release it was identified as"
    position = next(row for row in album["positions"] if row["source"] == "discogs")
    assert position["in_use"] is True, "the source in use never reports itself as unasked"
    assert position["titles_compared"] is not None
    assert position["url"], "and it is still a link to what it says"


def test_restoring_an_album_that_carries_a_correction_asks_no_source_either(
    tmp_path: Path,
) -> None:
    """Restoring an album that carries a correction reaches no source.

    An album with a standing correction is *re-planned* while the Library is
    read back, and re-planning must not fetch the cover — which addresses the
    archive through MusicBrainz, two requests against a rate-limited source,
    and would delay exactly the albums that carry a correction.

    Counted, not caught: every one of these calls sits inside a handler that logs
    and carries on, so an exception would prove nothing.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])
    assert api.correct(unit_id, folder_name="Marina - Forró [mine]")["ok"]

    landmine = Landmine()
    archive = Landmine()
    reopened = _api(tmp_path, landmine, witness=archive)  # type: ignore[arg-type]
    _finish(reopened._restore_job, "restore")

    assert landmine.asked == 0, "a correction must not make the restore reach a source"
    assert archive.asked == 0, "and it must not address the art archive either"
    albums = reopened.state()["albums"]
    assert len(albums) == 1
    assert albums[0]["folder"] == "Marina - Forró [mine]" or albums[0]["title"] == "Forró"


def test_a_peer_folder_name_can_never_reach_outside_the_download_folder(tmp_path: Path) -> None:
    """The folder name on the other side of a Soulseek transfer is a stranger's text.

    Turning it into a path with `root / name.rsplit("\\", 1)[-1]` leaves a way
    out, by two facts: splitting on the Windows separator does nothing to a
    POSIX one, and an absolute right-hand side replaces the left, so
    `Path("/downloads") / "/Users/someone/Music"` *is* `/Users/someone/Music`.

    What it would reach: the collector scans wherever it decides a download
    landed and marks everything it finds as downloaded, so a peer folder named
    for the local library would have the app walk that library, claim it, and
    hand every album of it to `Clear downloaded`. The same value is passed to
    the platform's `open`.
    """
    from diglibrary.application.api import _landed_folder

    root = tmp_path / "downloads"
    root.mkdir()

    # Contained: every one of these lands under the download folder, whatever
    # the name tried to say. The separators are both honoured, so only the last
    # component survives.
    for hostile, expected in (
        (str(tmp_path / "Music"), "Music"),
        ("/etc", "etc"),
        ("../../Music", "Music"),
        ("@@peer\\..\\..\\Music", "Music"),
        ("some/../../elsewhere", "elsewhere"),
    ):
        landed = _landed_folder(root, hostile)
        assert landed == root / expected, hostile
        assert landed.resolve().is_relative_to(root.resolve()), hostile

    # Refused: no component at all, so there is no folder to honestly name.
    for empty in ("..", ".", "   ", "a/.."):
        assert _landed_folder(root, empty) is None, empty

    # Kept: the ordinary shapes, including the Windows-shared name this split
    # was written for in the first place.
    assert _landed_folder(root, "Quarteto Rufo - Pale As A Lantern") == (
        root / "Quarteto Rufo - Pale As A Lantern"
    )
    assert _landed_folder(root, "@@abcde\\music\\Quarteto Rufo") == root / "Quarteto Rufo"
    assert _landed_folder(root, "") == root


def test_a_verdict_on_the_quality_bench_reaches_the_plan_that_writes_the_files(
    tmp_path: Path,
) -> None:
    """The bench and the album dialog are two screens over one fact.

    Tracks cleared on the Quality bench must stop being named `[Lossy]` in the
    Library, which is one `Approve & apply` away from writing that word to
    disk. The map the names are built from is filled during a scan, so a
    verdict that only reaches the database reaches nobody else.

    This test never calls `_measure_for_naming` itself after the verdict:
    re-running it by hand would do for the application exactly the step under
    test, and prove the rule without proving the journey.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    api._pipeline = replace(api._pipeline, quality=_FakeSurvey({"marina do acordeao - forro"}))
    _scan_and_wait(api, library)

    state = api._album_snapshot()[0]
    signature = state.unit.unit_signature
    api._transcoded = api._measure_for_naming([state.unit])
    named_lossy = [
        operation
        for operation in api.album(state.unit_id)["album"]["operations"]
        if DEFAULT_TRANSCODED_LABEL in str(operation.get("after"))
    ]
    assert named_lossy, "the measurement has to be calling this album lossy to begin with"

    # The gesture on the Quality bench, and nothing else. No rescan, no
    # re-plan, no second call.
    response = api.set_quality_verdict(signature, "honest")

    assert response["ok"]
    # The harm first, and the bookkeeping after: what matters is that the word
    # `Lossy` is gone from what would be written to disk. A counter can be
    # kept correct by a refactor that breaks the journey it is counting.
    after = api.album(state.unit_id)["album"]["operations"]
    assert not [
        operation for operation in after if DEFAULT_TRANSCODED_LABEL in str(operation.get("after"))
    ], "the verdict must reach the names before they are written, not after a rescan"
    assert response["replanned"] == 1, "and the window has to be told the Library moved"


def test_an_album_restored_from_a_restart_still_applies_with_a_trail(tmp_path: Path) -> None:
    """No write without a trail, whatever produced the plan.

    Every path that produces an outcome persists it and gets an id back. The
    restore builds the state directly, so `plan_id` may be None — and an
    `_apply` that answered a missing id by doing less (no witness, no
    `record_execution`, nothing in a run) would rename the files and leave
    History with no row and nothing to revert, on every album restored and not
    rescanned.

    The guard is at the moment of writing rather than at the restore, so a path
    written later cannot get past it.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    # Exactly what a restart leaves behind: the same album, its plan in hand,
    # and no memory of the rows that were written when it was scanned.
    state = api._albums[unit_id]
    state.identification_id = None
    state.plan_id = None

    run = api.apply_automatic()

    assert run["ok"], run
    history = api.state()["history"]
    assert history, "an applied album has to be in History, or there is no way back"
    assert history[0]["operations"] > 0, "the run has to carry what it did"
    assert api.revert_run(run["run_id"])["ok"], "and it has to be revertible"


def _album_with_one_measured_transcode(tmp_path: Path) -> tuple[LibraryApi, int]:
    """One album whose plan marks a single track `[Lossy]`, through the app's own path.

    One track and not all of them, because the mark goes on the track that
    *differs* from what the album mostly holds — an album that agrees with itself
    says so on its folder and repeating it on every file would be noise. So a
    single measured transcode is what puts a word on a file name at all.

    The verdict goes into the store and the plan is rebuilt from it, exactly as a
    Quality verdict given after the scan reaches the shelf. Nothing here sets
    `_transcoded` or `_rates` by hand.

    **Both files are measured.** Measuring only one would make the album itself
    a transcode — a majority of one out of one — so every file would be marked,
    and the mark would ride on which file happened to have been measured rather
    than on a difference in the audio.

    The transcode is the **second** file, which is what makes it the odd one
    here: two files split one-one have no majority, so which label the folder
    speaks for falls to insertion order, and the album has to hold the honest
    one first for the accusation to land on the file that earned it.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api._album_snapshot()[0]
    honest, odd = state.unit.audio_files[0], state.unit.audio_files[1]
    api._store.record_quality(
        {
            odd.content_signature: StoredQuality(
                encoding="transcoded",
                effective_bitrate_kbps=256,
                cutoff_hertz=16_000,
                steepest_drop_db=40.0,
                findings=(),
                reason="A wall at 16 kHz.",
                decay_db=30.0,
                ceiling_db=-100.0,
            ),
            honest.content_signature: StoredQuality(
                encoding="lossless",
                effective_bitrate_kbps=None,
                cutoff_hertz=None,
                steepest_drop_db=8.0,
                findings=(),
                reason="Energy fades gradually and the top band is still alive.",
                decay_db=6.0,
                ceiling_db=-70.0,
            ),
        }
    )
    api._facts_from_store([state.unit])
    api._replan(api._albums[state.unit_id], by_hand=True)
    return api, int(state.unit_id)


def _track_names(payload: dict[str, object]) -> list[str]:
    album = payload["album"]
    return [
        Path(str(operation["after"]["path"])).name
        for operation in album["operations"]  # type: ignore[index]
        if operation["kind"] == "rename_file"
    ]


def test_correcting_a_name_does_not_take_the_lossy_mark_off_a_track(tmp_path: Path) -> None:
    """Correcting a name keeps the `[Lossy]` mark on a track.

    `[Lossy]` is the one thing in a name that warns a file is not what its
    container says. A rebuild that is not handed `measured_rates` does not turn
    the track's mark into a word instead of a number — the mark disappears. So
    typing a preferred folder name would silently undress a proven transcode,
    and `Approve & apply` would write that to disk.

    The folder gesture is asserted first because it is the one nobody
    would think to check: it is not about that track, or about audio, at all.
    """
    api, unit_id = _album_with_one_measured_transcode(tmp_path)
    assert _track_names(api.album(unit_id)) == [
        "01. Faixa 1.flac",
        f"02. Faixa 2 [{DEFAULT_TRANSCODED_LABEL}].flac",
    ], "the mark is on the odd track in the first place"

    renamed = api.correct(unit_id, folder_name="A Folder Typed")

    assert _track_names(renamed) == [
        "01. Faixa 1.flac",
        f"02. Faixa 2 [{DEFAULT_TRANSCODED_LABEL}].flac",
    ], "renaming the folder says nothing about the audio"


def test_correcting_a_title_does_not_take_the_lossy_mark_off_another_track(
    tmp_path: Path,
) -> None:
    """The same rule, through the title correction."""
    api, unit_id = _album_with_one_measured_transcode(tmp_path)

    corrected = api.correct(unit_id, track_titles={"1": "Another Name"})

    assert _track_names(corrected) == [
        "01. Another Name.flac",
        f"02. Faixa 2 [{DEFAULT_TRANSCODED_LABEL}].flac",
    ]


def test_discarding_the_pairings_does_not_take_the_lossy_mark_off_a_track(
    tmp_path: Path,
) -> None:
    """`discard_pairings` keeps the mark too.

    It re-plans through `_replanned`, which is a different function from the
    one every other correction ends in, so the rule has to hold there
    separately.
    """
    api, unit_id = _album_with_one_measured_transcode(tmp_path)

    assert api.discard_pairings(unit_id)["ok"]

    assert _track_names(api.album(unit_id)) == [
        "01. Faixa 1.flac",
        f"02. Faixa 2 [{DEFAULT_TRANSCODED_LABEL}].flac",
    ]


def test_no_gesture_in_the_album_dialog_asks_a_catalogue_for_the_cover_again(
    tmp_path: Path,
) -> None:
    """No gesture of the album dialog asks a catalogue for the cover again.

    The cover lookup addresses the Cover Art Archive *through* MusicBrainz — two
    requests against a rate-limited source — and it happens while the user
    waits, which is why every re-plan is offline and hands back the art it
    already has. `_replanned` is the path four gestures of this dialog take:
    adopting a track, dropping one, reusing pairings, discarding them.

    Counted rather than reasoned about: none of these changes which release this
    is, so there is no question here a catalogue could answer that this album is
    not already holding. Every gesture is asserted, not only the ones that could
    be expected to ask.

    **The album with no art is the case that matters.** Handing the outcome's
    own artwork back is what stops the lookup where there is art to hand back —
    so with an album that has some, every gesture is silent whether or not
    anybody said `offline`. The album whose outcome carries none is where
    `offline` is the only thing standing between the gesture and the network,
    and it is an ordinary album: nothing was found for it.
    """
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )

    _gestures_ask_no_catalogue(tmp_path / "with-art", source, keep_art=True)
    _gestures_ask_no_catalogue(tmp_path / "without-art", source, keep_art=False)


def _gestures_ask_no_catalogue(root: Path, source: FakeSource, *, keep_art: bool) -> None:
    """Run the album dialog's four re-planning gestures and count what was asked."""
    library = _library(root)
    api = _api(root, source)
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id

    service = api._pipeline.workflow._artwork
    asked: list[object] = []
    answering = service.fetch

    def counting(*arguments: object, **words: object) -> object:
        asked.append(words or arguments)
        return answering(*arguments, **words)

    service.fetch = counting  # type: ignore[method-assign]

    def strip_art() -> None:
        # Emptied before *each* gesture, because a re-plan puts art back on the
        # outcome — emptied once, only the first gesture would meet the case.
        if not keep_art:
            state = api._albums[unit_id]
            state.outcome = replace(state.outcome, artwork=None)

    strip_art()
    api.correct(unit_id, folder_name="A Folder Typed")
    strip_art()
    api.discard_pairings(unit_id)
    strip_art()
    api.reuse_pairings(unit_id)
    strip_art()
    api._replan(api._albums[unit_id], by_hand=True)

    assert asked == [], (
        "a gesture that says nothing about which release this is must not spend "
        f"a catalogue's rate limit, and it happens while the user waits — art={keep_art}"
    )


def test_no_gesture_in_the_album_dialog_drops_the_cover_from_the_plan(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """No gesture of the album dialog drops the cover from the plan.

    Handing the outcome's art back is what lets a rebuild be offline *without*
    losing what was already fetched — say nothing and the plan comes back with
    no `write_image` and no `embed_image` at all, so a cover already fetched
    silently stops being part of what Approve would write.

    Asserting only the network would pass either way for an album with art, so
    this case is asserted on its own.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id

    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(1000, 1000))
    store = FilesystemArtworkStore()
    staged = StagedImage(kind=ImageKind.FRONT, path=sleeve, facts=store.describe(sleeve))
    state = api._albums[unit_id]
    state.outcome = replace(state.outcome, artwork=Artwork(files=(staged,), embedded=staged))
    api._replan(state, by_hand=True)

    def picture_work(payload: dict[str, object]) -> set[str]:
        album = payload["album"]
        return {
            str(operation["kind"])
            for operation in album["operations"]  # type: ignore[index]
            if "image" in str(operation["kind"])
        }

    assert picture_work(api.album(unit_id)) == {
        "write_image",
        "embed_image",
    }, "the cover is part of the plan in the first place"

    assert api.discard_pairings(unit_id)["ok"]

    assert picture_work(api.album(unit_id)) == {
        "write_image",
        "embed_image",
    }, "a gesture about pairings must not take the cover out of the plan"


def _remember_that_the_audio_is_lossy(api: LibraryApi, unit: object) -> None:
    """Put a transcode verdict on record for every file of one album.

    Written straight into the store, because that is where a survey leaves it
    and where every later screen has to find it. A wall at 16 kHz is the one
    sign that convicts alone (`track_is_transcoded`), so this needs no ffmpeg
    and no decoding — it is the measurement, already taken.
    """
    api._store.record_quality(
        {
            file.content_signature: StoredQuality(
                encoding="transcoded",
                effective_bitrate_kbps=256,
                cutoff_hertz=16_000,
                steepest_drop_db=40.0,
                findings=(),
                reason="A wall at 16 kHz.",
                decay_db=30.0,
                ceiling_db=-100.0,
            )
            for file in unit.audio_files  # type: ignore[attr-defined]
        }
    )


def test_a_restored_album_still_wears_the_verdict_its_audio_earned(tmp_path: Path) -> None:
    """A restored album plans from the verdict already on record.

    Every album is restored when the window opens, through `adopt_release`. If
    that call did not carry `transcoded=`, the plan waiting on screen after a
    restart would name a proven transcode `[FLAC]`, and `Approve & apply` would
    write that word to disk with the measurement on record the whole time.

    The journey is what is asserted: the verdict goes into the database through
    the store, the window is closed, and the *reopened* app is asked what it
    would write. Nothing in this test measures anything or touches `_transcoded`
    by hand.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    scanned = api._album_snapshot()[0]
    _remember_that_the_audio_is_lossy(api, scanned.unit)

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")

    restored = reopened._album_snapshot()
    assert restored, "the album has to come back at all"
    planned = str(reopened.album(restored[0].unit_id)["album"]["planned_folder"])
    assert DEFAULT_TRANSCODED_LABEL in planned, (
        "a restored album plans from a measurement already on record, "
        f"and this one would be written to disk as {planned!r}"
    )


def test_adopting_another_release_keeps_the_verdict_the_audio_earned(tmp_path: Path) -> None:
    """Choosing a pressing is a statement about the catalogue, not about the audio.

    `adopt` and `adopt_link` both plan through `adopt_release`, which has to
    carry the transcode verdict and the measured bitrates — otherwise choosing
    *which* release this is would silently drop `[Lossy]` from the name the
    app is about to write.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900)), "r2": _release("r2", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api._album_snapshot()[0]
    _remember_that_the_audio_is_lossy(api, state.unit)
    # The scan is what fills the map the names are built from, and it ran before
    # the verdict existed. No re-scan happens here: the album is opened and
    # another pressing is picked.
    assert api.adopt(state.unit_id, "discogs", "r2")["ok"]

    planned = str(api.album(state.unit_id)["album"]["planned_folder"])

    assert DEFAULT_TRANSCODED_LABEL in planned, (
        "adopting a release must not undress the album; "
        f"this one would be written to disk as {planned!r}"
    )


def test_adopting_another_release_keeps_a_corrected_name(tmp_path: Path) -> None:
    """A correction overrules the catalogue, and it outlives the release.

    `_with_corrections` is applied on every scan and on every restore, and has
    to be applied on `adopt` and `adopt_link` as well — otherwise a typed
    folder name would vanish the moment a different pressing is chosen, with
    the row still standing in the database.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900)), "r2": _release("r2", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api._album_snapshot()[0]
    assert api.correct(state.unit_id, folder_name="Forró, typed by hand")["ok"]

    assert api.adopt(state.unit_id, "discogs", "r2")["ok"]

    planned = str(api.album(state.unit_id)["album"]["planned_folder"])
    assert (
        planned == "Forró, typed by hand"
    ), f"the correction has to survive a change of release; the plan says {planned!r}"


def test_adopting_a_release_keeps_what_the_other_catalogues_answered(
    tmp_path: Path,
) -> None:
    """Adopting a release keeps what the other catalogues answered.

    Choosing a release says which record this is and nothing about what the
    other catalogues published — those answers were measured against the files.
    An adoption that built a fresh outcome with an empty ``verification`` and
    wrote it over the one on record would make a source read as *not asked*:
    the catalogue would not lose the header's first place, it would leave the
    line entirely.

    The case driven here: the second catalogue answered and agreed with every
    name, a pasted link to the first agrees with fewer, and that disagreement
    has to stay on screen.
    """
    library = _library(tmp_path)
    # Discogs answers with a record that settles nothing, which is what sends
    # the question on to the second catalogue at all.
    wrong = _release("10203040", (12_000, 47_000), title="Something Else")
    right = _release("mb1", (400, 900), title="Forró")
    discogs = FakeSource(summaries=(wrong,), details={"10203040": wrong})
    other = FakeSource(summaries=(right,), details={"mb1": right})
    api = _api(tmp_path, discogs, witness=other)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    before = {str(row["source"]): row for row in _opened(api, unit_id)["positions"]}
    assert before["musicbrainz"]["in_use"] is True, "the second catalogue is what settled it"
    assert before["discogs"]["asked"] is True, "and the first one did answer"

    adopted = api.adopt_link(unit_id, "https://www.discogs.com/release/10203040")
    assert adopted["ok"], adopted

    after = {str(row["source"]): row for row in adopted["album"]["positions"]}
    assert after["discogs"]["in_use"] is True, "the pasted release is the one in use"
    assert after["musicbrainz"]["asked"] is True, (
        "the catalogue that answered has to stay on the header as a witness; "
        "it reads as never asked, which is a screen hiding an answer this app paid for"
    )
    assert after["musicbrainz"]["in_use"] is False
    assert (
        after["musicbrainz"]["titles_agreeing"] == before["musicbrainz"]["titles_agreeing"]
    ), "and it keeps its own number, which is what makes a worse choice visible"
    # The number beside the source in use is the one just measured against this
    # copy, never the previous winner's.
    assert after["discogs"]["titles_agreeing"] == 0, after["discogs"]


def test_adopting_a_candidate_keeps_it_too(tmp_path: Path) -> None:
    """The sibling route keeps it too.

    `adopt` and `adopt_link` differ only in where the release id comes from, so
    the two share one helper and both are asserted.
    """
    library = _library(tmp_path)
    wrong = _release("10203040", (12_000, 47_000), title="Something Else")
    right = _release("mb1", (400, 900), title="Forró")
    discogs = FakeSource(summaries=(wrong,), details={"10203040": wrong})
    other = FakeSource(summaries=(right,), details={"mb1": right})
    api = _api(tmp_path, discogs, witness=other)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    _opened(api, unit_id)

    adopted = api.adopt(unit_id, "discogs", "10203040")
    assert adopted["ok"], adopted

    after = {str(row["source"]): row for row in adopted["album"]["positions"]}
    assert after["discogs"]["in_use"] is True
    assert after["musicbrainz"]["asked"] is True, (
        "adopting a candidate from the search must keep the other catalogue, "
        "as pasting a link does"
    )


def test_scanning_a_second_folder_keeps_what_the_first_one_measured(tmp_path: Path) -> None:
    """`_identify` updates the verdict map instead of rebinding it.

    Two albums, scanned one folder at a time. A second scan that replaced the
    whole verdict map with its own albums' answers would take `[Lossy]` off the
    first folder's proven transcode — on screen, and in what `Apply N
    automatic` would write.
    """
    library = tmp_path / "library"
    first = library / "first album"
    second = library / "second album"
    for folder in (first, second):
        folder.mkdir(parents=True)
        shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", first / "bbb.flac")
    shutil.copy(FIXTURES / "tone-long.flac", second / "ccc.flac")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)

    _scan_and_wait(api, first)
    scanned = api._album_snapshot()[0]
    _remember_that_the_audio_is_lossy(api, scanned.unit)
    # Scanned again so the verdict reaches the map the ordinary way, exactly as
    # a survey would have left it.
    _scan_and_wait(api, first)
    kept = next(state for state in api._album_snapshot() if state.unit.folder_path == first)
    assert DEFAULT_TRANSCODED_LABEL in str(api.album(kept.unit_id)["album"]["planned_folder"])

    _scan_and_wait(api, second)

    planned = str(api.album(kept.unit_id)["album"]["planned_folder"])
    assert DEFAULT_TRANSCODED_LABEL in planned, (
        "scanning another folder must not undress the album already on the shelf; "
        f"the first one now plans as {planned!r}"
    )


def test_a_rejected_album_is_still_rejected_after_a_restart(tmp_path: Path) -> None:
    """A refused identification stays refused when the app is reopened.

    `reject` writes `skipped` on the album's row. A restore that read only
    `organized` would bring the refusal back as an ordinary automatic album
    with an applicable plan, which is exactly what `Apply N automatic` is for.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id
    assert api.reject(unit_id)["ok"]

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")

    restored = reopened._album_snapshot()
    assert restored, "the album still belongs on the shelf; only the answer was refused"
    assert restored[0].rejected, "the refusal has to come back with the album"
    run = reopened.apply_automatic()
    assert run["applied"] == 0, "nothing that was rejected may be renamed by the batch"


def test_an_album_approved_after_a_rejection_is_organized_and_not_rejected(
    tmp_path: Path,
) -> None:
    """Approving a plan withdraws the rejection that stood before it.

    Rejecting marks the album in memory, and a correction typed afterwards
    re-plans the same release without touching that mark. If applying leaves it
    standing, the card reads `Rejected` over files this application has just
    organized, until the next start reads the row and says `organized`.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.reject(unit_id)["ok"]
    assert api.correct(unit_id, folder_name="Typed By Hand")["ok"]

    assert api.approve(unit_id)["ok"]

    album = api.state()["albums"][0]
    assert album["organized"] is True
    assert album["rejected"] is False, "the card says Rejected over an organized album"


def test_a_rejection_survives_a_replan_and_the_restart_after_it(tmp_path: Path) -> None:
    """The row keeps `skipped` for as long as the album in memory is rejected.

    Every re-plan writes the album's state again. Writing `identified` over
    `skipped` leaves the session saying `Rejected` and the database saying
    otherwise, and the next start reads the database: the album comes back
    automatic, with an applicable plan, in reach of the batch.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.reject(unit_id)["ok"]

    # A change of naming style re-plans every album on the shelf.
    assert api.set_settings({"naming_style": "minimal"})["ok"]

    assert api.state()["albums"][0]["rejected"] is True
    with api._store._database.connect() as connection:
        row = connection.execute(
            "SELECT state FROM album_units WHERE id = ?", (unit_id,)
        ).fetchone()
    assert row[0] == "skipped", "the re-plan wrote over the rejection in the database"

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    assert reopened.state()["albums"][0]["rejected"] is True
    assert reopened.apply_automatic()["applied"] == 0, "the batch reached a rejected album"


def test_opening_an_album_this_app_just_organized_finds_its_folder(tmp_path: Path) -> None:
    """The album in memory follows the rename this app has just made.

    `_apply` follows the rename into the database — the download's row and the
    album's row both move. If the album the window is holding went on pointing
    at the name it was scanned under, `Open album` would answer "that folder is
    no longer on disk" about the folder this app had just written, and
    `Send to bench` would refuse the same album for the same reason.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    opened: list[str] = []
    api._path_opener = opened.append
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id
    assert api.apply_automatic()["ok"]

    response = api.open_album(unit_id)

    assert response["ok"], response
    assert opened and Path(opened[-1]).is_dir(), "the path handed to the file browser has to exist"
    assert (
        Path(opened[-1]).name == ORGANIZED
    ), f"and it has to be the name this app wrote, not {Path(opened[-1]).name!r}"


def test_every_registered_file_still_names_a_file_after_an_apply(tmp_path: Path) -> None:
    """An apply that renames files and folder updates `audio_files` with them.

    A file registered under the name it had and the name it has is two names
    over one content signature, so `ambiguous_signatures` calls it ambiguous
    and the bitrate measured from a file is withheld from that file's own
    name.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    before = _registered_paths(api)
    assert before and all(path.exists() for path in before), "the scan starts out honest"

    assert api.apply_automatic()["ok"]

    after = _registered_paths(api)
    assert len(after) == len(before), "an apply renames files; it does not register new ones"
    missing = [path for path in after if not path.exists()]
    assert not missing, f"every registered file has to still name a file: {missing}"
    signatures = _registered_signatures(api)
    assert (
        api._store.ambiguous_signatures(signatures) == set()
    ), "and one file that was renamed is still one file, not two names over one signature"


def test_every_registered_file_still_names_a_file_after_a_revert(tmp_path: Path) -> None:
    """A reversal puts the files back, and the registry has to go back with them.

    The mirror of the apply, and the order is what makes it work: a reversal
    undoes the most recent operation first, so the folder goes back before the
    files do — and a file's two names were both recorded under the folder's old
    name.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    run = api.apply_automatic()
    assert run["ok"], run

    assert api.revert_run(run["run_id"])["ok"]

    missing = [path for path in _registered_paths(api) if not path.exists()]
    assert not missing, f"every registered file has to name a file again: {missing}"


def _registered_paths(api: LibraryApi) -> list[Path]:
    with api._store._database.connect() as connection:
        return [Path(row[0]) for row in connection.execute("SELECT path FROM audio_files")]


def _registered_signatures(api: LibraryApi) -> list[str]:
    with api._store._database.connect() as connection:
        return [row[0] for row in connection.execute("SELECT content_signature FROM audio_files")]


def test_rewriting_the_old_quality_word_keeps_the_album_in_the_library(tmp_path: Path) -> None:
    """A sweep that renames folders has to tell the registry.

    `rename_old_labels` applies through the executor like every other change, so
    History is right. Without `relocate_unit`, the album's own row would still
    name the folder the sweep had just renamed: on the next start the restore
    looks for that folder, does not find it, looks for it by name in the places
    albums live, does not find that either because the name is what changed,
    and takes the row away.
    """
    library = tmp_path / "library"
    folder = library / "Some Record (1978) [FLAC Fake]"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    source = FakeSource()
    api = _api(tmp_path, source)
    _finish(api._restore_job, "restore")

    swept = api.rename_old_labels(str(library))
    assert swept["ok"], swept
    assert swept["renamed"], "the sweep has to have renamed something to be worth testing"

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    assert (
        reopened._album_snapshot()
    ), "the album the sweep renamed has to still be on the shelf after a restart"


def test_raising_the_bar_reaches_the_plans_already_on_the_shelf(tmp_path: Path) -> None:
    """A raised threshold reaches the plans already on the shelf.

    If `set_settings` rebuilt the pipeline and stopped there, every plan already
    on screen would keep the decision it was given under the old rules, so the
    new bar would apply to nothing visible — and the batch is what acts on
    those plans.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id
    assert (
        api.album(unit_id)["album"]["decision"] == "automatic"
    ), "the album has to be automatic under the old bar for this to mean anything"

    assert api.set_settings({"threshold": 1.01})["ok"]

    assert (
        api.album(unit_id)["album"]["decision"] != "automatic"
    ), "a bar just raised has to reach the plans on screen"
    assert api.apply_automatic()["applied"] == 0, "and nothing under it may be renamed"


def test_changing_the_naming_style_reaches_the_plans_already_on_the_shelf(
    tmp_path: Path,
) -> None:
    """The same rule, for the naming style.

    A style chosen in Settings has to name what is already planned, or the
    screen goes on offering the old arrangement and `Approve & apply` writes it.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id
    assert str(api.album(unit_id)["album"]["planned_folder"]) == ORGANIZED

    assert api.set_settings({"naming_style": "minimal"})["ok"]

    assert (
        str(api.album(unit_id)["album"]["planned_folder"]) == "Marina do Acordeão - Forró"
    ), "the style just chosen has to name what is on screen"


def test_an_accepted_deep_measurement_reaches_the_names_it_was_taken_for(
    tmp_path: Path,
) -> None:
    """`Analyze in depth` exists to earn back a number the app is withholding.

    A bitrate that cannot be proved to be this file's is refused, and the deep
    measurement is how a file earns one — it writes the row with the audio key
    that makes it provable. Writing is not enough: the store has to be re-read,
    or the plan goes on naming the album from the withheld number until the
    next full scan and the gesture changes nothing on screen.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api._album_snapshot()[0]
    assert DEFAULT_TRANSCODED_LABEL not in str(api.album(state.unit_id)["album"]["planned_folder"])

    # What `bench_adopt` does to the store on acceptance, and only that.
    _remember_that_the_audio_is_lossy(api, state.unit)
    api._bench_analysis = _DeepAnalysisThatSaysLossy()
    assert api.bench_adopt(accept=True)["ok"]

    planned = str(api.album(state.unit_id)["album"]["planned_folder"])
    assert DEFAULT_TRANSCODED_LABEL in planned, (
        "an accepted measurement has to reach the names it was taken for; "
        f"the plan still says {planned!r}"
    )


class _DeepAnalysisThatSaysLossy:
    """A finished deep analysis with nothing of its own to write.

    The rows are already in the store, put there the way a real adopt puts them.
    What this exercises is everything `bench_adopt` does *after* writing.
    """

    files = 2
    seconds = 30.0
    albums: tuple[object, ...] = ()


def test_a_quality_verdict_does_not_hand_an_organized_album_a_plan(tmp_path: Path) -> None:
    """An organized album is protected by having no plan, and a re-plan builds one.

    A verdict on the Quality bench reaches every Library album that holds the
    same audio, and an album this app already organized is on that shelf.
    Re-planning it would give `Apply N automatic` an album that is finished —
    and possibly renamed by hand since.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    assert api.apply_automatic()["ok"]
    # Scanned again, which is how an organized album comes back on screen: named
    # by what the database remembers, and deliberately holding no plan.
    _scan_and_wait(api, library / ORGANIZED)
    organized = next(state for state in api._album_snapshot() if state.organized)
    assert organized.outcome.plan is None, "an organized album starts with no plan, by design"

    assert api.set_quality_verdict(organized.unit.unit_signature, "transcoded")["ok"]

    assert (
        organized.outcome.plan is None
    ), "a verdict must not hand an organized album a plan the batch can reach"
    assert api.apply_automatic()["applied"] == 0


def test_a_key_pasted_into_the_window_reaches_the_engine_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key pasted into the window reaches the engine without a restart.

    The acoustic path is composed from the environment, once, before the window
    exists — so a pasted key could be stored correctly, reported correctly, and
    reach no engine until the app was reopened. Discogs and MusicBrainz
    resolve theirs per request.

    What is asserted is that the engine is rebuilt around the new key, because
    that is the only way a client composed from the environment can pick one up.
    The store is faked: this test must not write to the real environment file.
    """
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(credentials, "store", lambda name, value: stored.append((name, value)))
    api = _api(tmp_path, FakeSource())
    before = api._pipeline

    response = api.connect("acoustid", "a-pasted-key")

    assert response["ok"], response
    assert stored == [(credentials.ACOUSTID, "a-pasted-key")]
    assert api._pipeline is not before, (
        "storing a key has to rebuild the engine around it, or the screen is "
        "stating a connection the app does not have"
    )


def test_changing_a_setting_rebuilds_the_shelf_without_asking_anyone(tmp_path: Path) -> None:
    """A whole shelf re-planned at once must not be a whole shelf of requests.

    Re-planning reaches for cover art, and addressing the archive costs a
    MusicBrainz search and an archive request per album against a rate-limited
    source. Rebuilding every plan when a setting changes is right; doing it
    online would make one setting cost minutes of a frozen window — and on a
    shelf restored from a previous session, where nothing was ever fetched,
    every one of those would be a live request.
    """
    library = _library(tmp_path)
    detail = _release("r1", (400, 900))
    source = FakeSource(summaries=(detail,), details={"r1": detail})
    witness = FakeSource(summaries=(detail,), details={"r1": detail})
    api = _api(tmp_path, source, witness=witness)
    _scan_and_wait(api, library)
    asked = len(witness.searches)

    assert api.set_settings({"naming_style": "minimal"})["replanned"] == 1

    assert len(witness.searches) == asked, (
        "rebuilding the shelf around a setting has to cost no requests; it made "
        f"{len(witness.searches) - asked}"
    )


def test_planning_again_reads_the_verdict_already_given_about_the_album(tmp_path: Path) -> None:
    """Planning an organized album again reads verdicts given since the scan.

    `Plan this album again` is the one way an organized album gets a plan. The
    transcode map in memory is filled by a scan, so a plan built straight from
    it would not see a verdict given afterwards — on the Quality bench, about
    this very album — and would go on planning `[Lossy]` into the folder name
    of an album marked honest.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api._album_snapshot()[0]
    _remember_that_the_audio_is_lossy(api, state.unit)
    _scan_and_wait(api, library)
    assert api.apply_automatic()["ok"]
    # Back on screen as an organized album, as after a restart: no plan, and
    # the button is the only way to ask for one.
    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    organized = reopened._album_snapshot()[0]
    assert organized.organized

    # The verdict, given on the bench about the whole album — and then the button.
    assert reopened.set_quality_verdict(organized.unit.unit_signature, "honest")["ok"]
    assert reopened.plan_anyway(organized.unit_id)["ok"]

    planned = str(reopened.album(organized.unit_id)["album"]["planned_folder"])
    assert DEFAULT_TRANSCODED_LABEL not in planned, (
        "the verdict about this album has to reach the plan this button builds; "
        f"it would be written to disk as {planned!r}"
    )


def test_a_held_album_does_not_come_back_automatic(tmp_path: Path) -> None:
    """An album held for review is not batch-applied after a restart.

    `hold` means the answer may well be right and is to be looked at first. It
    writes `needs_review` on the album's row — and so does `_persist_outcome`,
    for every album the app itself did not call automatic. The two cannot be
    told apart, so what is read back is the conclusion the row actually
    records: *this album was not automatic*. Coming back as one is the only
    outcome that can rename a folder nobody approved.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api._album_snapshot()[0].unit_id
    assert api.album(unit_id)["album"]["decision"] == "automatic"
    assert api.hold(unit_id)["ok"]

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")

    restored = reopened._album_snapshot()[0]
    assert (
        restored.outcome.decision is not Decision.AUTOMATIC
    ), "an album pulled back must not return ready for the batch"
    assert reopened.apply_automatic()["applied"] == 0


def test_what_the_audio_answered_survives_a_restart_even_when_it_was_no(
    tmp_path: Path,
) -> None:
    """ "Asked, and the service has never heard of this record" is a fact worth keeping.

    For music outside the mainstream catalogues it is the *ordinary* answer. If
    only the positive answer survived a restart, through `method='acoustic'`, a
    reopened window would say "not asked" about an album it had asked about and
    paid a rate-limited request for.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    state = api._album_snapshot()[0]
    # What the workflow leaves behind when the fingerprint was sent and the
    # service knew nothing. Persisting it is what this asserts.
    state.outcome = replace(state.outcome, acoustic="unknown")
    state.identification_id, state.plan_id = api._persist_outcome(state)

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")

    restored = reopened._album_snapshot()[0]
    assert (
        restored.outcome.acoustic == "unknown"
    ), "the answer was paid for; a restart must not turn it back into 'not asked'"


def test_the_bench_says_what_the_audio_answered_about_each_track(tmp_path: Path) -> None:
    """Three states, and two of them must not share one blank column.

    `named`, `unknown` and never asked are different things. Collapsing the last
    two makes an engine that has run read as one that never ran.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    one, other = unit.audio_files
    first, second = one.content_signature, other.content_signature
    # What a lookup leaves behind: one recording named, one asked and unknown —
    # each written against the audio it was drawn from (migration 19).
    api._store.remember_fingerprint(
        first, audio_key(one.path), AudioFingerprint(fingerprint="fp-1", duration_seconds=4)
    )
    api._store.remember_fingerprint(
        second, audio_key(other.path), AudioFingerprint(fingerprint="fp-2", duration_seconds=9)
    )
    api._store.remember_lookup(first, audio_key(one.path), "rec-1", 0.93)
    api._store.remember_lookup(second, audio_key(other.path), None, None)

    answer = api.quality_tracks(str(unit.folder_path))

    heard = {track["signature"]: track for track in answer["tracks"]}
    assert heard[first]["heard"] == "named"
    assert heard[first]["recording_id"] == "rec-1"
    assert heard[first]["recording_score"] == 0.93
    assert (
        heard[second]["heard"] == "unknown"
    ), "asked and unknown is a fact about the library, not an absence to leave blank"
    assert answer["acoustic"] == {"named": 1, "unknown": 1, "tracks": 2}, (
        "and the album says how far the audio reached, so a column of dashes "
        "cannot be mistaken for an engine that never ran"
    )


def test_one_recording_is_not_shown_another_recordings_identity(tmp_path: Path) -> None:
    """One recording is not shown another recording's identity.

    A content signature is a declared shape, not a recording: two different
    recordings of the same length in the same format share one signature. Read
    by shape alone, a fingerprint stored for one of them would be offered to
    the other as its own identity, link included.

    So an answer is shown on the same terms as a bitrate: the row names the
    audio it came from, or no other file this app has ever read shares the
    signature.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    one = unit.audio_files[0]
    # A print and a paid-for lookup from before the audio had a name, and a
    # second file of the same shape under another name.
    api._store.remember_fingerprint(
        one.content_signature,
        None,
        AudioFingerprint(fingerprint="fp-first", duration_seconds=271),
    )
    api._store.remember_lookup(one.content_signature, None, "rec-first", 0.94)
    # The other recording, on disk under another name — a row naming a file that
    # is gone is history rather than a rival, and does not contest anything.
    other_album = tmp_path / "other record"
    other_album.mkdir()
    shutil.copy(FIXTURES / "tone-long.flac", other_album / "02. Same Length.flac")
    with api._store._database.connect() as connection:
        connection.execute(
            "INSERT INTO audio_files (path, content_signature, file_size_bytes, modified_at) "
            "VALUES (?, ?, 1, 'now')",
            (str(other_album / "02. Same Length.flac"), one.content_signature),
        )

    answer = api.quality_tracks(str(unit.folder_path))

    heard = {track["signature"]: track for track in answer["tracks"]}
    assert heard[one.content_signature]["heard"] == "", (
        "an identity that cannot be shown to be this file's is not this file's, "
        "and a link that leads to another record is the worst kind of wrong this "
        "screen can be"
    )
    assert heard[one.content_signature]["recording_id"] is None

    # And the print claims its row the moment the audio is fingerprinted again
    # and comes back identical — the judge — so the answer already paid for returns.
    api._store.remember_fingerprint(
        one.content_signature,
        audio_key(one.path),
        AudioFingerprint(fingerprint="fp-first", duration_seconds=271),
    )

    claimed = api.quality_tracks(str(unit.folder_path))

    named = {track["signature"]: track for track in claimed["tracks"]}
    assert named[one.content_signature]["heard"] == "named"
    assert named[one.content_signature]["recording_id"] == "rec-first"


def test_an_album_restored_before_the_answer_was_kept_still_says_it_was_asked(
    tmp_path: Path,
) -> None:
    """An album identified before the answer was kept still says it was asked.

    Every album is restored from an identification, and an identification
    recorded before the audio's answer was written down carries none — so the
    dialog would say *"The audio was not fingerprinted"* about files that carry
    a fingerprint and a lookup, and the only way out would be re-scanning.

    So the album's state is derived from the files, where the fact actually
    lives: `acoustic_fingerprints` records what was asked and what came back per
    audio, and it survives every re-identification.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    for file in unit.audio_files:
        api._store.remember_fingerprint(
            file.content_signature,
            audio_key(file.path),
            AudioFingerprint(fingerprint=f"fp-{file.path.name}", duration_seconds=4),
        )
        api._store.remember_lookup(file.content_signature, audio_key(file.path), None, None)

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")

    restored = reopened._album_snapshot()[0]
    assert restored.outcome.acoustic == "", "the identification on record carries nothing"
    assert reopened.album(restored.unit_id)["album"]["acoustic"] == "unknown", (
        "and the dialog has to read the fact off the files instead of saying "
        "the audio was never fingerprinted"
    )


def test_a_drop_refused_while_files_are_being_renamed_still_answers(tmp_path: Path) -> None:
    """The window waits from the moment something lands on it, and must be let go.

    `receive_drop` in the UI layer calls this and throws the answer away — only
    the platform knows where a dropped folder is, so the return value has
    nowhere to go. A refusal that only returns is therefore a refusal nobody
    hears, and `Reading what you dropped…` would spin in the top bar until the
    window was restarted.

    The one job a drop waits for is one that renames the user's files; a scan
    does not refuse it. The refusal that remains must answer the same way, and
    must say it is about the read so a scan beside it keeps its bar.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    renaming = threading.Event()
    api._job = threading.Thread(target=renaming.wait, args=(30,), daemon=True, name=_WRITES_FILES)
    api._job.start()
    try:
        refused = api.adopt_folders([str(library)])
        opened = api.open_folder(str(library))
    finally:
        renaming.set()
        _finish(api._job, "rename")

    assert refused["ok"] is False
    assert opened["ok"] is False, "the picker reads the same folder the drop does"
    # Drained once: `events` empties the queue, so asking twice loses half of it.
    announced = api.events()
    errors = [event["payload"] for event in announced if event["type"] == "error"]
    assert errors and all(
        error.get("reading") for error in errors
    ), "the user is told why, as a read"
    released = [
        event
        for event in announced
        if event["type"] == "adopted" and event["payload"].get("refused")
    ]
    assert released, "and the window is let go of"
    assert api._reads == [], "nothing was read under the renames"


class _HeldSource(FakeSource):
    """A catalogue that stops answering on one album until it is let go."""

    def __init__(self, held: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._held = held
        self.reached = threading.Event()
        self.release = threading.Event()

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        if self._held in repr(query).lower():
            self.reached.set()
            self.release.wait(30)
        return super().search_releases(query)


def test_a_folder_is_read_while_a_scan_is_running_and_leaves_its_answers_alone(
    tmp_path: Path,
) -> None:
    """A folder can be added while a scan is running.

    Held in the middle of a real scan: the first album has its answer, the
    second is waiting on the catalogue. A folder elsewhere is read and arrives
    while the scan is still held — and a drop of the album the scan has just
    answered does not put it back to *not looked at*, which is what a read
    finishing after the scan's answer would otherwise do.
    """
    library = _library(tmp_path)
    second = library / "zz second record"
    second.mkdir()
    shutil.copy(FIXTURES / "tone.flac", second / "aaa.flac")
    elsewhere = tmp_path / "elsewhere" / "nilo varga - ônix"
    elsewhere.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone-long.flac", elsewhere / "aaa.flac")
    source = _HeldSource(
        "second",
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    answered = library / "marina do acordeao - forro"

    assert api.scan(str(library))["ok"]
    try:
        assert source.reached.wait(30), "the scan never reached the album it is held on"
        first = next(state for state in api._album_snapshot() if state.unit.folder_path == answered)
        assert first.looked_at, "the first album has the scan's answer"

        assert api.open_folder(str(elsewhere))["ok"], "a scan refused the folder"
        assert api.adopt_folders([str(answered)])["ok"], "a scan refused the drop"
        _finish_reads(api)
        assert api._job.is_alive(), "the reads finished while the scan was still held"

        folders = {state.unit.folder_path: state for state in api._album_snapshot()}
        assert elsewhere in folders, "the folder read beside the scan is not on the shelf"
        assert folders[answered].looked_at, "the drop put the scan's answer back to unread"
    finally:
        source.release.set()
        _finish(api._job, "scan")

    after = {state.unit.folder_path: state for state in api._album_snapshot()}
    assert after[answered].looked_at and after[second].looked_at
    assert not api._identifying_folders, "the run left folders marked as being identified"


def test_a_drop_of_something_that_is_not_a_folder_still_answers(tmp_path: Path) -> None:
    """The other silent refusal, released the same way."""
    api = _api(tmp_path, FakeSource())

    refused = api.adopt_folders([str(tmp_path / "nowhere")])

    assert refused["ok"] is False
    events = api.events()
    assert [event["type"] for event in events] == ["error", "adopted"]
    assert events[1]["payload"]["refused"] is True
    assert "Not a folder" in str(events[0]["payload"]["message"])


def test_the_dialog_is_answered_without_waiting_for_the_witness(tmp_path: Path) -> None:
    """The dialog is drawn without waiting for the network.

    Every store read behind the dialog answers in under a millisecond; the one
    thing that reaches the network is the witness. The question is still put —
    one request for the album being opened — but the screen is drawn from what
    is already known and gains the witness's line when the answer comes.
    """

    class SlowWitness(FakeWitness):
        """A witness that will not answer until the test says so."""

        def __init__(self) -> None:
            super().__init__()
            self.may_answer = threading.Event()

        def verify(self, *arguments: object, **keywords: object) -> object:
            self.may_answer.wait(timeout=5)
            return super().verify(*arguments, **keywords)

    library = _library(tmp_path)
    witness = SlowWitness()
    api = _api(tmp_path, FakeSource(), verifier=witness)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    started = time.perf_counter()
    answered = api.album(unit_id)
    elapsed = time.perf_counter() - started

    assert answered["ok"]
    assert elapsed < 1.0, "the dialog does not wait on a network nobody can see"
    itunes = next(row for row in answered["album"]["positions"] if row["source"] == "itunes")
    assert itunes["asked"] is False, "and it says so, rather than claiming an answer it lacks"

    witness.may_answer.set()
    settled = _after_the_witness(api, unit_id, answered["album"])

    heard = next(row for row in settled["positions"] if row["source"] == "itunes")
    assert heard["asked"] is True, "the answer arrives on its own, as the album it belongs to"


def test_one_album_is_never_put_to_the_witness_twice_at_once(tmp_path: Path) -> None:
    """A dialog opened again while the request is out does not ask again.

    The witness is rate-limited, and a dialog reopened three times must not
    spend three requests on one album.
    """

    class SlowWitness(FakeWitness):
        def __init__(self) -> None:
            super().__init__()
            self.may_answer = threading.Event()

        def verify(self, *arguments: object, **keywords: object) -> object:
            self.may_answer.wait(timeout=5)
            return super().verify(*arguments, **keywords)

    library = _library(tmp_path)
    witness = SlowWitness()
    api = _api(tmp_path, FakeSource(), verifier=witness)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])

    for _ in range(3):
        assert api.album(unit_id)["ok"]
    witness.may_answer.set()
    _after_the_witness(api, unit_id, {})

    assert len(witness.asked) == 1, "one album, one request, however often it is opened"


def test_an_album_filed_away_is_followed_before_it_is_planned_again(tmp_path: Path) -> None:
    """An album moved by hand is looked for before it is planned again.

    An album organized, filed into a collection folder by hand, and then
    planned again: if the copy in memory still named the old place, the tag read
    would fail and the screen would say `Tags could not be read` about a file
    that is perfectly fine one folder up — an absence wearing the words of
    damage.

    It is looked for the way a restart looks for it: only where this app has
    been shown, and adopted only when the audio agrees it is the same album.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    organized = next(path for path in library.iterdir() if path.is_dir())
    collection = tmp_path / "collection"
    collection.mkdir()
    moved = Path(shutil.move(str(organized), str(collection / organized.name)))
    # The collection folder is a place this app has been shown, because it
    # holds an album it has already seen.
    api._store.record_unit(_scan_once(collection))

    resumed = api.plan_anyway(unit_id)

    assert resumed["ok"], resumed.get("error")
    assert api._albums[unit_id].unit.folder_path == moved
    stored = api._store.unit_by_signature(api._albums[unit_id].unit.unit_signature)
    assert stored is not None and stored.folder_path == moved, "and the row followed it too"


def test_an_album_nowhere_this_app_has_seen_says_so_instead_of_blaming_a_file(
    tmp_path: Path,
) -> None:
    """The other half: when it cannot be found, the sentence has to be true.

    Nothing searches the whole disk, so an album carried off somewhere this
    app has never been shown is honestly beyond it — and it says that, and says
    what can be done about it, rather than reporting damage to a file.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    organized = next(path for path in library.iterdir() if path.is_dir())
    away = tmp_path / "somewhere else entirely"
    away.mkdir()
    shutil.move(str(organized), str(away / organized.name))

    refused = api.plan_anyway(unit_id)

    assert refused["ok"] is False
    assert "not where it was" in refused["error"]
    assert "Drop it on the window" in refused["error"]
    assert "could not be read" not in refused["error"], "no file is blamed for a move"


def _scan_once(folder: Path) -> "AlbumUnit":
    """The one album under a folder, read off the disk."""
    scanner = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.api"))
    return scanner.scan(folder)[0]


def test_the_badge_says_the_stated_verdict_and_the_numbers_stay_as_measured(
    tmp_path: Path,
) -> None:
    """A track marked not lossy stops wearing `Was lossy` in the Quality table.

    The album header yields to a stated verdict through `_with_user_word`, and
    the track rows have to as well: otherwise a word that saved and was ignored
    cannot be told from a word that failed to save.

    What must NOT move is the evidence: the wall, the fall and the ceiling are
    the measurement, and a disagreement that erases them cannot be audited or
    withdrawn.
    """
    library = tmp_path / "library"
    folder = library / "Duo Varela"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "01. Track.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    one = unit.audio_files[0]
    api._store.record_quality(
        {
            one.content_signature: StoredQuality(
                encoding="transcoded",
                effective_bitrate_kbps=192,
                cutoff_hertz=17_500,
                steepest_drop_db=40.0,
                findings=(),
                reason="A wall at 17.5 kHz.",
                decay_db=30.0,
                ceiling_db=-100.0,
                audio_key=one.audio_key,
            )
        }
    )

    measured = api.quality_tracks(str(folder))["tracks"][0]
    assert measured["transcoded"], "the measurement stands where nothing has been said"

    api.set_quality_verdict(unit.unit_signature, "honest", one.content_signature, one.audio_key)

    said = api.quality_tracks(str(folder))["tracks"][0]
    assert not said["transcoded"], "marked not lossy, so the badge stops saying was lossy"
    assert said["encoding"] == "lossless"
    assert said["override"] == "honest", "and `Yours` still shows whose word it is"
    assert said["cutoff"] == 17_500, "the wall is evidence and does not move"
    assert said["decay_db"] == 30.0
    assert said["ceiling_db"] == -100.0
    assert (
        "What it measured is unchanged" in said["reason"]
    ), "the reason carries both, so the disagreement stays auditable"

    # And the accusation comes back the moment the word is withdrawn.
    api.set_quality_verdict(unit.unit_signature, "clear", one.content_signature, one.audio_key)
    assert api.quality_tracks(str(folder))["tracks"][0]["transcoded"]


def test_an_honest_mp3_is_not_made_lossless_by_a_word_about_it(tmp_path: Path) -> None:
    """A word about an honest MP3 does not make it lossless.

    A word decides whether a lossless container was lossy before it arrived. An
    honest MP3 is `lossy` because that is what it *is*, and no word about it
    makes it something else — so the re-judging stops at the container, the
    same boundary `_album_word` draws.
    """
    library = tmp_path / "library"
    folder = library / "Real MP3"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "01. Track.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    one = unit.audio_files[0]
    api._store.record_quality(
        {
            one.content_signature: StoredQuality(
                encoding="lossy",
                effective_bitrate_kbps=320,
                cutoff_hertz=20_000,
                steepest_drop_db=30.0,
                findings=(),
                reason="A lossy stream whose audio stops at 20000 Hz.",
                decay_db=8.0,
                ceiling_db=-100.0,
                audio_key=one.audio_key,
            )
        }
    )

    api.set_quality_verdict(unit.unit_signature, "honest", one.content_signature, one.audio_key)

    track = api.quality_tracks(str(folder))["tracks"][0]
    assert track["encoding"] == "lossy", "an honest MP3 stays what it is"
    assert not track["transcoded"]
    assert track["override"] == "honest", "the word is kept, it just decides nothing here"


def test_marking_one_copy_marks_the_other_when_they_are_the_same_audio(
    tmp_path: Path,
) -> None:
    """A word about one copy of an audio is a word about the other.

    One folder may hold the instrumental and the regular version of a track as
    the same recording, bit for bit. A word is about audio, so one word about
    that audio is the only answer that does not contradict itself.
    """
    library = tmp_path / "library"
    folder = library / "Pale Kiln"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "Pale Kiln.flac")
    shutil.copy(FIXTURES / "tone.flac", folder / "Pale Kiln - Instrumental.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    one = unit.audio_files[0]

    api.set_quality_verdict(unit.unit_signature, "honest", one.content_signature, one.audio_key)

    tracks = api.quality_tracks(str(folder))["tracks"]
    assert [track["override"] for track in tracks] == [
        "honest",
        "honest",
    ], "one word, one audio, both copies of it"


def test_marking_one_track_leaves_alone_another_of_the_same_shape(tmp_path: Path) -> None:
    """A word about one recording does not land on another of the same shape.

    Two tracks of one album can share a content signature with different
    audio. The table that holds the word is keyed by the audio as well as the
    shape, so a word about one does not reach the other.
    """
    library = tmp_path / "library"
    folder = library / "Orvo & Zell (1974) [FLAC]"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "07. First Shape.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "09. Second Shape.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit = api._album_snapshot()[0].unit
    first, second = unit.audio_files
    # What a real library holds and a fixture cannot: one shape, two recordings.
    with api._store._database.connect() as connection:
        connection.execute(
            "UPDATE audio_files SET content_signature = ? WHERE path = ?",
            (first.content_signature, str(second.path)),
        )

    api.set_quality_verdict(unit.unit_signature, "honest", first.content_signature, first.audio_key)

    words = api._store.quality_words(unit.unit_signature)
    assert word_for(words, first.content_signature, first.audio_key) == "honest"
    assert (
        word_for(words, first.content_signature, second.audio_key) is None
    ), "the other recording was never spoken about and must not wear the word"


def test_a_pairing_the_name_made_says_when_the_published_length_disagrees(
    tmp_path: Path,
) -> None:
    """A pairing made by name passes, and the row says the length is off.

    A name can pair a file with the track the release publishes while the
    length disagrees by many seconds, because the audio in that file is another
    take. Drawn like any other gap, the one sign that a file is not what its
    own name claims would read as one more measurement.

    The threshold is the matcher's own, so the screen cannot drift from what the
    pairing was judged against.
    """
    library = tmp_path / "library"
    folder = library / "Artist - Album"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "Alpha.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "Beta.flac")
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Album",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(
            TrackMetadata(title="Alpha", position=1, duration_ms=400),
            # Named exactly like the file and nowhere near its length: only the
            # name can pair these two.
            TrackMetadata(title="Beta", position=2, duration_ms=60_000),
        ),
    )
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    album = _opened(api, api.state()["albums"][0]["unit_id"])

    paired = {row["track"]: row for row in album["evidence"] if row["status"] == "paired"}
    assert paired["Beta"]["file"] == "Beta.flac", "the name is what pairs this one"
    assert paired["Beta"]["length_disagrees"] is True
    assert paired["Alpha"]["length_disagrees"] is False, "a length that agrees says nothing"


def test_a_file_renamed_by_organising_is_not_reported_as_its_own_duplicate(
    tmp_path: Path,
) -> None:
    """A file renamed by organising is not reported as its own duplicate.

    The registry keeps the row of a file under the name it had before it was
    organised. A map that matched by audio key and asked the disk nothing
    would count one file as two places, and accuse an album of sharing audio
    with an album that does not exist.
    """
    library = tmp_path / "library"
    folder = library / "Brunela de Zinco"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "Brunela de Zinco - Pelma Tral.flac")
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)

    # Organising renames it. The row under the old name stays: these rows are
    # not deleted.
    (folder / "Brunela de Zinco - Pelma Tral.flac").rename(folder / "01. Pelma Tral.flac")
    _scan_and_wait(api, library)

    pairs = api.shared_albums()["pairs"]

    assert pairs == [], "one file under two names is one file, and the disk says which name"


def test_an_album_dropped_again_arrives_marked_and_says_where_it_moved_to(
    tmp_path: Path,
) -> None:
    """An album dropped again arrives marked, and says where it moved to.

    Dropping a folder is pointing at it, and everything it brought in arrives
    marked so that Scan has something to do — including an album already on
    the shelf. Counted and then dropped from the list, the one album just
    pointed at would be the one thing on the shelf that cannot be acted on.

    The store follows a moved album by its audio, and the answer says so: a
    moved album can be hard to find, and its location may need updating.
    """
    library = tmp_path / "dropped"
    folder = library / "quarteto rufo - vesperal"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    api = _api(tmp_path, FakeSource(summaries=(), details={}))

    assert api.adopt_folders([str(folder)])["started"] == 1
    _finish_reads(api)
    first = [event for event in api.events() if event["type"] == "adopted"][-1]["payload"]
    unit_id = first["unit_ids"][0]

    # The same album, in a place the library has never heard of.
    moved_to = tmp_path / "elsewhere" / "Quarteto Rufo - Vesperal (1980) [FLAC]"
    moved_to.parent.mkdir(parents=True)
    folder.rename(moved_to)

    assert api.adopt_folders([str(moved_to)])["started"] == 1
    _finish_reads(api)
    again = [event for event in api.events() if event["type"] == "adopted"][-1]["payload"]

    assert again["albums"] == 0, "it is the same album, not a second one"
    assert again["unit_ids"] == [unit_id], "the album pointed at arrives marked"
    assert [entry["album"] for entry in again["moved"]] == [moved_to.name]
    assert again["moved"][0]["now"] == str(moved_to)
    assert again["moved"][0]["was"] == str(folder)


# --- named by its own tags ---------------------------------------------------


def _tagged_album(
    folder: Path,
    *,
    artist: str = "Quarteto Rufo",
    album: str = "Vesperal",
    year: str = "1980",
    titles: tuple[str, ...] = ("Tio Comunicador", "Salto Carnival"),
    numbers: tuple[str, ...] | None = None,
    album_artist: bool = True,
    fixtures: tuple[str, ...] = ("tone.flac", "tone-long.flac"),
) -> Path:
    """An album folder whose files carry everything a name needs.

    Written with the real tag store onto the real fixtures, because the whole
    question this mode answers is what the files themselves say — a fake that
    returns a dictionary would prove only that the code agrees with itself.
    """
    folder.mkdir(parents=True, exist_ok=True)
    store = MutagenTagStore()
    for index, title in enumerate(titles, start=1):
        # Two folders holding the same files are one album, because a unit is
        # keyed by its audio — so a test that wants two albums has to give them
        # different audio, and `fixtures` is how.
        chosen = fixtures[(index - 1) % len(fixtures)]
        path = folder / f"track{index}{Path(chosen).suffix}"
        shutil.copy(FIXTURES / chosen, path)
        tags: dict[str, tuple[str, ...]] = {
            "title": (title,),
            "artist": (artist,),
            "album": (album,),
            "tracknumber": ((numbers[index - 1] if numbers else str(index)),),
        }
        if album_artist:
            tags["albumartist"] = (artist,)
        if year:
            tags["date"] = (year,)
        store.write(path, tags)
    return folder


def _open_event(api: LibraryApi) -> dict[str, object]:
    """The payload of the last `opened` event, which is where the marks come from.

    Named for the event and not for the album: `_opened(api, unit_id)` above is
    the dialog of one album, and a second `_opened` here would silently take
    the first one's calls.
    """
    return [event for event in api.events() if event["type"] == "opened"][-1]["payload"]


def test_an_album_arrives_already_named_by_its_own_tags(tmp_path: Path) -> None:
    """An album arrives already proposed by its own tags, and no source is asked.

    Tags are read when a folder is opened or dropped, so an album whose files
    already say enough arrives with a proposal, without a request to any
    catalogue.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    source = FakeSource(summaries=(_release("r1", (400, 900)),))
    api = _api(tmp_path, source)

    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    album = api.state()["albums"][0]
    assert source.searches == [], "the tags cost no catalogue a request"
    assert album["source"] == "tags", "the screen says which proposal this is"
    assert album["title"] == "Vesperal"
    # It has a *proposal*, and it has not been looked up. The two are different
    # facts: the card is drawn from a database holding no identification for
    # the album, and must not claim one.
    assert album["looked_at"] is False, "nobody was asked, which is what the word means"
    assert album["applicable"] is True, "the plan is ready and one press away"
    # Never automatic, however completely the tags close it: a batch apply
    # would rename folders on the strength of tags alone, and could trade a
    # year written by hand for a reissue's.
    assert album["decision"] == "review"

    opened = api.album(album["unit_id"])["album"]
    renames = [
        Path(str(operation["after"]["path"])).name
        for operation in opened["operations"]
        if operation["kind"] == "rename_folder"
    ]
    assert renames == ["Quarteto Rufo - Vesperal (1980) [FLAC]"]


def test_an_album_the_tags_could_not_name_still_waits_to_be_scanned(tmp_path: Path) -> None:
    """The tags answer what they can, and say nothing about what they cannot.

    An album with no plausible year anywhere arrives exactly as it would
    without this mode: on screen, not looked up, waiting to be scanned. The
    album beside it, whose tags do say enough, arrives with an answer.

    Neither of them arrives marked: marking is the window's to do, and it is
    checked in the window's own guard.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    # No year anywhere, which is what makes an album unnameable by its tags.
    _tagged_album(
        library / "no year at all",
        album="Pale As A Lantern",
        year="",
        titles=("Pelma", "Runo", "Tral"),
    )
    api = _api(tmp_path, FakeSource())

    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    by_folder = {album["folder"]: album for album in api.state()["albums"]}
    assert len(by_folder) == 2
    payload = _open_event(api)
    assert sorted(payload["albums"]) == sorted(
        album["unit_id"] for album in by_folder.values()
    ), "everything that arrived is reported"
    assert by_folder["no year at all"]["looked_at"] is False
    assert by_folder["no year at all"]["source"] is None
    # Both of them are albums nobody has looked up: one arrives with a proposal
    # and one without, and neither has had a catalogue asked about it.
    assert by_folder["quarteto rufo vesperal"]["looked_at"] is False
    assert by_folder["quarteto rufo vesperal"]["source"] == "tags"


def test_the_tag_proposal_renames_and_does_nothing_else(tmp_path: Path) -> None:
    """The tag proposal renames, and does nothing else.

    The tags are the source here. Handing them back would let one track's value
    overwrite another's unseen, and no cover is fetched either. Proven on a
    folder whose tags disagree with what the release implies: `tracknumber`
    says 1 and 2, the titles differ from the file names, and still not one
    `write_tags` is planned.

    The two exceptions are both the file's own picture put somewhere the file
    is not: the Finder icon and the folder's `cover.jpg`. These fixture files
    carry no picture at all, so nothing beyond renames may appear here — which
    is what keeps this test about the tags rather than about the artwork.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    api = _api(tmp_path, FakeSource())

    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    album = api.album(api.state()["albums"][0]["unit_id"])["album"]
    kinds = {operation["kind"] for operation in album["operations"]}
    assert kinds <= {"rename_file", "rename_folder"}, f"the tag path planned {kinds}"
    assert "rename_folder" in kinds, "it did plan something, so the check above means something"


def test_an_arrangement_offers_the_finder_icon_the_file_already_holds(tmp_path: Path) -> None:
    """An arrangement offers the Finder icon and the folder cover, from the file's own picture.

    The icon is an image gesture that fits the arrangement's rule — made from
    the picture the file already carries, offered only to a file that draws
    none, reaching nobody. **The folder's `cover.jpg` is the second one**: the
    same picture, written beside the album rather than into it.

    What must not appear is anything that enters one of the files: no
    `write_tags`, and no `embed_image` that is not the icon. The offline mode
    whole — names in the configured pattern, a cover the folder can show,
    covers visible in the Finder, and nothing fetched from anyone.
    """
    if sys.platform != "darwin":
        pytest.skip("the Finder's icon is written through the platform's own call")
    from tests.library.test_artwork_store import _drawable_jpeg

    library = tmp_path / "library"
    folder = _tagged_album(library / "quarteto rufo vesperal")
    store = FilesystemArtworkStore()
    image = tmp_path / "cover.jpg"
    image.write_bytes(_drawable_jpeg())
    for track in sorted(folder.glob("*.flac")):
        store.embed(track, image)
    api = _api(tmp_path, FakeSource())

    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    album = api.album(api.state()["albums"][0]["unit_id"])["album"]
    operations = album["operations"]
    kinds = {operation["kind"] for operation in operations}
    assert kinds <= {"rename_file", "rename_folder", "embed_image", "write_image"}, kinds
    icons = [op for op in operations if op["kind"] == "embed_image"]
    assert len(icons) == 2, "each file carrying a drawable picture is offered its icon"
    assert all(op["after"].get("icon") for op in icons), "and only ever the icon"
    assert "write_tags" not in kinds, "the arrangement writes no tag, which is its whole promise"
    covers = [op for op in operations if op["kind"] == "write_image"]
    assert len(covers) == 1, "the folder it lands in gets the picture the files already carry"
    assert Path(covers[0]["target"]).name == "cover.jpg", covers[0]["target"]


def test_two_albums_whose_tags_read_alike_send_the_second_to_a_scan(tmp_path: Path) -> None:
    """A collision between two tag proposals sends the second album to a scan.

    The planner breaks a collision by naming the pressing year, and it cannot
    here — a release built from tags has one year and no pressing to fall back
    on. Two volumes of a series tagged with one album title between them would
    both be proposed the same folder name.
    """
    library = tmp_path / "library"
    _tagged_album(library / "first time one", titles=("A", "B"))
    _tagged_album(library / "first time two", titles=("C", "D", "E"))
    api = _api(tmp_path, FakeSource())

    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    albums = api.state()["albums"]
    settled = [album for album in albums if album["applicable"]]
    contested = [album for album in albums if not album["applicable"]]
    assert len(settled) == 1 and len(contested) == 1, "one claims the name, one asks"
    reason = str(api.album(contested[0]["unit_id"])["album"]["reason"])
    assert "already claims the name" in reason and "waiting for a scan" in reason


def test_an_album_a_scan_already_answered_is_not_re_proposed_by_its_tags(
    tmp_path: Path,
) -> None:
    """The other way round: the scan's answer supersedes the tags.

    An album is scanned because there were doubts about it. So the tags never
    propose over an album a catalogue has answered — dropping its folder again
    leaves it waiting to be scanned rather than renamed on a re-reading of its
    files.
    """
    library = tmp_path / "library"
    folder = _tagged_album(library / "quarteto rufo vesperal")
    right = _release("r1", (400, 900), title="Vesperal")
    api = _api(tmp_path, FakeSource(summaries=(right,), details={"r1": right}))
    _scan_and_wait(api, library)
    assert api.state()["albums"][0]["source"] == "discogs"

    assert api.adopt_folders([str(folder)])["started"] == 1
    _finish_reads(api)

    album = api.state()["albums"][0]
    assert album["source"] != "tags", "the tags did not propose over the scan"
    assert album["looked_at"] is False, "and it says so, rather than claiming an answer"


def test_reopening_the_app_brings_the_album_back_proposed_by_its_tags(
    tmp_path: Path,
) -> None:
    """The proposal is there when the app is reopened too.

    Nothing is fetched on the way in and nothing needs to be: the answer is in
    the files themselves, and it is re-read rather than stored.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    source = FakeSource()
    api = _api(tmp_path, source)
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    assert api.state()["albums"][0]["source"] == "tags"

    reopened = _api(tmp_path, source)
    reopened.library_present()
    _finish(reopened._restore_job, "restore")

    album = reopened.state()["albums"][0]
    assert album["source"] == "tags"
    assert album["applicable"] is True
    assert source.searches == [], "coming back asked no catalogue anything"


def test_applying_an_album_its_tags_named_records_that_they_named_it(
    tmp_path: Path,
) -> None:
    """Applying an arrangement records `existing_tags` as the method.

    Without it, an album applied from its own files would come back from a
    restart looking like a text search against a catalogue that was never
    asked.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    api = _api(tmp_path, FakeSource())
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.approve(unit_id)["ok"]

    assert (library / "Quarteto Rufo - Vesperal (1980) [FLAC]").is_dir()
    recorded = api._store.identification_for(unit_id)
    assert recorded is not None and recorded["method"] == "existing_tags"


def test_planning_again_an_album_its_tags_named_goes_back_to_the_arrangement(
    tmp_path: Path,
) -> None:
    """An album organized from its own tags opens as that arrangement, asking nobody.

    A catalogue is standing by with an answer here, and the gesture does not
    ask it: the album was organized as its files name it, with one title typed
    over them, and that is the screen to open. Going to a catalogue would
    propose another record's names over a finished album. Asked in the same
    session and after a restart, which read the album from different places.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.correct(unit_id, track_titles={"1": "A Title Typed Now"})["ok"]
    assert api.approve(unit_id)["ok"]
    organized = library / "Quarteto Rufo - Vesperal (1980) [FLAC]"
    assert organized.is_dir()

    def assert_it_is_the_arrangement(answer: dict[str, object]) -> None:
        assert answer["ok"], answer.get("error")
        album = answer["album"]
        assert album["source"] == "tags", "a catalogue's answer took the arrangement's place"
        assert source.searches == [], "no catalogue is asked by this gesture"
        assert album["asleep"] is False, "the names are still closed"
        assert album["organized"] is True and album["operations"] == []
        assert [
            operation for operation in album["operations"] if operation["kind"] == "write_tags"
        ] == [], "an arrangement hands no tag back to the file it was read from"
        assert [
            entry["value"] for entry in album["corrections"] if entry["field"] == "track_title"
        ] == ["A Title Typed Now"]

    assert_it_is_the_arrangement(api.plan_anyway(unit_id))
    assert api.cancel(unit_id)["ok"]

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")
    assert_it_is_the_arrangement(reopened.plan_anyway(unit_id))


def test_a_pairing_held_for_another_release_does_not_turn_an_arrangement_into_tag_writes(
    tmp_path: Path,
) -> None:
    """Rows that speak about another release leave an arrangement an arrangement.

    A pairing is kept against the release it was made on. Made on a catalogue's
    release and then set aside for the album's own tags, it is held and not
    applied — and its mere presence must not send the arrangement down the path
    of an identified album, which plans tag writes and an embedded cover.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    release = _release("r1", (400, 900))
    source = FakeSource(summaries=(release,), details={"r1": release})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    signature = str(_row_for(_opened(api, unit_id), 2)["signature"])
    assert api.correct(unit_id, track_files={"2": signature})["ok"]
    assert api.arrange_by_tags(unit_id)["ok"]
    assert api.approve(unit_id)["ok"]

    again = api.plan_anyway(unit_id)

    assert again["ok"], again.get("error")
    assert again["album"]["source"] == "tags"
    assert again["album"]["operations"] == [], "it was applied as an arrangement, and it is one"


def test_no_batch_gesture_reaches_an_album_its_tags_named(tmp_path: Path) -> None:
    """No batch gesture reaches an album its tags named.

    `Apply N automatic` over tag proposals would rename folders nobody looked
    at, some of them trading a year written by hand for a reissue's or losing
    a word. So the answer is complete and the plan is ready, and reaching it is
    one press on that album — the same protection an album paired by hand has.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    api = _api(tmp_path, FakeSource())
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    batch = api.apply_automatic()
    assert batch["ok"] and batch["applied"] == 0 and batch["failures"] == []

    assert (library / "quarteto rufo vesperal").is_dir(), "the batch left the folder alone"
    # And it is not the batch refusing a broken plan: a single Apply runs it.
    assert api.approve(api.state()["albums"][0]["unit_id"])["ok"]
    assert (library / "Quarteto Rufo - Vesperal (1980) [FLAC]").is_dir()


def test_an_arrangement_does_not_come_back_automatic_after_a_restart(
    tmp_path: Path,
) -> None:
    """An arrangement restored from an `identified` row still comes back `review`.

    A database can hold an `existing_tags` identification against a unit whose
    state is `identified`, which is the value `_persist_outcome` writes for an
    album it called automatic. Rebuilt from that row, the album must come back
    `review`.

    What the batch refuses is separate and is enforced where the writing
    happens, since that is the only place a refusal can be certain.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    source = FakeSource()
    api = _api(tmp_path, source)
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    # An `existing_tags` identification against a unit whose state is
    # `identified`. Written here rather than relying on the fixture's own
    # `needs_review`: with `needs_review` an older clamp fires first and the
    # guard under test is never reached.
    api._store.set_unit_state(api.state()["albums"][0]["unit_id"], "identified")

    reopened = _api(tmp_path, source)
    reopened.library_present()
    _finish(reopened._restore_job, "restore")

    album = reopened.state()["albums"][0]
    assert album["source"] == "tags"
    assert album["decision"] != "automatic", (
        "an arrangement came back as an answer the app is sure about, so the "
        "shelf counts it into a batch it is kept out of"
    )
    # And the gesture that writes refuses it whatever the decision says, because
    # that is the only place the refusal can be certain.
    batch = reopened.apply_automatic([album["unit_id"]])
    assert batch["applied"] == 0 and batch["failures"] == []
    assert (library / "quarteto rufo vesperal").is_dir(), "the batch renamed the folder"


def test_the_tags_never_propose_at_an_album_that_was_already_on_the_shelf(
    tmp_path: Path,
) -> None:
    """The tags never propose at an album that was already on the shelf.

    The shelf as it stood when this mode first opened the database is written
    down once, and everything on it keeps whatever it had — including when the
    app is reopened, which is the moment the mode would otherwise repaint cards
    nobody asked about. What arrives afterwards is proposed.
    """
    library = tmp_path / "library"
    before = _tagged_album(library / "already here", album="Pale As A Lantern")
    logger = logging.getLogger("test.api")
    database = Database(tmp_path / "app" / "library.sqlite3", logger)
    database.initialize()
    scanner = LibraryScanner(MutagenAudioProbe(), logger)
    # Recorded before any API exists, which is what "already in the library"
    # means: the row is there when the watermark is taken.
    LibraryStore(database, logger).record_units(list(scanner.scan(before)))

    api = _api(tmp_path, FakeSource())
    after = _tagged_album(library / "arrived later", titles=("One", "Two", "Three"))
    assert api.adopt_folders([str(before), str(after)])["started"] == 2
    _finish_reads(api)

    by_folder = {album["folder"]: album for album in api.state()["albums"]}
    assert by_folder["already here"]["source"] is None
    assert by_folder["already here"]["looked_at"] is False
    assert by_folder["arrived later"]["source"] == "tags"


def test_an_arrangement_wears_no_identification_clothes(tmp_path: Path) -> None:
    """Arranging is not identifying, and the payload claims neither.

    A dialog that wore `Matches 100%`, a published length and a source panel
    over an arrangement would be a file agreeing with itself dressed as proof.
    An arrangement claims nothing about the world, so the payload carries
    provenance instead: what was read, how unanimously, and which files
    dissented.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    api = _api(tmp_path, FakeSource())
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)

    album = api.album(api.state()["albums"][0]["unit_id"])["album"]

    assert album["positions"] == [], "an arrangement has no source panel"
    assert album["proof"] == "none", "nothing proved anything"
    assert album["reason"].startswith("Arranged from this album's own tags"), album["reason"]
    assert "Confidence" not in album["reason"]
    reading = album["tag_reading"]
    assert reading["artist"] == "Quarteto Rufo" and reading["album"] == "Vesperal"
    assert reading["year"] == 1980 and reading["files"] == 2
    assert reading["dissent"] == []
    assert album["tags_say"] == {}, "the tags are not compared against themselves"


def test_arrange_by_tags_is_explicit_and_outranks_the_defaults(tmp_path: Path) -> None:
    """The button is an explicit request, so the default guards yield.

    The watermark stops *unasked* repainting of albums that predate the mode,
    and a scan's answer supersedes the *automatic* proposal — but `Arrange by
    tags` pressed on this album is neither unasked nor automatic. Both guards
    step aside; nothing is renamed until Approve.
    """
    library = tmp_path / "library"
    before = _tagged_album(library / "already here")
    logger = logging.getLogger("test.api")
    database = Database(tmp_path / "app" / "library.sqlite3", logger)
    database.initialize()
    scanner = LibraryScanner(MutagenAudioProbe(), logger)
    LibraryStore(database, logger).record_units(list(scanner.scan(before)))

    api = _api(tmp_path, FakeSource())
    assert api.adopt_folders([str(before)])["started"] == 1
    _finish_reads(api)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.state()["albums"][0]["source"] is None, "older than the watermark: untouched"

    answer = api.arrange_by_tags(unit_id)

    assert answer["ok"], answer
    assert answer["album"]["source"] == "tags"
    assert (library / "already here").is_dir(), "proposed, not applied"
    assert api.approve(unit_id)["ok"]
    assert (library / "Quarteto Rufo - Vesperal (1980) [FLAC]").is_dir()


def test_arrange_marked_renames_the_marked_and_says_what_it_skipped(tmp_path: Path) -> None:
    """Mark, press, renamed — and every refusal counted by reason.

    The batch never reaches an album a scan answered (the dialog's own button
    is the override) and never an organized one. What it skips it says,
    because a number that silently shrank from what was marked reads as albums
    lost.
    """
    library = tmp_path / "library"
    _tagged_album(library / "arrangeable")
    _tagged_album(library / "scanned one", album="Pale As A Lantern", titles=("A", "B", "C"))
    right = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(right,), details={"r1": right}))
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    by_folder = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}
    assert api.scan_selected([by_folder["scanned one"]])["ok"]
    _finish(api._job, "scan")

    assert api.arrange_marked(list(by_folder.values()))["ok"]
    _finish(api._job, "arrange")

    arranged = [event for event in api.events() if event["type"] == "arranged"][-1]["payload"]
    assert arranged["albums"] == 1
    assert arranged["skipped"] == {"scanned": 1}
    assert (library / "Quarteto Rufo - Vesperal (1980) [FLAC]").is_dir()
    assert (library / "scanned one").is_dir(), "the scanned album kept the catalogue's answer"
    recorded = api._store.identification_for(by_folder["arrangeable"])
    assert recorded is not None and recorded["method"] == "existing_tags"


def test_a_scans_answer_is_read_against_what_the_tags_said(tmp_path: Path) -> None:
    """A scan's answer is read against what the tags said, in one line.

    This is the verification the tag mode could never provide for itself,
    arriving at the only moment it exists — and it reads identity, not spelling.
    """
    library = tmp_path / "library"
    _tagged_album(library / "quarteto rufo vesperal")
    right = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(right,), details={"r1": right}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    album = api.album(unit_id)["album"]

    said = album["tags_say"]
    assert said["agrees"] is False
    assert any("year 1980" in diff and "1955" in diff for diff in said["diffs"]), said
    assert any("Quarteto Rufo" in diff for diff in said["diffs"]), "the artist differs too"


def test_an_mp3_apply_passes_its_own_certificate(tmp_path: Path) -> None:
    """The certificate compares tags in the form the container writes them.

    The writer folds `7` into the `7/11` that ID3 expects natively. A verifier
    that compared the pre-fold value would stamp every MP3 apply `differs:
    tracknumber` over a write that was right — and a proof that fails on every
    MP3 album is a proof nobody believes when it matters.
    """
    library = tmp_path / "library"
    folder = library / "marina mp3"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.mp3", folder / "aaa.mp3")
    release = _release("r9", (444,))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r9": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.approve(unit_id)["ok"]

    album = api.state()["albums"][0]
    assert album["audio_ok"] is True
    assert album["writes_ok"] is True, "the certificate reported a difference that is not there"


def test_a_finder_icon_operation_is_verified_not_crashed_on(tmp_path: Path) -> None:
    """The verifier knows the icon operation the plan writes.

    `_finder_icons` plans `embed_image` with `{"icon": true}` and no digest. A
    verifier that reached for `after["digest"]` would raise a KeyError,
    swallowed as `unreadable: 'digest'`, and fail the certificate about an icon
    that had in fact been written.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource(summaries=(RIGHT,), details={"r2": RIGHT}))
    _scan_and_wait(api, library)
    state = next(iter(api._albums.values()))
    icon_op = ChangeOperation(
        sequence=1,
        kind=OperationKind.EMBED_IMAGE,
        target_path=state.unit.audio_files[0].path,
        after_state={"icon": True},
        before_state={},
    )

    said = api._verify_operation(icon_op, ())

    # The fixture file draws no icon, so the honest answer is `missing` — and
    # never `unreadable: 'digest'`, which would be the verifier failing on an
    # operation it does not know.
    assert said in ("verified", "missing")


def test_an_mp3_album_already_right_plans_nothing(tmp_path: Path) -> None:
    """The planner compares tags in the container's own form, like the certificate.

    An ID3 file reads `tracknumber` back as `7/11`. Compared against the
    pre-fold `7`, an already-correct MP3 album would plan a rewrite of identical
    values on every track, and could never reach "already exactly as it should
    be".
    """
    library = tmp_path / "library"
    folder = library / "marina mp3"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.mp3", folder / "aaa.mp3")
    release = _release("r9", (444,))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r9": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]

    replanned = api.plan_anyway(unit_id)

    assert replanned["ok"], replanned
    kinds = [operation["kind"] for operation in replanned["album"]["operations"]]
    assert "write_tags" not in kinds, (
        "an album whose tags are already exactly what the release implies "
        f"still plans rewriting them: {kinds}"
    )


def test_following_a_moved_album_is_said_and_not_only_logged(tmp_path: Path) -> None:
    """Following a moved album is said, and not only logged.

    Dropping a folder announces a re-anchoring, and the two gestures that do the
    same thing — planning an album again, and arranging one by its own tags —
    have to announce it too. A move nobody is told about is indistinguishable
    from a screen claiming nothing happened.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    organized = next(path for path in library.iterdir() if path.is_dir())
    collection = tmp_path / "collection"
    collection.mkdir()
    moved = Path(shutil.move(str(organized), str(collection / organized.name)))
    # A place this app has been shown, which is the only kind it looks in.
    api._store.record_unit(_scan_once(collection))

    answer = api.plan_anyway(unit_id)

    assert answer["ok"], answer.get("error")
    assert answer["moved"] is not None, "it followed the album and said nothing"
    assert answer["moved"]["now"] == str(moved)
    assert answer["moved"]["was"] == str(organized)
    # And an album that had not moved says nothing, so the sentence means what
    # it says when it appears.
    assert api.plan_anyway(unit_id)["moved"] is None


def test_the_composer_shows_the_arrangement_even_where_the_album_fills_nothing(
    tmp_path: Path,
) -> None:
    """The composer's preview shows every chosen piece, filled or not.

    A name with the year missing looks precisely like an arrangement that was
    never asked for a year, and the panel has to tell those apart.

    So the preview is two lines and a sentence: the shape, where every piece
    stands for itself and nothing disappears; the rendered name, which is what
    this album really produces; and the pieces that came back empty, named.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    preview = api.naming_preview("%albumartist%{ - %album%}{ (%year%)}{ (%catalog%)}", "")

    assert preview["ok"]
    assert (
        preview["folder_shape"] == "Artist - Album (Year) (Catalogue no.)"
    ), "the shape names every piece, whatever this album holds"
    assert (
        "Catalogue no." in preview["folder_missing"]
    ), "the fake source publishes no catalogue number, and the panel says so"
    assert "(Catalogue no.)" not in preview["folder"], "the rendered name is still the real one"


def test_the_composer_previews_against_the_album_that_fills_the_most(tmp_path: Path) -> None:
    """Not the first on the shelf, which is what "first" would quietly mean.

    The album map is walked in insertion order and the window draws it reversed,
    so the first identified album is the *oldest* one — an arbitrary choice. If
    that album happened to have no year, the preview would make the year look
    broken.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    chosen = api.naming_composer()["sample"]

    assert chosen["available"] is True
    # And it is an album that actually renders a year, which is the whole point
    # of preferring the fullest one.
    assert "(" in api.naming_preview("%albumartist%{ (%year%)}", "")["folder"]


def test_a_new_arrangement_never_reaches_an_album_already_organized(tmp_path: Path) -> None:
    """A change of naming format applies from then on, never to organized albums.

    This is the wiring for that, end to end and against real files: organize an
    album, compose a completely different arrangement, and assert that the
    folder on disk did not move, that the album was given no plan, and that
    `Apply automatic` cannot reach it.

    It matters because settings deliberately rebuild every plan on the shelf —
    a raised threshold has to apply to what is on screen. A rebuild that did
    not stop at organized albums would hand `Apply N automatic` a rename for
    every one of them.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()
    organized = {path.name for path in library.iterdir() if path.is_dir()}
    assert organized, "the album has to be organized for this test to mean anything"

    saved = api.set_settings(
        {"folder_template": "%year%{ - %albumartist%}{ - %album%}", "track_template": "%title%"}
    )

    assert saved["ok"]
    assert saved["replanned"] == 0, "an organized album is not re-planned by a new arrangement"
    assert {
        path.name for path in library.iterdir() if path.is_dir()
    } == organized, "nothing on disk moved"
    album = api.state()["albums"][0]
    assert album["organized"] is True
    assert api.apply_automatic()["applied"] == 0, "and no batch can reach it"


def test_the_new_arrangement_does_apply_to_an_album_that_arrives_afterwards(
    tmp_path: Path,
) -> None:
    """The other half: "from then on" is a promise in both directions.

    A guarantee that nothing changes is easy to keep by changing nothing. What
    has to be true as well is that the next album in gets the arrangement just
    composed.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    api.set_settings({"folder_template": "%albumartist%{ = %album%}", "track_template": "%title%"})

    _scan_and_wait(api, library)
    api.apply_automatic()

    assert any(
        "=" in path.name for path in library.iterdir() if path.is_dir()
    ), "the album scanned after the change carries the new arrangement"


def test_an_album_whose_folder_moved_stays_on_the_shelf_across_a_restart(
    tmp_path: Path,
) -> None:
    """An album whose folder does not answer stays on the shelf across a restart.

    A folder that does not answer at this instant is not an album that is gone:
    an unmounted disk, a cloud folder mid-sync and a rename in the file manager
    are indistinguishable here. A restore that cleared such an album would do
    it permanently and silently, to albums that are not organized and live in
    folders things are moved through.

    The window draws this case with `folder_missing`, a `not where it was`
    badge and `Relocate…`; what it needs is the album reaching the screen at
    all.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    album = api.state()["albums"][0]
    assert album["organized"] is False, "the case is an album that is not organized"

    # Filed away by hand, somewhere this app has never been shown.
    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()
    shutil.move(str(Path(album["folder_path"])), str(elsewhere / "kept"))

    reopened = _api(tmp_path, source)
    _finish(reopened._restore_job, "restore")

    survivors = reopened.state()["albums"]
    assert len(survivors) == 1, "the album is still in the Library"
    assert survivors[0]["folder_missing"] is True, "and it says its folder did not answer"
    # Nothing was written down as gone: only the user takes an album off this
    # shelf. Asked of the store the API actually holds, rather than of a path
    # this test believes it uses.
    assert reopened._store.library_units(), "the row is still there to be restored next time"


def test_a_source_that_answered_is_never_reported_as_one_that_did_not(tmp_path: Path) -> None:
    """One failed request is not an outage, and the sentence it raises says it is.

    The window's notice reads *MusicBrainz was not consulted in that scan… what
    was identified was decided by the other sources*. A source that answered
    several times in a run and failed once must not be announced as absent from
    a run it took part in.
    """
    release = _release("r1", (400, 900))
    api = _api(
        tmp_path,
        FakeSource(summaries=(), details={}),
        witness=FakeSource(summaries=(release,), details={"r1": release}),
    )
    workflow = api._pipeline.workflow
    workflow.sources_unavailable = {"musicbrainz": "unreachable", "discogs": "no_key"}
    # Four real answers, through the Engine that counts them. Arranged by
    # calling rather than by writing the number in: the count is the Engine's,
    # and a test that sets a field proves the reader and never the writer.
    for _ in range(4):
        workflow._metadata.search_source(MetadataSources.MUSICBRAINZ, MetadataQuery(album="Album"))
    assert workflow.sources_answered == {"musicbrainz": 4}

    down = api._sources_down()

    assert down == {
        "discogs": "no_key"
    }, "a source that answered even once was consulted, however many times it also failed"
    # Reading it does not empty it. What makes the answer about this run is that
    # the record was emptied where the run began, which is its own test.
    api._forget_source_failures()
    assert (
        workflow.sources_unavailable == {} and workflow.sources_answered == {}
    ), "both halves are forgotten together, so a count can never outlive its failure"


def test_a_source_that_answered_through_another_door_is_not_reported_absent(
    tmp_path: Path,
) -> None:
    """An answer through any door counts as the source having taken part.

    A search can time out while the acoustic path fetches a release from the
    same source by its id. The album is then settled by the very source a
    notice would say had taken no part, if the count of answers sat beside the
    search loop only.

    So the arrangement here is that shape and not a number typed into a field:
    the search is out, and the only answer arrives through another door.
    """
    release = _release("r1", (400, 900))
    api = _api(
        tmp_path,
        FakeSource(summaries=(), details={}),
        witness=FakeSource(summaries=(release,), details={"r1": release}),
    )
    workflow = api._pipeline.workflow
    workflow.sources_unavailable = {"musicbrainz": "unreachable"}

    assert api._sources_down() == {"musicbrainz": "unreachable"}, "so far it has only failed"

    fetched = workflow._metadata.fetch_release(MetadataSources.MUSICBRAINZ, "r1")

    assert fetched is not None, "the acoustic path's door answered"
    assert api._sources_down() == {}, (
        "MusicBrainz answered in that run, so the window must not say the albums "
        "were decided without it — it is what decided them"
    )


def test_the_covers_of_an_unmarked_album_are_left_alone(tmp_path: Path) -> None:
    """The marks govern the cover half of the batch too.

    The organizing half of this gesture is scoped to what is ticked, and the
    cover half has to be as well. Otherwise `Write 1 embedded cover` stands on
    the bar over a shelf with nothing ticked, and the click writes that picture
    into an album nobody pointed at.
    """
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    written: list[int] = []

    def _apply(plan: object, witness: object | None = None) -> object:
        written.append(int(plan.whose))  # type: ignore[attr-defined]
        return SimpleNamespace(
            state=ExecutionState.APPLIED, applied=(), is_complete=True, backups=()
        )

    api._pipeline.executor = SimpleNamespace(apply=_apply)
    shelf = [
        SimpleNamespace(
            unit_id=identifier,
            cover_plan=SimpleNamespace(whose=identifier, operations=()),
            cover_plan_id=None,
            cover_applied=False,
            applied=False,
            # No audio in the stand-in: this asks which albums are written to,
            # and the weighing that follows a cover write has nothing to weigh
            # here. What that rule does is asserted on real files, in
            # `test_a_chosen_cover_leaves_the_weights_describing_the_files`.
            unit=SimpleNamespace(folder_path=tmp_path / f"album {identifier}", audio_files=()),
        )
        for identifier in (1, 2)
    ]
    api._album_snapshot = lambda: tuple(shelf)  # type: ignore[method-assign]

    failures: list[str] = []
    covers = api._apply_pending_covers(failures, None, [], {2})

    assert written == [2], "only the marked album was written to"
    assert covers == 1 and failures == []


def test_a_folder_counts_a_state_it_has_never_met_as_still_coming(tmp_path: Path) -> None:
    """The bytes still to arrive are written as the complement, never as a list.

    slskd words its states in its own vocabulary, and one this connector has not
    met maps to `unknown`. Counted by naming what is still moving, such a file
    would belong to no list and weigh nothing — and the folder would announce
    itself finished with a file of it never sent.
    """
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    folder = "@@peer\\Music\\Album"
    api._acquisition = SimpleNamespace(
        identifier="slskd",
        progress=lambda source=None: (
            Transfer(
                identifier="a",
                source="slskd",
                name=f"{folder}\\01.flac",
                state=TransferState.COMPLETED,
                size_bytes=1_000,
                transferred_bytes=1_000,
            ),
            Transfer(
                identifier="b",
                source="slskd",
                name=f"{folder}\\02.flac",
                state=TransferState.IN_PROGRESS,
                size_bytes=1_000,
                transferred_bytes=400,
                average_speed=100.0,
            ),
            Transfer(
                identifier="c",
                source="slskd",
                name=f"{folder}\\03.flac",
                state=TransferState.UNKNOWN,
                size_bytes=1_000,
                transferred_bytes=0,
            ),
        ),
    )

    answer = api.acquisition_transfers()

    only = answer["folders"][0]  # type: ignore[index]
    assert only["still_coming"] == 1_600, (
        "600 bytes left of the one moving and the whole of the one this "
        "vocabulary has no word for"
    )
    assert answer["users"][0]["still_coming"] == 1_600  # type: ignore[index]


def test_a_cover_is_planned_only_for_the_albums_the_run_read(tmp_path: Path) -> None:
    """The run's own albums, not the shelf.

    Walking the whole library after every run would let a scan of two albums
    arm a cover plan on a third, and then report *1 coverless album carries a
    picture in the files* about a folder it had never opened. That plan is what
    would put `Write 1 embedded cover` on the bar with nothing ticked.
    """
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    seen: list[int] = []

    def _staged(paths: tuple[Path, ...]) -> object:
        seen.append(len(paths))
        return None

    api._pipeline.artwork = SimpleNamespace(extract_local_front=_staged)
    shelf = [
        SimpleNamespace(
            unit_id=identifier,
            organized=False,
            outcome=SimpleNamespace(plan=None),
            # The run skips an album holding a cover chosen by the user, so a
            # stand-in for an album has to be able to answer that.
            cover_plan=None,
            cover_applied=False,
            cover_chosen_by_user=False,
            unit=SimpleNamespace(
                unit_signature=f"sig-{identifier}",
                audio_files=(SimpleNamespace(path=tmp_path / f"{identifier}.flac"),),
            ),
        )
        for identifier in (1, 2)
    ]
    api._album_snapshot = lambda: tuple(shelf)  # type: ignore[method-assign]

    planned = api._plan_missing_covers([shelf[1].unit])

    assert planned == 0, "nothing was stageable, which is not what this test is about"
    assert seen == [1], "only the album this run read was even looked at"


def test_a_run_says_nothing_about_a_failure_that_happened_before_it(tmp_path: Path) -> None:
    """The record of a run's sources is emptied where the run begins.

    Emptying it on the way out of `_sources_down` is the same thing only while
    runs are the only callers, and they are not: identifying one album from its
    dialog searches the same sources and reports nothing. A failure left behind
    by such a gesture would be raised as a notice by the next run, about a scan
    that had never met it.
    """
    library = _library(tmp_path)
    source = FakeSource(summaries=(), details={})
    api = _api(tmp_path, source)
    workflow = api._pipeline.workflow
    # A failure with nobody to report it, exactly as a single-album gesture
    # leaves one behind.
    workflow.sources_unavailable = {"musicbrainz": "unreachable"}

    _scan_and_wait(api, library)

    assert (
        workflow.sources_unavailable == {}
    ), "the run that started here emptied the record it is going to speak from"


def test_a_drop_counts_its_own_albums_and_not_what_the_restore_read(tmp_path: Path) -> None:
    """Which albums a drop brought is a question for the database.

    Diffing `self._albums` around the read does not answer it: the restore
    fills that same map from another thread, so a drop of one folder made while
    the restore is running would report every album the restore happened to
    read back in the meantime, and the sentence for an album already on the
    shelf would never appear.
    """
    library = _library(tmp_path)
    source = FakeSource(summaries=(), details={})
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    # The album about to be dropped is already in the library, and the map a
    # diff would watch is emptied — exactly what a restore still walking the
    # shelf looks like from here.
    with api._albums_lock:
        api._albums.clear()

    assert api.adopt_folders([str(library / "marina do acordeao - forro")])["started"] == 1
    _finish_reads(api)

    announced = [event for event in api.events() if event["type"] == "adopted"]
    payload = announced[-1]["payload"]
    assert payload["albums"] == 0, "nothing arrived: the database already had this album"
    assert payload["known"] == 1, "and it is counted as one already on the shelf"


def test_an_organized_album_replanned_can_still_be_read_and_approved(tmp_path: Path) -> None:
    """An organized album planned again can still be read and approved.

    `applied` does not answer *has the plan on screen been written*: for an
    album applied earlier and re-planned since, a fresh plan is waiting. Read
    as that answer, `applied` would hide the names editor, disable Approve, and
    hide Approve outright for being organized.

    `written` is that question asked of the album directly. It is checked here
    rather than in the window because the window's three conditions all read
    this one field.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.approve(unit_id)["ok"]
    applied = api.album(unit_id)["album"]
    assert applied["applied"] is True
    assert applied["written"] is True, "the plan on screen is the one that ran"

    again = api.plan_anyway(unit_id)
    assert again["ok"], again.get("error")

    replanned = again["album"]
    assert replanned["applied"] is True, "the album is still one this app has applied"
    assert (
        replanned["written"] is False
    ), "a fresh plan has not been written, whatever was written before it"
    # And the gesture that reads it stays available.
    assert api.plan_anyway(unit_id)["ok"], "planning again must not work only once"
    # The window hides the names editor on two facts, and both must be right:
    # the plan has not been written, and the release has tracks to draw.
    # Asserted here because the screen reads exactly these.
    assert replanned["release_tracks"], "there are names to draw"


def test_an_organized_albums_names_open_only_on_a_request_for_a_fresh_plan(
    tmp_path: Path,
) -> None:
    """An organized album's names are editable only after `Plan this album again`.

    An album restored from a previous session draws its names. Typing into
    them would be correcting an answer nobody has asked a catalogue about since
    the last window was open, so of the planning gestures only `Plan this album
    again` opens them.

    The window reads `planned_again` beside `organized`; both facts are asserted
    here because the screen has no other source for either.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    # An album still in review has never been organized, and this never touches it.
    assert api.album(unit_id)["album"]["planned_again"] is False

    assert api.approve(unit_id)["ok"]
    assert (
        api.album(unit_id)["album"]["planned_again"] is False
    ), "applying is not asking for a fresh plan"

    assert api.plan_anyway(unit_id)["ok"]
    assert (
        api.album(unit_id)["album"]["planned_again"] is True
    ), "the one gesture that asks for a fresh plan opens the names"


def test_a_file_the_release_does_not_list_becomes_a_track_where_it_is_put(
    tmp_path: Path,
) -> None:
    """A file the release does not list can be added as a track.

    Two tracks on the release and three files in the folder. The offered
    position is the one after the release's last, which renumbers nothing, and
    the album stops being partial the moment it is taken.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    album = api.album(unit_id)["album"]
    assert album["partial_applicable"], "one file over, so it is partial"
    assert album["next_position"] == 3, "the offered number is past the release's last"
    assert album["extra_positions"] == {}
    assert album["user_numbering"] is False

    spare = next(
        row["signature"]
        for row in album["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )
    answer = api.adopt_extra_track(unit_id, spare)

    assert answer["ok"], answer.get("error")
    grown = answer["album"]
    assert grown["tracks"] == 3 or grown["applicable"], "the album now closes"
    assert list(grown["extra_positions"].values()) == [3]
    assert grown["user_numbering"] is False, "appending renumbers nothing the catalogue did"

    # And it is undone without anything on disk having been touched.
    back = api.drop_extra_track(unit_id, 3)
    assert back["ok"], back.get("error")
    assert back["album"]["extra_positions"] == {}
    assert back["album"]["partial_applicable"], "left alone again"


def test_a_position_the_release_already_uses_says_the_numbering_is_the_users(
    tmp_path: Path,
) -> None:
    """The unmatched track is not always the last, so the position is a field.

    Inserting in the middle moves tracks the catalogue numbered, and the screen
    has to stop calling that numbering the catalogue's.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    album = api.album(unit_id)["album"]
    spare = next(
        row["signature"]
        for row in album["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )

    answer = api.adopt_extra_track(unit_id, spare, position=1)

    assert answer["ok"], answer.get("error")
    assert answer["album"]["user_numbering"] is True

    # A gap is refused: a position far past the album's last file is a number
    # nobody can explain.
    assert api.drop_extra_track(unit_id, 1)["ok"]
    refused = api.adopt_extra_track(unit_id, spare, position=9)
    assert refused["ok"] is False
    assert "3" in str(refused["error"])


def test_moving_an_added_track_leaves_it_in_one_place(tmp_path: Path) -> None:
    """An added track can be given another position, and stays one track.

    The correction row is unique per position and not per file, so recording the
    move without clearing the old row would leave the album carrying the track
    twice — once where it was and once where it was put. Nothing on screen
    would say so: both rows are valid, and the tracklist would simply grow a
    duplicate.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spare = next(
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )

    assert api.adopt_extra_track(unit_id, spare, position=3)["ok"]
    moved = api.adopt_extra_track(unit_id, spare, position=1)

    assert moved["ok"], moved.get("error")
    assert list(moved["album"]["extra_positions"].values()) == [1], "one file, one place"
    assert moved["album"]["tracks"] == 3, "three files, three tracks — not four"
    assert moved["album"]["user_numbering"] is True, "position 1 moves what the catalogue numbered"

    # And pressing the same number again is not a move at all.
    again = api.adopt_extra_track(unit_id, spare, position=1)
    assert again["ok"]
    assert list(again["album"]["extra_positions"].values()) == [1]


def test_a_second_unlisted_file_can_take_the_place_after_the_first(tmp_path: Path) -> None:
    """A second unlisted file can take the place after the first.

    If the ceiling were the catalogue's next free position, computed with the
    added tracks stripped out, the second file would be offered no place past
    the first one's, and taking it would evict the first in a circle. The place
    after the last track of the FINISHED list is a place this album can hold.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    shutil.copy(FIXTURES / "tone.aiff", folder / "ddd.aiff")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    album = api.album(unit_id)["album"]
    spares = [
        row["signature"]
        for row in album["evidence"]
        if row["status"] != "paired" and row.get("signature")
    ]
    assert len(spares) == 2, "two files the release does not list"

    first = api.adopt_extra_track(unit_id, spares[0])
    assert first["ok"], first.get("error")
    assert list(first["album"]["extra_positions"].values()) == [3]
    assert first["album"]["next_position"] == 4, "the next offer is past the added track too"

    second = api.adopt_extra_track(unit_id, spares[1])
    assert second["ok"], second.get("error")
    positions = second["album"]["extra_positions"]
    assert sorted(positions.values()) == [3, 4], "both stand, one after the other"
    assert positions[spares[0]] == 3, "the first adoption was not evicted"


def test_adopting_at_a_place_another_added_track_holds_moves_it_down_not_out(
    tmp_path: Path,
) -> None:
    """Adopting at a place another added track holds moves that one down.

    The correction row is unique per position, so a second adoption at a held
    position would silently replace the first. Inserting at a place an added
    track holds does what inserting at a catalogue track's place does:
    everything from there down moves one.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    shutil.copy(FIXTURES / "tone.aiff", folder / "ddd.aiff")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spares = [
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    ]

    assert api.adopt_extra_track(unit_id, spares[0], position=3)["ok"]
    answer = api.adopt_extra_track(unit_id, spares[1], position=3)

    assert answer["ok"], answer.get("error")
    positions = answer["album"]["extra_positions"]
    assert positions[spares[1]] == 3, "the newcomer holds the place chosen for it"
    assert positions[spares[0]] == 4, "and the one that held it moved down, not out"


def test_dropping_an_added_track_closes_the_gap_it_leaves(tmp_path: Path) -> None:
    """Two added tracks at 3 and 4; taking back 3 must not leave 4 hanging.

    A track at 4 of an album whose list now stops at 3 is the gap this API
    refuses to create on the way in, so it cannot leave one behind on the way
    out.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    shutil.copy(FIXTURES / "tone.aiff", folder / "ddd.aiff")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spares = [
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    ]
    assert api.adopt_extra_track(unit_id, spares[0])["ok"]
    grown = api.adopt_extra_track(unit_id, spares[1])
    assert grown["ok"] and sorted(grown["album"]["extra_positions"].values()) == [3, 4]

    back = api.drop_extra_track(unit_id, 3)

    assert back["ok"], back.get("error")
    positions = back["album"]["extra_positions"]
    assert list(positions.values()) == [3], "the one that stayed slid into the gap"
    assert positions[spares[1]] == 3


def test_an_added_track_never_reaches_the_cache_of_the_source_s_release(
    tmp_path: Path,
) -> None:
    """An added track is kept out of the cache of the source's release.

    `metadata_releases` is the cache of a *source's* release, keyed by source and
    release id. Writing the grown release there would put a track the source
    never published inside this app's record of that release — and `identify`
    reads that cache, so the album would come back with the added track looking
    like the catalogue's, be grown past a second time, and end up carrying it
    twice: once as a track holding the audio and once as a track no file could
    hold.

    Both halves are asserted: the cache, which is the cause, and the tracklist,
    which is what the screen shows.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spare = next(
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )

    assert api.adopt_extra_track(unit_id, spare)["ok"]

    with api._store._database.connect() as connection:
        cached = connection.execute(
            "SELECT track_count FROM metadata_releases WHERE source_release_id = 'r1'"
        ).fetchone()
    assert cached is not None
    assert cached[0] == 2, "the catalogue published two tracks, and its cache must go on saying two"

    # And the album itself holds the added track exactly once, however often it
    # is planned again — `plan_anyway` plans around the release on record, which
    # is the path that reads that cache back.
    assert api.approve(unit_id)["ok"]
    for _ in range(3):
        answer = api.plan_anyway(unit_id)
        assert answer["ok"], answer.get("error")
    album = api.album(unit_id)["album"]
    assert len(album["release_tracks"]) == 3, "two published and one added, not four"
    assert list(album["extra_positions"].values()) == [3]
    assert not [
        row for row in album["evidence"] if row["status"] == "missing_file"
    ], "no track without a file"


def test_adopting_and_moving_write_nothing_until_approved(tmp_path: Path) -> None:
    """Adopting a file and moving it write nothing; only `approve` does.

    Every file on disk keeps the name it had, through an adoption and through a
    move, and the album goes on holding a plan that is waiting to be approved.
    The one thing that writes is `approve`, and only after it do the names
    change.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.copy(FIXTURES / "tone.wav", folder / "ccc.wav")
    before = sorted(path.name for path in folder.iterdir())
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    spare = next(
        row["signature"]
        for row in api.album(unit_id)["album"]["evidence"]
        if row["status"] != "paired" and row.get("signature")
    )

    adopted = api.adopt_extra_track(unit_id, spare)
    assert adopted["ok"]
    assert sorted(path.name for path in folder.iterdir()) == before, "adopting wrote nothing"
    assert adopted["album"]["operations"], "and left a plan waiting"
    assert adopted["album"]["applicable"], "which Approve would run"
    assert adopted["album"]["written"] is False, "so the footer offers it"

    moved = api.adopt_extra_track(unit_id, spare, position=1)
    assert moved["ok"]
    assert sorted(path.name for path in folder.iterdir()) == before, "moving wrote nothing either"
    assert moved["album"]["applicable"] and moved["album"]["written"] is False

    # And only now does anything reach the disk — the folder itself is renamed,
    # so the path these files were read from stops existing, which is the
    # plainest possible statement that nothing before this had touched it.
    assert api.approve(unit_id)["ok"]
    assert not folder.exists(), "approving is what writes"
    landed = next(path for path in library.iterdir() if path.is_dir())
    assert sorted(one.name for one in landed.iterdir()) != before


def test_a_chosen_cover_is_read_before_it_is_written(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A cover can be chosen by hand, and it is shown before it is written.

    The Cover Art Archive is addressed through MusicBrainz, so an album no
    MusicBrainz release matched can never be offered a sleeve: the automatic
    rule has nothing to choose between.

    Two steps, and this asserts the first writes nothing: choosing plans, and a
    second gesture writes. A picture replacing a picture is exactly the write
    that must be read first.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(700, 700))
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: str(sleeve)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    before = {path.name: path.stat().st_mtime_ns for path in folder.iterdir()}

    chosen = api.choose_cover(unit_id)

    assert chosen["ok"], chosen.get("error")
    waiting = chosen["album"]["cover_choice"]
    assert waiting is not None, "a cover is waiting"
    assert waiting["width"] == 700 and waiting["height"] == 700
    assert waiting["files"] == 2, "one for each track"
    assert {
        path.name: path.stat().st_mtime_ns for path in folder.iterdir()
    } == before, "choosing a cover writes nothing at all"

    written = api.write_chosen_cover(unit_id)

    assert written["ok"], written.get("error")
    assert (folder / "cover.jpg").exists(), "and writing it puts the picture beside the files"
    assert written["album"]["cover_choice"] is None, "nothing is waiting any more"


def test_choosing_a_file_that_is_not_an_image_changes_nothing(tmp_path: Path) -> None:
    """The bytes decide what it is, not the extension it wears."""
    library = _library(tmp_path)
    pretender = tmp_path / "not-a-picture.jpg"
    pretender.write_text("this is a text file wearing a jpg name")
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: str(pretender)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    answer = api.choose_cover(unit_id)

    assert answer["ok"] is False
    assert "not an image" in str(answer["error"])
    assert api.album(unit_id)["album"]["cover_choice"] is None


def test_choosing_nothing_leaves_the_album_exactly_as_it_was(tmp_path: Path) -> None:
    """Cancelling the panel is an answer, and it is not a failure."""
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: None
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    answer = api.choose_cover(unit_id)

    assert answer["ok"] and answer["cancelled"]
    assert answer["album"]["cover_choice"] is None


def test_a_cover_chosen_for_an_album_already_applied_is_actually_written(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A cover chosen for an album already applied is written, not discarded.

    `_apply_pending_covers` discards a plan on `state.applied` when the cover
    plan was made *before* the album's own plan ran, because it names paths that
    plan then moved. A cover chosen afterwards is planned from where the files
    now are, and must not be thrown away while the window says it was written.

    Both halves are asserted, because a silent discard and a screen claiming a
    write are two failures and the second is the worse one.
    """
    library = _library(tmp_path)
    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(700, 700))
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: str(sleeve)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.approve(unit_id)["ok"], "the album is one this app has already applied"
    assert api.plan_anyway(unit_id)["ok"], "and it was planned again before choosing"
    assert api.choose_cover(unit_id)["ok"]

    written = api.write_chosen_cover(unit_id)

    assert written["ok"], written.get("error")
    assert written["covers"] == 1
    landed = next(path for path in library.iterdir() if path.is_dir())
    assert (landed / "cover.jpg").exists(), "the picture reached the folder it was planned for"
    assert written["album"]["cover_choice"] is None


def test_a_cover_that_could_not_be_written_is_never_reported_as_written(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A cover that could not be written is never reported as written.

    A count is what the executor did. A gesture that answers with the intention
    instead is the screen naming something it did not measure.
    """
    library = _library(tmp_path)
    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(700, 700))
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: str(sleeve)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.choose_cover(unit_id)["ok"]

    # The folder goes out from under the plan, which is the one thing that
    # genuinely makes it stale.
    folder = next(path for path in library.iterdir() if path.is_dir())
    shutil.rmtree(folder)

    written = api.write_chosen_cover(unit_id)

    assert written["ok"] is False, "nothing was written, so nothing is claimed"
    assert "nothing was changed" in str(written["error"])


def test_a_finished_album_takes_a_cover_without_being_planned_again(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A finished album takes a cover without being planned again.

    A cover names nothing and asks no catalogue, so a re-plan in front of it
    buys nothing the picture needs — and it would leave a proposal standing,
    which draws a finished album back on the shelf as `Auto`.

    Four facts: the panel opens at once, the picture is shown before it is
    written, the write reaches the files, and the album is still organized
    afterwards.
    """
    library = _library(tmp_path)
    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(700, 700))
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    asked: list[int] = []

    def picker() -> str:
        asked.append(1)
        return str(sleeve)

    api._image_picker = picker
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    # A second scan is what makes it an album an earlier run organized, which is
    # the state most albums on a shelf are in and the one this rule is about.
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    album = api.album(unit_id)["album"]
    assert album["organized"] is True and album["asleep"] is True

    chosen = api.choose_cover(unit_id)

    assert chosen["ok"], chosen.get("error")
    assert asked == [1], "the picture panel opens without a re-plan in front of it"
    choice = chosen["album"]["cover_choice"]
    assert choice is not None
    assert choice["preview"], "the picture is shown before it is written, not only its size"
    assert chosen["album"]["can_cancel"] is False, "no proposal was left standing"

    written = api.write_chosen_cover(unit_id)

    assert written["ok"], written.get("error")
    assert written["covers"] == 1
    after = api.album(unit_id)["album"]
    assert after["organized"] is True, "a picture made a finished album unfinished"
    assert after["cover_choice"] is None


def test_the_waiting_cover_counts_files_and_finder_icons_apart(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """An icon is an `EMBED_IMAGE` too, and it is not counted as a file.

    Counted together, ten tracks with their icons would read as twenty files,
    and the Finder — the one place a cover is seen without opening anything —
    would never be named. The icons are appended here by hand because the bench
    cannot draw one; the planner's own icon operation carries exactly this
    `after_state`.
    """
    library = _library(tmp_path)
    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(700, 700))
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: str(sleeve)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.choose_cover(unit_id)["ok"]
    state = api._albums[unit_id]
    tracks = [file.path for file in state.unit.audio_files]
    icons = tuple(
        ChangeOperation(
            sequence=100 + index,
            kind=OperationKind.EMBED_IMAGE,
            target_path=path,
            after_state={"icon": True},
            before_state={},
        )
        for index, path in enumerate(tracks)
    )
    state.cover_plan = replace(state.cover_plan, operations=state.cover_plan.operations + icons)

    choice = api.album(unit_id)["album"]["cover_choice"]

    assert choice["files"] == len(tracks), "an icon was counted as a file"
    assert choice["icons"] == len(tracks)


def test_closing_a_finished_album_planned_again_puts_it_back(tmp_path: Path) -> None:
    """Closing a re-planned finished album keeps it as it was.

    `Plan this album again` is offered only on an album already finished. Asking
    on close whether to leave the proposal waiting would draw the album as
    `Auto`. The server says closing takes it back, and says it instead of
    asking: the window reads `leaving_takes_back` and never the gesture's name.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.plan_anyway(unit_id)["ok"]
    again = api.album(unit_id)["album"]

    assert again["leaving_takes_back"] is True
    assert again["ask_before_leaving"] is False, "the question is gone for this gesture"

    back = api.cancel(unit_id)

    assert back["ok"], back.get("error")
    assert back["album"]["organized"] is True
    assert back["album"]["leaving_takes_back"] is False, "nothing is left to take back"


def test_only_a_chosen_cover_puts_the_write_panel_on_the_album(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """`Write this cover` is offered only for a cover chosen with its own button.

    Two gestures write `state.cover_plan`: `Choose a cover…` and the run that
    works a folder cover out of the album's own files. The notice reads *A
    cover you chose is waiting* and carries a button, so it has to be built
    from the gesture rather than from the field — otherwise the automatic plan
    wears that sentence, over a plan that writes the folder and never a track.

    Both halves are asserted, because the panel disappearing is no good if the
    automatic cover stops being planned at all — the batch still writes it.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    embedded = Picture()
    embedded.type, embedded.mime, embedded.depth = 3, "image/jpeg", 24
    embedded.width = embedded.height = 500
    embedded.data = jpeg(500, 500)
    for track in sorted(folder.glob("*.flac")):
        audio = FLAC(track)
        audio.add_picture(embedded)
        audio.save()
    # No catalogue answers, so the album carries no plan of its own and the
    # folder cover is exactly what the run has to work out from the files.
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api._plan_missing_covers(None) == 1, "the app worked a folder cover out of the files"
    album = api.album(unit_id)["album"]

    assert album["cover_pending"] is True, "and it is waiting to be written by the batch"
    assert album["cover_choice"] is None, (
        "an album nothing was chosen for is offering to write a picture nobody "
        "pointed at, under a sentence saying it was chosen"
    )

    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(700, 700))
    api._image_picker = lambda: str(sleeve)

    assert api.choose_cover(unit_id)["ok"]

    assert api.album(unit_id)["album"]["cover_choice"] is not None, "now one is chosen"

    assert api.discard_chosen_cover(unit_id)["ok"]

    assert api.album(unit_id)["album"]["cover_choice"] is None, "and putting it down takes it away"


def test_the_run_never_replaces_a_chosen_cover_with_one_it_worked_out(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The other half of the same field being written by two gestures.

    A chosen picture is waiting, unwritten, and the run that plans covers from
    the files would overwrite the plan that was read — leaving the notice on
    screen describing a picture that is no longer the one it would write.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    embedded = Picture()
    embedded.type, embedded.mime, embedded.depth = 3, "image/jpeg", 24
    embedded.width = embedded.height = 500
    embedded.data = jpeg(500, 500)
    for track in sorted(folder.glob("*.flac")):
        audio = FLAC(track)
        audio.add_picture(embedded)
        audio.save()
    sleeve = tmp_path / "sleeve.jpg"
    sleeve.write_bytes(jpeg(900, 900))
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    api._image_picker = lambda: str(sleeve)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    assert api.choose_cover(unit_id)["ok"]
    chosen = api.album(unit_id)["album"]["cover_choice"]
    assert chosen and chosen["width"] == 900

    api._plan_missing_covers(None)

    still = api.album(unit_id)["album"]["cover_choice"]
    assert still == chosen, "the run replaced the chosen picture with the one in the files"


def test_the_card_shows_the_picture_in_the_folder_before_any_other(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The card shows the picture in the folder before any other.

    Two cases go wrong otherwise: files carrying no picture beside a
    `cover.jpg` nobody reads, which draws a blank card; and tracks carrying an
    embedded picture that is not the sleeve — a download site's logo, say —
    drawn over the real sleeve sitting in the folder beside them.

    The folder's picture is the album's cover in the only sense anything outside
    this application uses: it is what the Finder draws and what a player reads,
    and it is one picture for the record rather than one per track.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    # The tracks carry one picture and the folder holds another.
    embedded = Picture()
    embedded.type, embedded.mime, embedded.depth = 3, "image/jpeg", 24
    embedded.width = embedded.height = 500
    embedded.data = jpeg(500, 500)
    for track in sorted(folder.glob("*.flac")):
        audio = FLAC(track)
        audio.add_picture(embedded)
        audio.save()
    sleeve = jpeg(1900, 1900)
    (folder / "cover.jpg").write_bytes(sleeve)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    shown = api.cover(unit_id)

    assert shown, "an album with a picture in its folder is never a blank card"
    # Served as a URL where an image server is wired and as a `data:` URI where
    # none is; the question here is *which picture*, so both are read for bytes.
    said = str(shown)
    if said.startswith("data:"):
        drawn = base64.b64decode(said.split(",", 1)[1])
    else:
        drawn = (tmp_path / "covers" / Path(said.split("?")[0]).name).read_bytes()
    assert (
        hashlib.sha256(drawn).hexdigest() == hashlib.sha256(sleeve).hexdigest()
    ), "the folder's picture is the one shown, not the one inside the tracks"


def test_the_card_never_draws_the_back_of_the_sleeve(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The card draws the front, never the back of the sleeve or the booklet.

    Reading the folder is right; reading it *alphabetically* is not: `back.jpg`
    sorts before `cover.jpg`, and folders hold both. A front cover goes by
    several names, so what reads reliably is the exclusions — and the largest
    of what is left is taken, which is why a big back cover still loses to a
    small front.

    Nothing is written from this; the cards draw it. That is why this asserts
    what `cover()` answers rather than what is on disk.
    """
    library = _library(tmp_path)
    folder = next(path for path in library.iterdir() if path.is_dir())
    front = jpeg(900, 900)
    # Deliberately larger, and deliberately first in every alphabet.
    (folder / "back.jpg").write_bytes(jpeg(1600, 1600))
    (folder / "booklet.jpg").write_bytes(jpeg(1500, 1500))
    (folder / "cover.jpg").write_bytes(front)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    shown = api.cover(unit_id)

    assert shown, "the album has a front, so the card is not blank"
    said = str(shown)
    if said.startswith("data:"):
        drawn = base64.b64decode(said.split(",", 1)[1])
    else:
        drawn = (tmp_path / "covers" / Path(said.split("?")[0]).name).read_bytes()
    assert (
        hashlib.sha256(drawn).hexdigest() == hashlib.sha256(front).hexdigest()
    ), "a folder holding a back cover and a booklet still shows the front"


def test_the_dashboard_says_how_long_it_took(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim about speed the log cannot answer is a memory.

    `api.album` reports its milliseconds, and `state` — the call every gesture
    ends with — has to report its own, or the cost of drawing the shelf can
    only be guessed at.
    """
    library = _library(tmp_path)
    release = _release("r1", (400, 900))
    api = _api(tmp_path, FakeSource(summaries=(release,), details={"r1": release}))
    _scan_and_wait(api, library)
    with caplog.at_level(logging.INFO, logger="test.api"):
        api.state()

    timed = [
        record
        for record in caplog.records
        if getattr(record, "operation", "") == "api.state.timing"
    ]
    assert timed, "the dashboard reports nothing about its own cost"
    assert isinstance(timed[0].milliseconds, int)
    assert timed[0].albums == 1, "and it says how many albums that was for"


def test_rating_an_album_reaches_the_card_behind_the_dialog(tmp_path: Path) -> None:
    """The album in hand and the shelf's row are two copies of one fact.

    The shelf is drawn from the rows read at startup, so a rating that only
    reached the open album would leave the card showing nothing until the next
    restart.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.rate_album(unit_id, 4) == {"ok": True, "rating": 4}

    assert api.album(unit_id)["album"]["rating"] == 4
    # And the row the *next* opening of the window draws its card from,
    # before anything has been read back off the disk.
    api._draw_the_shelf()
    assert api._shelf[unit_id]["rating"] == 4

    # Pressing the star already chosen is the way back out.
    assert api.rate_album(unit_id, None)["ok"]
    assert api.album(unit_id)["album"]["rating"] is None


def test_only_the_five_values_are_a_rating(tmp_path: Path) -> None:
    """Nothing else is offered by the control, so nothing else is accepted here."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    for impossible in (0, 6, -1):
        assert not api.rate_album(unit_id, impossible)["ok"]
    assert not api.rate_album(999_999, 3)["ok"], "an album that is not there"
    assert api.album(unit_id)["album"]["rating"] is None


def test_a_witness_backing_the_file_marks_the_line_the_letters_could_not() -> None:
    """A witness that agrees with every title lets a wider difference be offered.

    The file's `Tavo Lontra` against the catalogue's `Tavina Lontra` is three
    edits apart, so the typo rule refuses it — and the album's
    witness testimony, fourteen titles compared and fourteen agreeing, is the
    evidence that lets the wider question be asked at all.
    """
    backing = _witness_backing_user_titles(
        {
            "sources": [{"source": "discogs", "winner": True}],
            "itunes": {"witness": "itunes", "titles_compared": 14, "titles_agreeing": 14},
        }
    )

    assert backing == ("itunes", 14)
    assert _user_spelling_offer("Tavo Lontra", "Tavina Lontra", backing, standing=None) == {
        "yours": "Tavo Lontra",
        "yours_reason": "witness",
        "yours_witness": "itunes",
        "kept": "",
    }


def test_a_witness_that_agreed_with_all_but_one_title_backs_no_line_at_all() -> None:
    """Thirteen of fourteen names no line, and the mark is about a line.

    The count is all this application keeps of a witness, so only *every* is a
    statement about the row being marked — with one disagreement anywhere on the
    album, that row could be the one the witness actually disagreed about, and
    the sentence would be false on the only line it was written for.
    """
    partial = _witness_backing_user_titles(
        {"itunes": {"witness": "itunes", "titles_compared": 14, "titles_agreeing": 13}}
    )

    assert partial is None
    assert _user_spelling_offer("Tavo Lontra", "Tavina Lontra", partial, standing=None) == {
        "yours": "",
        "yours_reason": "",
        "yours_witness": "",
        "kept": "",
    }


def test_a_witness_this_application_has_not_met_yet_can_still_back_the_file() -> None:
    """Selected by what the entry *is*, never by naming iTunes.

    A lookup by key would leave the next witness silently unable to back a
    file's spelling, and that failure draws nothing at all.
    """
    backing = _witness_backing_user_titles(
        {"somewhere_new": {"witness": "somewhere_new", "titles_compared": 9, "titles_agreeing": 9}}
    )

    assert backing == ("somewhere_new", 9)


def test_the_narrow_reason_still_answers_where_no_witness_was_asked() -> None:
    """An album nobody witnessed keeps the narrow typo rule."""
    assert _user_spelling_offer("Brumal Tavo", "Brumal Tago", None, standing=None) == {
        "yours": "Brumal Tavo",
        "yours_reason": "typo",
        "yours_witness": "",
        "kept": "",
    }


def test_a_line_already_answered_is_not_argued_with() -> None:
    """A standing correction settles this row: the offer stops being made.

    What replaces it is the way back out — the aside changes state rather than
    disappearing, so the one gesture that overrides the catalogue is not the
    one gesture with no undo.
    """
    backing = ("itunes", 14)

    assert _user_spelling_offer(
        "Tavo Lontra", "Tavina Lontra", backing, standing="Tavo Lontra"
    ) == {
        "yours": "",
        "yours_reason": "",
        "yours_witness": "",
        "kept": "Tavo Lontra",
    }


def test_a_typed_title_is_not_offered_a_one_click_way_to_lose_it() -> None:
    """The way back is drawn only where it restores the offer it came from.

    `Keep yours` writes the spelling this line was already showing, so
    withdrawing it brings that same offer back and pressing both in turn changes
    nothing. A typed title is not on any file and not in any catalogue: one
    click would take words nothing on this screen could give back, and a button
    that destroys must not sit where the reversible one sits. Those go through
    the album's own gesture, which says how many first.
    """
    assert _user_spelling_offer("Brumal Tavo", "Brumal Tago", None, standing="Typed By Hand") == {
        "yours": "",
        "yours_reason": "",
        "yours_witness": "",
        "kept": "",
    }


def test_an_empty_reason_and_an_empty_spelling_always_travel_together() -> None:
    """One thing for the window to test, on every path out of this function."""
    backing = ("itunes", 14)
    cases = [
        ("Zurél", "zurel"),
        ("Pelma Runo Tral", "Drusa Runo Vel"),
        ("Tavo Lontra", "Tavo Lontra"),
        (None, "Tavina Lontra"),
    ]

    for mine, theirs in cases:
        offer = _user_spelling_offer(mine, theirs, backing, standing=None)
        assert bool(offer["yours"]) == bool(offer["yours_reason"]), offer


def test_one_press_says_the_same_word_about_every_track(tmp_path: Path) -> None:
    """One press says the same word about every track of an album.

    The rule that only a file can have been lossy is kept — this writes track
    words and never an album word — so the album still ends up being the
    majority of its tracks, exactly as it would have after the same presses one
    at a time.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    folder = str(next(library.iterdir()))

    answer = api.say_about_every_track(folder, "transcoded")

    assert answer["ok"]
    assert answer["tracks"] >= 1
    tracks = api.quality_tracks(folder)["tracks"]
    assert tracks, "the album has tracks to have spoken about"
    assert all(track["override"] == "transcoded" for track in tracks)
    assert (
        api.quality_tracks(folder)["album_override"] is None
    ), "no album-wide word is written, because only a file can have been lossy"


def test_pressing_it_again_withdraws_the_word_from_every_track(tmp_path: Path) -> None:
    """A word is withdrawn, never stacked, and here it is withdrawn in bulk."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    folder = str(next(library.iterdir()))
    api.say_about_every_track(folder, "honest")

    api.say_about_every_track(folder, "clear")

    tracks = api.quality_tracks(folder)["tracks"]
    assert all(not track["override"] for track in tracks)


def test_a_word_nobody_has_a_meaning_for_is_refused(tmp_path: Path) -> None:
    """The same three words the single-track control offers, and no fourth."""
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    _scan_and_wait(api, library)
    folder = str(next(library.iterdir()))

    assert not api.say_about_every_track(folder, "probably")["ok"]
    assert not api.say_about_every_track(str(tmp_path / "gone"), "honest")["ok"]


def test_reverting_an_album_filed_away_asks_first_and_then_undoes_it_there(
    tmp_path: Path,
) -> None:
    """The safety net follows the album out of the folder it was organized in.

    Filing the organized folder into a music library is an ordinary last step,
    and the trail names absolute paths — so a reversal that opened them as
    recorded would raise `FileNotFoundError` on the first file.

    Two things are asserted, and the first matters as much as the second:
    nothing is written until the question is answered yes, because undoing
    inside a music library is a different act from undoing inside a downloads
    folder.
    """
    music = tmp_path / "music"
    (music / "another album").mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", music / "another album" / "only.flac")
    library = _library(tmp_path)
    scanned_name = "marina do acordeao - forro"
    before = _snapshot(library / scanned_name)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    # Scanned first so that the music folder is a place this app has been
    # shown; the search never walks the disk, only where it has been taken.
    _scan_and_wait(api, music)
    _scan_and_wait(api, library)
    run = api.apply_automatic()
    organized = library / ORGANIZED
    assert organized.is_dir()
    filed = music / ORGANIZED
    organized.rename(filed)
    once_filed = _snapshot(music)

    asked = api.revert_run(run["run_id"])

    assert asked["confirm"] == "filed_away", "it asks before writing in the library"
    assert asked["folder"] == ORGANIZED and asked["parent"] == str(music)
    assert _snapshot(music) == once_filed, "the question wrote nothing"

    undone = api.revert_run(run["run_id"], confirmed=True)

    assert undone["ok"]
    # The name came back and the filing did not: what this application did is
    # undone, and what was done by hand is left alone.
    assert _snapshot(music / scanned_name) == before, "the album is back, byte for byte"
    assert not filed.exists()
    assert _snapshot(library) == [], "nothing was dragged back into the downloads folder"


def test_a_reverted_album_stops_saying_organized_without_a_restart(tmp_path: Path) -> None:
    """A reverted album stops saying `organized` without a restart.

    The shelf is drawn from rows read out of the database, and most albums on a
    shelf are only a row — so writing the new state to the store and to
    `_AlbumState` would leave the third place still saying `organized` until
    the app was reopened.

    Reopened first, deliberately. Reverting an album the window has already read
    off the disk does not go through the shelf row, and testing that path would
    pass either way.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    api.apply_automatic()
    plan_id = api.state()["history"][0]["plan_id"]
    reopened = _reopen(api)
    assert [album["organized"] for album in reopened.state()["albums"]] == [True]

    assert reopened.revert(plan_id)["ok"]

    assert [album["organized"] for album in reopened.state()["albums"]] == [
        False
    ], "the card must stop saying organized the moment the revert returns"


def test_every_setting_that_is_written_is_read_back_at_launch(tmp_path: Path) -> None:
    """Every setting that is written is read back at launch.

    A key can be in `_UI_SETTING_KEYS`, so the window writes it faithfully,
    while `_load_settings` — the only thing that reads any of them when the
    window opens — never mentions it. The setting then comes back at its
    default on every launch.

    Written as a walk over the keys rather than as two assertions, because the
    rule is not about these settings: it is about a key added to the list of
    what is saved and forgotten in the list of what is loaded.
    """
    api = _api(tmp_path, FakeSource())
    api.set_settings(
        {
            "library_sort": "rating_best",
            "column_widths": '{"queue": [30, 300, 200, 20]}',
            "ui_density": "roomy",
        }
    )

    # A second API over the same database is what the next launch is.
    reopened = _api(tmp_path, FakeSource())
    settings = reopened.settings()

    assert settings["library_sort"] == "rating_best", "the shelf's order did not survive a launch"
    assert (
        settings["column_widths"] == '{"queue": [30, 300, 200, 20]}'
    ), "the column widths did not survive a launch"
    assert settings["ui_density"] == "roomy"


def _shaped(folder: Path, signature: str, key: str) -> AlbumUnit:
    """One album of one file, with a chosen signature and a chosen audio key."""
    folder.mkdir(parents=True, exist_ok=True)
    return AlbumUnit(
        folder_path=folder,
        unit_signature=folder.name,
        audio_files=(
            AudioFileFacts(
                path=folder / "01.flac",
                content_signature=signature,
                file_size_bytes=1_000,
                modified_at=datetime(2001, 2, 3, 4, 5, tzinfo=UTC),
                audio_key=key,
            ),
        ),
    )


def test_two_recordings_of_one_shape_keep_their_own_verdicts(tmp_path: Path) -> None:
    """Two recordings of one declared shape keep their own verdicts.

    The collision points both ways: an honest album can be accused of being the
    transcode beside it, and — after a deep measurement acquits the honest
    album — the transcode can lose its `[Lossy]` too. Two albums, one answer
    between them.

    This is the path a launch takes: nothing is decoded, the store is replayed.
    Asking `quality_for`, which is keyed by the declared shape alone, cannot
    tell two recordings of one shape apart: whichever row came back first would
    decide for both folders.

    Written against `_facts_from_store` because that is what the Library dialog
    draws from.
    """
    api = _api(tmp_path, FakeSource(summaries=(), details={}))
    shared = "one-declared-shape"
    api._store.record_quality(
        {
            shared: StoredQuality(
                encoding="transcoded",
                effective_bitrate_kbps=None,
                cutoff_hertz=None,
                steepest_drop_db=4.0,
                findings=("transcoded",),
                reason="This file was an MP3 before it was a FLAC.",
                decay_db=5.0,
                ceiling_db=-71.0,
                audio_key="flac:the-transcode",
                frame_grid_z=50.0,
                frame_grid_agrees=True,
            )
        }
    )
    # Written after it, and the way `Analyze in depth` writes one: its own key,
    # its own numbers. Newest first is what makes this the row a lookup by
    # signature alone hands to both albums.
    api._store.record_quality(
        {
            shared: StoredQuality(
                encoding="lossless",
                effective_bitrate_kbps=None,
                cutoff_hertz=None,
                steepest_drop_db=8.0,
                findings=(),
                reason="Energy fades gradually and the top band is still alive.",
                decay_db=6.0,
                ceiling_db=-70.0,
                audio_key="flac:the-honest-one",
                frame_grid_z=2.0,
                frame_grid_agrees=False,
            )
        }
    )

    fake = _shaped(tmp_path / "shelf" / "transcoded", shared, "flac:the-transcode")
    real = _shaped(tmp_path / "shelf" / "honest", shared, "flac:the-honest-one")

    facts = api._facts_from_store([fake, real])

    assert facts["transcoded"] == frozenset(
        {shared}
    ), "the transcode lost its verdict to the honest album measured beside it"
    assert (
        facts["honest"] == frozenset()
    ), "the honest album wore the verdict measured from the transcode"


def test_a_moved_album_still_goes_to_the_bench(tmp_path: Path) -> None:
    """`Send to Quality` follows an album that moved, as the other gestures do.

    Reading the recorded path and asking the disk whether it is a folder would
    refuse an album organized and then moved one folder up, with every file
    still on the disk and every size still matching its row. The refusal would
    be an absence wearing the words of damage, naming a path nothing can be
    done about.
    """
    library = _library(tmp_path)
    moving = library / "box" / "an album"
    moving.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    api = _api(tmp_path, FakeSource())
    api.open_folder(str(library))
    _finish_reads(api)
    mine = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}["an album"]
    shutil.move(str(moving), str(library / "an album"))

    answer = api.send_to_bench(mine)

    assert answer["ok"], answer.get("error")
    followed = {album["unit_id"]: album for album in api.state()["albums"]}[mine]
    assert followed["folder_path"] == str(
        library / "an album"
    ), "the bench took it, and the row still names where it used to be"


def test_opening_the_folder_of_a_moved_album_opens_where_it_is_now(tmp_path: Path) -> None:
    """The same rule, in the gesture whose own docstring states it.

    *Open one album's own folder, wherever it is now*: reading the recorded
    path would contradict it.
    """
    library = _library(tmp_path)
    moving = library / "box" / "an album"
    moving.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    opened: list[str] = []
    api = _api(tmp_path, FakeSource())
    api._path_opener = opened.append
    api.open_folder(str(library))
    _finish_reads(api)
    mine = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}["an album"]
    shutil.move(str(moving), str(library / "an album"))

    answer = api.open_album(mine)

    assert answer["ok"], answer.get("error")
    assert opened == [str(library / "an album")], "it opened the folder it used to live in"


def test_a_second_copy_of_an_album_is_an_album_of_its_own(tmp_path: Path) -> None:
    """A second copy of an album is an album of its own.

    If being organized were remembered by the audio's signature, a second
    folder holding the same recording would arrive on the shelf already wearing
    `organized`, with an empty plan and therefore no button. A folder is
    organized or not whatever else holds the same audio.

    The organized album keeps its state, which is asserted here as well:
    `_upsert_unit` adopts the old row exactly when the folder it named has gone.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    first = api.state()["albums"][0]["unit_id"]
    assert api.approve(first)["ok"]
    organized = next(path for path in library.iterdir() if path.is_dir())

    # A second copy of the same recording, in a folder of its own: same audio,
    # different names, both on the disk at once.
    second_folder = library / "1974 - another copy"
    second_folder.mkdir()
    for track in sorted(organized.glob("*.flac")):
        shutil.copy(track, second_folder / f"copy {track.name}")
    assert api.open_folder(str(second_folder))["ok"]
    _finish_reads(api)

    shelf = {album["folder"]: album for album in api.state()["albums"]}
    second = shelf["1974 - another copy"]
    assert (
        second["organized"] is False
    ), "the second copy arrived organized, so it had nothing to plan and no button"
    assert shelf[organized.name]["organized"] is True, "and the first one stopped being organized"

    assert api.scan_selected([second["unit_id"]])["ok"]
    _finish(api._job, "scan")
    planned = api.album(second["unit_id"])["album"]

    assert planned["operations"], "the second copy still gets no plan of its own"
    refused = api.approve(second["unit_id"])
    assert refused["ok"] is False, "the name is taken by the first copy, so this must refuse"
    assert "already exists" in str(refused["error"]), refused["error"]
    assert sorted(path.name for path in second_folder.iterdir()) == [
        f"copy {track.name}" for track in sorted(organized.glob("*.flac"))
    ], "the refusal wrote to the files before refusing"


def test_a_deleted_copy_is_not_followed_into_the_folder_of_the_copy_that_was_kept(
    tmp_path: Path,
) -> None:
    """Two copies of one record organized under one name.

    A record downloaded twice, each copy organized under the same name, one kept
    in the music folder and one later deleted from the downloads. The search
    for the deleted copy must not find the kept one by its name: the sizes
    refuse a folder another row holds, and the name has to as well. Otherwise
    the database refuses the move on every visit to the Library, and the
    reversal of the deleted copy's plan is pointed at the kept copy, one
    confirmation away from renaming back the files of the album that was kept.
    """
    downloads = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, downloads)
    deleted = api.state()["albums"][0]["unit_id"]
    assert api.approve(deleted)["ok"]
    organized = next(path for path in downloads.iterdir() if path.is_dir())
    kept = tmp_path / "music" / organized.name
    shutil.copytree(organized, kept)
    assert api.open_folder(str(kept))["ok"]
    _finish_reads(api)
    assert {album["folder_path"] for album in api.state()["albums"]} >= {
        str(organized),
        str(kept),
    }, "the two copies are two rows"
    with api._store._database.connect() as connection:
        (plan_id,) = connection.execute(
            "SELECT id FROM change_plans WHERE album_unit_id = ? AND state = 'applied'",
            (deleted,),
        ).fetchone()
    before = _snapshot(kept.parent)
    shutil.rmtree(organized)

    answer = api.revert(plan_id)

    assert answer["ok"] is False
    assert "confirm" not in answer, f"the reversal offered to undo the kept copy: {answer}"
    assert _snapshot(kept.parent) == before, "the kept copy was written to"
    assert api._store.unit_by_id(deleted).folder_path == organized


def test_sending_an_album_to_quality_does_not_unsay_that_it_was_scanned(tmp_path: Path) -> None:
    """Sending an album to Quality does not unsay that it was scanned.

    `looked_at` answers *has a catalogue spoken about this album*, and it is
    carried on the state in memory while the shelf row answers from the release
    the row joined — two places, and a gesture that replaces one of them
    without the other makes the card contradict the database.

    The window is reopened in between: the album is scanned in one session and
    sent to the bench from the next, which is the path that reads it back from
    the row rather than finding it in memory.
    """
    library = _library(tmp_path)
    poor = _release("r1", (61000, 92000), title="Pelma Runo")
    api = _api(tmp_path, FakeSource(summaries=(poor,), details={"r1": poor}))
    _scan_and_wait(api, library)
    scanned = api.state()["albums"][0]
    assert scanned["looked_at"] is True, "the scan itself did not say a catalogue answered"

    # The next launch: a second API over the same database — the album is a
    # row again, not a state in memory.
    next_launch = _api(tmp_path, FakeSource(summaries=(poor,), details={"r1": poor}))
    from_the_row = next(
        album for album in next_launch.state()["albums"] if album["unit_id"] == scanned["unit_id"]
    )
    assert from_the_row["looked_at"] is True, "the row forgot the album had been looked up"

    assert next_launch.send_to_bench(scanned["unit_id"])["ok"]

    back_on_the_shelf = next(
        album for album in next_launch.state()["albums"] if album["unit_id"] == scanned["unit_id"]
    )
    assert (
        back_on_the_shelf["looked_at"] is True
    ), "the album came back from Quality saying it had never been scanned"


def test_a_moved_album_opens_from_a_row_alone(tmp_path: Path) -> None:
    """A moved album that is only a row can still be opened.

    Every gesture that follows a moved album goes through `_bring_in` first. If
    that scanned the recorded path and raised, then for an album that is no
    longer in memory — which is every album after a restart — the following
    could not be reached at all: the door would fail before the gesture ran.

    The album is moved and then reached from a **fresh API over the same
    database**, which is what the next launch is.
    """
    library = _library(tmp_path)
    moving = library / "box" / "an album"
    moving.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", moving / "aaa.flac")
    api = _api(tmp_path, FakeSource())
    api.open_folder(str(library))
    _finish_reads(api)
    mine = {album["folder"]: album["unit_id"] for album in api.state()["albums"]}["an album"]
    shutil.move(str(moving), str(library / "an album"))

    next_launch = _api(tmp_path, FakeSource())
    opened = next_launch.album(mine)

    assert "album" in opened, f"the album could not be opened at all: {opened}"
    assert opened["album"]["folder_path"] == str(
        library / "an album"
    ), "it opened, still naming the folder it used to live in"


def test_the_witness_says_when_it_is_looking_at_another_record() -> None:
    """The witness says when it is looking at another record.

    The catalogues may offer only editions of an album while the witness finds
    a different record — a remix EP, say — with every title and every length
    agreeing, against one in four for the record in use. With both numbers on
    the screen in different folds, the one sentence that decides has to be said.

    Rates and not counts: the two are compared against track lists of different
    lengths, which is the whole point when the record in use has ten tracks and
    the folder has four.
    """
    other_record = {
        "sources": [
            {"source": "discogs", "titles_agreeing": 1, "titles_compared": 4, "winner": True}
        ],
        "itunes": {
            "found": True,
            "titles_agreeing": 4,
            "titles_compared": 4,
            "durations_agree": True,
            "durations_agreeing": 4,
            "durations_compared": 4,
            "url": "https://music.apple.com/us/album/example/1000000001",
        },
    }
    said = _witness_knows_better(other_record)
    assert said is not None, "the witness recognised another record and the screen said nothing"
    assert said["titles_agreeing"] == 4 and said["in_use_agreeing"] == 1
    assert said["url"].endswith("1000000001")

    agreed = {
        "sources": [
            {"source": "discogs", "titles_agreeing": 11, "titles_compared": 11, "winner": True}
        ],
        "itunes": {"found": True, "titles_agreeing": 11, "titles_compared": 11},
    }
    assert _witness_knows_better(agreed) is None, "it speaks about an album nobody disagrees on"
    assert _witness_knows_better({"sources": [], "itunes": {"found": False}}) is None
    assert _witness_knows_better(None) is None


def test_a_scan_does_not_charge_a_question_on_the_way_out(tmp_path: Path) -> None:
    """A re-scan does not make leaving the album a question.

    Leaving an album asks wherever something can be taken back, and a re-scan
    arms that — so a gesture made all the time would cost a second click every
    time the dialog is closed, with the emphasis on the answer that undoes it.

    The way back is not withdrawn: `can_cancel` stays true and `Cancel` stays on
    the row. What changes is that leaving the album alone stops being a question.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.scan_selected([unit_id])["ok"]
    _finish(api._job, "scan")
    rescanned = api.album(unit_id)["album"]

    assert rescanned["can_cancel"] is True, "the way back went away with the question"
    assert rescanned["ask_before_leaving"] is False, "closing a scanned album still asks"


def test_a_plan_that_changes_nothing_is_not_a_plan_waiting(tmp_path: Path) -> None:
    """The question asked over a fold that reads `Nothing would change`.

    The dialog states a fact — *This album has a plan waiting* — and offers to
    keep it for next time. Over an album whose plan has no operations both
    halves are false: there is nothing waiting and nothing to keep, and leaving
    costs exactly what staying costs.

    `Cancel` is untouched: the way back is not withdrawn, the question about it
    is.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])
    api.apply_automatic()

    assert api.plan_anyway(unit_id)["ok"]
    again = api.album(unit_id)["album"]

    assert again["operations"] == [], "the album is already named the way the plan wants"
    assert again["can_cancel"] is True, "the way back is not what is being taken away"
    assert again["ask_before_leaving"] is False, "there is nothing waiting to leave waiting"


def test_arranging_by_tags_still_names_the_two_ways_out(tmp_path: Path) -> None:
    """The narrowing is a narrowing and not a removal.

    The ✕, a click outside and `Cancel` leave the album three ways, and the
    question on leaving is what says which is which. That still holds for the
    gesture that produced a proposal asked for by name.
    """
    library = tmp_path / "library"
    _tagged_album(library / "an album")
    api = _api(tmp_path, FakeSource())
    assert api.open_folder(str(library))["ok"]
    _finish_reads(api)
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.arrange_by_tags(unit_id)["ok"]
    arranged = api.album(unit_id)["album"]

    assert arranged["can_cancel"] is True
    assert (
        arranged["ask_before_leaving"] is True
    ), "leaving an arrangement asked for by name stopped naming the two ways out"
    assert arranged["leaving_takes_back"] is False, "only a re-planned finished album goes back"
