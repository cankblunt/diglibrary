"""The window's three files have to agree, and nothing at runtime says when they do not.

`api()` reports a bridge method that is missing, because a call that is
not there throws where the name is no longer in hand. Nothing does the same for
the other three ways these files reach across to each other, and each of them
fails the same way — quietly, with the screen simply lacking something:

- `$("some-id")` for an element that is not in the markup returns `null`, and the
  line after it throws inside whatever was drawing.
- `data-str="key"` for a string that is not in `strings.js` writes `undefined`
  into the page, or leaves the English placeholder standing.
- `var(--name)` for a variable nothing defines makes the whole declaration
  invalid at computed-value time, so the property simply has no value. A search
  box asking for `--surface` and `--ink`, which this palette does not have, reads
  as an outline instead of a field, and a heading hover asking for `--ink` does
  nothing at all.

Text, not a browser: these are three files read as text, so the check is
deterministic, offline and instant.
"""

import re
import shutil
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent.parent / "src" / "diglibrary" / "ui" / "web"

MARKUP = (WEB / "index.html").read_text(encoding="utf-8")
SCRIPT = (WEB / "app.js").read_text(encoding="utf-8")
STRINGS = (WEB / "strings.js").read_text(encoding="utf-8")
STYLES = (WEB / "styles.css").read_text(encoding="utf-8")


def _element_ids() -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', MARKUP))


def _string_keys() -> list[str]:
    """Every key declared at the top level of the `STR` literal, in order.

    Two forms are written in that file and both count: `key:` for a value or an
    arrow function, and `key(args) {` for a method — `sourceName` and
    `viewSource` are methods, because they read the tables beside them. Read at
    one indent, so nested object literals are values rather than keys.
    """
    return re.findall(
        r"^  ([A-Za-z_][A-Za-z0-9_]*)\s*(?::|\([^)]*\)\s*\{)", STRINGS, flags=re.MULTILINE
    )


def test_every_element_the_script_asks_for_is_in_the_markup() -> None:
    """`$("id")` for an element that is not there returns null and throws one line later."""
    wanted = set(re.findall(r'\$\("([^"]+)"\)', SCRIPT))
    missing = sorted(wanted - _element_ids())
    assert missing == [], f"app.js asks for elements the markup does not have: {missing}"


def test_every_string_the_markup_asks_for_is_declared() -> None:
    """A `data-str` with no entry writes `undefined` into the window."""
    keys = set(_string_keys())
    wanted = set(re.findall(r'data-str(?:-title)?="([^"]+)"', MARKUP))
    missing = sorted(wanted - keys)
    assert missing == [], f"index.html names strings that strings.js does not declare: {missing}"


def test_every_string_the_script_asks_for_is_declared() -> None:
    """`STR.name` for an absent entry is `undefined`, which renders as the word."""
    keys = set(_string_keys())
    wanted = set(re.findall(r"\bSTR\.([A-Za-z_][A-Za-z0-9_]*)", SCRIPT))
    missing = sorted(wanted - keys)
    assert missing == [], f"app.js reads strings that strings.js does not declare: {missing}"


def test_no_string_is_declared_twice() -> None:
    """A duplicate key in one object literal is a silent overwrite: the later wins.

    With two entries of one name, a Transfers row reads the search screen's
    sentence, because the second replaces the first.
    """
    keys = _string_keys()
    twice = sorted({key for key in keys if keys.count(key) > 1})
    assert twice == [], f"strings.js declares these more than once: {twice}"


def test_no_function_in_the_script_is_declared_twice() -> None:
    """Two top-level functions of one name are a SyntaxError, and the window dies.

    `app.js` is loaded as a module, and a module is strict: redeclaring a
    top-level function does not overwrite the first one as it would in a plain
    script — it refuses to parse, so nothing runs, no listener is registered and
    the window draws *nothing*.

    Read as text rather than parsed, for the same reason the rest of this file
    is: it must run offline, in milliseconds, without Node.
    """
    declared = re.findall(r"^(?:async )?function ([A-Za-z_$][\w$]*)\s*\(", SCRIPT, re.MULTILINE)
    twice = sorted({name for name in declared if declared.count(name) > 1})
    assert twice == [], f"app.js declares these functions more than once: {twice}"


def test_every_custom_property_the_stylesheet_reads_is_defined() -> None:
    """An undefined `var()` leaves the property with no value at all.

    A `var(--name, fallback)` is fine — the fallback is the answer — so only the
    bare ones are checked. Properties the window writes at runtime count as
    defined: the chrome's measured height and the search columns are set from
    `app.js` and never appear in the stylesheet.
    """
    # Comments are stripped first, because the comment explaining this very check
    # names the two properties that failed it — and a check that its own
    # explanation trips is a check nobody will keep.
    styles = re.sub(r"/\*.*?\*/", "", STYLES, flags=re.DOTALL)
    defined = set(re.findall(r"(--[A-Za-z0-9-]+)\s*:", styles))
    defined |= set(re.findall(r'setProperty\(\s*"(--[A-Za-z0-9-]+)"', SCRIPT))
    read = set(re.findall(r"var\(\s*(--[A-Za-z0-9-]+)\s*\)", styles))
    missing = sorted(read - defined)
    assert missing == [], f"styles.css reads custom properties nothing defines: {missing}"


def test_every_bridge_method_the_script_calls_exists_on_the_api() -> None:
    """`api().whatever()` for a method that is not there is caught at runtime, not before.

    The proxy reports it, where the call would otherwise throw one line after
    the name is out of hand. But reporting it still means finding out
    crossing as the other three, read as text: every `api().name` in `app.js`
    against the public methods of ``LibraryApi``, which is what pywebview exposes.
    """
    from diglibrary.application.api import LibraryApi

    exposed = {
        name
        for name in dir(LibraryApi)
        if not name.startswith("_") and callable(getattr(LibraryApi, name))
    }
    # Whitespace between the two, because a call that wraps is still a call:
    # `collectDownloads` writes `api()` on one line and `.acquisition_collect()`
    # on the next, and a pattern that demands them adjacent reads this file as
    # having no such call at all.
    wanted = set(re.findall(r"\bapi\(\)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", SCRIPT))
    assert "acquisition_collect" in wanted, "the pattern must see a call that wraps a line"
    missing = sorted(wanted - exposed)
    assert missing == [], f"app.js calls bridge methods LibraryApi does not have: {missing}"


def test_the_window_files_hold_no_stray_control_characters() -> None:
    """A NUL in JavaScript is a character like any other, and it reads as a space.

    In a key builder such as `` `${path} ${file}` `` a NUL where the space
    belongs is drawn as a space by every terminal and editor, and the code
    *works* — the write and the read go through the same function, so the keys
    agree with each other and with nothing else, until a key is typed by hand.

    Python refuses a NUL in source outright, so `.py` files guard themselves.
    These four do not: JavaScript, CSS and HTML accept it silently, which is the
    same failure mode as the other checks in this file.
    """
    allowed = {"\n", "\t"}
    for name, text in (
        ("index.html", MARKUP),
        ("app.js", SCRIPT),
        ("strings.js", STRINGS),
        ("styles.css", STYLES),
    ):
        found = sorted(
            {
                f"U+{ord(character):04X} on line {text[:index].count(chr(10)) + 1}"
                for index, character in enumerate(text)
                if character.isprintable() is False and character not in allowed
            }
        )
        assert found == [], f"{name} holds control characters that read as blanks: {found}"


def test_no_dialog_is_shown_by_a_rule_that_forgot_it_could_be_closed() -> None:
    """A `display` on a dialog outranks the browser's own way of hiding it.

    Every closed `<dialog>` is hidden by the user agent's
    `dialog:not([open]) { display: none }`, which is a plain element selector —
    so an id or a class beats it, and the moment a rule here declares `display`
    without saying `[open]`, that dialog is on screen always.

    `#album-dialog { display: flex }`, written to let the dialog lay itself out
    in a column, draws the whole album panel into the middle of whichever
    screen is in front. Nothing fails — the window simply has a second, empty
    album dialog in it.
    """
    stylesheet = (WEB / "styles.css").read_text(encoding="utf-8")
    offenders: list[str] = []
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", stylesheet):
        selector = " ".join(rule.group(1).split())
        block = rule.group(2)
        if "backdrop" in selector or "display" not in block:
            continue
        # Only the dialog element itself: its head, body and actions are
        # ordinary boxes inside one, and hiding those is not this trap.
        parts = [part.strip() for part in selector.split(",")]
        for part in parts:
            last = part.split()[-1] if part.split() else ""
            names_a_dialog = last == "dialog" or (
                "dialog" in last and not re.search(r"dialog[-_][a-z]", last)
            )
            if names_a_dialog and "[open]" not in part and ":not([open])" not in part:
                offenders.append(f"{part} {{{block.strip()}}}")
    assert not offenders, (
        "a dialog with a `display` rule that does not say `[open]` is a dialog "
        f"drawn while closed: {offenders}"
    )


def _classes_worn_by_table_cells() -> dict[str, str]:
    """Every class this window puts on a `<td>` or a `<th>`, and where it does.

    Both places cells are made: written in the markup, and built by the script,
    where the class lands on the line or two after `createElement("td")`.
    """
    worn: dict[str, str] = {}
    for match in re.finditer(r"<(td|th)\b[^>]*\bclass=\"([^\"]+)\"", MARKUP):
        for name in match.group(2).split():
            worn.setdefault(name, f"index.html <{match.group(1)}>")
    for match in re.finditer(r"(\w+)\s*=\s*document\.createElement\(\"(td|th)\"\)", SCRIPT):
        variable = match.group(1)
        window = SCRIPT[match.end() : match.end() + 400]
        for assignment in re.finditer(
            rf"\b{re.escape(variable)}\.(?:className\s*=\s*\"([^\"]*)\""
            rf"|classList\.add\(\"([^\"]*)\"\))",
            window,
        ):
            for name in (assignment.group(1) or assignment.group(2) or "").split():
                worn.setdefault(name, f'app.js {variable} = createElement("{match.group(2)}")')
    return worn


def test_no_class_on_a_table_cell_stops_it_being_one() -> None:
    """`display: flex` on a `<td>` takes the cell out of its own row.

    It beats `display: table-cell`, so the table wraps the element in an
    anonymous cell that stretches to the row's height while the flex box inside
    is only as tall as its content — and the row's `border-bottom` is painted on
    the flex box, not on the cell. The column's separators end up above every
    other column's, which is a ragged table and nothing else: no error, no
    warning, and every screenshot of a full row looks nearly right.

    Measured in the window's own engine on a findings cell that was `display:
    flex` for the sake of a 4px `gap`: a few pixels above the rest of the row
    with chips in it, and more on an album with no findings, where the box holds
    only its padding.

    `none` is allowed — hiding a column is a thing this window does on purpose —
    and so are the keywords that put the value back.
    """
    kept = {"table-cell", "none", "inherit", "initial", "revert", "revert-layer", "unset"}
    worn = _classes_worn_by_table_cells()
    offenders: list[str] = []
    # Comments out first: this file explains itself at length, and a paragraph
    # sitting in front of a rule would be read as part of its selector.
    stylesheet = re.sub(r"/\*.*?\*/", " ", STYLES, flags=re.DOTALL)
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", stylesheet):
        selector = " ".join(rule.group(1).split())
        declared = re.search(r"(?:^|;)\s*display\s*:\s*([a-z-]+)", rule.group(2))
        if declared is None or declared.group(1) in kept:
            continue
        for part in (piece.strip() for piece in selector.split(",")):
            last = part.split()[-1] if part.split() else ""
            for name in re.findall(r"\.([A-Za-z0-9_-]+)", last):
                if name in worn:
                    offenders.append(
                        f"{part} {{ display: {declared.group(1)} }} — "
                        f".{name} is worn by a cell ({worn[name]})"
                    )
    assert not offenders, (
        "a class on a table cell must not change its `display`, or the cell "
        f"leaves the row and its border floats: {offenders}"
    )


BROWSER_GLOBALS = frozenset(
    {
        # Everything the window is given rather than defines. Deliberately a
        # closed list: a name that is neither defined here nor on it is exactly
        # what this test is for.
        "Array",
        "Boolean",
        "Date",
        "Error",
        "Image",
        "Intl",
        "JSON",
        "Map",
        "Math",
        "Number",
        "Object",
        "Promise",
        "RegExp",
        "Set",
        "String",
        "URL",
        "WeakMap",
        "alert",
        "atob",
        "btoa",
        "clearInterval",
        "clearTimeout",
        "confirm",
        "decodeURIComponent",
        "encodeURIComponent",
        "fetch",
        "isNaN",
        "parseFloat",
        "parseInt",
        "prompt",
        "queueMicrotask",
        "requestAnimationFrame",
        "setInterval",
        "setTimeout",
        "structuredClone",
        "IntersectionObserver",
        "MutationObserver",
        "ResizeObserver",
        "AbortController",
        "Proxy",
        "Reflect",
        "async",
        "minmax",
        # Statements and operators that a bare regexp reads as calls.
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "typeof",
        "function",
        "await",
        "new",
        "delete",
        "void",
        "in",
        "of",
        "else",
        "do",
        "yield",
        "case",
    }
)


