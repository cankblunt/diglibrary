"""The Finder tags of an album folder: read, planned, written, and taken back."""

import logging
import plistlib
import shutil
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import ChangeExecutor, StalePlanError
from diglibrary.library.finder_tags import (
    ATTRIBUTE,
    FinderTag,
    finder_tag_catalogue,
    read_finder_tags,
    tags_taken_back,
    with_tags_changed,
    write_finder_tags,
)
from diglibrary.library.matching import align_tracks
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangePlanner, OperationKind
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.library.xattrs import read_attribute, write_attribute

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="Finder tags are macOS extended attributes"
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"

LISTEN = FinderTag("listen", 1)
EX = FinderTag("EX", 6)


def _put(folder: Path, *entries: str) -> None:
    """Tag a folder the way the Finder does, without going through the module."""
    assert write_attribute(
        folder, ATTRIBUTE, plistlib.dumps(list(entries), fmt=plistlib.FMT_BINARY)
    )


def _stored(folder: Path) -> list[str]:
    raw = read_attribute(folder, ATTRIBUTE)
    return [] if raw is None else plistlib.loads(raw)


def test_a_folder_reads_back_in_the_finders_own_form(tmp_path: Path) -> None:
    """A coloured tag is its name, a line break and a number; an uncoloured one, its name."""
    _put(tmp_path, "Jazz-Funk", "listen\n1", "EX\n6")

    assert read_finder_tags(tmp_path) == (FinderTag("Jazz-Funk"), LISTEN, EX)

    write_finder_tags(tmp_path, (FinderTag("Soul"), LISTEN))

    assert _stored(tmp_path) == ["Soul", "listen\n1"]


def test_a_folder_with_no_tags_answers_empty_and_a_missing_one_answers_nothing(
    tmp_path: Path,
) -> None:
    assert read_finder_tags(tmp_path) == ()
    assert read_finder_tags(tmp_path / "gone") is None


def test_taking_off_the_last_tag_removes_the_attribute(tmp_path: Path) -> None:
    _put(tmp_path, "listen\n1")

    write_finder_tags(tmp_path, ())

    assert read_attribute(tmp_path, ATTRIBUTE) is None
    assert read_finder_tags(tmp_path) == ()


def test_a_decomposed_name_is_read_and_written_composed(tmp_path: Path) -> None:
    """One word stored two ways is two tags to the Finder; this module knows one."""
    decomposed = unicodedata.normalize("NFD", "Trevão")
    assert decomposed != "Trevão"
    _put(tmp_path, decomposed)

    assert read_finder_tags(tmp_path) == (FinderTag("Trevão"),)

    write_finder_tags(tmp_path, read_finder_tags(tmp_path) or ())

    assert _stored(tmp_path) == ["Trevão"]


def test_a_change_keeps_every_other_tag_in_its_place() -> None:
    current = (FinderTag("Funk"), EX, FinderTag("Soul"))

    assert with_tags_changed(current, (LISTEN, FinderTag("Funk")), ("Soul",)) == (
        FinderTag("Funk"),
        EX,
        LISTEN,
    )


def test_taking_back_restores_exactly_what_was_there_when_nothing_moved_since() -> None:
    before = (FinderTag("Funk"), EX)
    written = with_tags_changed(before, (LISTEN,), ("Funk",))

    assert tags_taken_back(written, written, before) == before


def test_taking_back_keeps_a_tag_the_user_put_on_since() -> None:
    """Only what the change did comes off; the user's later word stays."""
    before = (FinderTag("Funk"), EX)
    written = with_tags_changed(before, (LISTEN,), ("Funk",))
    since = (*written, FinderTag("Soul"))

    assert tags_taken_back(since, written, before) == (
        FinderTag("Funk"),
        EX,
        FinderTag("Soul"),
    )


def test_the_catalogue_puts_the_favourites_first_and_the_rest_in_order(tmp_path: Path) -> None:
    one, other = tmp_path / "one", tmp_path / "other"
    one.mkdir()
    other.mkdir()
    _put(one, "listen\n1", "Quelvo", unicodedata.normalize("NFD", "Trevão"))
    _put(other, "EX\n6", "vimesbeat", "Trevão", "Quelvo")
    preferences = tmp_path / "finder.plist"
    preferences.write_bytes(plistlib.dumps({"FavoriteTagNames": ["", "EX", "VG", "listen"]}))

    favourites, others = finder_tag_catalogue(
        (other,), search=lambda: (one,), preferences=preferences
    )

    # `VG` is a favourite nothing carries, so nothing says what colour it is.
    assert favourites == (EX, LISTEN)
    assert others == (FinderTag("Quelvo"), FinderTag("Trevão"), FinderTag("vimesbeat"))


def _release() -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Forró",
        artists=(ArtistMetadata(name="Marina do Acordeão"),),
        tracks=(
            TrackMetadata(title="Primeira", position=1, position_on_medium=1, duration_ms=400),
            TrackMetadata(title="Segunda", position=2, position_on_medium=2, duration_ms=900),
        ),
        released_on=date(1955, 3, 1),
    )


