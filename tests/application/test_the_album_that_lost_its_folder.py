"""An album whose folder is gone, and what the window is able to say about it.

The case: an album is organized, the same audio arrives again in a second
folder and is scanned, and the second folder is then deleted. The card left
behind points at a folder that no longer exists, while the application still
holds the album's name, its identification, and another row with the same
audio whose folder is on disk. Answering *Unknown album* for that card is
false.

Two copies of one album are two rows on purpose: `_upsert_unit` refuses to
collapse a signature match whose folder still exists, because that would lose
one of them. The refusal to open must say what is known.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tests.application.test_api import (
    FIXTURES,
    FakeSource,
    _api,
    _library,
    _release,
    _scan_and_wait,
)


def _second_copy(library: Path) -> Path:
    """The same audio in another folder, which is what a second download is.

    Read from whatever the album is called now: applying a plan renames the
    folder, so the name it was scanned under is gone by the time this runs.
    """
    original = next(path for path in sorted(library.iterdir()) if path.is_dir())
    again = library / "downloaded again"
    again.mkdir(parents=True)
    for file in sorted(original.iterdir()):
        if file.is_file():
            shutil.copy(file, again / file.name)
    return again


def _both(tmp_path: Path) -> tuple[object, int, int, Path]:
    """One album organized, then the same audio dropped a second time."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    first = api.state()["albums"][0]["unit_id"]
    assert api.approve(first)["ok"]
    again = _second_copy(library)
    _scan_and_wait(api, library)
    second = next(album["unit_id"] for album in api.state()["albums"] if album["unit_id"] != first)
    return api, first, second, again


def test_two_copies_of_one_album_are_two_albums(tmp_path: Path) -> None:
    """The premise, stated so the rest is not read as a bug in this part.

    A signature match whose folder is still on disk is a second copy, not the
    same album moved, and collapsing them would lose one.
    """
    api, first, second, _ = _both(tmp_path)

    assert first != second
    assert api._store.unit_by_id(first).unit_signature == (
        api._store.unit_by_id(second).unit_signature
    ), "the same audio, twice"


def test_an_album_whose_folder_is_gone_stops_being_called_unknown(tmp_path: Path) -> None:
    """A missing folder is reported as a missing folder, not as an unknown album.

    Read after a restart: the shelf is drawn from the database and the album
    has never been read off the disk, so opening it is what tries to read the
    folder. The row carries the album's name, its state and its
    identification, so `Unknown album` would answer a different question.
    """
    _, _, second, again = _both(tmp_path)
    shutil.rmtree(again)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    reopened = _api(tmp_path, source)

    refused = reopened.album(second)

    assert refused["ok"] is False, "there is no album to draw a dialog from"
    assert "Unknown album" not in refused["error"], "the album is known; only its folder is gone"
    assert refused["folder_is_gone"] is True
    assert str(again) in refused["error"], "the folder it cannot find is named"


def test_it_names_the_copy_that_is_still_there(tmp_path: Path) -> None:
    """The refusal names the other row that holds the same audio.

    Another row carries the same audio and its folder is on disk. That is the
    difference between *this album is lost* and *this card is a copy that can
    be let go of*, and only one of them is true here.
    """
    _, first, second, again = _both(tmp_path)
    shutil.rmtree(again)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    reopened = _api(tmp_path, source)

    refused = reopened.album(second)

    twin = refused["same_audio_at"]
    assert twin, "the twin is named"
    assert twin["unit_id"] == first
    assert twin["organized"] is True
    assert Path(twin["folder"]).is_dir(), "and it is somewhere that still exists"
    assert twin["folder"] in refused["error"], "the message says where, not only that"


def test_an_album_with_no_twin_says_only_what_is_true(tmp_path: Path) -> None:
    """One folder, deleted, and no second copy: there is nothing to point at.

    The two repairs that do exist are named instead — the card's own gestures,
    which is where they live for an album whose dialog cannot open.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    only = api.state()["albums"][0]["unit_id"]
    shutil.rmtree(next(path for path in library.iterdir() if path.is_dir()))
    reopened = _api(tmp_path, source)

    refused = reopened.album(only)

    assert refused["ok"] is False
    assert refused["folder_is_gone"] is True
    assert refused["same_audio_at"] is None, "nothing is invented to point at"
    assert "✕" in refused["error"], "and the way to let the card go is said"


def test_an_album_this_app_never_had_is_still_unknown(tmp_path: Path) -> None:
    """The sentence keeps its meaning where it was the true one."""
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)

    assert api.album(999_999) == {"ok": False, "error": "Unknown album."}


def test_the_screen_appears_for_an_album_this_session_is_holding(tmp_path: Path) -> None:
    """The same refusal applies to an album already held in memory.

    `album()` asks `_bring_in`, which returns an album already in memory. If
    that path does not ask about the folder, the repair screen appears only
    for a card never read off the disk, and an album this session scanned or
    organized opens the full dialog over files that are not there.

    The album is moved to a place outside everything this application has
    been shown. The search for a moved album looks only in folders it knows,
    so this one cannot be followed and the refusal is the whole answer.
    """
    library = _library(tmp_path)
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = int(api.state()["albums"][0]["unit_id"])
    assert api.approve(unit_id)["ok"]
    assert unit_id in api._albums, "this session is holding it, which is the whole case"
    folder = api._store.unit_by_id(unit_id).folder_path

    # Nowhere this app has been shown: not under the scanned root, and no album
    # of this library has ever lived beside it.
    elsewhere = tmp_path.parent / f"outside-{tmp_path.name}"
    elsewhere.mkdir()
    shutil.move(str(folder), str(elsewhere / folder.name))

    refused = api.album(unit_id)

    assert refused["ok"] is False, "there are no files to draw a dialog from"
    assert refused["folder_is_gone"] is True
    assert str(folder) in refused["error"], "and it names the folder it cannot read"


def test_an_album_this_session_is_holding_is_followed_where_it_can_be(
    tmp_path: Path,
) -> None:
    """The album is searched for before the refusal, or moved albums are refused.

    The same question asked of an album that went somewhere this application
    knows: the dialog opens, on the folder it lives in now. Refusing here
    would put the repair screen in front of an album that needs no repair,
    which is why the check cannot be `is_dir()` alone.
    """
    library = _library(tmp_path)
    scanned = next(path for path in library.iterdir() if path.is_dir())
    # A second album, so `collection` is a folder this application has been
    # shown. The search for a moved album looks in the parents of albums it
    # has recorded and nowhere else, so without this the test would ask about
    # a place the application does not look in.
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
        if Path(str(album["folder_path"])).name == scanned.name
    )
    assert unit_id in api._albums
    folder = api._store.unit_by_id(unit_id).folder_path

    shutil.move(str(folder), str(collection / folder.name))

    opened = api.album(unit_id)

    assert opened["ok"] is True, "the album moved to a folder this application has been shown"
    assert opened["album"]["folder_path"] == str(collection / folder.name)
    assert opened["album"]["folder_missing"] is False
