"""Text coming back from another program is decoded by a named codec, not by luck.

``subprocess.run(..., text=True)`` with no ``encoding`` decodes with whatever
``locale.getpreferredencoding()`` says, which is UTF-8 on the machine this was
written on and a legacy codepage on Windows. Nothing raises: the bytes decode
into different characters, and what arrives is a string that is wrong rather
than a failure anybody can see. Measured on a Windows runner, 62 of the suite's
failures were this shape, and two of the calls were this application's own —
one of them the reader of a program that is asked for JSON, which is UTF-8 by
its own specification.

Written over every call that asks for text rather than over the two that were
found, because the next one will be written by somebody who did not read this.
"""

import ast
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
#: Every folder this repository runs Python out of. The rule is about reading
#: another program's answer, which the suite and the tools do as much as the
#: application — and the suite is where it was first measured going wrong.
ROOTS = (PROJECT / "src", PROJECT / "tests", PROJECT / "tools", PROJECT / "benchmark")

RUNNERS = {"run", "Popen", "check_output", "call", "check_call"}
"""The ways this project starts another program; `call` and friends for the day
somebody reaches for one."""

ASKS_FOR_TEXT = {"text", "universal_newlines"}


def _calls_that_ask_for_text() -> list[tuple[str, int, bool]]:
    """Every subprocess call in the application, and whether it names a codec."""
    found = []
    for path in sorted(file for root in ROOTS for file in root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else None
            if name not in RUNNERS:
                continue
            receiver = function.value
            if not (isinstance(receiver, ast.Name) and receiver.id == "subprocess"):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            if not keywords & ASKS_FOR_TEXT:
                continue
            found.append((str(path.relative_to(PROJECT)), node.lineno, "encoding" in keywords))
    return found


#: What each interpreter calls *the next argument is the program*. Paired with
#: the interpreter rather than read on its own, because the same letter is
#: something else elsewhere: `git -c` sets a configuration value and
#: `iconutil -c` names a conversion. The first version of this named `python`
#: and looked for `-e`, which is `node`'s spelling and not one `python` has, so
#: it could never have caught a `python` call at all.
A_PROGRAM_FOLLOWS = {
    "node": {"-e", "--eval", "-p", "--print"},
    "python": {"-c"},
    "python3": {"-c"},
}


def _programs_handed_over_as_an_argument() -> list[str]:
    """Every place a program is given to an interpreter on the command line.

    It reads the first word of an argument list, so an interpreter this project
    has never started is not covered — the enumeration is of the ways this
    repository runs code, and it is the thing to extend when another one is
    reached for. A shell started with `-c` is deliberately not here: what is
    passed to one is a command line of its own, bounded by what is written at
    the call, not a program assembled out of a file.
    """
    found = []
    for path in sorted(file for root in ROOTS for file in root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or not node.elts:
                continue
            words = [
                element.value if isinstance(element, ast.Constant) else None
                for element in node.elts
            ]
            flags = A_PROGRAM_FOLLOWS.get(words[0] if isinstance(words[0], str) else "")
            if flags and flags & {word for word in words if isinstance(word, str)}:
                found.append(f"{path.relative_to(PROJECT)}:{node.lineno}")
    return found


def test_every_program_this_reads_as_text_is_decoded_by_a_named_codec() -> None:
    """A call that asks for text and names no codec is decoded by the machine."""
    calls = _calls_that_ask_for_text()

    # A check that found no call to check passes, and proves nothing.
    assert calls, "no subprocess call asking for text was found at all"

    silent = [f"{where}:{line}" for where, line, named in calls if not named]
    assert not silent, (
        "these decode another program's output with whatever codec the machine "
        f"happens to prefer: {silent}"
    )


def test_no_program_is_handed_to_an_interpreter_on_the_command_line() -> None:
    """A generated program goes in a file; the command line has a length.

    The window's script is tested by pasting functions out of it beside stubs,
    which makes a program of tens of thousands of characters. Handed over as an
    argument it works wherever the limit is counted in megabytes and is refused
    outright where the whole command line has to fit in 32,767 characters — and
    a test that never reached the interpreter proved nothing about the window.
    Ninety-nine of them did that at once.
    """
    handed = _programs_handed_over_as_an_argument()

    assert handed == [], (
        "these put a whole program on the command line, which a platform may "
        f"refuse by its length alone; write it to a file instead: {handed}"
    )