def _album(root: Path) -> Path:
    folder = root / "misnamed folder"
    folder.mkdir()
    shutil.copy(FIXTURES / "tone.flac", folder / "aaa.flac")
    shutil.copy(FIXTURES / "tone-long.flac", folder / "bbb.flac")
    return folder


def _plan(root: Path, finder_tags):
    unit = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.finder")).scan(root)[0]
    release = _release()
    return ChangePlanner(NamingPolicy(), MutagenTagStore()).plan(
        unit,
        release,
        align_tracks(unit, release, tolerance_ms=100, ordered_tolerance_ms=100),
        finder_tags=finder_tags,
    )


def _executor() -> ChangeExecutor:
    return ChangeExecutor(MutagenTagStore(), logging.getLogger("test.finder"))


def test_the_tags_are_written_on_the_folder_once_it_has_its_new_name(tmp_path: Path) -> None:
    """Planned after the rename, so the operation names the folder that will exist."""
    folder = _album(tmp_path)
    _put(folder, "Funk", "EX\n6")

    plan = _plan(tmp_path, (EX, LISTEN))
    tagging = [op for op in plan.operations if op.kind is OperationKind.WRITE_FINDER_TAGS]
    renamed = [op for op in plan.operations if op.kind is OperationKind.RENAME_FOLDER]

    assert len(tagging) == 1 and len(renamed) == 1
    assert tagging[0].sequence > renamed[0].sequence
    assert tagging[0].target_path == Path(str(renamed[0].after_state["path"]))
    assert tagging[0].after_state == {
        "add": ["listen\n1"],
        "remove": ["Funk"],
        "result": ["EX\n6", "listen\n1"],
    }

    result = _executor().apply(plan)

    assert result.is_complete
    assert _stored(tagging[0].target_path) == ["EX\n6", "listen\n1"]


def test_a_choice_equal_to_the_folder_plans_nothing(tmp_path: Path) -> None:
    folder = _album(tmp_path)
    _put(folder, "EX\n6", "listen\n1")

    plan = _plan(tmp_path, (LISTEN, EX))

    assert all(op.kind is not OperationKind.WRITE_FINDER_TAGS for op in plan.operations)


def test_no_choice_leaves_the_folder_alone(tmp_path: Path) -> None:
    folder = _album(tmp_path)
    _put(folder, "EX\n6")

    plan = _plan(tmp_path, None)

    assert all(op.kind is not OperationKind.WRITE_FINDER_TAGS for op in plan.operations)
    _executor().apply(plan)
    renamed = next(path for path in tmp_path.iterdir() if path.is_dir())
    assert _stored(renamed) == ["EX\n6"]


def test_a_tag_put_on_in_the_finder_after_planning_survives_the_apply(tmp_path: Path) -> None:
    folder = _album(tmp_path)
    _put(folder, "EX\n6")
    plan = _plan(tmp_path, (EX, LISTEN))
    _put(folder, "EX\n6", "Soul")

    _executor().apply(plan)

    target = next(op for op in plan.operations if op.kind is OperationKind.WRITE_FINDER_TAGS)
    assert _stored(target.target_path) == ["EX\n6", "Soul", "listen\n1"]


def test_the_revert_gives_the_folder_back_its_name_and_its_tags(tmp_path: Path) -> None:
    folder = _album(tmp_path)
    _put(folder, "Funk", "EX\n6")
    plan = _plan(tmp_path, (EX, LISTEN))
    executor = _executor()
    applied = executor.apply(plan)

    executor.revert(applied)

    assert folder.is_dir()
    assert _stored(folder) == ["Funk", "EX\n6"]


def test_the_revert_takes_off_only_what_the_apply_wrote(tmp_path: Path) -> None:
    """A tag the plan chose and the Finder put on first was never the change's.

    Planned to add `listen` and take off `Funk`; before the apply the Finder put
    `listen` on and took `Funk` off itself, so the apply wrote nothing of its
    own. Tagged again in the Finder afterwards, the folder gives nothing back
    and loses nothing on the revert.
    """
    folder = _album(tmp_path)
    _put(folder, "Funk", "EX\n6")
    plan = _plan(tmp_path, (EX, LISTEN))
    _put(folder, "EX\n6", "listen\n1")
    executor = _executor()
    applied = executor.apply(plan)
    target = next(op for op in plan.operations if op.kind is OperationKind.WRITE_FINDER_TAGS)
    _put(target.target_path, "EX\n6", "listen\n1", "Soul")

    executor.revert(applied)

    assert _stored(folder) == ["EX\n6", "listen\n1", "Soul"]


def test_the_revert_of_a_folder_that_had_no_tags_leaves_it_with_none(tmp_path: Path) -> None:
    folder = _album(tmp_path)
    plan = _plan(tmp_path, (LISTEN,))
    executor = _executor()

    executor.revert(executor.apply(plan))

    assert read_attribute(folder, ATTRIBUTE) is None


def test_a_folder_gone_before_the_apply_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    folder = _album(tmp_path)
    plan = _plan(tmp_path, (LISTEN,))
    shutil.rmtree(folder)

    with pytest.raises(StalePlanError):
        _executor().apply(plan)
