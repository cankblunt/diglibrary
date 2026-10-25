"""The Collection tab, run rather than read.

Each test lifts the real functions out of `app.js`, runs them in node over a
small stand-in for the page, and reads what they drew or asked. Every one of
these is valid JavaScript whichever way it is wired, so only running it says
whether an empty shelf, a search that matched nothing and a collection that
could not be read look different, and whether a list entry in use is removed
without being asked about.

Skipped where `node` is not installed; nothing here is a dependency of the
application, only of checking it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"
STRINGS = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/strings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

FUNCTIONS = (
    "refreshCollection",
    "collectionName",
    "collectionRecord",
    "collectionEntryName",
    "collectionYear",
    "collectionPressing",
    "collectionCategoryNames",
    "collectionWords",
    "collectionAwaits",
    "visibleCollection",
    "collectionCell",
    "collectionGradeShort",
    "collectionGradeCell",
    "renderCollection",
    "openCollectionAdd",
    "collectionStatus",
    "finishCollectionAdd",
    "confirmCollection",
    "collectionListEntries",
    "renderCollectionList",
    "afterCollectionListChange",
    "removeCollectionListEntry",
    "renderCollectionMapping",
    "previewCollectionImport",
    "collectionFind",
    "collectionCandidateLine",
)

HARNESS = """
const said = [];
function element(tag = "div") {
  const made = {
    tag, hidden: false, textContent: "", className: "", value: "", open: false,
    children: [], listeners: {}, dataset: {},
    append(...kids) { this.children.push(...kids); },
    prepend(...kids) { this.children.unshift(...kids); },
    classList: { add(name) { made.className = `${made.className} ${name}`.trim(); } },
    replaceChildren(...kids) { this.children = kids; },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    showModal() { this.open = true; said.push(["opened", this.id]); },
    close() { this.open = false; },
  };
  return made;
}
const nodes = {};
const $ = (id) => (nodes[id] ||= Object.assign(element(), { id }));
const document = {
  createElement: (tag) => element(tag),
  createTextNode: (text) => ({ textContent: text }),
};
const state = {
  collection: { records: [], locations: [], categories: [], mediaGrades: [], sleeveGrades: [] },
  collectionError: null, collectionSearch: "", collectionOpen: null,
  collectionAddFor: null, collectionListKind: null, collectionReviewOnly: false,
};
let answers = {};
const calls = [];
const api = () => new Proxy({ open_url: () => ({ ok: true }) }, {
  get(target, name) {
    if (name in target) return target[name];
    return async (...args) => {
      calls.push(JSON.parse(JSON.stringify([name, ...args])));
      const answer = answers[name];
      return typeof answer === "function" ? answer(...args) : answer;
    };
  },
});
let confirmAnswer = true;
async function askThrough(dialog, yes, no) {
  said.push(["asked", $("collection-confirm-message").textContent]);
  return confirmAnswer;
}
function toast(message) { said.push(["toast", message]); }
function toastError(message) { said.push(["error", message]); }
function openCollectionRecord(recordId) { said.push(["record", recordId]); }
"""

RECORDS = [
    {
        "record_id": 2,
        "artist": "harbour trio",
        "album": "night songs",
        "year": 1976,
        "identified": True,
        "identified_by": "link",
        "media_grade": "Very Good (VG)",
        "sleeve_grade": None,
        "location_id": 10,
        "notes": "gatefold",
        "category_ids": [20],
        "label": "",
        "catalog_number": "",
        "edition": "",
        "country": "",
        "candidate_count": 0,
        "release": {
            "source": "discogs",
            "title": "Night Songs",
            "artist": "The Harbour Trio",
            "released": "1988-01-01",
            "original_year": 1976,
            "labels": ["Sample Records"],
            "catalog_numbers": ["SR-101"],
            "url": "https://www.discogs.com/release/101",
        },
    },
    {
        "record_id": 1,
        "artist": "Some Band",
        "album": "Some Album",
        "year": 1981,
        "identified": False,
        "identified_by": None,
        "media_grade": None,
        "sleeve_grade": None,
        "location_id": None,
        "notes": "",
        "category_ids": [],
        "label": "Own Label",
        "catalog_number": "OL-7",
        "edition": "",
        "country": "",
        "candidate_count": 3,
        "release": None,
    },
]


def _function(source: str, name: str) -> str:
    """One whole function out of `app.js`, matched by its braces."""
    for opening in (f"async function {name}(", f"function {name}("):
        if opening in source:
            start = source.index(opening)
            break
    else:
        raise AssertionError(f"{name} is not in app.js; this guard reads the wrong file")
    depth = 0
    kept: list[str] = []
    for index, line in enumerate(source[start:].split("\n")):
        kept.append(line)
        depth += line.count("{") - line.count("}")
        if index and depth == 0:
            return "\n".join(kept)
    raise AssertionError(f"{name} never closes")


def _run(body: str) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    program = (
        f"const {{ STR }} = await import({json.dumps(STRINGS.as_uri())});\n"
        + HARNESS
        + "\n".join(_function(source, name) for name in FUNCTIONS)
        + f"\nconst RECORDS = {json.dumps(RECORDS)};\n"
        + "state.collection.locations = [{ id: 10, name: 'Shelf 1' }];\n"
        + "state.collection.categories = [{ id: 20, name: 'Soul' }];\n"
        + body
        + "\nconst rows = $('collection-body').children.map((row) => row.children.map("
        + "(cell) => cell.textContent"
        + " + (cell.children || []).map((c) => c.textContent).join('')));\n"
        + "console.log(JSON.stringify({ said, calls, rows,"
        + " hidden: Object.fromEntries(Object.entries(nodes).map(([k, v]) => [k, v.hidden])),"
        + " text: Object.fromEntries("
        + "Object.entries(nodes).map(([k, v]) => [k, v.textContent])) }));"
    )
    answer = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(answer.stdout.strip())


def test_a_shelf_draws_each_record_in_the_catalogues_words_or_the_users() -> None:
    drawn = _run("state.collection.records = RECORDS; renderCollection();")

    assert drawn["rows"] == [
        [
            "The Harbour Trio - Night Songs",
            "1976",
            "Sample Records · SR-101",
            "Shelf 1",
            "Soul",
            "VG",
            "Ungraded",
        ],
        [
            "Some Band - Some Album · not identified · 3 to review",
            "1981",
            "Own Label · OL-7",
            "",
            "",
            "Ungraded",
            "Ungraded",
        ],
    ]
    assert drawn["hidden"]["collection-table"] is False
    assert drawn["hidden"]["collection-attribution"] is False, "Discogs data drawn unattributed"


def test_an_empty_shelf_a_search_that_matched_nothing_and_a_failure_look_different() -> None:
    empty = _run("renderCollection();")
    nothing = _run(
        "state.collection.records = RECORDS; state.collectionSearch = 'zzz'; renderCollection();"
    )
    failed = _run(
        "answers.collection = { ok: false, error: 'the database is locked' };"
        " await refreshCollection();"
    )

    assert (empty["hidden"]["empty-collection"], empty["hidden"]["collection-none"]) == (
        False,
        True,
    )
    assert (nothing["hidden"]["empty-collection"], nothing["hidden"]["collection-none"]) == (
        True,
        False,
    )
    assert failed["hidden"]["collection-error"] is False
    assert failed["text"]["collection-error"] == "the database is locked"
    assert failed["hidden"]["empty-collection"] is True, "a failure was drawn as an empty shelf"


def test_a_search_finds_a_record_by_its_location_category_or_note() -> None:
    for words in ("shelf 1", "soul", "gatefold", "sr-101"):
        found = _run(
            f"state.collection.records = RECORDS; state.collectionSearch = {json.dumps(words)};"
            " renderCollection();"
        )
        assert [row[0] for row in found["rows"]] == ["The Harbour Trio - Night Songs"], words


def test_a_shelf_with_no_catalogue_record_on_screen_carries_no_attribution() -> None:
    drawn = _run("state.collection.records = [RECORDS[1]]; renderCollection();")

    assert drawn["hidden"]["collection-attribution"] is True


def test_identifying_a_record_never_offers_to_add_it_a_second_time() -> None:
    adding = _run("openCollectionAdd(null);")
    identifying = _run("openCollectionAdd(RECORDS[1]);")

    assert adding["hidden"]["collection-add-unidentified"] is False
    assert identifying["hidden"]["collection-add-unidentified"] is True
    assert identifying["text"]["collection-add-heading"] == "Identify “Some Band - Some Album”"


def test_identifying_returns_to_the_record_and_adding_does_not() -> None:
    added = _run(
        "answers.collection = { ok: true, records: RECORDS };"
        " state.collectionAddFor = null; await finishCollectionAdd({ ok: true, record_id: 3 });"
    )
    identified = _run(
        "answers.collection = { ok: true, records: RECORDS };"
        " state.collectionAddFor = 1; await finishCollectionAdd({ ok: true, record_id: 1 });"
    )
    refused = _run("await finishCollectionAdd({ ok: false, error: 'Discogs answered 401' });")

    assert ["record", 3] not in added["said"]
    assert ["record", 1] in identified["said"]
    assert refused["text"]["collection-add-status"] == "Discogs answered 401"


def test_an_entry_records_use_is_removed_only_after_the_user_agrees() -> None:
    setup = (
        "state.collectionListKind = 'category';"
        " answers.collection = { ok: true, records: RECORDS, categories: [] };"
        " answers.collection_list_remove = (kind, id, confirmed) =>"
        " confirmed ? { ok: true } : { ok: false, confirm: true, uses: 2 };"
    )
    declined = _run(
        setup + " confirmAnswer = false; await removeCollectionListEntry({ id: 20, name: 'Soul' });"
    )
    agreed = _run(
        setup + " confirmAnswer = true; await removeCollectionListEntry({ id: 20, name: 'Soul' });"
    )

    assert declined["said"][0] == [
        "asked",
        "“Soul” is used by 2 records, which keep everything else.",
    ]
    assert [call for call in declined["calls"] if call[0] == "collection_list_remove"] == [
        ["collection_list_remove", "category", 20]
    ], "it was removed although the user said no"
    assert ["collection_list_remove", "category", 20, True] in agreed["calls"]


def test_to_review_shows_only_the_records_with_offers_waiting() -> None:
    all_of_them = _run("state.collection.records = RECORDS; renderCollection();")
    waiting = _run(
        "state.collection.records = RECORDS; state.collectionReviewOnly = true; renderCollection();"
    )
    nothing = _run(
        "state.collection.records = [RECORDS[0]]; state.collectionReviewOnly = true;"
        " renderCollection();"
    )

    assert all_of_them["text"]["collection-review"] == "To review (1)"
    assert [row[0] for row in waiting["rows"]] == [
        "Some Band - Some Album · not identified · 3 to review"
    ]
    assert nothing["hidden"]["collection-review"] is True
    assert len(nothing["rows"]) == 1, "a filter with nothing left to review hid the shelf"


def test_a_changed_mapping_must_be_checked_again_before_it_imports() -> None:
    body = (
        "state.collectionImport = { headers: ['Artista', 'Preço'], sample: [['Ana', '10']],"
        " fields: ['artist', 'album'], mapping: { '0': 'artist' } };"
        " renderCollectionMapping();"
        " answers.collection_import_preview = { ok: true, records: 1, already_here: [],"
        " skipped: [], ungraded: 0, no_year: 0, new_locations: [], new_categories: [] };"
        " await previewCollectionImport();"
        " const armed = $('collection-import-apply').disabled;"
        " const select = $('collection-mapping-body').children[0].children[2].children[0];"
        " select.value = ''; select.listeners.change();"
        " said.push(['armed', armed, $('collection-import-apply').disabled]);"
    )

    ran = _run(body)

    assert ["armed", False, True] in ran["said"], "an unchecked mapping could import"
    assert ["collection_import_preview", {"0": "artist"}] in ran["calls"]


def test_a_rehearsal_that_adds_nothing_cannot_import() -> None:
    body = (
        "state.collectionImport = { headers: ['A'], sample: [], fields: ['artist'], mapping: {} };"
        " answers.collection_import_preview = { ok: true, records: 0, already_here: [{ line: 2 }],"
        " skipped: [], ungraded: 0, no_year: 0, new_locations: [], new_categories: [] };"
        " await previewCollectionImport();"
    )

    ran = _run(body)

    assert ran["text"]["collection-import-summary"].startswith("0 records will be added.")
    assert ran["hidden"]["collection-import-summary"] is False


OFFERS = [
    {
        "source": "discogs",
        "release_id": "",
        "group_id": "900",
        "is_group": True,
        "title": "Night Songs",
        "artist": "The Harbour Trio",
        "year": None,
        "original_year": 1976,
        "country": "",
        "formats": [],
        "labels": [],
        "catalog_numbers": [],
        "url": "https://www.discogs.com/master/900",
    },
    {
        "source": "discogs",
        "release_id": "102",
        "group_id": "900",
        "is_group": False,
        "title": "Night Songs",
        "artist": "The Harbour Trio",
        "year": 1988,
        "original_year": 1976,
        "country": "XX",
        "formats": ["Vinyl", "LP"],
        "labels": ["Sample Records"],
        "catalog_numbers": ["SR-101"],
        "url": "https://www.discogs.com/release/102",
    },
    {
        "source": "discogs",
        "release_id": "401",
        "group_id": "",
        "is_group": False,
        "title": "Night Songs",
        "artist": "",
        "year": 1990,
        "original_year": None,
        "country": "",
        "formats": [],
        "labels": [],
        "catalog_numbers": [],
        "url": "https://www.discogs.com/release/401",
    },
]

DRAW_OFFERS = (
    f"answers.collection_find = {{ ok: true, via: 'search', candidates: {json.dumps(OFFERS)} }};"
    " await collectionFind(false);"
    " const drawn = $('collection-candidates').children.map((line) => ({"
    "   cls: line.className,"
    "   words: line.children[0].textContent,"
    "   badge: (line.children[0].children[0] || {}).textContent || '',"
    "   buttons: line.children[1].children.map((b) => [b.textContent, b.title || '']),"
    " }));"
    " said.push(['drawn', drawn]);"
)


def _drawn(result: dict) -> list[dict]:
    return next(entry[1] for entry in result["said"] if entry[0] == "drawn")


def test_the_album_is_drawn_as_a_heading_and_every_line_names_both_years() -> None:
    result = _run(DRAW_OFFERS)
    album, pressing, alone = _drawn(result)

    assert album["cls"] == "collection-candidate collection-candidate-album"
    assert album["badge"] == "Master"
    assert album["words"] == "The Harbour Trio - Night Songs (first released 1976)"
    assert [name for name, _ in album["buttons"]] == ["See it", "This is mine"]
    assert album["buttons"][1][1], "choosing an album does not say which pressing it adds"
    assert pressing["cls"] == "collection-candidate collection-candidate-pressing"
    assert pressing["words"] == (
        "The Harbour Trio - Night Songs (first released 1976 · this release 1988 · XX · "
        "Vinyl, LP · Sample Records · SR-101)"
    )
    assert pressing["buttons"][1][1] == "", "a pressing is the pressing: nothing to explain"
    assert alone["cls"] == "collection-candidate"
    assert alone["words"] == "Night Songs (1990)", "a pressing of no album has one year to say"
    assert (
        result["text"]["collection-add-status"] == "2 releases found. Pick the one you hold."
    ), "the album's own line was counted as a release found"


def test_choosing_the_album_asks_for_it_by_its_group_and_a_pressing_by_itself() -> None:
    result = _run(
        DRAW_OFFERS + " answers.collection_add = { ok: false, error: 'stop here' };"
        " for (const line of $('collection-candidates').children.slice(0, 2)) {"
        "   await line.children[1].children[1].listeners.click();"
        " }"
    )

    adds = [call for call in result["calls"] if call[0] == "collection_add"]
    assert adds == [
        ["collection_add", "", "discogs", "", "search", None, "900"],
        ["collection_add", "", "discogs", "102", "search", None, ""],
    ], "a pressing that also names its album was sent as the album"


def test_a_grade_is_drawn_by_its_abbreviation_and_keeps_its_whole_words() -> None:
    """Every grade of both scales, so one the scale gains is read the same way."""
    from diglibrary.collection.model import SLEEVE_GRADES

    result = _run(
        f"const grades = {json.dumps(list(SLEEVE_GRADES))};"
        " said.push(['short', grades.map(collectionGradeShort)]);"
        " const cell = collectionGradeCell(grades[1]); const none = collectionGradeCell(null);"
        " said.push(['cell', cell.textContent, cell.title, cell.className]);"
        " said.push(['none', none.textContent, none.title || '', none.className]);"
    )
    said = {entry[0]: entry[1:] for entry in result["said"]}

    assert said["short"] == [["M", "NM", "VG+", "VG", "G+", "G", "F", "P", "Generic", "No Cover"]]
    assert said["cell"] == ["NM", "Near Mint (NM or M-)", ""]
    assert said["none"] == ["Ungraded", "", "muted-cell"]
