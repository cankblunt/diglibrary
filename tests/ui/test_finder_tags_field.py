"""The folder's Finder tags in the plan, run rather than read.

The row draws what the folder carries and what the plan leaves on it, and the
field under it is a keyboard gesture: type, see a suggestion, take it with Tab
or Return, and carry on typing. Every step is valid JavaScript whichever way it
goes, so this runs the real functions over a small element stub and reads what
they drew and what they sent.

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
SCRIPT = WEB / "app.js"
STRINGS = WEB / "strings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

# Elements that keep children, classes and listeners — enough to type into a
# field and read the chips back, and nothing about layout.
STUBS = """
const sent = [];
const make = (tag) => {
  const element = {
    tag, textContent: "", hidden: false, title: "", className: "", value: "",
    placeholder: "", type: "", children: [], listeners: {}, dataset: {}, style: {},
    replaceChildren(...k) { this.children = k; },
    append(...k) { this.children.push(...k); },
    focus() { focused = this; },
    addEventListener(name, run) { (this.listeners[name] ||= []).push(run); },
    fire(name, event = {}) {
      const full = { preventDefault() { this.prevented = true; }, stopPropagation() {}, ...event };
      for (const run of this.listeners[name] || []) run(full);
      return full;
    },
    querySelector(selector) { return find(this, selector)[0] || null; },
    querySelectorAll(selector) { return find(this, selector); },
  };
  element.classList = {
    add: (...names) => {
      const kept = element.className.split(" ").filter(Boolean);
      element.className = [...new Set([...kept, ...names])].join(" ");
    },
    remove: (...names) => {
      const kept = element.className.split(" ").filter((n) => !names.includes(n));
      element.className = kept.join(" ");
    },
    toggle: (name, on) => (on ? element.classList.add(name) : element.classList.remove(name)),
    contains: (name) => element.className.split(" ").includes(name),
  };
  return element;
};
const find = (root, selector) => {
  const name = selector.replace(/^\\./, "");
  const found = [];
  const walk = (node) => {
    for (const child of node.children || []) {
      if (typeof child !== "object") continue;
      if ((child.className || "").split(" ").includes(name)) found.push(child);
      walk(child);
    }
  };
  walk(root);
  return found;
};
const text = (node) =>
  typeof node === "string"
    ? node
    : (node.textContent || "") + (node.children || []).map(text).join("");
