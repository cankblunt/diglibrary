"""Reading the folders out of a drop, which is the one thing the page cannot do."""

from pathlib import Path

from webview.dom import _dnd_state

from diglibrary.ui import drops


def _record(*entries: tuple[str, Path]) -> None:
    """Record paths the way the platform does while a drop is being performed."""
    _dnd_state["paths"].clear()
    _dnd_state["paths"].extend((key, str(path)) for key, path in entries)


def test_a_dropped_folder_is_read_from_where_its_path_is_recorded(tmp_path: Path) -> None:
    """A directory is recorded under its parent's name, and is read all the same."""
    album = tmp_path / "Orvalim Tessaro - Vimbração (1974) [FLAC]"
    album.mkdir()
    # Keyed by the parent's name, which is what cocoa records for a directory.
    _record((tmp_path.name, album))

    assert drops.take() == (str(album),)


def test_several_folders_arrive_in_the_order_they_were_dropped(tmp_path: Path) -> None:
    """Dropping several albums at once is the point of the gesture."""
    albums = [tmp_path / f"Album {n}" for n in range(3)]
    for album in albums:
        album.mkdir()
    _record(*((tmp_path.name, album) for album in albums))

    assert drops.take() == tuple(str(album) for album in albums)


def test_dropping_the_tracks_adds_the_album_holding_them_once(tmp_path: Path) -> None:
    """Selecting the files inside a folder means the same album, said differently."""
    album = tmp_path / "Album"
    album.mkdir()
    tracks = [album / "01.flac", album / "02.flac"]
    for track in tracks:
        track.write_bytes(b"\x00")
    # A file is recorded under its own name.
    _record(*((track.name, track) for track in tracks))

    assert drops.take() == (str(album),)


def test_a_drop_is_taken_so_the_next_one_does_not_repeat_it(tmp_path: Path) -> None:
    """The collected paths are a queue the platform appends to and never empties.

    Left in place, the second drop would add the first drop's folders again.
    """
    album = tmp_path / "Album"
    album.mkdir()
    _record((tmp_path.name, album))

    assert drops.take() == (str(album),)
    assert drops.take() == ()


def test_something_that_is_not_a_folder_is_no_folder(tmp_path: Path) -> None:
    """A path that has gone, or was never a directory, contributes nothing."""
    _record((tmp_path.name, tmp_path / "gone"))

    assert drops.take() == ()


def test_listening_switches_the_collection_on_once() -> None:
    """The platform reads the pasteboard only while its listener count is above zero.

    Counted once however often it is asked: the count is never taken back, so a
    second increment would be a second listener that does not exist.
    """
    drops.listen()
    counted = _dnd_state["num_listeners"]
    drops.listen()

    assert counted > 0
    assert _dnd_state["num_listeners"] == counted


def test_nothing_here_listens_to_the_page_through_the_platform() -> None:
    """A DOM listener registered through pywebview sends its whole target across.

    The event is serialised with its `currentTarget` and every descendant, so a
    listener on `<body>` carried the entire window on each drop until the string
    was too long to build — and then carried nothing, silently. The page asks
    for the paths instead; this refuses the listener coming back.
    """
    source = Path(drops.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.split('"""', 2)[2].splitlines() if not line.lstrip().startswith("#")
    )

    assert ".events." not in code
    assert ".dom." not in code.replace("webview.dom import", "")
