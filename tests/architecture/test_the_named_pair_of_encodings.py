"""Every place asking *lossless or transcoded* is a place that must answer why.

This project's standing rule is that a set is written as its complement, so a
member added later falls inside it. This pair is the one shape that escapes the
rule and must be named instead: `undecided` is not a case of the thing being
classified but the statement that it could not be classified, returned by
`judge` before any container has been looked at. Written as a complement —
`not in ("lossy", "overstated")` — a user's word about a track would re-judge an
honest MP3, which a word about a lossless container must never do.

So the pair is named on purpose in a fixed set of places, and the danger is a
new one that writes the complement instead. The question is spelled three ways
in this tree — strings in parentheses, the enum in braces, and the enum in
parentheses — so a search by text for any one of them misses the others.

This walks every comparison in the source and asks which ones mention both
labels, in any spelling. A new one fails here, and it is not to be added to the
list without a reason: if what is wanted is *inside a lossless container*, the
source of that fact is the file's codec, not this field.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent.parent / "src" / "diglibrary"

# Where the question is asked today, with what each one decides. Written as
# file:line would rot on the first edit above them, so they are located by the
# function that holds them — which is also what a reader needs to judge a new one.
ASKED_IN = {
    # Whether a user's word re-judges one track on the Quality table.
    ("application/api.py", "_what_stands_about"),
    # Whether a user's words re-judge a whole album.
    ("application/bench.py", "_album_word"),
    # Which stored rows are re-judged when a verdict is replayed on read.
    ("application/quality.py", "_stored_encoding"),
    # Whether one stored row counts as a transcode under today's rule.
    ("application/quality.py", "stored_track_is_transcoded"),
    # Which rows a replayed album verdict reads its numbers from.
    ("application/quality.py", "verdict_from_stored"),
    # Whether an album has a margin to report at all.
    ("quality/confidence.py", "_margin_db"),
    # Whether the shared-measurement warning applies to this file.
    ("quality/confidence.py", "judge_confidence"),
}

BOTH = {"lossless", "transcoded"}


def _labels(node: ast.AST) -> set[str]:
    """Every label a comparison mentions, whether written as a string or an enum."""
    found: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            found.add(inner.value.lower())
        elif isinstance(inner, ast.Attribute):
            found.add(inner.attr.lower())
    return found


def _asking_functions() -> set[tuple[str, str]]:
    """Return (file, function) for every comparison naming both labels."""
    asking: set[tuple[str, str]] = set()
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for holder in ast.walk(tree):
            if not isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(holder):
                if isinstance(node, ast.Compare) and _labels(node) >= BOTH:
                    asking.add((str(path.relative_to(SOURCE)), holder.name))
    return asking


def test_no_new_place_asks_lossless_or_transcoded_without_saying_why() -> None:
    """A new site is a decision, not an edit: it is listed with what it decides."""
    found = _asking_functions()

    appeared = found - ASKED_IN
    assert not appeared, (
        "a new place asks *lossless or transcoded*: "
        f"{sorted(appeared)}. If it means *inside a lossless container*, that is a "
        "fact of the file's codec and not of this field; if it belongs "
        "here, add it above with what it decides."
    )

    gone = ASKED_IN - found
    assert not gone, (
        f"these no longer ask it: {sorted(gone)}. If the question moved, this list "
        "moves with it; a guard listing places that are not there passes on nothing."
    )


def test_the_pair_is_never_written_as_its_complement() -> None:
    """The complement is the specific mistake this guard exists to prevent.

    `not in ("lossy", "overstated")` reads as the careful thing to write here and
    is the one thing that must not be: it puts `undecided` inside, and `undecided`
    is where a silent MP3 and a silent FLAC are the same value.
    """
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and _labels(node) >= {"lossy", "overstated"}:
                offenders.append(f"{path.relative_to(SOURCE)}:{node.lineno}")

    assert not offenders, (
        f"the pair is written as its complement at {offenders} — this lets a word "
        "about an honest MP3 re-judge it, which the named pair exists to prevent."
    )
