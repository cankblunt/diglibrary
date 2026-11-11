"""A track's guests are written into its file name as `(feat. …)`.

The album's own artist leaves the track's name, as on every other album, and
whoever else the track credits follows the title. Only on an album that is not a
compilation, only where the album's artist leads the credit, and only in the
file name: the tags already carry every artist of the credit.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.matching import align_tracks
from diglibrary.library.naming import NamingPolicy, guest_credit
from diglibrary.library.planner import ChangePlanner, OperationKind
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"
LEAD = "Zed Marrowby"


def _track(title: str, *names: str, position: int = 1, duration_ms: int = 400) -> TrackMetadata:
    return TrackMetadata(
        title=title,
        position=position,
        position_on_medium=position,
        duration_ms=duration_ms,
        artists=tuple(ArtistMetadata(name=name) for name in names),
    )


def _release(*tracks: TrackMetadata, artists: tuple[str, ...] = (LEAD,)) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="g1",
        title="Lantern Quarry",
        artists=tuple(ArtistMetadata(name=name) for name in artists),
        tracks=tracks,
        released_on=date(2020, 5, 29),
    )


def test_the_guests_a_catalogue_lists_follow_the_album_s_artist() -> None:
    track = _track("Every Hollow", LEAD, "Mirén Zoélle", "Ty Quarrel")
    assert guest_credit(_release(track), track) == "Mirén Zoélle & Ty Quarrel"


def test_three_guests_are_joined_with_commas_and_one_ampersand() -> None:
    track = _track("Day Hollow", LEAD, "Ana Lume", "Beto Lume", "Caio Lume")
    assert guest_credit(_release(track), track) == "Ana Lume, Beto Lume & Caio Lume"


def test_a_credit_read_from_one_tag_value_is_read_by_its_beginning() -> None:
    """A band whose name holds a comma passes through whole, never split."""
    track = _track("Ember Hollow", f"{LEAD}, Salt, Stone & Ember")
    assert guest_credit(_release(track), track) == "Salt, Stone & Ember"
    track = _track("Ember Hollow", f"{LEAD} & Quarry 70")
    assert guest_credit(_release(track), track) == "Quarry 70"


def test_a_track_the_album_s_artist_does_not_lead_gets_no_guests() -> None:
    """`Ojel Varn With Zed Marrowby` is not that artist's track with a guest."""
    led_by_another = _track("Hollow Blues", "Ojel Varn", LEAD)
    without_the_artist = _track("Hollow Song", "Ojel Varn")
    release = _release(led_by_another, without_the_artist)
    assert guest_credit(release, led_by_another) is None
    assert guest_credit(release, without_the_artist) is None


def test_a_track_credited_to_the_album_s_artist_alone_gets_no_guests() -> None:
    track = _track("Plain Hollow", LEAD)
    assert guest_credit(_release(track), track) is None
    assert guest_credit(_release(_track("No Credit")), _track("No Credit")) is None


def test_a_compilation_gets_no_guests_because_it_writes_the_whole_credit() -> None:
    first = _track("One", "The Lanterners", "Quarrelle", position=1)
    second = _track("Two", "Mirén Zoélle", position=2)
    release = _release(first, second, artists=("Various Artists",))
    assert guest_credit(release, first) is None


def test_every_artist_of_an_album_credited_to_two_leaves_the_track() -> None:
    track = _track("Twin Hollow", "Ana Lume", "Beto Lume", "Caio Lume")
    release = _release(track, artists=("Ana Lume", "Beto Lume"))
    assert guest_credit(release, track) == "Caio Lume"


def _album(root: Path, names: tuple[str, ...]) -> object:
    folder = root / "Lantern Quarry"
    folder.mkdir(parents=True)
    for index, name in enumerate(names):
        source = ("tone.flac", "tone-long.flac")[index % 2]
        shutil.copy(FIXTURES / source, folder / name)
    return LibraryScanner(MutagenAudioProbe(), __import__("logging").getLogger("t")).scan(root)[0]


def _plan(root: Path, names: tuple[str, ...]):
    unit = _album(root, names)
    release = _release(
        _track("Every Hollow", LEAD, "Mirén Zoélle", "Ty Quarrel", position=1, duration_ms=400),
        _track("Plain Hollow", LEAD, position=2, duration_ms=900),
    )
    alignment = align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100)
    planner = ChangePlanner(NamingPolicy(), MutagenTagStore())
    return planner.plan(unit, release, alignment)


def _renames(plan) -> list[str]:
    return sorted(
        Path(operation.after_state["path"]).name
        for operation in plan.operations
        if operation.kind is OperationKind.RENAME_FILE
    )


def test_the_plan_names_the_guests_in_the_file_and_not_in_the_title_tag(tmp_path: Path) -> None:
    plan = _plan(tmp_path, ("aaa.flac", "bbb.flac"))

    assert _renames(plan) == [
        "01. Every Hollow (feat. Mirén Zoélle & Ty Quarrel).flac",
        "02. Plain Hollow.flac",
    ]
    titles = [
        operation.after_state["tags"]["title"]
        for operation in plan.operations
        if operation.kind is OperationKind.WRITE_TAGS
    ]
    assert sorted(titles) == [["Every Hollow"], ["Plain Hollow"]]
    artists = [
        operation.after_state["tags"]["artist"]
        for operation in plan.operations
        if operation.kind is OperationKind.WRITE_TAGS
    ]
    assert [LEAD, "Mirén Zoélle", "Ty Quarrel"] in artists, "the tag keeps the whole credit"


def test_a_name_that_already_says_feat_is_not_given_it_twice(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        ("01 - Every Hollow (feat. Mirén Zoélle & Ty Quarrel).flac", "02 - Plain Hollow.flac"),
    )

    assert _renames(plan)[0] == "01. Every Hollow (feat. Mirén Zoélle & Ty Quarrel).flac"
