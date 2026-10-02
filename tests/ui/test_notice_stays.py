"""A notice that is an instruction waits to be read.

A notice that names a folder and asks for something to be done with it is gone
too soon if it is timed like an announcement.

It is not a fault any reading of `app.js` can see: every string is right, the
call is right, and the notice appears. What decides it is a `setTimeout` armed
a few seconds away, three functions from the call — so this runs the real
`showNotice` over a forged DOM and reads the timers that were armed.

The colour and the staying are two decisions. With one flag for both, a green
notice with reading in it has nowhere to go but `toastError`, which would put a
red band on something that worked.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

# Enough DOM for these three functions and not one element more, so a function
# reaching for something the markup does not have fails here loudly rather than
# being invented for it. `armed` is the whole answer: the timers that were set.
HARNESS = """
const armed = [];
globalThis.setTimeout = (fn, ms) => { armed.push(ms); return armed.length; };
globalThis.clearTimeout = () => {};
const node = () => ({
  className: "", textContent: "", type: "", title: "",
  children: [],
  append(...kids) { this.children.push(...kids); },
  prepend(...kids) { this.children.unshift(...kids); },
  addEventListener() {},
  remove() {},
  querySelector() { return { textContent: "" }; },
  matches() { return true; },
  showPopover() {},
});
const toastBox = node();
const $ = (id) => { if (id !== "toast") throw new Error("asked for " + id); return toastBox; };
const document = { createElement: () => node() };
"""


def _armed(call: str) -> list[int]:
    """Run one notice through the real code and return the timers it set."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    strings = (
        (WEB / "strings.js").read_text(encoding="utf-8").replace("export const STR", "const STR")
    )

    wanted = [
        "TOAST_FLOOR_MS",
        "TOAST_CEILING_MS",
        "TOAST_MS_PER_CHARACTER",
        "STAYING_FLOOR_MS",
        "STAYING_CEILING_MS",
        "notices",
    ]
    pieces = [
        found.group(0)
        for name in wanted
        if (found := re.search(rf"^const {name} = .+?;$", script, flags=re.MULTILINE))
    ]
    assert len(pieces) == len(wanted), "the notice state is not where this expects it"

    for name in (
        "showNotice",
        "restartNoticeTimer",
        "toastMilliseconds",
        "dismissNotice",
        "hideToast",
        "toast",
        "toastError",
        "toastRefusal",
        "toastToBeRead",
    ):
        found = re.search(rf"^function {name}\(.*?\n\}}", script, flags=re.MULTILINE | re.DOTALL)
        assert found, f"no {name}() to run, so this guard is reading the wrong file"
        pieces.append(found.group(0))

    body = HARNESS + strings + "\n".join(pieces) + f"\n{call}\nconsole.log(JSON.stringify(armed));"
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", body],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def _length(expression: str) -> int:
    """How many characters one of the window's own messages runs to."""
    strings = (
        (WEB / "strings.js").read_text(encoding="utf-8").replace("export const STR", "const STR")
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", strings + f"\nconsole.log(({expression}).length);"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return int(answer.stdout.strip())


def test_an_announcement_still_goes_away_on_its_own() -> None:
    """The behaviour everything else in the window depends on."""
    assert _armed('toast("scan finished");') == [5000 + len("scan finished") * 67]


def test_a_notice_that_asks_for_a_look_is_given_time_to_be_read() -> None:
    """The icon's answer names a folder and asks for it to be opened.

    It must not be taken away before it is read, and it must not stay until it
    is clicked either: it is timed, on the longer scale.
    """
    armed = _armed('toastToBeRead("Created ~/Applications/DigLibrary.app");')

    assert len(armed) == 1, "it is no longer waiting for a click"
    assert armed[0] == 10000 + len("Created ~/Applications/DigLibrary.app") * 67


def test_a_failure_goes_away_on_its_own_but_is_given_twice_as_long() -> None:
    """A failure is given a limit long enough to read it calmly.

    A failure that stays until dismissed is a red band over the shelf until it
    is closed by hand, every time — so it is timed, on a scale that starts at
    ten seconds where an announcement starts at five.
    """
    short = _armed('toastError("it went wrong");')

    assert short == [10000 + len("it went wrong") * 67], "the longer floor, plus its words"
    assert short[0] > 5000 + len("it went wrong") * 67, "and longer than an announcement earns"


def test_a_longer_sentence_is_given_longer_to_be_read() -> None:
    """A fixed number is the wrong shape for the time a notice stays.

    `Saved.` and a sentence many times its length would be given the same five
    seconds, so one of the two is always wrong — and it is the long one, which
    is the one carrying something worth saying.
    """
    message = 'STR.followedTo("Vimbrel", "Vimbrel")'
    short = _armed('toast("Saved.");')
    longer = _armed(f"toast({message});")

    assert short == [5000 + 6 * 67], "a glance still costs the floor"
    assert _length(message) > 6, "the sentence is no longer than the glance, so this reads nothing"
    assert longer == [5000 + _length(message) * 67], "a sentence earns its own length"
    assert longer[0] > short[0]


def test_the_message_carrying_an_album_name_is_what_the_ceiling_is_for() -> None:
    """A message that carries a name is as long as the name is.

    Rendered with a short stand-in name, no message reaches the ceiling, and
    that is a fact about the stand-in: several timed messages carry a name, a
    folder name of ordinary length runs to dozens of characters, and
    `followedTo` carries two of them.

    This is that message with a folder of ordinary length in it, which is not
    an edge case.
    """
    ordinary = "Zorvane Plitt e Quenby Dramm - Ulmos de Krevant [FLAC]"
    assert len(ordinary) > 40

    armed = _armed(f"toast(STR.followedTo({ordinary!r}, {ordinary!r}));")

    assert armed == [12000], "un-capped it would hold the screen for fifteen seconds"


def test_no_notice_holds_the_screen_however_long_it_is() -> None:
    """And the ceiling holds however far past it a message goes."""
    huge = _armed(f'toast("{"a" * 400}");')

    assert huge == [12000]


def test_the_longest_refusal_earns_twenty_seconds_and_no_more() -> None:
    """Twenty seconds is the ceiling, and it must not become *for ever*.

    A refusal names every problem a plan has, including full paths, so it is the
    longest thing this window writes.
    """
    huge = _armed(f'toastError("{"a" * 400}");')

    assert huge == [20000]


def test_the_same_thing_happening_again_is_timed_by_its_words() -> None:
    """The tally is not part of the reading, and the restart must not shrink.

    The repeat path re-arms from the stored entry. Reading the message back
    out of the DOM would measure whatever the stub returned, and the guard
    would agree with itself instead of with the window.
    """
    twice = _armed('toast("Saved.");\ntoast("Saved.");')

    assert twice == [5000 + 6 * 67, 5000 + 6 * 67], "counted, and timed by the same words"


def test_every_way_this_window_says_something_leaves_on_its_own() -> None:
    """Every notice leaves on its own, after a time that fits its message.

    Written over every entry point rather than one by one, because a fifth added
    later is exactly the one that would be forgotten.
    Two scales and one rate: an announcement starts at five seconds, something
    to be read or acted on starts at ten, and both cost 67 ms a character.

    The one notice that does *not* leave is deliberately not here:
    `window-failure` is the band that says the window never reached the
    application, and that is a state rather than an event — nothing
    arrives later to make it untrue, and it is the only thing on screen saying
    the window is dead.
    """
    for call, floor in (
        ('toast("something happened");', 5000),
        ('toastError("something broke");', 10000),
        ('toastRefusal("a scan is already running");', 5000),
        ('toastToBeRead("go and look in this folder");', 10000),
    ):
        armed = _armed(call)
        assert len(armed) == 1, f"{call} armed {armed}, so it waits for a click"
        assert floor < armed[0] <= 20000, f"{call} earned {armed[0]} ms"
