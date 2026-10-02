"""Reading the folders out of a drop, which is the one thing the page cannot do."""

from pathlib import Path

from webview.dom import _dnd_state

from diglibrary.ui import drops


def _drop(names: list[str]) -> dict[str, object]:
    """Shape one drop the way pywebview hands it over."""
    return {"dataTransfer": {"files": [{"name": name} for name in names]}}


def test_a_dropped_folder_is_read_from_where_its_path_is_actually_recorded(
    tmp_path: Path,
) -> None:
    """The attached path is never there for a directory, and that is not a failure.

    pywebview attaches `pywebviewFullPath` by matching a recorded path against
    the name the page reports, and for a directory the recorded key is the name
    of its *parent* — so the two never match. The path is still collected, and
    that is where it is read from.
    """
    album = tmp_path / "Orvalim Tessaro - Vimbração (1974) [FLAC]"
    album.mkdir()
    _dnd_state["paths"].clear()
    # Keyed by the parent's name, which is what cocoa records for a directory.
    _dnd_state["paths"].append((tmp_path.name, str(album)))

    assert drops._folders(_drop([album.name])) == (str(album),)


def test_several_folders_arrive_in_the_order_they_were_dropped(tmp_path: Path) -> None:
    """Dropping several albums at once is the point of the gesture."""
    albums = [tmp_path / f"Album {n}" for n in range(3)]
    for album in albums:
        album.mkdir()
    _dnd_state["paths"].clear()
    _dnd_state["paths"].extend((tmp_path.name, str(album)) for album in albums)

    assert drops._folders(_drop([album.name for album in albums])) == tuple(
        str(album) for album in albums
    )


def test_dropping_the_tracks_adds_the_album_holding_them_once(tmp_path: Path) -> None:
    """Selecting the files inside a folder means the same album, said differently."""
    album = tmp_path / "Album"
    album.mkdir()
    tracks = [album / "01.flac", album / "02.flac"]
    for track in tracks:
        track.write_bytes(b"\x00")
    _dnd_state["paths"].clear()
    # A file is keyed by its own name, so this route does attach the path.
    event = {
        "dataTransfer": {
            "files": [{"name": track.name, "pywebviewFullPath": str(track)} for track in tracks]
        }
    }

    assert drops._folders(event) == (str(album),)


def test_a_drop_is_taken_so_the_next_one_does_not_repeat_it(tmp_path: Path) -> None:
    """The collected paths are a queue the platform appends to and never empties.

    Left in place, the second drop would add the first drop's folders again.
    """
    album = tmp_path / "Album"
    album.mkdir()
    _dnd_state["paths"].clear()
    _dnd_state["paths"].append((tmp_path.name, str(album)))

    assert drops._folders(_drop([album.name])) == (str(album),)
    assert drops._folders(_drop([])) == ()


def test_something_that_is_not_a_folder_is_no_folder(tmp_path: Path) -> None:
    """A path that has gone, or was never a directory, contributes nothing."""
    _dnd_state["paths"].clear()
    _dnd_state["paths"].append((tmp_path.name, str(tmp_path / "gone")))

    assert drops._folders(_drop(["gone"])) == ()