let focused = null;
const document = { createElement: (tag) => make(tag) };
const editor = make("table");
const $ = (id) => (id === "plan-editor" ? editor : make(id));
const state = globalThis.EMPTY_LIST ? {
  openAlbum: { unit_id: 9 },
  finderTagList: globalThis.EMPTY_LIST === "unread" ? null : { favourites: [], others: [] },
} : {
  openAlbum: { unit_id: 9 },
  finderTagList: {
    favourites: [{ name: "EX", color: 6 }, { name: "listen", color: 1 }],
    others: [
      { name: "Vimes-Jazz", color: 0 },
      { name: "Vimesbeat", color: 0 },
      { name: "Trevão", color: 0 },
    ],
  },
};
let drawn = null;
const renderAlbumDialog = (album) => { drawn = album; };
const refresh = async () => {};
const toastError = (message) => sent.push({ error: message });
const api = () => ({
  finder_tag_list: async () => ({ ok: true, favourites: [], others: [] }),
  correct: async (...args) => { sent.push(args[5]); return { ok: true, album: {} }; },
});
let correctionInFlight = null;
"""

NAMES = (
    "whileCorrecting",
    "finderTagsNoneExist",
    "loadFinderTagList",
    "foldedTagName",
    "finderChip",
    "finderTagsRow",
    "finderTagOptions",
    "wireFinderField",
    "sendFinderTags",
)


def _strings() -> str:
    source = STRINGS.read_text(encoding="utf-8")
    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", source)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    return found.group(0).replace("export const STR", "const STR", 1)


def _function(source: str, name: str) -> str:
    start = re.search(rf"^(?:async )?function {name}\(", source, flags=re.MULTILINE)
    assert start, f"no {name}() to run, so this guard is reading the wrong file"
    depth = 0
    kept: list[str] = []
    for index, line in enumerate(source[start.start() :].split("\n")):
        kept.append(line)
        depth += line.count("{") - line.count("}")
        if index and depth == 0:
            return "\n".join(kept)
    raise AssertionError(f"{name}() never closes")


def _run(album: dict, offered: list, body: str, *, empty: bool | str = False) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    program = (
        f"globalThis.EMPTY_LIST = {json.dumps(empty)};\n"
        + _strings()
        + STUBS
        + "\n".join(_function(source, name) for name in NAMES)
        + f"\nconst row = finderTagsRow({json.dumps(album)}, {json.dumps(offered)});\n"
        + "const input = row && find(row, '.finder-input')[0];\n"
        + "const menu = row && find(row, '.finder-menu')[0];\n"
        + body
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", f"(async () => {{ {program} }})();"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


NOW = {"finder_tags_now": [{"name": "EX", "color": 6}, {"name": "Funk", "color": 0}]}


def _chips(cell: int, into: str) -> str:
    return f"""
    const {into} = find(row.children[{cell}], '.finder-chip').map((chip) => ({{
      name: find(chip, '.finder-chip-name')[0].textContent,
      cls: chip.className,
      dot: (find(chip, '.finder-dot')[0] || {{}}).className || null,
    }}));
    """


def test_no_row_where_the_folder_cannot_carry_tags() -> None:
    said = _run({"finder_tags_now": None}, [], "console.log(JSON.stringify({ row }));")

    assert said["row"] is None


def test_the_row_draws_both_sides_with_the_finders_colours() -> None:
    offered = [
        {
            "kind": "write_finder_tags",
            "finder_tags": {
                "result": [{"name": "EX", "color": 6}, {"name": "listen", "color": 1}],
                "added": [{"name": "listen", "color": 1}],
                "removed": ["Funk"],
            },
        }
    ]
    said = _run(
        NOW,
        offered,
        _chips(1, "now")
        + _chips(3, "after")
        + "console.log(JSON.stringify({ now, after, arrow: row.children[2].textContent,"
        " unchanged: row.className.includes('is-unchanged') }));",
    )

    assert said["arrow"] == "→" and said["unchanged"] is False
    assert [chip["name"] for chip in said["now"]] == ["EX", "Funk"]
    assert said["now"][0]["dot"] == "finder-dot finder-color-6"
    assert said["now"][1]["dot"] is None, "a tag with no colour draws no dot"
    assert [
        (chip["name"], "is-added" in chip["cls"], "is-removed" in chip["cls"])
        for chip in said["after"]
    ] == [
        ("EX", False, False),
        ("listen", True, False),
        ("Funk", False, True),
    ], "the added tag is marked, and the one taken off stays on screen, struck"


def test_typing_suggests_and_return_takes_the_lit_one() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "vimes";
        input.fire("input");
        const offered = find(menu, '.finder-chip-name').map((n) => n.textContent);
        const enter = input.fire("keydown", { key: "Enter" });
        await correctionInFlight;
        const prevented = !!enter.prevented;
        console.log(JSON.stringify({ offered, sent, prevented, value: input.value }));
        """,
    )

    assert said["offered"] == ["Vimes-Jazz", "Vimesbeat"], "the line to create carries no chip"
    assert said["sent"] == [["EX", "Funk", "Vimes-Jazz"]], "the whole set is sent, the new tag last"
    assert said["prevented"] is True and said["value"] == ""


def test_tab_takes_the_suggestion_after_moving_down() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "VIMES";
        input.fire("input");
        input.fire("keydown", { key: "ArrowDown" });
        input.fire("keydown", { key: "Tab" });
        await correctionInFlight;
        console.log(JSON.stringify({ sent }));
        """,
    )

    assert said["sent"] == [["EX", "Funk", "Vimesbeat"]]


def test_an_accent_is_not_needed_to_find_a_tag() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "treva";
        input.fire("input");
        const offered = find(menu, '.finder-chip-name').map((n) => n.textContent);
        console.log(JSON.stringify({ offered }));
        """,
    )

    assert said["offered"][0] == "Trevão"


