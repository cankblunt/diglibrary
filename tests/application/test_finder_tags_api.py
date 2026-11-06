"""The folder's Finder tags, from the dialog's gesture to the disk and back."""

import plistlib
import sys

import pytest
from tests.application.test_api import (
    ORGANIZED,
    FakeSource,
    _api,
    _library,
    _release,
    _scan_and_wait,
)

from diglibrary.library.finder_tags import ATTRIBUTE, FinderTag
from diglibrary.library.xattrs import read_attribute, write_attribute

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="Finder tags are macOS extended attributes"
)

EX = FinderTag("EX", 6)
LISTEN = FinderTag("listen", 1)
SOUL = FinderTag("Soul")


def _put(folder, *entries: str) -> None:
    assert write_attribute(
        folder, ATTRIBUTE, plistlib.dumps(list(entries), fmt=plistlib.FMT_BINARY)
    )


def _stored(folder) -> list[str]:
    raw = read_attribute(folder, ATTRIBUTE)
    return [] if raw is None else plistlib.loads(raw)


@pytest.fixture
def scanned(tmp_path, monkeypatch):
    """An album in review, tagged `EX` in the Finder, with three tags existing."""
    library = _library(tmp_path)
    folder = next(library.iterdir())
    _put(folder, "EX\n6")
    monkeypatch.setattr(
        "diglibrary.application.api.finder_tag_catalogue",
        lambda folders: ((EX, LISTEN), (SOUL,)),
    )
    source = FakeSource(
        summaries=(_release("r1", (400, 900)),),
        details={"r1": _release("r1", (400, 900))},
    )
    api = _api(tmp_path, source)
    _scan_and_wait(api, library)
    unit_id = api.state()["albums"][0]["unit_id"]
    return api, unit_id, library, folder


def _finder_operation(album):
    return next(op for op in album["operations"] if op["kind"] == "write_finder_tags")


def test_the_list_offers_the_favourites_first(scanned) -> None:
    api, _, _, _ = scanned

    listed = api.finder_tag_list()

    assert listed["favourites"] == [{"name": "EX", "color": 6}, {"name": "listen", "color": 1}]
    assert listed["others"] == [{"name": "Soul", "color": 0}]


def test_choosing_a_tag_plans_it_and_the_dialog_reads_both_sides(scanned) -> None:
    api, unit_id, _, _ = scanned
    api.finder_tag_list()

    corrected = api.correct(unit_id, finder_tags=["EX", "listen"])

    assert corrected["ok"]
    album = corrected["album"]
    assert album["finder_tags_now"] == [{"name": "EX", "color": 6}]
    assert _finder_operation(album)["finder_tags"] == {
        "result": [{"name": "EX", "color": 6}, {"name": "listen", "color": 1}],
        "added": [{"name": "listen", "color": 1}],
        "removed": [],
    }


def test_a_name_the_finder_does_not_have_is_a_new_tag_without_colour(scanned) -> None:
    api, unit_id, library, _ = scanned
    api.finder_tag_list()

    corrected = api.correct(unit_id, finder_tags=["EX", "Quelvo Novo"])

    assert corrected["ok"]
    assert _finder_operation(corrected["album"])["finder_tags"]["added"] == [
        {"name": "Quelvo Novo", "color": 0}
    ]
    assert api.approve(unit_id)["ok"]
    assert _stored(library / ORGANIZED) == ["EX\n6", "Quelvo Novo"]


def test_a_name_that_differs_only_in_case_is_the_existing_tag(scanned) -> None:
    """A slip of the shift key does not make a second spelling of one word."""
    api, unit_id, _, _ = scanned
    api.finder_tag_list()

    corrected = api.correct(unit_id, finder_tags=["ex", "LISTEN"])

    assert corrected["ok"]
    assert _finder_operation(corrected["album"])["finder_tags"]["result"] == [
        {"name": "EX", "color": 6},
        {"name": "listen", "color": 1},
    ]


def test_a_name_with_a_line_break_is_refused(scanned) -> None:
    """The attribute reads what follows a line break as the colour."""
    api, unit_id, _, _ = scanned
    api.finder_tag_list()

    refused = api.correct(unit_id, finder_tags=["EX", "Soul\n3"])

    assert refused["ok"] is False
    assert all(row["field"] != "finder_tags" for row in api._store.corrections(unit_id))


def test_the_folders_own_set_withdraws_the_choice(scanned) -> None:
    api, unit_id, _, _ = scanned
    api.finder_tag_list()
    api.correct(unit_id, finder_tags=["EX", "listen"])

    back = api.correct(unit_id, finder_tags=["EX"])

    assert back["ok"]
    assert all(op["kind"] != "write_finder_tags" for op in back["album"]["operations"])
    assert all(row["field"] != "finder_tags" for row in api._store.corrections(unit_id))


def test_the_chosen_colour_survives_a_restart_before_any_list_is_read(scanned) -> None:
    """The plan rebuilt at launch knows no list yet, and still writes the colour."""
    api, unit_id, _, _ = scanned
    api.finder_tag_list()
    api.correct(unit_id, finder_tags=["EX", "listen"])

    assert api._stored_finder_tags(unit_id) == (EX, LISTEN)


def test_apply_writes_the_tags_and_spends_the_choice_and_revert_takes_them_back(
    scanned,
) -> None:
    api, unit_id, library, folder = scanned
    api.finder_tag_list()
    api.correct(unit_id, finder_tags=["listen", "Soul"])

    assert api.approve(unit_id)["ok"]

    organized = library / ORGANIZED
    assert organized.is_dir()
    assert _stored(organized) == ["listen\n1", "Soul"]
    assert all(
        row["field"] != "finder_tags" for row in api._store.corrections(unit_id)
    ), "a choice kept after it was applied would put back a tag taken off in the Finder since"

    history = api.state()["history"]
    assert api.revert(history[0]["plan_id"])["ok"]

    assert folder.is_dir()
    assert _stored(folder) == ["EX\n6"]


def test_an_organized_album_is_tagged_through_plan_this_album_again(scanned) -> None:
    """An organized album stays as it is until it is planned again, and then it can be."""
    api, unit_id, library, _ = scanned
    assert api.approve(unit_id)["ok"]
    _scan_and_wait(api, library)
    organized = library / ORGANIZED
    assert _stored(organized) == ["EX\n6"], "nothing was chosen, so nothing changed"

    assert api.plan_anyway(unit_id)["ok"]
    api.finder_tag_list()
    corrected = api.correct(unit_id, finder_tags=["EX", "listen"])

    assert corrected["ok"]
    assert [op["kind"] for op in corrected["album"]["operations"]] == ["write_finder_tags"]
    assert api.approve(unit_id)["ok"]
    assert _stored(organized) == ["EX\n6", "listen\n1"]


def test_the_apply_certifies_the_tags_it_wrote(scanned) -> None:
    """The certificate reads the folder back, and does not mistake it for a cover."""
    api, unit_id, _, _ = scanned
    api.finder_tag_list()
    api.correct(unit_id, finder_tags=["EX", "listen"])

    result = api.approve(unit_id)

    assert result["ok"]
    assert api._albums[unit_id].writes_ok is True


def test_the_change_report_names_the_tags_put_on_and_taken_off(scanned) -> None:
    from diglibrary.application.api import _album_reports

    api, unit_id, _, _ = scanned
    api.finder_tag_list()
    api.correct(unit_id, finder_tags=["listen"])
    assert api.approve(unit_id)["ok"]

    rows = _album_reports(api._store.change_log(), {}, {}, {})

    words = "".join(run.text for run in rows[0].tags)
    assert "Finder: +listen -EX" in words
