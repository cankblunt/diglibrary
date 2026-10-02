"""The ceiling on the folders that hold what can always be made again.

Every one of these is about something the module must *not* do. Pointing a
deleting loop at a folder is the cheapest way in this project to lose
somebody's music, so what is proven here is the narrowness: only these folders,
only these suffixes, never one level down, and never more than the ceiling asks
for.
"""

import logging
import os
from pathlib import Path

from diglibrary.cachedir import clear_folder, enforce_folder_limit, folder_bytes, touch

LOGGER = logging.getLogger("test.cachedir")
SUFFIXES = (".jpg", ".png")


def _picture(path: Path, size: int, age: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (age, age))
    return path


def test_the_least_recently_looked_at_goes_first_not_the_first_written(tmp_path: Path) -> None:
    """A cache hit touches the file, and that is what the ceiling must obey.

    Without it, a ceiling discards by age of *writing* — which throws away
    precisely the entries being used most, and does it silently.
    """
    for index in range(10):
        _picture(tmp_path / f"{index}.jpg", 1000, 1000 + index)
    oldest = tmp_path / "0.jpg"
    touch(oldest)

    held = enforce_folder_limit([tmp_path], 6000, SUFFIXES, LOGGER, "test.evicted")

    assert held == 6000
    assert oldest.exists(), "the file that was just looked at must be the last to go"
    assert sorted(path.name for path in tmp_path.glob("*.jpg")) == [
        "0.jpg",
        "5.jpg",
        "6.jpg",
        "7.jpg",
        "8.jpg",
        "9.jpg",
    ]


def test_several_folders_are_one_budget(tmp_path: Path) -> None:
    """The staged pictures and the drawn sleeves are two folders of one thing.

    Giving each its own ceiling would mean the number in Settings is not the
    number on disk, which is the whole point of showing a number.
    """
    staging, covers = tmp_path / "artwork", tmp_path / "artwork" / "covers"
    for index in range(5):
        _picture(staging / f"{index}.jpg", 1000, 1000 + index)
    for index in range(5):
        _picture(covers / f"{index}.png", 1000, 2000 + index)

    assert folder_bytes([staging, covers], SUFFIXES) == 10_000
    assert enforce_folder_limit([staging, covers], 4000, SUFFIXES, LOGGER, "test.evicted") == 4000
    # The staged ones are older, so they are what went — across the folder
    # boundary, which is the point.
    assert list(staging.glob("*.jpg")) == []
    assert len(list(covers.glob("*.png"))) == 4


def test_nothing_but_the_named_suffixes_and_nothing_below_the_folder(tmp_path: Path) -> None:
    """The two locks that keep this away from the user's own files.

    A folder is only ever handed to this module by the composition root, and
    even then it takes one kind of file and refuses to descend. `logs`, a
    `config.toml`, an album folder this is pointed at by mistake — none of
    them are files this can see.
    """
    _picture(tmp_path / "cover.jpg", 5000, 1000)
    keep = tmp_path / "notes.txt"
    keep.write_text("not a picture")
    below = _picture(tmp_path / "album" / "front.jpg", 5000, 1000)

    assert folder_bytes([tmp_path], SUFFIXES) == 5000, "what is one level down is not counted"
    enforce_folder_limit([tmp_path], 0, SUFFIXES, LOGGER, "test.evicted")
    freed = clear_folder([tmp_path], SUFFIXES)

    assert freed == 0, "the ceiling had already taken the only picture it could see"
    assert keep.exists()
    assert below.exists(), "a picture one level down was never this module's to take"


def test_a_folder_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    """The window opens before anything has been cached, and must not care."""
    missing = tmp_path / "never-written"
    assert folder_bytes([missing], SUFFIXES) == 0
    assert enforce_folder_limit([missing], 1000, SUFFIXES, LOGGER, "test.evicted") == 0
    assert clear_folder([missing], SUFFIXES) == 0