def test_return_over_a_name_the_finder_does_not_have_creates_it() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "Quelvo Novo ";
        input.fire("input");
        const lines = find(menu, '.finder-option').map(text);
        const enter = input.fire("keydown", { key: "Enter" });
        await correctionInFlight;
        console.log(JSON.stringify({ lines, sent, enter: !!enter.prevented }));
        """,
    )

    assert said["lines"] == ["+ Create tag “Quelvo Novo”"]
    assert said["sent"] == [["EX", "Funk", "Quelvo Novo"]]
    assert said["enter"] is True


def test_the_new_tag_comes_after_every_match_so_return_takes_the_match() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "vimes";
        input.fire("input");
        const lines = find(menu, '.finder-option').map(text);
        console.log(JSON.stringify({ lines }));
        """,
    )

    assert said["lines"] == ["Vimes-Jazz", "Vimesbeat", "+ Create tag “vimes”"]


def test_no_new_tag_is_offered_for_a_name_that_exists_in_another_case() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "LISTEN";
        input.fire("input");
        const first = find(menu, '.finder-option').map(text);
        input.value = "funk";
        input.fire("input");
        const chosen = find(menu, '.finder-option').map(text);
        console.log(JSON.stringify({ first, chosen }));
        """,
    )

    assert said["first"] == ["listen"]
    assert said["chosen"] == [], "Funk is on the folder already"


def test_an_accent_makes_another_word_so_a_new_tag_is_still_offered() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "trevao";
        input.fire("input");
        const lines = find(menu, '.finder-option').map(text);
        console.log(JSON.stringify({ lines }));
        """,
    )

    assert said["lines"] == ["Trevão", "+ Create tag “trevao”"]


def test_a_mac_with_no_tags_says_what_typing_does() -> None:
    said = _run(
        {"finder_tags_now": []},
        [],
        """
        const open = find(row, '.finder-open')[0];
        open.fire("click");
        const hint = find(menu, '.finder-menu-hint').map(text);
        console.log(JSON.stringify({
          placeholder: input.placeholder, hint, hidden: menu.hidden,
          options: find(menu, '.finder-option').length,
        }));
        """,
        empty=True,
    )

    assert said["placeholder"] == "Type a name to create a Finder tag"
    assert said["hidden"] is False and said["options"] == 0
    assert said["hint"] == ["No Finder tags on this Mac yet. Type a name to create one."]


