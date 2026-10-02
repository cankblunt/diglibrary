"""The shelf's own ordering, run rather than read.

Every other guard over `ui/web/` is static: it reads the files and asserts that
their shapes agree (`test_web_assets.py`). That catches an id nobody declared and
a string nobody wrote, and it cannot catch a comparator that is simply backwards
— which puts the first album dropped in a session above every album dropped
after it.

So this one executes the function. It is skipped where `node` is not installed,
because the application itself never needs it: nothing here is a dependency of
DigLibrary, only of checking DigLibrary on a machine that has it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _run_sort(
    albums: list[dict[str, object]], pointed_at: list[int], sort: str = "recent"
) -> list[int]:
    """Order these albums with the window's own `sortAlbums`, and say what came out."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"function sortAlbums\(albums\) \{[\s\S]+?\n\}", source)
    assert found, "no sortAlbums() to run, so this guard is looking at the wrong file"
    harness = f"""
    const state = {{ sort: {json.dumps(sort)}, pointedAt: {json.dumps(pointed_at)} }};
    {found.group(0)}
    const ordered = sortAlbums({json.dumps(albums)});
    console.log(JSON.stringify(ordered.map((album) => album.unit_id)));
    """
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return list(json.loads(answer.stdout.strip()))


def test_the_shelf_puts_the_newest_album_first_when_none_was_pointed_at() -> None:
    """`Recently added` is the rule, and `unit_id` is that date."""
    albums = [{"unit_id": identifier} for identifier in (12, 34, 35, 36)]

    assert _run_sort(albums, []) == [36, 35, 34, 12]


def test_the_album_pointed_at_last_is_the_one_on_top() -> None:
    """The gesture floats an album, and the *latest* gesture floats it highest.

    The index into `pointedAt` is the age of the gesture and not its rank: a drop
    lands at 0, so the album that comes first is the one with the smaller index.
    Subtracting the other way inverts the whole group — the first album dropped
    in the session sits above every one that follows, and each new drop appears
    underneath the last, which is the opposite of what the gesture is for.
    """
    albums = [{"unit_id": identifier} for identifier in (12, 34, 35, 36)]
    # Dropped in the order 12, 34, 35, 36: each drop goes to the
    # front of the list, so the newest gesture is the one at index 0.
    pointed_at = [36, 35, 34, 12]

    ordered = _run_sort(albums, pointed_at)

    assert ordered == [36, 35, 34, 12], "the last one dropped comes first"


def test_an_untouched_album_sits_below_the_ones_pointed_at() -> None:
    """Whatever the sort, a gesture outranks a date — and only for the session."""
    albums = [{"unit_id": identifier} for identifier in (12, 34, 35, 36)]

    ordered = _run_sort(albums, [12])

    assert ordered == [
        12,
        36,
        35,
        34,
    ], "the album pointed at is above every album that was not, and the rest keep the rule"


def test_the_rating_lens_runs_both_ways_and_ends_in_the_same_place() -> None:
    """Reversing the scale reverses the rated albums and moves nothing else.

    The order runs from best to worst and the other way round, and in both the
    unrated albums come after every rated one. So the two lenses are not each
    other's mirror — the unrated tail is the same tail in both, and a mirror
    would bring it to the top of one of them.
    """
    albums = [
        {"unit_id": 1, "rating": 3},
        {"unit_id": 2, "rating": None},
        {"unit_id": 3, "rating": 5},
        {"unit_id": 4, "rating": 1},
    ]

    assert _run_sort(albums, [], "rating_best") == [3, 1, 4, 2]
    assert _run_sort(albums, [], "rating_worst") == [4, 1, 3, 2]


def test_an_album_with_no_stars_is_not_an_album_with_nought_stars() -> None:
    """The unrated follow the rated even when the scale is running upwards.

    Read as a number, `null` is a nought and a nought is below one star — which
    would open `Rating, worst first` on every album that carries no rating at
    all, and bury the rated ones under them.
    """
    albums = [
        {"unit_id": 1, "rating": None},
        {"unit_id": 2, "rating": 1},
        {"unit_id": 3},
    ]

    assert _run_sort(albums, [], "rating_worst") == [2, 3, 1], (
        "the one star comes first, and the two albums with no rating keep the "
        "shelf's own rule between them"
    )