def test_every_function_the_script_calls_is_one_it_has() -> None:
    """A sixth way the window fails, and it fails by doing nothing at all.

    A call in a click handler to a function this module does not have is caught
    by nothing: the file parses, the module loads, the screen draws, and the
    button throws `ReferenceError` the first time it is pressed — which is the
    one moment nobody is looking at a console. The other crossings guarded here
    are all about reaching *out* of the module; this one is the module failing
    to reach itself.

    Read as text, like the others, and against a closed list of what the browser
    supplies: a name that is neither defined in the file nor on that list is the
    defect this exists to name.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.DOTALL)
    defined = set(re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", script))
    # `const name = (…) =>` and `const name = async (…) =>` are functions too.
    defined |= set(
        re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[({]", script)
    )
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*[A-Za-z_$]", script))
    # A function's own parameters are names it has: `withSpinner(node, run)`
    # calls `run()`, and an object method's shorthand — `get(target, name)` —
    # declares one the same way.
    for params in re.findall(
        r"(?:function\s*[A-Za-z_$\w]*|\b[A-Za-z_$][\w$]*)\s*\(([^()]*)\)\s*(?:=>|{)", script
    ):
        for part in params.split(","):
            name = part.split("=")[0].strip().lstrip(".").strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                defined.add(name)
    # `new Promise((decide) => …)` names its own resolver.
    defined |= set(re.findall(r"\(\s*\(?\s*([A-Za-z_$][\w$]*)\s*\)?\s*=>", script))
    # An object literal's shorthand method — `get(target, name) {` — declares a
    # name the same way a `function` statement does.
    defined |= set(re.findall(r"(?m)^\s*([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*{", script))
    # And a name taken out of an object is a name this file has: `const { read }
    # = ACQUIRE_SORTS[…]` is how the acquisition sorts are reached.
    for names in re.findall(r"(?:const|let|var)\s*{([^}]*)}\s*=", script):
        for part in names.split(","):
            name = part.split(":")[-1].split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                defined.add(name)
    # A call is a bare name followed by `(`, never one reached through a dot.
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", script))
    missing = sorted(called - defined - BROWSER_GLOBALS)
    assert missing == [], f"app.js calls functions it does not have: {missing}"


def test_the_sentence_for_an_album_found_already_right_cannot_reach_a_blocked_one() -> None:
    """An empty plan means two opposite things, and the screen must say which.

    A blocked album has no operations because the planner refused to make any
    (`_alignment_blockers`). Without consulting the blockers, the fold under the
    red line prints the sentence of an album that *was* compared and found
    correct — "Nothing here would change — every name below already reads as it
    should" — three lines from the refusal, over a table offering an `AFTER
    APPLYING` column for a plan that cannot be applied.

    Read as text, like the rest of this file: whatever expression feeds
    `plan-note` and `plan-summary` has to consult the blockers, because nothing
    at runtime says when it stopped.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    for element in ("plan-note", "plan-summary"):
        written = re.search(
            rf'\$\("{element}"\)\.textContent\s*=\s*(.+?);\n', script, flags=re.DOTALL
        )
        assert written, f"nothing writes {element}, so this guard is reading the wrong file"
        assert "blocked" in written.group(1), (
            f"the text of {element} does not consult the blockers, so an album that "
            f"could not be planned reads as one that needs no change: {written.group(1)!r}"
        )


def test_the_fold_does_not_announce_another_album_over_a_pair_inside_one() -> None:
    """An album can be its own pair, and a fixed heading says otherwise.

    A fold opening to `Also in another album` above a body reading `the same
    audio, in this same folder` states two things that cannot both be true. The
    pair carries `same_album` precisely so this can be said by case.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    written = re.search(
        r'\$\("shared-summary"\)\.textContent\s*=\s*(.+?);\n', script, flags=re.DOTALL
    )
    assert written, "nothing writes shared-summary, so this guard is reading the wrong file"
    assert "same_album" in written.group(1), (
        "the fold's heading does not consult `same_album`, so an album paired "
        f"with itself is announced as another album: {written.group(1)!r}"
    )


def test_the_scan_button_is_drawn_by_one_function() -> None:
    """A label that outlives the thing it counts is a button that refuses its own press.

    `renderMarkCount` draws the Scan button and decides whether it can be
    pressed. Called from only some of the places that change the marks, a scan
    finishing, a restore, an apply, a forgotten album and a dropped folder each
    leave the old words standing: `Scan 1 marked` over an empty set, followed
    by a notice that nothing is marked.

    Read as text: nothing may write that button's `disabled` except the one
    function that owns it, and `refresh` — the path every one of those events
    ends in — has to call it.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    rivals = re.findall(r'\$\("scan"\)\.disabled\s*=\s*([^;]+);', script)
    outside = [line for line in rivals if "count" not in line]
    assert outside == [], (
        "something other than renderMarkCount decides whether Scan can be "
        f"pressed, which is how its label and its gesture came apart: {outside}"
    )
    refresh = re.search(r"async function refresh\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert refresh, "no refresh() to read, so this guard is looking at the wrong file"
    assert "renderMarkCount()" in refresh.group(
        1
    ), "refresh() redraws the shelf without redrawing the button that counts it"


def test_every_transfer_that_is_over_can_be_taken_off_the_list() -> None:
    """A row nothing can remove stays on the screen forever.

    An album already cancelled must not offer `Cancel download`, which there
    reads as doing it again, and no state may be left without an entry: a slskd
    wording this window has no word for maps to `unknown`, which belongs to no
    list of terminal states a menu could be built from.

    Read as text: the removal must be defined as *not moving*, so that every
    state there is or ever will be falls into it.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    body = re.search(r"function transferActions\(.+?\n}", script, flags=re.DOTALL)
    assert body, "no transferActions() to read, so this guard is looking at the wrong file"
    text = body.group(1) if body.groups() else body.group(0)
    assert re.search(r"const done = files\.filter\(\(file\) => !\[", text), (
        "what can be removed is written as a list of terminal states, so a state "
        "this window does not know about can never be taken off the screen"
    )
    assert "STR.menuRemoveTransfer(done.length)" in text, (
        "the entry that removes a finished transfer has to say so; `Cancel` over "
        "something already cancelled is a different sentence"
    )


def test_the_shelf_is_ordered_in_one_place_and_the_picker_names_it() -> None:
    """One order, one function, and every option in the markup has a rule.

    The rule: recently added, last in first seen. The picker offers
    lenses on the same shelf, and an option the sorter does not know would be a
    control that changes nothing — which in this window is indistinguishable
    from a control that is broken.

    Read as text: the values in the `<select>` against the keys the sorter has,
    both ways, and the shelf drawn through the one function that orders it.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    picker = re.search(r'<select id="library-sort".*?</select>', MARKUP, flags=re.DOTALL)
    assert picker, "no order picker in the markup"
    offered = set(re.findall(r'<option value="([^"]+)"', picker.group(0)))
    body = re.search(r"function sortAlbums\(albums\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert body, "no sortAlbums() to read, so this guard is looking at the wrong file"
    # `[a-z_]` and not `[a-z]`: a lens whose name needs two words — `rating_best`
    # — is invisible to `[a-z]+`, so the picker would offer an order the sorter
    # reads past.
    known = set(re.findall(r"^\s{4}([a-z_]+):", body.group(1), flags=re.MULTILINE))
    known.add("recent")
    assert offered - known == set(), f"the picker offers orders nothing sorts by: {offered - known}"
    assert known - offered == set(), f"the sorter knows orders nothing offers: {known - offered}"
    visible = re.search(r"function visibleAlbums\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert visible and "sortAlbums(" in visible.group(1), (
        "what is on screen is not put through the one function that orders it, "
        "so the grid and the list can disagree about the same shelf"
    )


def test_a_name_too_long_to_be_drawn_can_still_be_read() -> None:
    """Both views cut their text off, so both have to answer on hover.

    A card is a fixed width and a column is a share of one, and the longest
    names are exactly the ones the ellipsis eats.

    Read as text, and on *both* builders, because the grid and the list are two
    functions and a fix applied to one of them leaves the other wrong. The
    inner name line must not carry a `title` of its own either: an inner tooltip
    wins over the one on the card, so the very place hovered would answer with
    something narrower.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    for builder in ("function card\\(album\\)", "function listRow\\(album\\)"):
        body = re.search(rf"{builder}\s*{{(.+?)\n}}", script, flags=re.DOTALL)
        assert body, f"no {builder} to read, so this guard is looking at the wrong file"
        assert "fullNameFor(album)" in body.group(1), (
            f"{builder} draws an album whose name it may be cutting off, and "
            "nothing on it says what the whole name is"
        )
        assert ".title = album.folder" not in body.group(1), (
            "an inner element carrying its own tooltip shadows the one that " "holds the whole name"
        )


def test_the_library_search_reaches_the_albums_on_screen() -> None:
    """The search box is only a box until something filters by it.

    `visibleAlbums` is the one place that decides which albums are drawn — the
    shelf and the status already cross there — so a search that did not is a
    field that types and does nothing.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    visible = re.search(r"function visibleAlbums\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert visible, "no visibleAlbums() to read"
    assert "matchesSearch" in visible.group(1), (
        "what is on screen does not consult the search box, so typing in it " "filters nothing"
    )


def test_planning_an_album_again_says_something() -> None:
    """A gesture that answers invisibly is a gesture pressed again.

    `Plan this album again` redraws a dialog that is identical when the answer
    is "nothing would change", which is indistinguishable from a dead button.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    body = re.search(r"async function planAnyway\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert body, "no planAnyway() to read"
    assert "toast(" in body.group(1), (
        "planning again reports nothing to the screen, so an album already right "
        "reads as a button that does not work"
    )


def test_the_filled_button_is_never_given_to_a_hidden_one() -> None:
    """Exactly one filled button is on screen, always.

    The amber moves to `Scan this album again` where no catalogue has answered.
    That button is hidden for an album this app has finished with and for one
    it has never looked at — so without asking whether it is on the row, an
    album arranged by its own tags gets no filled button anywhere, and nothing
    on the row says what comes next.

    Read as text, and only as far as text can answer: the two toggles have to
    be decided together, from one block that has asked whether the scan is on
    the row at all. **Which button each state lights is measured by running
    it** — `tests/ui/test_filled_button.py` walks every combination of the three
    facts this block reads. That guard is skipped where `node` is absent, which
    is why the wiring is still asserted here: an expression naming the
    condition is what this can see.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    block = re.search(
        r"const noSourceAsked[\s\S]+?\$\(\"album-scan-now\"\)\.classList\.toggle\([^;]+;",
        script,
    )
    assert block, "nothing decides which of the two buttons is the filled one"
    assert 'album-scan-now").hidden' in block.group(0), (
        "the decision never asks whether the scan button is even on the row, "
        "which is how the album ended with no filled button at all"
    )
    for control in ("album-approve", "album-scan-now"):
        assert f'$("{control}").classList.toggle(' in block.group(0), (
            f"{control} is filled somewhere other than the block that decides "
            "both, so the two can disagree about which is the invitation"
        )


def test_nothing_is_written_from_a_plan_a_scan_is_replacing() -> None:
    """A plan that a scan is replacing cannot be approved or rejected.

    `Scan this album again` leaves the dialog open, and for those seconds the
    footer is offering a reading this application has already decided to redo —
    approving it would rename files from it.

    Read as text because the state it depends on is set on another thread of the
    window's own event loop, and what has to be true is that both writing
    gestures consult it.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    for control in ("album-approve", "album-reject"):
        rule = re.search(rf'\$\("{control}"\)\.disabled\s*=\s*([^;]+);', script)
        assert rule, f"no rule saying when {control} is closed"
        said = rule.group(1)
        # A rule shared with the gesture it closes is written once, in a function
        # both of them call; it is read there.
        helper = re.fullmatch(r"\s*(\w+)\(album\)\s*", said)
        if helper:
            body = re.search(
                rf"^function {helper.group(1)}\(album\) \{{.*?\n\}}", script, re.M | re.S
            )
            assert body, f"{control} is closed by {helper.group(1)}(), which is not in the script"
            said = body.group(0)
        assert (
            "beingLookedUp" in said or "state.identifying.has(album.unit_id)" in said
        ), f"{control} stays live while the album under it is being looked up again"
    # And `Reject` is closed over a finished album, where the API refuses it:
    # a control that always fails is worse than an absent one.
    # And the gesture that starts a scan says the scan is running, on the dialog
    # it was pressed in: leaving the only sign on the shelf's progress bar would
    # answer a second press with a refusal.
    running = re.search(r"scanning\.disabled\s*=\s*([^;]+);", script)
    assert running and "beingLookedUp" in running.group(
        1
    ), "the scan button stays live while the scan it started is running"
    assert "STR.scanningThisAlbum" in script, "and it never says so in words"

    rule = re.search(r'\$\("album-reject"\)\.disabled\s*=\s*([^;]+);', script)
    assert rule and "album.organized_on_record" in rule.group(1), (
        "Reject is offered on an organized album, where it would write `skipped` "
        "over the word that makes a scan leave the album alone"
    )


def test_every_way_an_album_arrives_marks_it() -> None:
    """Albums brought to the shelf arrive already marked, by every route.

    A shelf may not have some cards checked and others not, which is the state
    produced by marking only the albums the tags could not settle. So the rule
    is not *some arrivals*: it is **every** arrival, and this reads the three
    branches by name rather than counting them.

    A guard written by absence is the hard kind to keep: one that looks for
    `state.marked.add` on the same line as `event.payload` passes unchanged
    when the marking moves one function away. This one asks each arrival what
    it does, which is a question a rewrite cannot answer by accident.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    marker = re.search(r"function markArrivals\(unitIds\)\s*\{(.+?)\n\}", script, flags=re.DOTALL)
    assert marker, "no markArrivals() for an arrival to call"
    assert "state.marked.add" in marker.group(1), "markArrivals marks nothing"
    for arrival, carries in (
        ("opened", "event.payload.albums"),
        ("adopted", "event.payload.unit_ids"),
        ("collected", "event.payload.unit_ids"),
    ):
        branch = re.search(
            rf'event\.type === "{arrival}"\) \{{(.+?)\}} else if',
            script,
            flags=re.DOTALL,
        )
        assert branch, f"no {arrival} branch to read"
        assert f"markArrivals({carries})" in branch.group(1), (
            f"albums arriving by {arrival} are not marked, so the shelf has some "
            "cards checked and others not"
        )
    # Coming back from a restart is not an arrival: nothing was pointed at, so
    # the first sight of the app has nothing marked.
    restored = re.search(r'event\.type === "restored"\) \{(.+?)\} else if', script, flags=re.DOTALL)
    assert restored, "no restored branch to read"
    assert "state.marked.clear()" in restored.group(
        1
    ), "a restart marks the whole shelf, which is not an arrival and not a gesture"


def test_the_two_proposals_are_not_allowed_to_look_alike() -> None:
    """Where the difference between the two proposals is said, and where it is not.

    An arrangement asked no catalogue, so from the shelf the album is what it
    was — waiting to be scanned — and that is the badge it wears. A badge of
    its own there adds little and reads as a warning, especially in amber. The
    distinction is made where there is room to explain it: the dialog's header
    through `sourceName`, and this badge's own tooltip.
    """
    assert '"your tags"' in STRINGS, "the header has no words for the source that is not a site"
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    badge = re.search(r"function badgeFor\(album\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert badge, "no badgeFor() to read, so this guard is looking at the wrong file"
    assert 'album.source === "tags"' in badge.group(1), (
        "the shelf's one badge never asks which proposal this is, so nothing can "
        "keep an arrangement out of the words a catalogue's answer earns"
    )
    assert "STR.tipFromTags" in badge.group(1), (
        "the arrangement's card explains itself nowhere — the words left the "
        "badge and did not arrive in the tooltip"
    )
    assert (
        "badge-tags" not in SCRIPT and "badge-tags" not in STYLES
    ), "the amber tags badge is back on the shelf, where it reads as a fault"
    # Said *instead of* `review`, not beside it: the card's foot is one row of
    # items, and a fourth that cannot wrap is clipped by the card's own
    # overflow. The row may wrap, and it still may not carry a badge nothing
    # chose.
    assert (
        "tags-badge" not in SCRIPT and "tags-badge" not in STYLES
    ), "the second badge is back, which is the thing that overflowed"
    foot = re.search(r"\.card-foot\s*{(.+?)}", STYLES, flags=re.DOTALL)
    assert foot and "flex-wrap: wrap" in foot.group(1), (
        "the card's foot cannot give, so anything too wide for it is cut off " "rather than wrapped"
    )


def test_the_frame_carries_nothing_a_tab_has_to_answer_for() -> None:
    """A control in the window's frame is a promise made on every screen.

    Library controls carried in the frame — the chosen path, `Choose folder…`,
    `Scan` and the arrangement of the marked — need a function only to hide
    them again on every other tab. On Find music they are a second row of
    instructions about somewhere else, and the frame needs a render pass to stay
    true. They live in the Library's own bar, where the screen they belong to
    hides them by being hidden itself.

    Read as text, and as the complement: not "these four ids moved",
    but *no id declared inside the top bar may have its visibility written by a
    rule that reads which tab is in front*. Whatever control someone parks in
    the frame next, if a tab has to answer for it, this fails.
    """
    header = re.search(r'<header class="topbar">(.+?)</header>', MARKUP, flags=re.DOTALL)
    assert header, "no topbar in the markup, so this guard is looking at the wrong file"
    framed = set(re.findall(r'\bid="([^"]+)"', header.group(1)))
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    tab_rules = [
        element
        for element in framed
        for statement in re.findall(rf'\$\("{re.escape(element)}"\)\.hidden\s*=\s*[^;]+;', script)
        if "state.tab" in statement or "onLibrary" in statement
    ]
    assert tab_rules == [], (
        "the frame is carrying controls a tab has to hide again, which is the "
        f"shape that puts Library buttons over every other screen: {tab_rules}"
    )


def test_the_dialog_footer_answers_only_the_dialogs_question() -> None:
    """The dialog's footer holds the decisions about the plan and nothing else.

    The context gestures live beside the ✕ in the head, the ways back in live
    behind the footer's own ⋯, and applying part of an album through a button
    of its own is not offered — a plan that only partly matches shows its
    blockers instead of a smaller offer.

    A whitelist on purpose, and fail-closed: another button parked here fails
    this test and forces the decision, instead of quietly rebuilding a toolbar
    in the footer.
    """
    dialog = re.search(r'<dialog id="album-dialog".*?</dialog>', MARKUP, flags=re.DOTALL)
    assert dialog, "no album dialog in the markup"
    footer = re.search(
        r'<footer class="dialog-actions">(.*?)</footer>', dialog.group(0), flags=re.DOTALL
    )
    assert footer, "the album dialog has no footer to hold its decisions"
    ids = set(re.findall(r'\bid="([^"]+)"', footer.group(1)))
    assert ids == {
        "album-more",
        # Asking for a plan to exist is the decision that comes before the two
        # that answer it.
        "album-scan-now",
        # Its other half, shown on organized albums and only on those. The two
        # are hidden by the same condition written once, so exactly one of them
        # is ever drawn — an album this app has finished with has nothing to
        # scan for, and one it has not has nothing to plan again. It is not in
        # the ⋯ as well, so the gesture has one home.
        "album-plan-again",
        # `Send to Quality` is in the footer and `Send to review` behind the ⋯:
        # the first is the more important of the two.
        "album-to-bench",
        # Shown on an album in review and nowhere else, so it is not a button
        # parked here for every album: where an identification matched none of
        # the names and nothing was planned, the one gesture that answers it
        # must not be behind the footer's own menu.
        "album-arrange-tags",
        # The way back from the last gesture. It belongs in the decision row
        # rather than behind the ⋯ because it is what an album re-planned or
        # re-scanned by mistake is waiting for, and a gesture behind the ⋯ is
        # hard to find. The row's rule is kept all the same: hidden unless
        # there is a gesture to take back.
        "album-cancel",
        "album-reject",
        "album-approve",
    }, (
        "the footer carries something beside the ⋯ and the three decisions "
        f"about the plan: {sorted(ids)}"
    )
    assert '"Send to review"' in STRINGS and '"Send back to review"' not in STRINGS, (
        'the hold button says "back" again, which claims a history an automatic '
        "album does not have"
    )
    # There is no *second button* for applying part of an album, and the
    # ability to apply an album that only partly paired up stays: without it,
    # albums in review stay there for ever. So: no second button, and Approve
    # reaches the partial plan.
    for name in ("album-approve-partial", "approvePartial"):
        assert name not in MARKUP and name not in SCRIPT, f"a second apply button is back as {name}"
    assert "partial_applicable" in SCRIPT, (
        "nothing reads whether the partial plan is applicable, so an album whose "
        "files did not all pair up cannot be applied at all and stays in review"
    )
    assert re.search(
        r"api\(\)\.approve\([^)]*partOnly\)", SCRIPT
    ), "Approve always sends the whole plan, so the partial one is unreachable"


def test_a_partial_plan_on_offer_is_not_announced_as_blocked() -> None:
    """The head must not say "Nothing was planned" over a column of planned names.

    An album whose partial plan is the one on offer demotes the full plan's
    blockers to warnings, shows the partial renames on the right and lights
    Approve. If `blocked` reads the raw blockers, the summary prints "Nothing
    was planned" and the note says nothing below will be written while the
    button offers `Approve & apply 19 of 19`. Blocked has to mean *nothing is
    on offer*: the raw blockers alone cannot decide it.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    # Anchored on `album.blockers`: a second, unrelated `blocked` exists in
    # another function, and this guard must read the dialog's one.
    written = re.search(r"const blocked = (.*album\.blockers.*?);", script)
    assert written, "nothing computes the dialog's `blocked`, so this is reading the wrong file"
    assert "offeringPart" in written.group(1), (
        "`blocked` reads the raw blockers alone, so an album whose partial plan "
        f"is on offer is announced as not planned: {written.group(1)!r}"
    )
    # The two "asked again" toasts have the same duty: a re-plan that lands
    # on a partial-applicable album must not say "could not be planned" over a
    # lit Approve button.
    for caller in ("planAnyway", "arrangeThisAlbum"):
        body = re.search(
            rf"async function {caller}\([^)]*\)\s*{{(.+?)\n}}", script, flags=re.DOTALL
        )
        assert body, f"no {caller}() to read"
        assert "plannedToast(" in body.group(1), (
            f"{caller} does not answer through plannedToast, so a partial album "
            "reads as one that could not be planned"
        )
    toaster = re.search(r"function plannedToast\(album\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert toaster, "no plannedToast() to read"
    assert "partial_applicable" in toaster.group(1) and "partial_operations" in toaster.group(1), (
        "plannedToast does not consult the partial plan, so its count and its "
        "blocked verdict are about a plan Approve would not run"
    )


def test_every_gesture_the_dialogs_footer_gave_up_still_has_a_home() -> None:
    """Every gesture that is not in the footer is reachable from the open album.

    A gesture taken out of the album dialog's footer has to reappear somewhere
    the album is still in front of the user — the ⋯, the footer's own
    survivors, or the header's folder icon. One that survives only on the
    shelf's right-click is not reachable from where the decision is made.

    Written as the complement rather than as a list of the ones remembered:
    **every one of them, checked, with `approve-partial` named as the single
    deliberate exception** — that button is excluded by decision, and a
    decision is not an omission.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    ids = _element_ids()
    reachable = {
        # The ⋯ built when the dialog opens, plus the footer and the header.
        "album-open-folder": "album-open-folder" in ids or "openAlbumFolder" in script,
        "album-scan-now": "album-scan-now" in ids,
        # A ⋯ entry, where it swapped places with the bench.
        "album-hold": "STR.hold" in _more_menu(script),
        "album-plan-anyway": "STR.planAnyway" in script,
        # The footer button, and not a ⋯ entry as well: one gesture with two
        # homes is the opposite fault to the one this test guards.
        "album-arrange": "album-arrange-tags" in ids,
        # The footer button, and neither a ⋯ entry nor an icon in the head: one
        # gesture offered in three places is two places too many.
        "album-to-bench": "album-to-bench" in ids,
    }
    homeless = sorted(name for name, found in reachable.items() if not found)
    assert homeless == [], (
        "these left the dialog's footer and the open album can no longer reach "
        f"them at all: {homeless}"
    )
    assert (
        "album-approve-partial" not in ids
    ), "the second apply button is back; it is the one deliberately excluded"


def test_applying_an_album_no_catalogue_answered_for_asks_first() -> None:
    """A write made of the album's own tags must say so before it happens.

    An album nobody has scanned still arrives with a plan — its own tags, worked
    out on the way in — and `Approve & apply` would apply it wearing the same
    filled button that applies a catalogue's answer. So approving without a
    scan first warns that the album has not been compared with any online
    source.

    Read off the gesture, because a sentence declared and never reached says
    nothing.

    It is asked through the window's own dialog, and awaited: a question raised
    and not waited for is a write that happens while it is still on screen.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    found = re.search(r"async function approveAlbum\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert found, "no approveAlbum() to read, so this guard is looking at the wrong file"
    gesture = found.group(1)
    asked = re.search(r'source === "tags"[^\n]*await confirmApplyWithoutScan\([^)]*\)', gesture)
    assert asked, (
        "the question has to be asked exactly of the album no source answered "
        "for, and awaited — otherwise it asks it of every album, of none, or "
        "writes while it is still asking"
    )
    # It takes an argument — *which* of the two albums this is —
    # so the words can differ while the question stays the same. What this guard
    # is about is that the question is asked here at all, and through the
    # window's own dialog; `test_arranged_album_says_it_was_scanned.py` runs it
    # over both albums and reads which sentence each one got.
    raised = re.search(r"function confirmApplyWithoutScan\([^)]*\) \{[\s\S]+?askThrough\(", SCRIPT)
    assert raised, (
        "the question left the window's own dialog, and a native box is one "
        "this webview may decline to show"
    )


def test_one_idea_is_drawn_with_one_mark() -> None:
    """Every menu button is drawn with the same mark.

    The Library's bar, the Mixing bar and every card row draw `⋯` for a menu. A
    dialog drawing `+` — the mark that means *add something* — over gestures
    that add nothing (scan again, plan again, arrange by tags) hides them:
    nobody looks for them there.

    Read over every menu button rather than over one, because the next surface
    is the one nobody has built yet.
    """
    menus = re.findall(r'<button id="([\w-]*more)"[^>]*>([^<]*)</button>', MARKUP)
    assert menus, "no menu buttons found, so this guard is reading the wrong file"
    wrong = [(name, mark) for name, mark in menus if mark.strip() != "⋯"]
    assert wrong == [], (
        f"these menus are drawn with a different mark: {wrong}. One idea, one "
        "mark — a `+` over gestures that add nothing is a menu nobody opens"
    )


def test_an_album_in_review_is_offered_the_way_out_in_the_row() -> None:
    """The gesture that answers a wrong identification, where the doubt is said.

    When a text search identifies an album as another record with a low score,
    none of the names matching and nothing planned, every line of the screen
    says *do not trust this* and `Approve & apply` is disabled. The one gesture
    that answers it — arrange this album by its own tags — must not be behind
    the menu.

    Hung on `review`, which is this application's own word for *look at this
    yourself*, so no threshold is invented here and none has to be maintained.
    """
    assert 'id="album-arrange-tags"' in MARKUP, "the way out left the footer"
    hidden = re.search(r'\$\("album-arrange-tags"\)\.hidden = ([^;]+);', SCRIPT)
    assert hidden, "nothing decides when the way out is drawn"
    assert 'album.decision !== "review"' in hidden.group(1), (
        "the way out is drawn by some other rule than the one word this "
        f"application uses for not being convinced: {hidden.group(1)!r}"
    )
    assert re.search(r'\$\("album-arrange-tags"\)\.addEventListener', SCRIPT), (
        "the button is drawn and wired to nothing, which is the deadest thing "
        "this window can show"
    )


def test_the_folder_column_lists_the_folder() -> None:
    """`NOW ON DISK` lists the files that are in the folder.

    The table is built from the release an album was identified as. If a file
    no track claimed gets a row of its own only when it is not already named as
    a candidate on some asking track, then on an album that matched nothing —
    where every file is such a candidate — the table is the release's tracks
    with no file, and none of the files on disk.

    The population and the sentence under it are counted off the same rows for
    the same reason: counting the candidates the asking tracks offered is
    counting a different set.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    loop = re.search(
        r'if \(row\.status !== "unmatched_file"\) continue;(.{0,400}?)planRow\(',
        script,
        flags=re.DOTALL,
    )
    assert loop, "nothing draws a row for a file no track claimed"
    assert "offered_above" not in loop.group(1), (
        "a file already named as a candidate above is skipped again, so a folder "
        "nothing matched draws none of its own files under NOW ON DISK"
    )
    unclaimed = re.search(r"function renderUnclaimed\([^)]*\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert unclaimed, "no renderUnclaimed() to read"
    assert 'status === "unmatched_file"' in unclaimed.group(1), (
        "the sentence under the table counts a different set of files than the " "table draws"
    )


def test_an_album_arranged_from_its_tags_is_not_called_unscanned() -> None:
    """An album arranged from its tags after a scan is not called unscanned.

    `looked_at` is about the outcome on screen — arranging by tags produces one
    no catalogue answered, so it goes false. Read as a fact about the album, it
    makes the banner say *no catalogue has been asked about this album* over
    one that was scanned, identified and set aside on purpose, with `Scan this
    album` in amber underneath: the road back to the answer just set aside.

    Two facts, two fields. `catalogue_asked` is read from the album's own row
    rather than from its outcome.
    """
    # Both ends of the pair, in one assertion: the window reads it and the
    # application sends it. A field read and never sent is `undefined`, which is
    # falsy — and would quietly draw the banner for every album.
    api_source = (
        Path(__file__).resolve().parents[2] / "src/diglibrary/application/api.py"
    ).read_text(encoding="utf-8")
    assert "catalogue_asked" in SCRIPT and '"catalogue_asked"' in api_source, (
        "the window cannot tell an album nobody asked about from one whose "
        "answer was set aside, so it says the wrong one of the two"
    )
    hidden = re.search(r'\$\("album-unscanned"\)\.hidden = ([^;]+);', SCRIPT)
    assert hidden and "catalogue_asked" in hidden.group(
        1
    ), f"the never-scanned notice is drawn without asking whether one was: {hidden}"
    assert "arrangedInsteadHeader" in SCRIPT and "arrangedInsteadHeader" in STRINGS, (
        "the header still says no source was asked about an album a source " "answered for"
    )


def test_a_record_nothing_matched_is_said_once_and_in_amber_once() -> None:
    """One fact is stated once on the screen, and claims amber once.

    Where nothing matched, the same fact can be said seven ways at once: the
    amber headline, two red lists naming files the table already lists and
    tracks of a record that is not this album, a sentence on each file row, an
    adoption control on each file row offering a place on the wrong record's
    tracklist, and a paragraph under the table. Amber is a claim on attention;
    two compete, and a column of reds shouts.

    The contract: one notice says the fact in amber and the ways out in grey,
    and every other surface defers to it — each deferral pinned here, because a
    rule applied to four surfaces of five is applied by half.
    """
    assert 'id="album-wrong-record"' in MARKUP, "the one notice is gone"
    assert "wrongRecordFact" in STRINGS and "wrongRecordWays" in STRINGS
    ways = _string_value("wrongRecordWays")
    assert "Arrange by its own tags" in ways and "until you approve" in ways, (
        "the ways out stopped naming the gesture and its safety, which is what "
        "makes it safe to press"
    )
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    # The blockers list defers: those sentences are the fact again with lists
    # appended, and the lists are the table and the details.
    blockers = re.search(r'lines\(\$\("album-blockers"\), ([^;]+)\);', script)
    assert blockers and "wrongRecord" in blockers.group(1), (
        "the red lists draw beside the notice, so the fact is said in two " "colours at once"
    )
    # The amber headline defers — two ambers compete.
    names = re.search(r"names\.hidden = ([^;]+);", script)
    assert names and "wrongRecord" in names.group(
        1
    ), "namesDisagree draws beside the notice, which is two amber claims at once"
    # Each file row goes quiet: no sentence repeated eleven times, no offer to
    # place a file onto the wrong record's tracklist.
    assert re.search(
        r"proof: wrongRecord \? \"\" : STR\.planFileOnly", script
    ), "every file row repeats the notice's fact again"
    assert re.search(r"adopt: wrongRecord \? null :", script), (
        "the adoption controls offer the gesture this screen itself calls "
        "answering the wrong question"
    )
    # And the details' diff line keeps its words but not its amber. What is
    # pinned is the colour rather than a special case for this album: a branch
    # would pass while the stylesheet undid it.
    differing = re.search(r"\.tags-say\.is-differing \{ color: ([^;]+); \}", STYLES)
    assert differing and differing.group(1) != "var(--warn)", (
        "the source-differs line claims amber again, and it competes with the "
        "notice and with the blockers for a screen that may only claim once"
    )
    # The fold states the same fact several more times and the badge once more.
    # Each of those defers too, and is pinned here for the same reason the four
    # above are.
    assert re.search(r"if \(!compact && nothingMatchedHere\(album\)\) return badge;", script), (
        "the match badge draws `Matches 0%` beside the notice — the same fact as "
        "a percentage of the wrong record's tracklist"
    )
    assert re.search(
        r"const provedNothing = nothingMatchedHere\(album\);", script
    ), "the proof line's condition stopped being computed from the notice's own rule"
    # It defers to the notice and to nothing else. The evidence sentence does
    # not state the lengths — the server subtracts them from it — so this line
    # is where the lengths are said, and a second condition would take it off
    # the screen for the very albums it is the only statement on.
    assert "alreadyCounted" not in script, (
        "the proof line is gated on the blockers again, and the sentence it was "
        "deferring to no longer states the lengths"
    )
    proof = re.search(r"proof\.hidden = ([^;]+);", script)
    assert proof and "provedNothing" in proof.group(1), (
        "`matched on text alone` draws beside the notice, which is its fact in a " "third wording"
    )
    # The source panel is not drawn at all: the header names the sources, and
    # the panel would say it again.
    assert 'id="album-positions"' not in MARKUP and "renderPositions" not in script, (
        "the source panel is back, saying in three lines what the header says "
        "one line under the title"
    )
    facts = re.search(r"const releaseFacts =\n?([^;]+);", script)
    assert facts and "album.source" not in facts.group(1), (
        "the fold's first line names the source again — the third place on the "
        "screen to do it, and the only one that is neither a link nor spelled "
        "the way this window spells it"
    )


def test_nothing_in_this_window_asks_through_the_system() -> None:
    """Every question this window asks goes through `askThrough`.

    Three things a native box costs, and the third is the one that matters: it
    is drawn by the operating system rather than by this application; its
    buttons say `OK` where every dialog here names the act it performs; and
    **this webview is free not to show it at all**, in which case `confirm()`
    answers `false` and the apply simply does not happen, with the button
    pressed and nothing said. The apply path is the only one that writes to
    files, so its questions need this most.

    Written as the complement — *no native modal anywhere* — rather than as a
    list of known questions, because the next question is the one nobody has
    written yet.
    """
    calls = re.findall(r"(?<![`\w.])(?:confirm|alert|prompt)\s*\(", SCRIPT)
    assert calls == [], (
        f"{len(calls)} native modal(s) are back in app.js: the system draws them, "
        "their buttons say OK, and a question this webview declines to show is a "
        "gesture that silently does nothing. Ask through one of the window's own "
        "dialogs with askThrough()"
    )


def test_the_marked_card_offers_the_one_gesture_that_repairs_it() -> None:
    """`Relocate…` cannot live only where a marked album cannot go.

    An album whose folder does not answer is kept on the shelf, marked. Such an
    album is never read back into memory, so its dialog answers "Unknown album"
    and never opens — and with `Relocate…` an entry of that dialog's ⋯ and
    nowhere else, the card is drawn with a badge and no way to act on it.

    Read off the card's own menu, because that is the surface a marked album
    actually has.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    found = re.search(r"function albumActions\(album\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert found, "no albumActions() to read, so this guard is looking at the wrong file"
    menu = found.group(1)
    assert "folder_missing" in menu and "STR.relocateThis" in menu, (
        "the shelf's own menu must offer `Relocate…` for a card marked `not where "
        "it was`; the album's dialog cannot be opened for one"
    )


def _more_menu(script: str) -> str:
    """The body of the album dialog's ⋯, which is where a rehomed gesture lands."""
    found = re.search(r"function albumMoreItems\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert found, "no albumMoreItems() to read, so this guard is looking at the wrong file"
    return found.group(1)


def test_the_batch_button_counts_and_sends_what_is_marked() -> None:
    """The batch button counts the marked albums and sends only those.

    Three things have to hold: the count is of marked albums, the click sends
    those ids, and a button that would do nothing is neither enabled nor
    wearing the accent. Otherwise `Apply 2 automatic` stands filled and amber
    over a shelf with nothing ticked, and a press writes to albums nobody
    chose.

    **And ticking a card has to redraw it**: marking a card calls
    `renderMarkCount`, so if only `renderSummary` draws this button it sits
    disabled over a marked automatic album.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    assert re.search(r"api\(\)\.apply_automatic\(markedOnScreen\(\)\)", script), (
        "the batch is sent with no ids, so the server applies every automatic "
        "album in the library rather than the ones that are ticked"
    )
    drawn = re.search(r"function renderBatchButton\(\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert drawn, "no renderBatchButton() to read"
    body = drawn.group(1)
    assert "markedOnScreen()" in body, "the button's count is not about what is marked"
    assert re.search(
        r"button\.disabled\s*=", body
    ), "the batch button is never disabled, so it invites a click that does nothing"
    assert re.search(r'classList\.toggle\("button-primary"', body), (
        "the accent is worn unconditionally, so a button with nothing to apply "
        "is still the loudest thing on the screen"
    )
    marking = re.search(r"function setMarked\([^)]*\)\s*{(.+?)\n}", script, flags=re.DOTALL)
    assert marking, "no setMarked() to read"
    assert "renderBatchButton()" in marking.group(1), (
        "ticking a card does not redraw the batch button, so it stays disabled "
        "over the album just marked"
    )


def test_the_structural_surfaces_space_themselves_from_one_scale() -> None:
    """Two rows a pixel apart read as a mistake nobody can name.

    Surfaces spaced by ear — 8, 9, 10, 12, 16, 18, 20 — are each almost aligned
    with the one beside them. The scale (`--space-1` to `--space-6`) owns the
    layout; component-internal padding and the density variables keep owning
    their own. A raw pixel gap put back on one of these surfaces fails here.

    The keyboard's focus is part of the same promise: a control with no focus
    style leaves the keyboard invisible on a dark screen.
    """
    for token in ("--space-1", "--space-2", "--space-3", "--space-4", "--space-5", "--space-6"):
        assert f"{token}:" in STYLES, f"the scale lost {token}"
    surfaces = [
        ".topbar",
        # One bar for every screen, named for what it is rather than for one of
        # the screens that wear it.
        ".screen-bar",
        ".screen-questions",
        ".screen-actions",
        ".dialog-head",
        ".dialog-head-actions",
        ".dialog-actions",
        ".settings-form",
    ]
    for selector in surfaces:
        block = re.search(re.escape(selector) + r"\s*{([^}]*)}", STYLES)
        assert block, f"{selector} is gone, so this guard is looking at the wrong file"
        for prop, value in re.findall(r"(gap|padding|margin-bottom):\s*([^;]+);", block.group(1)):
            assert "var(--space" in value, (
                f"{selector} spaces its {prop} by ear again ({value.strip()}) "
                "instead of from the scale"
            )
    focus = re.search(r":focus-visible\s*{([^}]*)}", STYLES)
    assert focus and "var(--accent)" in focus.group(
        1
    ), "the global focus ring is gone, so the keyboard is invisible again"


def test_every_dialog_offers_a_way_out_that_records_what_it_owes() -> None:
    """Every dialog has a way out, and the welcome's records that it was used.

    A dialog with no ✕ that does not close on a click outside can only be left
    by its one button, whatever that button says.

    Written by the complement: every dialog must carry a closer, so
    the next one added cannot arrive without one. The welcome's ✕ deliberately
    does **not** use `data-close` — the plain closer would shut it without
    recording that it has been through, and a first run dismissed that way
    would come up again next launch — so this asserts the wiring, not just
    the button.
    """
    for dialog in re.finditer(r'<dialog id="([^"]+)".*?</dialog>', MARKUP, flags=re.DOTALL):
        name, body = dialog.group(1), dialog.group(0)
        assert "dialog-close" in body, f"#{name} is a dialog with no way out"
    welcome = re.search(r'<dialog id="welcome-dialog".*?</dialog>', MARKUP, flags=re.DOTALL)
    assert welcome, "no welcome dialog to check"
    closer = re.search(r'<button id="welcome-close"[^>]*>', welcome.group(0))
    assert closer and "data-close" not in closer.group(0), (
        "the welcome's ✕ closes the dialog without recording that it has been "
        "through, so a dismissed first run comes up again next launch"
    )
    for exit_id in ("welcome-start", "welcome-close"):
        assert re.search(
            rf'\$\("{exit_id}"\)\.addEventListener\("click", finishWelcome\)', SCRIPT
        ), f"{exit_id} leaves the welcome by some route other than finishWelcome"
    assert 'dismissOnBackdrop($("welcome-dialog"), finishWelcome)' in SCRIPT, (
        "clicking outside the welcome either does nothing or forgets to record "
        "that it has been through"
    )


def test_the_screen_a_tab_opens_is_found_and_not_listed() -> None:
    """A list of the tabs somebody already knew about makes the next one invisible.

    If `switchTab` sets `hidden` on a list of named sections, a screen added
    later draws itself perfectly into a section that stays hidden. Written as
    the complement, every `.view` is hidden except the one arriving, and a
    screen added later needs no edit here.
    """
    body = re.search(r"function switchTab\(name\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert body, "no switchTab() to read"
    named = re.findall(r'\$\("view-([a-z]+)"\)\.hidden', body.group(1))
    assert not named, (
        "switchTab hides screens by naming them, so a tab added later opens " f"nothing: {named}"
    )
    assert 'querySelectorAll(".view")' in body.group(1), (
        "switchTab no longer reaches every screen, so some screen is decided "
        "by a rule that does not know about it"
    )


def test_the_mixing_screen_never_states_what_it_did_not_measure() -> None:
    """A tempo the audio did not settle is shown with its twin, and so is a key.

    A first pass over real audio can report twice the tempo a track has, and
    key confidence varies widely across one album. A screen printing all of
    those the same way states what it did not measure.

    The key's half of it is the margin, and it has a third state the tempo does
    not: a row measured before the margin existed. That is not a key that won
    by nothing, and the screen must not dress it as one — it is a reading taken
    with an earlier profile.
    """
    assert "bpm_alternative" in SCRIPT, "the other octave never reaches the screen"
    assert "key_margin" in SCRIPT, "how decided the key is never reaches the screen"
    assert "runner_up_camelot" in SCRIPT, "the key a close call beat never reaches the screen"
    assert "mixKeyStale" in SCRIPT and "mixKeyStale" in STRINGS, (
        "a key measured before the margin existed is being given one of the "
        "three words, which calibrates it on a scale it was never taken with"
    )
    assert "mixOr" in STRINGS and "mixTempoUnknown" in STRINGS, (
        "the words for an unsettled tempo are gone, so the screen has no way to "
        "say what it does not know"
    )


def test_the_wheel_says_it_is_still_filling_and_asks_again_when_it_is_full() -> None:
    """A part of the answer must not be drawn as the whole answer.

    `harmonics()` walks the album map the restore thread fills, so what the
    Mixing screen answers depends on when it is asked: moments after launch it
    holds a handful of tracks, and the rest arrive over the following seconds.

    And `restored` calls `refresh()`, which draws the shelf and nothing else —
    so a count taken mid-restore would outlive the condition that produced it
    until the tab is changed.

    Two halves, and one without the other is half the rule: the screen says it
    is still filling, and it asks again when the filling is done. The Library
    follows the same rule.
    """
    handler = re.search(
        r'else if \(event\.type === "restored"\) \{(.+?)\n    \}', SCRIPT, flags=re.DOTALL
    )
    assert handler, "no `restored` handler to read"
    assert "refreshMixing()" in handler.group(1), (
        "the wheel is not asked again when the library finishes arriving, so a "
        "count taken mid-restore stays on screen until the tab is changed"
    )
    body = re.search(
        r"function renderMixing\(\) \{(.+?)\n  drawCamelotWheel", SCRIPT, flags=re.DOTALL
    )
    assert body, "no renderMixing() to read"
    assert '$("mixing-restoring").hidden = !state.restoring' in body.group(1), (
        "the line that is still filling stopped saying so, so a part of the "
        "wheel draws as the whole of it"
    )
    assert "STR.restoringLibrary" in body.group(1), (
        "an empty wheel mid-restore asks for something to be measured again: "
        "nothing measured and not here yet are different facts"
    )
    assert (
        'id="mixing-restoring"' in MARKUP and "mixingStillArriving" in STRINGS
    ), "the sentence that says the list is still filling is gone"


def test_what_the_mixing_screen_offers_is_checked_by_key_and_by_tempo() -> None:
    """A mix suggestion considers the Camelot key and a close tempo, both.

    Read as text because nothing at runtime would say if a later edit dropped
    the tempo half: the suggestions would still appear, still look reasonable,
    and quietly stop being checked.
    """
    body = re.search(r"function mixingPartnersRow\(chosen\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert body, "no mixingPartnersRow() to read"
    assert "tempoMeets" in body.group(1), (
        "the partner list no longer checks the tempo, so it offers pairs by key "
        "alone — 92 and 140 BPM do not mix"
    )
    assert "camelotNeighbours" in body.group(1), "the partner list no longer checks the wheel"


def test_a_menu_opened_over_a_dialog_is_one_that_can_be_pressed() -> None:
    """A menu opened over a modal dialog has to live inside that dialog.

    A modal dialog makes everything outside itself unclickable, and this
    window's one menu lives at the end of the body. Measured in the engine:
    with the album dialog open, `elementFromPoint` over a drawn menu item
    answers `dialog` — the item is painted in the top layer and the press never
    reaches it, so every entry of the album's ⋯ is dead.

    A dispatched click still fires its handler, which is why this is read as
    text and not asserted with a synthetic event: the piece works and the
    wiring does not.

    Written as *the open dialog* rather than by naming one, so a menu
    opened from the next modal screen is live without anybody remembering.
    """
    body = re.search(
        r"function openAcquireMenu\(event, items\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL
    )
    assert body, "no openAcquireMenu() to read"
    assert 'querySelector("dialog[open]")' in body.group(1), (
        "the menu is no longer moved into the open dialog, so every entry it "
        "shows over one is painted but unpressable"
    )
    assert "appendChild(menu)" in body.group(1), "the menu is located but never re-parented"


def test_every_job_this_window_waits_on_is_polled_and_shown() -> None:
    """One list says which jobs are running, and both readers use it.

    Two places ask "is anything running?": the poll loop, which keeps itself
    alive, and the activity indicator. With a list each, a job named in
    neither — reading key and tempo, say — does its work and writes its rows
    while its progress and its finished notice go into a queue nobody is
    draining, and the window looks idle.

    One list, read by both, so a job added to it cannot be missing from the
    other.
    """
    assert (
        "const RUNNING_FLAGS" in SCRIPT
    ), "the flags that mean a job is running are no longer declared in one place"
    body = re.search(r"async function pollEvents\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert body, "no pollEvents() to read"
    assert "worthPolling()" in body.group(1), (
        "the poll loop decides on its own whether to keep going, so a job "
        "outside its list emits events nobody reads"
    )
    # And `worthPolling` is still built on the one list, not a second one beside
    # it: it adds the case where nothing is running *here* and an event is
    # coming anyway — a download the backend collects on its own thread.
    worth = re.search(r"function worthPolling\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert worth, "no worthPolling() to read"
    assert "anythingRunning()" in worth.group(
        1
    ), "worthPolling has grown its own list of jobs instead of reading the one"
    listed = re.findall(
        r"state\.(scanning|searching|qualityRunning|collecting|adopting|restoring)\s*\|\|",
        body.group(1),
    )
    assert not listed, f"pollEvents keeps its own list of jobs again: {listed}"
    activity = re.search(r"function renderActivity\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert activity and "measuringHarmonics" in activity.group(1), (
        "the indicator cannot name the key-and-tempo run, so the window claims "
        "to be idle while it works"
    )


def test_what_mixes_is_drawn_beside_the_track_it_is_about() -> None:
    """The partner list opens directly under the track it answers about.

    At the foot of a long shelf it is a scroll away from the question. It opens
    as a row under the track that was pressed — which means it is a `<td>`, and
    a `display` on one of those takes it out of its own row and paints the
    border above every other column's. The flex belongs to the box inside the
    cell.
    """
    assert (
        "mixing-partner-row" in SCRIPT
    ), "the partner list is no longer built as a row inside the table"
    assert (
        "chosenRow.after(partners)" in SCRIPT
    ), "the partner row is built but not placed under the track it is about"
    rule = re.search(r"\.mixing-partner-row\s*>\s*td\s*{([^}]*)}", STYLES)
    assert rule, "the partner row's cell has no rule of its own"
    assert "display" not in rule.group(1), (
        "a `display` on this cell stops it being one, and the row's border "
        "lands above every other column's"
    )


def test_the_column_widths_are_kept_by_the_application_and_not_by_the_browser() -> None:
    """Column widths are remembered by the application's own store.

    `localStorage` cannot hold anything here, for two independent reasons:
    pywebview's `private_mode` defaults to `True` and this window never turns
    it off, so the webview is handed a **non-persistent** data store; and the
    window is served from `http://127.0.0.1:<random port>`, so even a
    persistent one would be a different origin — a different `localStorage` —
    at every launch.

    They go where `library_sort` goes, which is the store the application owns.
    """
    assert "localStorage" not in re.sub(r"/\*.+?\*/", "", SCRIPT, flags=re.DOTALL), (
        "the window is trusting the browser to remember something again, and in "
        "private mode with a random port it cannot"
    )
    assert "column_widths" in SCRIPT, "the widths are no longer named as a setting"
    remember = re.search(r"async function rememberWidths\(.+?\)\s*{(.+?)\n}", SCRIPT, re.DOTALL)
    assert remember and "set_settings" in remember.group(
        1
    ), "dragging a column writes nothing the application will see"
    # Applied when the settings arrive, not when the grips are built: the grips
    # are built with the document, which is before the application has spoken.
    assert "applyStoredColumnWidths()" in SCRIPT
    assert re.search(
        r"columnAppliers\.push", SCRIPT
    ), "nothing remembers how to put the widths back once they arrive"
    # And every table with headings has its edges; the list is written by hand,
    # so each one is named.
    for key in ("library", "quality", "history", "mixing"):
        assert f'resizableTable("{key}"' in SCRIPT, f"the {key} table has no draggable edges"


def test_a_window_that_never_reaches_the_application_says_so() -> None:
    """A window whose bridge never arrives says so instead of waiting for ever.

    `whenBridgeReady` covers two orders — the methods are already there, or the
    event is still coming — and there is a third that is neither: **the event
    never comes**. Then the window waits for ever, drawing the empty shelf it
    starts with, and says nothing: no toast, no band, nothing in the log,
    because nothing threw. A library that looks empty and a library that could
    not be reached are the same picture.

    Measured in a real browser against the shipped files: with a bridge that
    never fills, the band comes up carrying this sentence; with one that fills,
    it stays hidden and the shelf draws — no false alarm.
    """
    waiting = re.search(r"function whenBridgeReady\(callback\)\s*{(.+?)\n}", SCRIPT, re.DOTALL)
    assert waiting, "nothing waits for the bridge"
    assert "setTimeout" in waiting.group(
        1
    ), "the window waits for the bridge for ever again, and says nothing while it does"
    assert "clearTimeout" in waiting.group(
        1
    ), "the wait is never called off, so a window that worked accuses itself"
    # It is said in the band rather than a toast: this is a state, and nothing
    # will arrive later to make it untrue.
    said = re.search(r"function sayTheWindowIsAlone\(\)\s*{(.+?)\n}", SCRIPT, re.DOTALL)
    assert said, "nothing says the window is alone"
    assert "window-failure" in said.group(1), "the sentence goes somewhere that disappears"
    assert "bridgeIsReady()" in said.group(1), (
        "the alarm does not check the bridge one last time, so a bridge that "
        "arrived late is announced as missing"
    )
    assert "windowNeverMetTheApplication" in STRINGS, "the sentence itself is gone"
    # The patience is a ceiling, not a measurement: too low is a false alarm on
    # somebody's first run, which is the worst possible audience for one.
    patience = re.search(r"const BRIDGE_PATIENCE_MS = ([\d_]+);", SCRIPT)
    assert (
        patience and int(patience.group(1).replace("_", "")) >= 5000
    ), "the window gives up on the bridge sooner than a slow launch may take"


def test_a_settled_pairing_says_so_and_does_not_leave_it_to_be_inferred() -> None:
    """A settled pairing is marked as settled, row by row.

    With the doubt in amber and everything else simply *not amber*, that every
    row is fine can only be learned by reading every row. So the settled row
    carries its own tick, in the column the labels already form, and one look
    shows whether everything matched or a single row did not. Red is not used
    for the doubt — in this window red is a blocker, and a pairing in doubt is
    not a fault.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    row = re.search(r"function planRow\(\{(.+?)\n\}\)", script, flags=re.DOTALL)
    assert row, "no planRow() signature to read"
    assert "confirmed" in row.group(1), "the row cannot be told that its pairing is settled"
    assert "plan-mark" in script, "nothing draws the per-row verdict"
    assert re.search(r"confirmed:\s*Boolean\(pairedRow && !pairedRow\.suggested\)", script), (
        "the tick is not asking both questions — a row is settled when it paired "
        "AND the pairing is not the one being offered"
    )
    for token in (".plan-mark", ".plan-mark.is-yes", ".plan-mark.is-asking"):
        assert token in STYLES, f"{token} has no rule, so the mark inherits the label's grey"
    assert "var(--bad)" not in re.search(r"\.plan-mark\.is-asking\s*{([^}]*)}", STYLES).group(1), (
        "the doubt is drawn in the fault colour, which claims something broke " "where nothing did"
    )


def test_the_mark_box_is_drawn_rather_than_left_at_the_platform_default() -> None:
    """The mark box is drawn by this window, at a size that can be read.

    The platform's 13px checkbox is small for the control that decides what
    gets written. It is drawn as a background image rather than a
    pseudo-element, because `::after` on a replaced element is the kind of rule
    that is silently dropped. Measured in the engine: 17 by 17, `appearance:
    none`, both states carrying their own image, amber fill when ticked.
    """
    block = re.search(r"\.card-mark\s*{([^}]*)}", STYLES)
    assert block, "the mark box has no rule of its own"
    body = block.group(1)
    assert "appearance: none" in body, "the box is the platform's, so its size is not ours to set"
    assert re.search(
        r"width:\s*1[4-9]px", body
    ), "the mark box is back under 14px, which is too small to read"
    assert "background" in body, "the empty box carries no mark at all"
    # `#` inside a URI starts a fragment, so a colour written raw truncates the
    # image and the box comes up blank.
    assert "%23" in body and 'url("data:image/svg+xml,%3Csvg' in body, (
        "the tick is not an inline image, or its colours are written with a raw "
        "`#`, which cuts the URI where the colour begins"
    )
    checked = re.search(r"\.card-mark:checked\s*{([^}]*)}", STYLES)
    assert checked and "var(--accent)" in checked.group(1), (
        "a ticked box does not fill, so it differs from an empty one only by the "
        "shade of its tick — which is what makes a ghost tick read as a real one"
    )


def test_an_event_kind_this_window_does_not_know_is_not_dropped_in_silence() -> None:
    """The chain in `pollEvents` names the kinds the window knows, and a kind
    the application learns to send tomorrow belongs to none of them.

    Written as a list of the known members, the chain drains such an event from
    the queue and drops it with nothing said, so the screen simply never learns
    what happened. It cannot be handled here — the window does not know what it
    means — but it can refuse to be invisible.
    """
    body = re.search(r"async function pollEvents\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert body, "no pollEvents() to read"
    tail = body.group(1)
    assert re.search(r"}\s*else\s*{[^}]*console\.warn", tail), (
        "pollEvents has no final else, so an event kind nobody has met yet "
        "vanishes without a word"
    )


def test_the_shelf_is_patched_inside_the_view_that_is_showing_it() -> None:
    """`renderGrid` rewrites the view in front and the other keeps its children,
    and `#grid` comes first in document order.

    So a document-wide `querySelector` finds the *hidden* card while the list
    view is in front, and every `identified` event of a long scan grafts a
    `<tr>` into a `<div>` nobody can see, while the row on screen keeps saying
    `not scanned`. Worse for an album with no card at all: History rows carry
    `data-unit-id` too.
    """
    replace = re.search(r"function replaceAlbum\(album\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert replace, "no replaceAlbum() to read"
    assert "document.querySelector" not in replace.group(1), (
        "replaceAlbum searches the whole document again, so it can find a card "
        "in the hidden view or a row in History"
    )
    assert "albumContainer()" in replace.group(1)
    append = re.search(r"function appendAlbum\(album\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert append, "no appendAlbum() to read"
    assert "matchesSearch" in append.group(1), (
        "an album appended during a scan ignores the search box, so typing in "
        "it while a run is going fills the shelf with albums it excludes"
    )


def test_an_estimate_is_compared_as_time_and_never_as_text() -> None:
    """`.sort()` with no comparator sorts strings, and these are strings like
    `45s`, `9m00s` and `1h02m` — which sort to `1h02m`, `45s`, `9m00s`.

    A folder with one file an hour out and one nine minutes out would therefore
    say nine minutes.

    What this row *says* is settled by running it (`test_transfer_estimate`),
    because a wait computed the wrong way has no shape to read. This stays for
    the part that does: the comparison is over seconds, never over the words.
    """
    eta = re.search(r"function etaOf\(node, files\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert eta, "no etaOf() to read"
    assert ".sort()" not in eta.group(1), "the estimates are being ordered as text again"
    assert "durationSeconds" in eta.group(1), "the longest estimate is found by comparing seconds"


def test_the_window_keeps_no_list_of_which_transfer_states_are_ends() -> None:
    """The ends are named once, in `connectors/models.py`, and read through
    `is_moving` — whose own comment says a second copy of that set somewhere else
    is the failure it exists to prevent.

    A folder's remaining bytes are counted on that side for exactly this reason.
    A copy here would be written by enumeration, and the first slskd wording this
    vocabulary has not met would fall outside it and be counted as nothing —
    so a download still moving would be treated as finished.
    """
    for ends in (
        '"completed", "failed", "cancelled"',
        '"cancelled", "completed", "failed"',
        '"failed", "cancelled", "completed"',
    ):
        assert ends not in SCRIPT, (
            "the window is listing the transfer states that are ends; ask the "
            "other side of the bridge, which owns that set"
        )


def test_a_download_row_reads_what_happened_and_not_one_fixed_word() -> None:
    """The state cell of a download says what became of it.

    Writing `historyDownloaded` into every row reads `Downloaded` for a
    download asked from a peer that went away, with the folder nowhere on disk
    and slskd no longer listing it. A screen that says a thing arrived when
    nothing did is wrong about the library itself.
    """
    row = re.search(r"function downloadRow\(entry\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert row, "no downloadRow() to read"
    body = row.group(1)
    assert "entry.arrival" in body, "the row states an outcome it never checked again"
    for word in ("historyArriving", "historyWaiting", "historyDownloaded"):
        assert word in body, f"the row cannot say {word}"


def test_a_download_that_has_not_arrived_can_be_stopped_waiting_for() -> None:
    """`acquisition_clear_downloads` only reaches the ones that arrived, so
    without this entry a download that never came has no way off the list."""
    actions = re.search(r"function historyActions\(entry\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert actions, "no historyActions() to read"
    body = actions.group(1)
    assert "forgetDownload" in body, "a download that never arrived has no way out"
    # Written as the complement: a state added later is offered the
    # way out too, rather than falling through to nothing.
    assert '!== "collected"' in body and '!== "cleared"' in body, (
        "the entry is offered by naming the states that get it, so a state "
        "nobody has met yet silently loses its way out"
    )


def test_every_service_the_backend_stores_a_key_for_has_a_field_in_the_window() -> None:
    """Every service a key is stored for can be given one in the window.

    `api.connect` accepts `slskd` and `credentials.connected()` reports it, so
    a welcome offering fields for the other services only would leave writing
    `~/.diglibrary/env` by hand as the one way to give this application an
    slskd key. Two hand-written lists in two languages, and nothing that could
    notice they disagreed. This is that notice.
    """
    from diglibrary.application.api import CONNECTABLE

    ids = _element_ids()
    for service in CONNECTABLE:
        field = f"connect-{service.replace('_', '-')}"
        assert field in ids, f"the window has no field for {service}, which the backend accepts"
        assert f"{field}-save" in ids, f"the field for {service} has no button to store it"
        assert f'connectService("{service}"' in SCRIPT, f"nothing in app.js stores {service}"


def test_the_connected_line_answers_for_every_service_and_not_for_four_of_five() -> None:
    """It is the only place on screen that says what is connected, so it has to
    name slskd too — the one service without which a tab does nothing."""
    from diglibrary.application.api import CONNECTABLE

    line = re.search(r"connectionState: \((.+?)\) =>\n(.+?),\n  [a-z]", STRINGS, flags=re.DOTALL)
    assert line, "no connectionState to read"
    # Spotify is one row from two keys, because half a key reaches nothing.
    wanted = {service.split("_")[0] for service in CONNECTABLE}
    named = line.group(1) + line.group(2)
    for service in wanted:
        assert service in named.lower(), f"the connected line never mentions {service}"


def test_the_reason_acquisition_cannot_be_used_is_never_guessed_at() -> None:
    """Each reason acquisition cannot be used has its own sentence.

    A map of a few reasons that falls back to the offline sentence makes a key
    that was never given, a key the server refused and an unexpected failure
    all accuse a server that is working. The fallback describes nothing and
    quotes the backend instead.
    """
    refresh = re.search(
        r"async function acquireRefreshState\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL
    )
    assert refresh, "no acquireRefreshState() to read"
    body = refresh.group(1)
    for reason in ("no_provider", "disabled", "no_credential", "credential_rejected", "offline"):
        assert reason in body, f"the window has no sentence for {reason}"
    assert (
        "|| STR.acquireOffline" not in body
    ), "the fallback claims the server is silent about every reason nobody listed"
    assert "STR.acquireUnusable(" in body, "the fallback does not carry the backend's own reason"


def test_a_missing_ffmpeg_is_said_on_both_screens_that_need_it() -> None:
    """The absence of the measuring tool is stated on both screens that need it.

    If Quality hides its measuring button on the same line that hides it with
    nothing marked, the tool's absence and an empty selection look identical;
    and "no audio here could be read" on Mixing blames the files. Both screens
    say it, with the same sentence, from the same fact.
    """
    ids = _element_ids()
    for element in ("quality-no-ffmpeg", "mixing-no-ffmpeg"):
        assert element in ids, f"{element} is not in the markup, so nothing can say why"
        assert (
            f'$("{element}").hidden = state.canMeasureAudio' in SCRIPT
        ), f"{element} is never shown, or is shown by something other than the one fact"
    assert (
        MARKUP.count('data-str="qualityUnavailable"') == 2
    ), "the sentence is drawn on both screens, or the screens have grown their own"
    # The one fact, under one name. Two flags read from two payloads would leave
    # one of the screens with no way of knowing.
    assert "benchCanMeasure" not in SCRIPT, "the bench is reading its own second answer again"


def _string_value(key: str) -> str:
    """What a string actually says, joined across the lines it is written over."""
    body = re.search(
        rf"^  {re.escape(key)}:\s*((?:.*\n)*?)(?=  [A-Za-z_$]+\s*[:(])", STRINGS, flags=re.MULTILINE
    )
    assert body, f"{key} is not declared"
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', body.group(1)))


def _visible_words(key: str) -> int:
    """How many words a string puts on screen, read from its own declaration."""
    return len(_string_value(key).split())


def test_the_three_longest_texts_stay_one_sentence_with_the_rest_on_hover() -> None:
    """Three long texts are one sentence on screen, with the rest on hover.

    Not a ceiling over every string — the paragraphs explaining an optional
    service are longer, which is what those paragraphs are for. These three
    are kept short, and each keeps what it lost: a tooltip costs nothing,
    because it only appears on hover.
    """
    for visible, hover in (
        ("contestedNote", "tipContestedNote"),
        ("planNoteUnchanged", "tipPlanNoteUnchanged"),
        ("settingDatabaseCopiesNote", "tipSettingDatabaseCopiesNote"),
    ):
        assert _visible_words(visible) <= 25, f"{visible} is growing back onto the screen"
        assert hover in _string_keys(), f"{visible} lost what it used to say instead of moving it"
        assert _visible_words(hover) > _visible_words(
            visible
        ), f"{hover} holds less than the sentence it is the rest of"
    # Two are written in the markup; the plan's is written in the script, because
    # a title left standing would explain the wrong reading of an element that
    # says three different things.
    assert 'data-str-title="tipContestedNote"' in MARKUP
    assert 'data-str-title="tipSettingDatabaseCopiesNote"' in MARKUP
    # **And it is attached to the line the sentence ended up on.** The note is
    # not drawn at all where the plan lists nothing, so asserting that
    # `plan-note` writes *a* title would pass over a hover nobody can reach —
    # which is the shape of a guard that reads nothing.
    assert (
        '$("plan-note").title = "";' in SCRIPT
    ), "the note explains a reading it no longer has; it must be emptied on the pass"
    assert (
        "item.title = STR.tipPlanNoteUnchanged;" in SCRIPT
    ), "the unchanged album's hover has to hang off the sentence that stays on screen"


def test_every_screen_wears_the_same_bar() -> None:
    """Six screens, one heading.

    Measured in this window's own engine, bars built from different classes
    come out at different heights — one has no note under its title, another
    holds more controls — and a screen can have no heading at all. A class
    named after one screen and worn by three has two meanings, so the shared
    name is part of the rule.

    The old names are asserted *gone*. A stylesheet that keeps dressing
    something nobody draws is where the next reader looks for a rule that is
    not there.
    """
    views = re.findall(r'<section id="view-([a-z]+)"[\s\S]*?</section>', MARKUP)
    assert len(views) == 6, "a screen was added or lost; this guard counts them"
    for name in ("library", "acquire", "transfers", "quality", "mixing", "history"):
        block = re.search(r'<section id="view-' + name + r'"([\s\S]*?)</section>', MARKUP)
        assert block, f"the {name} screen is gone"
        bars = block.group(1).count('class="screen-bar"')
        assert bars == 1, f"the {name} screen wears {bars} bars instead of one"
    for old in (
        ".library-bar",
        ".library-questions",
        ".library-bar-actions",
        ".quality-head",
        ".bench-actions",
        ".transfers-actions",
    ):
        assert not re.search(
            r"^" + re.escape(old) + r"[\s,{]", STYLES, re.M
        ), f"{old} is back, and with it a screen that does not match the others"

    # The long sentence that explains each screen is the title's
    # tooltip. A title without one is a screen that stopped explaining itself.
    #
    # Five, not six: the Library has none. It is the one screen whose tab name
    # says everything a title could, and the one carrying the most controls.
    # The other five explain a screen their tab does not.
    titles = re.findall(
        r'class="screen-title"\s+data-str="(\w+)"\s*\n?\s*data-str-title="(\w+)"', MARKUP
    )
    assert len(titles) == 5, "a screen's title lost its name or its explanation"
    library = re.search(r'<section id="view-library"([\s\S]*?)</section>', MARKUP)
    assert "screen-title" not in library.group(1), "the Library's title grew back"
    keys = _string_keys()
    for name, note in titles:
        assert name in keys, f"{name} is not a string this window holds"
        assert note in keys, f"{note} is not a string this window holds"


def test_the_two_row_floors_are_declared_at_every_density() -> None:
    """A file row and an album row are not the same row, and both have a floor.

    Measured in the engine: grid lists obey `--row-min`, and a `<table>` row
    with no floor of its own comes out at whatever height its content gives it
    — so the variable that is meant to own density would govern only half the
    application.

    `height` on a `<tr>`, not `min-height` on a cell: in table layout the first
    is a minimum and the second does nothing.
    """
    for token in ("--row-min", "--row-min-album", "--bar-control", "--row-cover"):
        found = re.findall(re.escape(token) + r":\s*(\d+)px", STYLES)
        assert len(found) == 3, (
            f"{token} is declared {len(found)} times; it has to answer all three "
            "densities or one of them is styled by accident"
        )
        assert found == sorted(found, key=int), f"{token} does not grow with the density"
    floor = re.search(r"\.albums-list tbody tr,\s*\n\.history tbody tr\s*{([^}]*)}", STYLES)
    assert floor and "height: var(--row-min-album)" in floor.group(1), (
        "the album tables stopped answering to the floor, so a sleeve decides "
        "the height of a screen again"
    )


def test_the_bench_does_not_promise_a_sweep_it_refuses_to_do() -> None:
    """The bench's sentences describe a bench, not a sweep.

    "Every track is decoded and measured" describes a survey over a whole
    library. The bench shows what is already known and decodes nothing until
    asked, so the sentence must say that. Correct words in `index.html` as the
    fallback are unreachable: `applyStrings` writes `textContent`
    unconditionally, so the markup's copy is never what anybody reads.

    That last part is why this asserts on `strings.js` and not on the markup.
    """
    for key in ("qualityNote", "emptyQuality"):
        said = _string_value(key).lower()
        assert "decoded and measured" not in said, f"{key} promises the sweep again"
        assert "survey" not in said, f"{key} is written as the survey this stopped being"
    assert (
        "nothing is decoded until you ask" in _string_value("qualityNote").lower()
    ), "the bench stopped saying the one thing that makes it a bench"


def test_an_svg_is_named_by_a_child_and_not_by_a_property() -> None:
    """The seventh way these files disagree in silence.

    `node.title = value` on an SVG element is nothing at all — no property, no
    attribute, no tooltip — and the Camelot wheel names itself through
    `data-str-title` like every control around it. Measured in this window's own
    engine: every sibling on that screen had a tooltip and the wheel had none,
    which looks exactly like a string that was never written.
    """
    assert (
        'createElementNS(node.namespaceURI, "title")' in SCRIPT
    ), "an SVG named through data-str-title is silently unnamed again"
    assert 'namespaceURI === "http://www.w3.org/2000/svg"' in SCRIPT


def test_a_menu_with_nothing_in_it_does_not_open() -> None:
    """A menu handed an empty list does not open.

    Measured in this window's own engine: handed an empty list and still
    calling `showPopover`, `openAcquireMenu` draws a thin box with a border, a
    background and no text — a sliver of panel beside the list. Guarded at the
    function rather than at each of its call sites.

    And the button that hands it the empty list says so: a control that answers
    a press with nothing reads as broken.
    """
    opener = re.search(r"function openAcquireMenu\([^)]*\)\s*{([\s\S]*?)\n}", SCRIPT)
    assert opener, "openAcquireMenu is gone, so this guard is looking at the wrong file"
    body = opener.group(1)
    guard = body.index("if (!items.length)")
    assert guard < body.index("showPopover"), "an empty menu can be opened again"
    assert "renderMixingMore" in SCRIPT, "nothing draws the Mixing ⋯ from what it can offer"
    assert (
        'data-str-title="tipMixMore"' not in MARKUP
    ), "the Mixing ⋯ is given one tooltip from two places again"
    assert "tipMixMoreEmpty" in _string_keys(), "the disabled ⋯ has no words for why"


def test_the_key_sureness_is_a_word_and_not_a_coloured_percentage() -> None:
    """The sureness of a key is a word, not a coloured percentage.

    The number behind that column is a Pearson correlation between the
    recording's chroma and a key profile (`harmonic/keys.py`), not a
    probability. Real music holds notes outside its own key, so the coefficient
    stays well under 1 even on an unmistakable track — and drawn as a red
    percentage it reads as the application being barely sure of itself.

    **And the word is not drawn from that number at all.** A correlation says
    how much a recording looks like music in some key; it does not say whether
    the reading beat its rivals, which is the question a word over a key
    answers. The margin does: measured against a reference analyser, agreement
    rises with the margin's bands, where the correlation's own bands follow no
    order.

    The cuts are pinned here because moving one moves what the window claims.
    What the tooltip says about each band is a word and never a share: how often
    a reading lands on the reference's answer depends on the music measured.
    """
    for gone in (".sure-low", ".sure-fair", ".sure-good"):
        assert not re.search(
            r"^" + re.escape(gone) + r"[\s,{]", STYLES, re.M
        ), f"{gone} is back, and a correlation is being painted as a grade again"
    assert (
        "KEY_WEAK_BELOW = 0.05" in SCRIPT and "KEY_STRONG_FROM = 0.1" in SCRIPT
    ), "the key sureness boundaries moved without the measurement behind them moving"
    assert re.search(r"function keySureness\(track\)", SCRIPT), (
        "the word over a key is being drawn from something other than the "
        "measurement again, and the correlation is the one thing measured not "
        "to predict it"
    )
    # And it reads the runner-up, not the margin alone: a tie between two keys
    # that mix with each other is not a doubtful reading for mixing, and must
    # not wear the worst word available.
    assert "runnerUpMixesWithIt" in SCRIPT and "mixableTie" in SCRIPT, (
        "a tie between two keys that mix with each other is being called weak " "again"
    )
    assert "badge-conf-${which.badge}" in SCRIPT, "the key column stopped wearing the bench's badge"
    # The tooltip is where the margin goes, and it has to say what the margin is.
    said = _string_value("mixKeySure").lower()
    assert "rekordbox" in said and "one that mixes" in said, (
        "the tooltip stopped saying what the word is compared with — the "
        "reference analyser's own key, or one that mixes with it"
    )
    assert (
        "${margin} ahead of the next" in STRINGS
    ), "the tooltip stopped saying what that figure actually is"
    assert (
        "${howOften}" in STRINGS
    ), "the tooltip stopped saying how often a reading this decided holds"
    cases = re.search(r"const KEY_CASES = \{([\s\S]+?)\n\};", SCRIPT)
    assert cases, "no table of cases in app.js, so this guard reads nothing"
    assert not re.search(r"\d", cases.group(1)), (
        "a case carries a number again, and a share measured over one set of "
        "tracks is not a property of this application"
    )
    for key in ("mixKeySure", "mixKeyTwin", "mixKeyTwinApart"):
        declared = re.search(
            rf"^  {key}:\s*((?:.*\n)*?)(?=  [A-Za-z_$]+\s*[:(])", STRINGS, flags=re.MULTILINE
        )
        assert declared, f"{key} is not declared"
        assert "%" not in declared.group(1), f"{key} quotes a percentage"


def test_the_library_bar_stopped_being_the_crowded_one() -> None:
    """The Library's bar carries no more than it needs.

    Four decisions keep it short: no title, the words `Sort by` replaced by the
    mark they labelled, the search field folded behind its own mark, and
    `Apply automatic` absent until it is about something.
    """
    # The search is a mark until it is wanted, on every bar that holds one, and
    # never in the picker dialog — there the search is the screen's whole point.
    assert MARKUP.count('class="button button-icon search-open"') == 3
    for field in ("library-search", "mixing-search", "history-search"):
        assert "wireBarSearch" in SCRIPT and field in SCRIPT
    collapse = re.search(r"\.screen-bar \.library-search \.search-field\s*{([^}]*)}", STYLES)
    assert collapse and "width: 0" in collapse.group(1), "the field stopped folding away"
    assert "hidden" not in re.search(r"\.measure-search\s*{([^}]*)}", STYLES).group(
        1
    ), "the picker's own search started folding away with the bars'"

    # Away entirely until there is something to accept, like the bench's pair.
    batch = re.search(r"function renderBatchButton\(\)\s*{([\s\S]*?)\n}", SCRIPT)
    assert batch and "button.hidden = !offers" in batch.group(
        1
    ), "Apply automatic stands on the bar disabled again"

    # **One folder control, wearing a +.** A second button beside it would be
    # two buttons for the same thing: `adopt_folders` and `open_folder` both
    # end in `_read_into_library`, and the only difference is which path is
    # remembered as the root Scan reads; choosing a folder *adds* its albums,
    # and nothing on the shelf is ever replaced.
    assert 'id="add-album"' not in MARKUP, "a second button for the same gesture is back"
    folder = re.search(r'id="choose-folder"[\s\S]*?</button>', MARKUP)
    assert folder and "icon-plus" in folder.group(0), "the folder mark stopped saying that it adds"
    # And the words say it too: they describe the albums that land the moment a
    # folder is chosen, not only what Scan would read.
    said = _string_value("tipChooseFolder").lower()
    assert (
        "keeping everything already on it" in said
    ), "the folder button went back to describing itself as a setting"


def test_the_window_says_which_sources_a_run_went_without() -> None:
    """A run that lost a source says so twice — quietly both times.

    On the run's own line, because an album opened later still needs the
    condition it was decided under, and a source failure that reaches no screen
    is invisible. And as a clause in the dialog of each album whose
    identification actually went without the source. Never as a notice over
    the shelf: such a notice puts a paragraph over the whole library and does
    not leave on its own.
    """
    assert (
        "sourceOutage(event.payload.sources_down)" in SCRIPT
    ), "the finished run stopped keeping the sources it lost"
    assert "STR.scanWithout" in SCRIPT, "the run's own line stopped naming what was missing"
    assert "toastError(STR.sourceDown" not in SCRIPT, "the shelf notice is back"
    assert "STR.headMissed" in SCRIPT, "the dialog stopped saying what an album went without"
    # Three states, and each of them has words. A reason with no sentence is a
    # notice that says `undefined` to whoever hits the state nobody enumerated.
    reasons = re.search(r"sourceDownReasons:\s*{([^}]*)}", STRINGS)
    assert reasons, "the three states lost their words"
    for state in ("no_key", "refused", "unreachable"):
        assert state in reasons.group(1), f"{state} has no sentence"
    # And the fallback, because a state nobody enumerated must not draw nothing.
    # `headMissed` is a method rather than a value — it reads its own table — so
    # it is asserted on the file, not through `_string_value`.
    assert (
        "it could not be searched" in STRINGS
    ), "a source failing in a way nobody listed would draw `undefined`"


def test_the_search_marks_are_drawn_rather_than_typed() -> None:
    """The search marks are drawn, not typed.

    `⌕` — U+2315 — is a hairline no type size in this bar can rescue. The box
    is unchanged and the mark inside it is what grows. Drawn, so it is crisp at
    any scale, costs no request, and inherits `currentColor` — a glyph could do
    none of the three.

    And the field on Find music carries the same mark as a watermark, with its
    sentence kept beside it. Measured in the engine: three marks at 15x15, and
    the watermark 16x16 at 14px from the edge with the text indented to 38px.
    """
    assert "⌕" not in MARKUP, "the hairline glyph is back"
    assert MARKUP.count('class="search-mark"') == 3, "a bar's search lost its mark"
    mark = re.search(r"\.search-mark\s*{([^}]*)}", STYLES)
    assert mark, "the mark has no size of its own, so it is whatever the box gives it"
    assert "width: 15px" in mark.group(1)

    field = re.search(r"\.acquire-input\s*{([^}]*)}", STYLES)
    assert field, "the Find music field is gone"
    body = field.group(1)
    assert "data:image/svg+xml" in body, "the watermark is not there"
    # `#` inside a URI begins a fragment: written raw, the colour is lost and the
    # whole declaration draws nothing. This is the mistake the marking box's own
    # comment already warns about, one rule further down the same file.
    assert "%23" in body and 'url("data:image/svg+xml,%3Csvg' in body
    assert (
        "#" not in body.split("data:image/svg+xml")[1].split('"')[0]
    ), "a raw # inside the URI truncates it at the fragment"
    # An SVG with a viewBox and no intrinsic size fills whatever box paints it.
    assert "16px 16px" in body, "the watermark has no declared size"
    # The text has to start clear of the mark, or the field looks broken.
    assert "padding: 11px 14px 11px 38px" in body, "the placeholder runs under the mark"


def test_no_string_reaches_its_neighbours_through_this() -> None:
    """The eighth way these files disagree in silence, and the first to take the window down.

    A method on `STR` is read as a value about as often as it is called:
    `missing.map(STR.sourceName)` hands the function over without its receiver.
    `this` is then undefined, and the whole Library is replaced by a red band
    reading *undefined is not an object (evaluating 'this.sourceNames')*.
    Nothing is wrong with the strings, the markup, or the call — only with how
    the function travels.

    So the rule is the simple one rather than a search for unbound callbacks:
    nothing in `strings.js` reaches its neighbours through `this`. Naming the
    object cannot come unbound, and the check cannot be argued with.
    """
    offenders = [
        line.strip()
        for line in STRINGS.splitlines()
        # A comment line is not code — and this one has to say the fault out
        # loud to be worth reading, including the expression that caused it.
        if not line.lstrip().startswith("//")
        # `this.` inside a sentence — "nothing is written from this." — is prose,
        # and prose is what this file is for. Code is `this.` followed by a name.
        and re.search(r"\bthis\.[A-Za-z_$]", line)
    ]

    assert offenders == [], (
        "a string reads a neighbour through `this`, which is undefined the moment "
        f"the method is handed to `map`/`forEach`/a callback: {offenders}"
    )


def test_the_band_that_says_the_window_broke_can_be_put_away() -> None:
    """The band that reports a failure of the window can be closed.

    The band is the last thing that still works when `app.js` is dead. Without
    a ✕, a fault that repeats on every redraw pins it across the top of the
    window for the rest of the session, over the application in use.

    Its button is wired in the same inline script that draws it, because a
    dismissal that depends on the module that just died is not one. Asserted
    here rather than by clicking, since what fails is the wiring, not the click.
    """
    assert 'id="window-failure-close"' in MARKUP, "the band offers no way out"
    inline = MARKUP.split('<div class="chrome">')[0]
    assert (
        "window-failure-close" in inline and "addEventListener" in inline
    ), "the ✕ is wired outside the one script that survives `app.js` failing"
    assert "#window-failure-close" in STYLES, "the ✕ is drawn by nothing"
    # And it stays away: the same message arriving again must not reopen what
    # was closed, or the ✕ is decoration.
    assert "dismissed" in inline, "a repeat of the same fault would reopen the band"


def test_the_composer_preview_shows_the_arrangement_that_is_on_screen() -> None:
    """The preview shows the answer to the last question asked.

    Every chip added, removed or moved asks the backend to render the name
    again, and the bridge does not promise to answer in the order it was asked.
    If the last answer to *arrive* wins over the last one *asked for*, two
    pieces removed in quick succession leave the preview reading
    `%track% - %title% [%bpm%]` while the chips beside it say `Title, BPM`.

    The panel's entire job is to show what was just built, and there is nothing
    else on screen to check it against.

    Guarded here as the shape rather than by clicking: what must exist is a
    sequence number taken before the call and compared after it.
    """
    body = SCRIPT.split("async function refreshComposerPreview()")[1].split("\n}")[0]

    assert "composer.asked" in body, "the preview does not number its questions"
    taken = body.index("++composer.asked")
    called = body.index("naming_preview")
    compared = body.index("!== composer.asked")
    assert taken < called < compared, (
        "the sequence number must be taken before the call and compared after it, "
        "or a slow answer still paints over a fast one"
    )


def test_the_window_keeps_listening_while_a_download_is_still_on_its_way() -> None:
    """A download still on its way is a reason to keep polling.

    A finished download joins the library by itself, from a collect that runs
    on the *backend's* sleep-watch thread at a fixed interval and emits
    `collected`. But the poll loop only re-arms while this window believes
    something is running, and a collect it never started sets none of those
    flags — so the poll has already stopped, the event sits in the queue, and
    the Library goes on showing what it showed before. Leaving the tab and
    coming back drains the queue, which makes it look like a redraw problem.

    Three things have to hold, and none of them is the spinner: waiting on a
    download is a reason to listen, not something to put a wheel on.
    """
    assert "state.awaiting" in SCRIPT, "the window never learns that something is still coming"
    # It has to be able to *start* a chain, not only continue one: the download
    # is queued while the window sits idle.
    assert "function ensurePolling()" in SCRIPT, "nothing can begin a poll once the window is idle"
    starts = re.search(r"function ensurePolling\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert starts and "state.polling" in starts.group(
        1
    ), "without a flag saying a chain is alive, every refresh stacks another one"
    # And the beat slows down when there is nothing to report: three polls a
    # second across an hour of downloading is thousands of crossings to learn
    # nothing.
    delay = re.search(r"function pollDelay\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert delay and "anythingRunning()" in delay.group(1), (
        "the poll beats at one rate, so waiting on a download costs what a " "running job costs"
    )


def test_the_dialog_says_what_is_left_behind_once() -> None:
    """The tracks left behind are named once in the dialog.

    Two sentences, one above the other, naming the same tracks — `3 release
    tracks have no file` and `3 release tracks have no file here` — come from
    two lists being concatenated: the blocked plan's reasons and the offered
    plan's warnings.

    `_partial_warnings` names an ambiguous pairing too, so the offered plan's
    own list is complete and the other has nothing to add. Asserted as *which
    list is read*, since the duplication is never in the text of either one.
    """
    body = re.search(r'lines\(\$\("album-warnings"\), \[(.+?)\]\);', SCRIPT, flags=re.DOTALL)
    assert body, "the dialog no longer fills album-warnings from a list"
    assert "album.blockers" not in body.group(1), (
        "the blocked plan's reasons are being shown beside the offered plan's "
        "warnings again, which prints the same alignment twice"
    )
    assert "partial_warnings" in body.group(1), "the offered plan's own warnings are not shown"


def test_the_line_that_stays_counts_the_run_and_never_the_shelf() -> None:
    """The line that stays after a scan counts that run.

    `state.albums.length` is the whole Library. Read there, a scan of two
    marked albums would say that every album on the shelf was identified,
    directly above a reach panel counting two folders. The flash and the line
    that stays both take the run's own count.
    """
    drawn = re.search(r"function renderScanDone\(\)\s*{(.+?)\n}", SCRIPT, flags=re.DOTALL)
    assert drawn, "no renderScanDone() to read"
    # Everything after the line is shown at all. Read from there rather than from
    # the label's own assignment: the sentence is composed above it, since a
    # clause joins it, and the early return above legitimately asks
    # whether the shelf is empty — which is not the count this is about.
    body = drawn.group(1)
    assert "done.hidden = false;" in body, "renderScanDone no longer has a line to read"
    composed = body.split("done.hidden = false;")[1]
    assert (
        "state.albums.length" not in composed
    ), "the line that stays is counting the shelf again, not the run that just ran"
    assert "state.scanIdentified" in composed, "it says what the run answered for"


def test_no_string_is_written_and_then_referenced_by_nothing() -> None:
    """A string nobody reads is dead weight, and only this guard says so.

    `test_no_string_is_declared_twice` catches a name written twice; an orphan is
    the other way the three files drift apart, and it is invisible: the window
    runs perfectly with it. A sentence that moves into another string leaves
    its own entry behind, and nothing else in the suite notices.

    A key counts as read if its bare name appears anywhere in the markup or the
    script. That is deliberately loose: the window reaches several of these
    through `STR[key]` — from `data-str` in the markup, and from tables like
    `STATUS_GROUPS` that pair a group with a string's name — so demanding a
    literal `STR.name` would fail on strings that are very much in use. Loose in
    this direction is the safe one: it lets an orphan whose name is also an
    ordinary word survive, and never accuses a string that is read.
    """
    # Top-level entries only: two spaces of indentation is the object's own
    # level, and the keys nested inside `sourceDownReasons` and its neighbours
    # belong to a value, not to `STR`.
    keys = re.findall(r"^  ([A-Za-z_][A-Za-z0-9_]*)[:(]", STRINGS, flags=re.MULTILINE)
    assert len(keys) > 500, "this is not reading strings.js as an object literal any more"

    def is_read(key: str) -> bool:
        word = rf"\b{re.escape(key)}\b"
        if re.search(word, MARKUP + SCRIPT):
            return True
        # Or by a neighbour in this same file — `sourceDown` reads
        # `STR.sourceDownReasons`, and `sourceName` reads `STR.sourceNames`.
        # Through `STR.` and never `this.`, which is the rule that keeps a method
        # of this literal working when it travels without a receiver.
        return len(re.findall(word, STRINGS)) > 1

    orphans = [key for key in keys if not is_read(key)]

    assert (
        orphans == []
    ), f"nothing in the window reads {', '.join(orphans)} — say it somewhere or remove it"


def test_only_the_user_writes_in_the_search_box() -> None:
    """A shelf filtered by the window reads as a library that emptied.

    If dropping an album already on the shelf types that album's name into the
    Library search — to answer *which one* — every other album goes off the
    screen. Nothing is lost and the shelf is empty, which looks the same.

    Two writers, and both are the user: the field that is typed in, and the ✕
    that clears it. Anything else is the window deciding what may be seen.
    """
    writers = re.findall(r"state\.search = ", SCRIPT)

    assert len(writers) == 2, (
        "something other than typing and the clear button is writing to "
        "the Library search; a shelf filtered by the app reads as a shelf that "
        "lost its albums"
    )


def test_nothing_the_dialog_reveals_waits_inside_a_fold_that_may_be_closed() -> None:
    """A notice drawn inside a collapsed `<details>` is drawn and unreadable.

    A waiting line built, filled and unhidden inside `plan-details` is closed
    by the same render whenever nothing would change. An album with nothing to
    change is exactly the album a cover is chosen for, so there the notice
    could only appear where it could never be seen.

    The rule this encodes: a pending write of its own is not part of *what will
    change* about the identification, and must not live in that fold. Listed by
    id rather than inferred, because what belongs inside the fold is a judgement
    and this is the record of one.
    """
    dialog = re.search(r'<dialog id="album-dialog".*?</dialog>', MARKUP, flags=re.DOTALL)
    assert dialog, "no album dialog in the markup"
    folds = re.findall(r"<details\b.*?</details>", dialog.group(0), flags=re.DOTALL)
    inside = {name for fold in folds for name in re.findall(r'\bid="([^"]+)"', fold)}

    assert (
        "cover-choice" not in inside
    ), "the chosen cover's notice is inside a fold the render may close"
    assert "cover-choice-write" not in inside


def test_a_finished_album_is_one_condition_that_both_halves_read() -> None:
    """The names and the cover ask the same question of the same word.

    A condition such as `organized && !planned_again`, written in the window
    and needed by a second gesture, becomes the second place stating one rule,
    in a second language, far from the first: a rule corrected in one of its
    homes and left standing in the other.

    So the server answers it, in `_asleep`, and the window reads the answer. This
    holds both ends of that: the payload carries the word, the window reads it,
    and nobody works it out again from the two facts it was made of.
    """
    api = (Path(__file__).parent.parent.parent / "src/diglibrary/application/api.py").read_text(
        encoding="utf-8"
    )

    assert re.search(r'"asleep":\s*_asleep\(state\)', api), (
        "the album payload no longer carries `asleep`, so every reader of it is "
        "asking a question nothing answers"
    )
    assert "album.asleep" in SCRIPT, "the window no longer reads the one answer"
    # Comments are stripped, because the one above the line in `app.js` quotes the
    # expression this refuses — a check its own explanation trips is a check
    # nobody keeps.
    code = re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.DOTALL)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("//"))
    reassembled = re.findall(r"organized[^;\n]*planned_again", code)

    assert reassembled == [], (
        "the window is working out for itself what `asleep` already answers: " f"{reassembled}"
    )


def test_a_pairing_held_by_a_length_does_not_wear_the_tick_of_one_held_by_a_name() -> None:
    """A pairing held only by a length is marked differently from one held by a name.

    The mark answers *is this pairing settled*, and a length within a few
    seconds settles one — so rows of a record that is not this album could all
    read as confirmed. Three things can hold a pairing — the user's word, the
    names, the clock — and the window draws each.

    Read as text because the mark is three lines inside a row builder: what this
    can hold is that the row is given the fact, that the fact reaches a class of
    its own, and that the class is defined. What it cannot see is the pixel, so
    the colour is asserted to be the warning one rather than the good one.
    """
    assert "paired_by" in SCRIPT, "the window never asks what holds a pairing together"
    assert "is-length" in SCRIPT, "and nothing distinguishes one held by a length"
    style = re.search(r"\.plan-mark\.is-length\s*\{([^}]*)\}", STYLES)
    assert style, "the mark has a class the stylesheet says nothing about, so it is drawn plain"
    assert "--good" not in style.group(1), (
        "a pairing held by three seconds of coincidence is wearing the colour of "
        "one the names agree on"
    )


def test_how_much_of_the_album_s_names_agreed_is_said_outside_the_fold() -> None:
    """How many of the album's names agreed is said on a line of its own.

    *1 of 9 track titles matched*, in the grey details paragraph between an
    album-title similarity and a track count, under a green line reading
    *lengths match the release*, cannot be found — while the plan below offers
    to rename nine files. It is its own line, and it must not be inside
    `album-details`, which the render closes whenever nothing would change.
    """
    assert 'id="album-names-disagree"' in MARKUP, "the notice is gone"
    folds = re.findall(r"<details\b.*?</details>", MARKUP, flags=re.DOTALL)
    inside = {name for fold in folds for name in re.findall(r'\bid="([^"]+)"', fold)}
    assert "album-names-disagree" not in inside, (
        "the one line that says this may be the wrong record is inside a fold "
        "that opens only when something would change"
    )
    assert (
        "names_compared" in SCRIPT and "names_agreeing" in SCRIPT
    ), "the window is not reading the counts the payload carries"


def test_the_shelf_never_says_two_different_things_in_one_colour() -> None:
    """`Organized` and `Not scanned` must not both be `--muted`.

    One chip filled at 7%, the other outlined, and at chip size that is one
    grey said twice. Amber is not the repair either: `--warn` and `--accent`
    are the same #ffb454, so `Not scanned` would become `Review`.

    Only the three that mean genuinely different things are compared. The fault
    family — rejected, blocked, no match, folder gone — shares `--bad` on
    purpose: they are four ways of saying *this one is wrong*, and telling them
    apart is the word's job, not the colour's.
    """
    palette = dict(re.findall(r"(--[a-z-]+):\s*([^;]+);", STYLES))

    def ink(name: str) -> str:
        rule = re.search(rf"\.badge-{name}\s*\{{([^}}]*)\}}", STYLES)
        assert rule, f"the shelf draws .badge-{name} and the stylesheet says nothing about it"
        found = re.search(r"(?:^|;)\s*color:\s*([^;]+)", rule.group(1))
        assert found, f".badge-{name} sets no colour of its own"
        value = found.group(1).strip()
        named = re.fullmatch(r"var\((--[a-z-]+)\)", value)
        if named:
            assert named.group(1) in palette, f".badge-{name} reads {named.group(1)}, undefined"
            return palette[named.group(1)].strip()
        return value

    worn = {name: ink(name) for name in ("organized", "unlooked", "review")}
    twins = [
        (one, other)
        for index, one in enumerate(worn)
        for other in list(worn)[index + 1 :]
        if worn[one] == worn[other]
    ]

    assert twins == [], (
        f"two badges that mean different things resolve to one colour: {twins} " f"({worn})"
    )


def test_both_filter_lists_offer_the_same_way_back() -> None:
    """`All` in the status list and `Any rating` in the other, or neither.

    Pressing the chosen answer withdraws it, but that toggle is invisible: with
    no entry for it there is no target to press for *show me everything*.

    Guarded as a pair rather than one at a time, because a rule stated in two
    places is easily applied in one. The
    status list says it by not filtering its own `all` out of the menu; the
    rating list says it by carrying `all` among its values.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    assert 'group !== "all"' not in script, (
        "the status menu is dropping its own `all` entry again, so the only way "
        "back to an unfiltered shelf is a gesture nothing on screen mentions"
    )
    values = re.search(r"const RATING_FILTERS = \[(.*?)\]", script, flags=re.DOTALL)
    assert values, "no RATING_FILTERS to read, so this guard is looking at the wrong file"
    assert '"all"' in values.group(1), "the rating list offers no way back and the status one does"
    assert "ratingAny:" in STRINGS, "the rating list's way back has no word to draw"


def test_the_two_rulers_on_a_spectrogram_name_their_own_units() -> None:
    """One picture, two scales, and each names its own unit.

    With the frequency gutter reading `20k` and an encoder reference line
    reading `320k` a few pixels apart on the same image, the two look like one
    measure. One is kilohertz and the other kilobits a second, and the picture
    exists precisely so a measured wall can be compared against where an
    encoder cuts.
    """
    for name, unit in (("specKilohertz", "kHz"), ("specEncoderWall", "kbps")):
        found = re.search(rf"{name}: \(\w+\) => `([^`]*)`", STRINGS)
        assert found, f"no {name} to read, so this guard is looking at the wrong file"
        assert unit in found.group(1), f"{name} draws a number with no unit: {found.group(1)!r}"


def test_nothing_is_ruled_across_a_spectrogram() -> None:
    """The marks over a spectrogram paint nothing but their own labels.

    A dashed border on an anchor that spans the picture is a horizontal line
    over a spectrum, and a horizontal line over a spectrum is what a low-pass
    filter looks like: the ruler showed a cut on files that had none.

    Read as *no rule for these anchors may paint*, whatever selector it is
    written under, so a state class added later is held to it too.
    """
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*[\s\S]*?\*/", "", STYLES))
    anchors = [
        (selector.strip(), body)
        for selector, body in rules
        if re.search(r"\.spec-(wall|reference)(?![\w-])", selector)
    ]
    assert {selector for selector, _ in anchors} >= {
        ".spec-wall",
        ".spec-reference",
    }, "no anchor rules to read, so this guard is looking at the wrong file"
    for selector, body in anchors:
        paints = r"(?:^|;)\s*((?:border|outline|background|box-shadow)[\w-]*)\s*:"
        painted = re.findall(paints, body)
        assert not painted, f"{selector} paints across the picture: {painted}"


def test_lossless_is_the_only_good_news_on_the_quality_screen() -> None:
    """No verdict but `lossless` is drawn in the colour of good news.

    `lossy` in `--good` at one fill and `lossless` at another is one colour
    said twice on the one axis this screen exists to answer. The same rule as
    the shelf's badges.

    Asserted as *nothing else may wear green* rather than as a list of which
    colour each verdict takes, because the failure is always the same one: a
    verdict that is bad news drawn in the colour of good news. `lossy` and `was
    lossy` may share the fault colour, exactly as the shelf's four fault badges
    do — they are two ways of saying *this is not lossless*, and telling them
    apart is the word's job.
    """
    palette = dict(re.findall(r"(--[a-z-]+):\s*([^;]+);", STYLES))

    def ink(name: str) -> str:
        rule = re.search(rf"\.badge-{name}\s*\{{([^}}]*)\}}", STYLES)
        assert rule, f"the quality screen draws .badge-{name} and the stylesheet is silent"
        found = re.search(r"(?:^|;)\s*color:\s*([^;]+)", rule.group(1))
        assert found, f".badge-{name} sets no colour of its own"
        value = found.group(1).strip()
        named = re.fullmatch(r"var\((--[a-z-]+)\)", value)
        if named:
            assert named.group(1) in palette, f".badge-{name} reads {named.group(1)}, undefined"
            return palette[named.group(1)].strip()
        return value

    good = ink("lossless")
    for verdict in ("lossy", "transcoded", "overstated", "undecided"):
        assert ink(verdict) != good, (
            f"`{verdict}` is drawn in the colour of `lossless`, so the one question "
            "this screen answers is answered in one colour"
        )


def test_saying_a_word_about_a_track_redraws_the_album_above_it() -> None:
    """Only the track table was re-read, so the header kept its old verdict.

    Pressing `It is lossy` on every track of one album has to change the chip
    above them. Half a screen refreshed is a screen that disagrees with itself,
    which in this window is indistinguishable from a control that does not
    work.

    Read as text: the handler that saves a verdict must also re-read the bench,
    which is what re-judges an album from what has been said.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    handler = re.search(r"api\(\)\.set_quality_verdict\((.+?)\n    \}\);", script, flags=re.DOTALL)
    assert handler, "no set_quality_verdict handler to read, so this guard reads the wrong file"
    assert "refreshBench()" in handler.group(1), (
        "a word about a track redraws only the tracks, so the album above them "
        "keeps announcing the verdict it had before the word was given"
    )


def test_no_chip_rule_is_left_behind_with_nothing_to_draw_it() -> None:
    """A dead `.badge-*` rule is a colour waiting to be picked up by the wrong thing.

    A `.badge-applied` left in green — the colour that on this screen means
    *lossless* — after the state it was named for stopped being a badge would
    hand good news to the next verdict or state called `applied`.

    Reachable means one of four things: the stylesheet's suffix appears as a
    literal in the script, or it is an `Encoding` the verdict chip is built
    from, or it is one of the three words the confidence chip is built from, or
    it is a proof the proof chip's own map can name.

    **The last one is read out of the script rather than listed here.** Writing
    the three proofs into this test would be an enumeration: a fourth proof
    added to the stylesheet and never wired to anything would be waved through
    by a list that had not been updated. Read from the map, an unwired rule
    still fails.
    """
    drawn = set(re.findall(r"\.badge-([a-z-]+)\s*[,{]", STYLES))
    # Anywhere in the script, not only against a quote: the class is written
    # `"badge badge-spoken"`, so the suffix never touches one.
    literal = {name.rstrip("-") for name in re.findall(r"badge-([a-z-]+)", SCRIPT)}
    encodings = set(
        re.findall(r"^\s{2}([a-z]+): \(\) => STR\.encoding", SCRIPT, flags=re.MULTILINE)
    )
    assert encodings, "no ENCODING_LABELS to read, so this guard is looking at the wrong file"
    labels = re.search(r"const label = \{(.+?)\}\[proof\]", SCRIPT)
    assert labels, "no proof-chip map to read, so this guard is looking at the wrong file"
    proofs = {f"proof-{name}" for name in re.findall(r"(\w+): STR\.", labels.group(1))}
    assert proofs, "the proof-chip map named nothing, which would make this guard vacuous"
    reachable = literal | encodings | proofs | {"conf", "conf-strong", "conf-fair", "conf-weak"}

    assert (
        drawn - reachable == set()
    ), f"the stylesheet colours chips nothing can draw: {sorted(drawn - reachable)}"


def test_the_bulk_word_reaches_the_tracks_and_never_the_album() -> None:
    """One word said to a whole table is written to the tracks, not the album.

    Only a file can have been lossy before it arrived, so only a file is asked;
    the control that says one word to a whole table must therefore write track
    words, and the album goes on being the majority of its tracks. A gesture
    that wrote an album word instead would look identical on screen and would
    make the album stop answering to the rows under it.
    """
    script = re.sub(r"//[^\n]*", "", SCRIPT)
    body = re.search(r"function everyTrackControls\(album, detail\) \{([\s\S]+?)\n\}", script)
    assert body, "no everyTrackControls to read, so this guard is looking at the wrong file"
    assert "say_about_every_track" in body.group(1)
    assert "set_quality_verdict" not in body.group(1), (
        "the bulk control is writing single words one by one, which replans once "
        "a track instead of once an album"
    )
    assert "refreshBench()" in body.group(
        1
    ), "the album above the table keeps the verdict it had before the word was given"


def test_the_folder_the_dialog_promises_is_the_one_the_plan_will_write() -> None:
    """The window never works out a folder name of its own.

    The plan is built *with* the `folder_name` correction folded into it, so
    reading the correction here and reading the plan agree only while the plan
    keeps every part of the correction. Where the plan decides a segment for
    itself — the format, say — a window reading the correction would show
    `AFTER APPLYING` naming a folder that is not the one about to be written,
    on the one table whose heading promises *what you see here is what gets
    written*.

    A condition two languages both state is two places, and the worst pair of
    them: the server decides the name and the window reads it.
    """
    assert (
        'field === "folder_name"' not in SCRIPT
    ), "the window is reading the folder correction instead of the plan's rename"
    assert "folderCorrection" not in SCRIPT
    # And the one that does the reading is still there, so this cannot pass by
    # the folder row having been deleted.
    assert 'operation.kind === "rename_folder"' in SCRIPT


# Classes the markup carries that the stylesheet is not expected to define.
# Written as what may be undefined rather than as a count, so the next one is a
# decision somebody makes here on purpose — the same shape as the root listing
# in `tests/packaging`. Each is a wrapper whose children carry the styling.
CLASSES_WITH_NO_RULE = {
    "mixing-tracks",  # a wrapper on the Mixing screen
    "plan-held",  # the plan's held-back box; `#plan-held` is toggled by app.js
    "shared-pairs",  # likewise, filled by app.js and laid out by its children
    "view",  # the six screens; which one shows is decided by app.js, not by this
}


def test_no_class_in_the_markup_is_styled_by_nothing() -> None:
    """The fifth way these files disagree in silence, and it fails by drawing.

    A class the stylesheet never mentions is not an error anywhere: the element
    draws with the browser's defaults and the window looks *nearly* right. An
    id, a `data-str` and a string with nothing behind them are all refused by
    the guards above; without this one, a class is not.

    The defect it finds is **a name that does not exist beside a name that
    does**: a button carrying `linkish` while the stylesheet defines
    `.link-button` (no background, no border, accent colour, underline on
    hover) draws as a plain grey browser button.
    """
    in_markup: set[str] = set()
    for value in re.findall(r'class="([^"]+)"', MARKUP):
        in_markup.update(value.split())

    _refuse_classes_with_no_rule(in_markup)


def _refuse_classes_with_no_rule(names: set[str]) -> None:
    """Fail on any of these the stylesheet never defines."""
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", STYLES))
    orphans = (names - defined) - CLASSES_WITH_NO_RULE

    assert orphans == set(), (
        "these are written on elements and styled by nothing, so they draw with "
        f"the browser's defaults and nothing says so: {sorted(orphans)}"
    )


def test_no_class_the_script_writes_is_styled_by_nothing() -> None:
    """The same rule, on the source of classes that carries most of them.

    The guard above reads `index.html`, and `app.js` is where this window's
    classes mostly come from. A rule that holds on one of its two places is
    applied by half, so the script is read as well.

    Only literal names are read. A class assembled from a value — `badge-${x}` —
    names something this cannot know, and the rules for those families are
    guarded where the family is (`badge-proof`, `badge-conf`).
    """
    written: set[str] = set()
    for value in re.findall(r'className\s*=\s*"([^"$]+)"', SCRIPT):
        written.update(value.split())
    for name in re.findall(r'classList\.(?:add|toggle|remove)\(\s*"([^"$]+)"', SCRIPT):
        written.add(name)
    for value in re.findall(r"\.className\s*=\s*`([^`]*)`", SCRIPT):
        if "${" not in value:
            written.update(value.split())
    for value in re.findall(r'class="([^"$]+)"', SCRIPT):
        written.update(value.split())

    # A guard that reads nothing passes, and looks exactly like a window with
    # nothing wrong.
    assert len(written) > 150, f"only {len(written)} classes read, so this reads the wrong file"
    _refuse_classes_with_no_rule(written)


def test_the_terminal_and_the_window_say_the_same_thing_about_the_icon() -> None:
    """Two mouths, one gesture, one sentence.

    `diglibrary make-icon` and the button in Settings do the same thing, so they
    answer with the same sentence. A wording changed in `strings.js` only
    leaves the terminal printing the old one — for instance promising a Dock
    this application cannot reach into.
    """
    command = (WEB.parent.parent / "__main__.py").read_text(encoding="utf-8")

    said = re.search(r"iconCreated: \(where\) =>\n\s*`Created \$\{where\} — (.+?)`,", STRINGS)
    assert said, "no iconCreated() to compare against, so this guard reads the wrong file"
    sentence = said.group(1).strip()

    # The window writes it after a dash on one line; the terminal prints it as
    # its own sentence, so it begins with a capital. Everything after that word
    # has to match exactly.
    assert sentence[0].upper() + sentence[1:] in command, (
        "the terminal's answer for `make-icon` is not the sentence the window "
        f"gives for the same gesture: expected {sentence!r}"
    )


def test_nothing_the_terminal_prints_promises_a_dock_this_application_cannot_reach() -> None:
    """The help of every command keeps the same promise as the window.

    This application writes `~/Applications/DigLibrary.app`, and **macOS only
    lets a person put something in a Dock**. The window's strings and the
    sentence `make-icon` prints say so, and the *help* has to as well: neither
    `diglibrary --help` nor `diglibrary make-icon --help` may promise an icon
    in the Dock.

    So this one asks the help itself, by running it: whatever `argparse`
    assembles out of a prog, a description, an epilog and every `help=` is what
    a person reads, and none of it may name the Dock except to say it may be
    dragged there.
    """
    import subprocess
    import sys

    allowed = "drag it to the Dock if you want it there"
    # All three. A command that did not answer `--help` at all — reading the
    # word as the name of a copy — would otherwise be left out of this list,
    # and a command left out of a guard is a command the guard does not cover.
    for words in ([], ["make-icon"], ["restore-copy"]):
        printed = subprocess.run(
            [sys.executable, "-m", "diglibrary", *words, "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        offending = [
            line for line in printed.splitlines() if "Dock" in line and allowed not in line
        ]
        assert offending == [], (
            f"`diglibrary {' '.join([*words, '--help'])}` promises a Dock this "
            f"application does not reach into: {offending}"
        )


def test_every_word_this_application_answers_to_can_describe_itself() -> None:
    """A command that cannot say what it is, is a command nobody can use.

    A command that keeps the one option it knows and treats **every other
    word** as the name of a copy is a set written by enumeration. The first
    thing anybody types then goes looking for a file called `--help`, and the
    answer is `There is no copy at …/--help`.

    Asked of both words at once, because *the two commands answer `--help`* is
    one rule and two places implement it.
    """
    import subprocess
    import sys

    for word in ("make-icon", "restore-copy"):
        answered = subprocess.run(
            [sys.executable, "-m", "diglibrary", word, "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert (
            f"usage: diglibrary {word}" in answered.stdout
        ), f"`diglibrary {word} --help` does not describe itself: {answered.stdout!r}"


def test_a_word_a_command_does_not_know_is_refused_by_name() -> None:
    """The complement of the rule above, and the half that keeps it true.

    Answering `--help` by hand would leave the next undefined option — `-h`,
    `--version`, a typo — read as the name of a file again. What makes this a
    rule rather than a patch is that anything beginning with `-` is an option,
    and an option the command does not define is named back to whoever typed it.
    """
    import subprocess
    import sys

    refused = subprocess.run(
        [sys.executable, "-m", "diglibrary", "restore-copy", "--newst"],
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "--newst" in refused.stderr, (
        "the word nobody defined was not named back; it was read as something "
        f"else: {refused.stdout!r} {refused.stderr!r}"
    )


def test_no_answer_the_window_shows_names_a_dock_this_application_cannot_reach() -> None:
    """The refusals `api.py` hands the window keep the same promise.

    The sentence about the Dock is said by the window's strings, by what
    `make-icon` prints and by the help of both commands. One more place is in
    `api.py`: the sentence the window puts on screen when the gesture cannot
    run at all, which no terminal prints and no `strings.js` holds.

    So this reads every refusal `api.py` hands the window — a `"error"` whose
    value is written right there as text — and asks the same question of all of
    them at once, rather than of one.
    """
    import ast

    source = (WEB.parent.parent / "application" / "api.py").read_text(encoding="utf-8")
    allowed = "drag it to the Dock if you want it there"
    offending = [
        value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
        and key.value == "error"
        and isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and "Dock" in value.value
        and allowed not in value.value
    ]
    assert offending == [], (
        f"the window would show a sentence promising a Dock this application "
        f"does not reach into: {offending}"
    )


def test_the_evidence_table_and_the_one_step_down_come_from_the_palette() -> None:
    """The evidence table's grey and the one step down in size are the palette's.

    A raw `#9aa3b2` in the evidence table's numeric columns is brighter than
    `--muted` against the ground (7.24:1 against 5.68:1), so the least
    important cells of that table would read stronger than every other piece of
    secondary text on the screen.

    `0.92em` is the same shape as a size rather than a colour: many rules each
    stating one step down, none of which can be moved without finding the
    others. Both are the palette's.

    Other greys written into rules — panel fills, a border, a placeholder — are
    outside what this asserts.
    """
    numeric = re.search(
        r"\.evidence td:nth-child\(2\), \.evidence td:nth-child\(4\) \{([^}]+)\}", STYLES
    )
    assert numeric, "the evidence table's numeric columns are styled somewhere else now"
    assert "var(--muted)" in numeric.group(1), (
        "those cells state a colour of their own again, and the last one was "
        "brighter than the window's own secondary text"
    )
    assert "#" not in numeric.group(1), "a raw colour is back in the rule"

    assert "--text-step-down:" in STYLES, "the one step down stopped being declared"
    assert "font-size: 0.92em" not in STYLES, (
        "a rule states the step down as its own number again; stated rule by "
        "rule, moving one moves nothing"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this machine")
def test_the_match_sentence_names_which_half_is_open() -> None:
    """`10 of 13 tracks` reads as thirteen files in a folder that holds twelve.

    A total of twelve tracks plus the one file no track took cannot say which
    half it came from. Run rather than read: the sentence is a function, and
    what it says is what a person sees.
    """
    import json
    import subprocess

    found = re.search(r"(?:export )?const STR = \{[\s\S]+?\n\};", STRINGS)
    assert found, "no STR object in strings.js, so this guard reads the wrong file"
    program = (
        found.group(0).replace("export const STR", "const STR", 1)
        + "\nconsole.log(JSON.stringify(["
        + "STR.planMatchesDetail(10, 1, 2), STR.planMatchesDetail(12, 0, 0),"
        + "STR.planMatchesDetail(3, 2, 1)]));"
    )
    said = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", program],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    )
    assert said == [
        "10 paired both ways · 1 file without a track · 2 tracks without a file",
        "12 paired both ways",
        "3 paired both ways · 2 files without a track · 1 track without a file",
    ]
    assert "of" not in said[0].split(" · ")[0], "no total that reads as a count of the files"