def test_a_tag_already_chosen_is_not_offered_again() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "e";
        input.fire("input");
        const offered = find(menu, '.finder-chip-name').map((n) => n.textContent);
        console.log(JSON.stringify({ offered }));
        """,
    )

    assert "EX" not in said["offered"]


def test_backspace_in_an_empty_field_takes_the_last_tag_off() -> None:
    said = _run(
        NOW,
        [],
        """
        input.fire("keydown", { key: "Backspace" });
        await correctionInFlight;
        console.log(JSON.stringify({ sent }));
        """,
    )

    assert said["sent"] == [["EX"]]


def test_the_cross_takes_one_tag_off_and_the_arrow_back_restores_one() -> None:
    offered = [
        {
            "kind": "write_finder_tags",
            "finder_tags": {
                "result": [{"name": "EX", "color": 6}],
                "added": [],
                "removed": ["Funk"],
            },
        }
    ]
    said = _run(
        NOW,
        offered,
        """
        const buttons = find(row.children[3], '.finder-chip-button');
        buttons[0].fire("click");
        await correctionInFlight;
        buttons[1].fire("click");
        await correctionInFlight;
        console.log(JSON.stringify({ sent, marks: buttons.map((b) => b.textContent) }));
        """,
    )

    assert said["marks"] == ["✕", "↺"]
    assert said["sent"] == [[], ["EX", "Funk"]]


def test_the_list_opens_with_the_favourites_then_the_rest_and_escape_closes_it() -> None:
    said = _run(
        NOW,
        [],
        """
        const open = find(row, '.finder-open')[0];
        open.fire("click");
        const listed = menu.children.map((child) =>
          child.className === "context-heading" ? `# ${text(child)}` : text(child));
        const escape = input.fire("keydown", { key: "Escape" });
        console.log(JSON.stringify({ listed, hidden: menu.hidden, prevented: !!escape.prevented }));
        """,
    )

    assert said["listed"] == [
        "# Favorites",
        "listen",
        "# All tags",
        "Vimes-Jazz",
        "Vimesbeat",
        "Trevão",
    ], "EX is on the folder already, so the favourites start at listen; no line of keys below"
    assert said["hidden"] is True and said["prevented"] is True


def test_a_list_still_on_its_way_is_not_read_as_a_mac_with_no_tags() -> None:
    said = _run(
        {"finder_tags_now": []},
        [],
        """
        find(row, '.finder-open')[0].fire("click");
        console.log(JSON.stringify({ placeholder: input.placeholder, hidden: menu.hidden }));
        """,
        empty="unread",
    )

    assert said["placeholder"] == "Add a Finder tag"
    assert said["hidden"] is True


def test_the_field_says_so_once_the_list_arrives_empty() -> None:
    """The dialog draws first; the list that answers afterwards rewrites the prompt."""
    said = _run(
        {"finder_tags_now": []},
        [],
        """
        const editorField = make("input");
        editorField.className = "finder-input";
        editorField.placeholder = input.placeholder;
        editor.children.push(editorField);
        const before = editorField.placeholder;
        await loadFinderTagList();
        console.log(JSON.stringify({ before, after: editorField.placeholder }));
        """,
        empty="unread",
    )

    assert said == {"before": "Add a Finder tag", "after": "Type a name to create a Finder tag"}


def test_typed_matches_sit_under_their_headings_and_the_new_name_apart() -> None:
    """A favourite comes first even when another tag starts with the letters."""
    said = _run(
        NOW,
        [],
        """
        input.value = "e";
        input.fire("input");
        const listed = menu.children.map((child) =>
          child.className === "context-heading" ? `# ${text(child)}`
            : child.className === "finder-menu-rule" ? "|" : text(child));
        input.fire("keydown", { key: "Enter" });
        await correctionInFlight;
        console.log(JSON.stringify({ listed, sent }));
        """,
    )

    assert said["listed"] == [
        "# Favorites",
        "listen",
        "# All tags",
        "Vimes-Jazz",
        "Vimesbeat",
        "Trevão",
        "|",
        "+ Create tag “e”",
    ]
    assert said["sent"] == [["EX", "Funk", "listen"]]


def test_a_name_nothing_matches_is_offered_alone_with_no_heading() -> None:
    said = _run(
        NOW,
        [],
        """
        input.value = "Quelvo";
        input.fire("input");
        console.log(JSON.stringify({ listed: menu.children.map(text) }));
        """,
    )

    assert said["listed"] == ["+ Create tag “Quelvo”"]


def test_a_row_with_its_tag_list_open_is_not_dimmed() -> None:
    """The dimming of an unchanged row would carry into the list drawn over the rows below."""
    styles = (WEB / "styles.css").read_text(encoding="utf-8")

    assert ".plan-row.is-unchanged { opacity: 0.82; }" in styles, "the dimming moved; reread this"
    assert ".plan-row.is-unchanged:has(.finder-menu:not([hidden]))" in styles


def test_return_takes_the_first_tag_that_starts_with_the_letters_under_either_heading() -> None:
    """A favourite that only holds the letters is shown first and not taken."""
    said = _run(
        NOW,
        [],
        """
        input.value = "t";
        input.fire("input");
        const listed = menu.children.map(text);
        const lit = find(menu, '.is-lit').map(text);
        input.fire("keydown", { key: "Tab" });
        await correctionInFlight;
        console.log(JSON.stringify({ listed, lit, sent }));
        """,
    )

    assert said["listed"][:2] == ["Favorites", "listen"]
    assert said["lit"] == ["Trevão"]
    assert said["sent"] == [["EX", "Funk", "Trevão"]]