def test_two_albums_rated_the_same_keep_the_shelf_rule() -> None:
    """A lens orders what it can see and hands the rest to the rule."""
    albums = [
        {"unit_id": 10, "rating": 4},
        {"unit_id": 11, "rating": 4},
        {"unit_id": 12, "rating": 4},
    ]

    assert _run_sort(albums, [], "rating_best") == [12, 11, 10]
    assert _run_sort(albums, [], "rating_worst") == [12, 11, 10]


def test_a_pointed_album_does_not_outrank_the_rating_it_does_not_have() -> None:
    """The float applies to the default order and to no other.

    A gesture placed above every lens leaves an album with no rating sitting
    above the five-star records on a shelf sorted by rating. The float keeps
    the order it exists to rescue and gives up every other.
    """
    albums = [
        {"unit_id": 1, "rating": None},
        {"unit_id": 2, "rating": 5},
    ]

    assert _run_sort(albums, [1], "rating_best") == [2, 1], (
        "the lens chosen explicitly decides, and the unrated album just "
        "dropped goes where the rating puts it"
    )


def _run_scannable(albums: list[dict[str, object]], marked: list[int]) -> list[int]:
    """Ask the window's own `scannableMarks` which marks a scan would act on."""
    source = SCRIPT.read_text(encoding="utf-8")
    parts = []
    for name in ("markedOnScreen", "scannableMarks"):
        found = re.search(rf"function {name}\(\) \{{[\s\S]+?\n\}}", source)
        assert found, f"no {name}() to run, so this guard is looking at the wrong file"
        parts.append(found.group(0))
    harness = f"""
    const state = {{ albums: {json.dumps(albums)}, marked: new Set({json.dumps(marked)}) }};
    {parts[0]}
    {parts[1]}
    console.log(JSON.stringify(scannableMarks()));
    """
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return list(json.loads(answer.stdout.strip()))


def test_scan_does_not_count_a_mark_it_has_promised_to_leave_alone() -> None:
    """The mark is on every card, and a run hands an organized album no plan.

    So counting every mark would light `Scan 2 albums` in amber for two
    organized albums, over a gesture guaranteed to change nothing.
    """
    albums = [
        {"unit_id": 1, "organized": True},
        {"unit_id": 2, "organized": True},
        {"unit_id": 3, "organized": False},
    ]

    assert _run_scannable(albums, [1, 2]) == [], "two organized marks are nothing to scan"
    assert _run_scannable(albums, [1, 2, 3]) == [3], "only the one it would act on is counted"


def test_an_album_in_a_state_nobody_has_met_is_offered_to_the_scan() -> None:
    """Written as the complement, so a card carrying a word this window does not
    know yet is scanned rather than silently withheld.

    A set written as a list of known members leaves the member nobody has met
    invisible.
    """
    albums = [{"unit_id": 7}, {"unit_id": 8, "organized": None}]

    assert _run_scannable(albums, [7, 8]) == [7, 8]


def test_an_order_asked_for_in_words_is_not_overruled_by_a_gesture() -> None:
    """The last album dropped must not stay first under every lens.

    An album just pointed at floats because a sort by *date* buries a record
    already on the shelf: the row is old and the gesture is not. Under a lens
    picked explicitly the same float refuses the request — an album with no
    rating sitting above the five-star records on a shelf sorted by rating.

    A default that is usually right must not be what refuses an explicit
    request.
    """
    albums = [
        {"unit_id": 1, "rating": 5},
        {"unit_id": 2, "rating": 1},
        {"unit_id": 3, "rating": None},
    ]

    assert _run_sort(albums, [3], "rating_best") == [1, 2, 3], "the rating order, untouched"
    assert _run_sort(albums, [3], "recent") == [3, 2, 1], "and the float still rescues the default"


def test_the_float_survives_where_it_was_built_to_work() -> None:
    """An album already on the shelf and dropped again returns to the front.

    An old row with a new gesture, on the order that is the shelf's own rule.
    """
    albums = [{"unit_id": identifier} for identifier in (12, 34, 35, 36)]

    assert _run_sort(albums, [12], "recent") == [12, 36, 35, 34]
