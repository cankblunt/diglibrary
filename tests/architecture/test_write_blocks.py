"""A block that writes says so when it opens, and this is what keeps it true.

In `sqlite3`'s legacy mode a `SELECT` runs in autocommit and the implicit
`BEGIN` arrives only with the `INSERT` — so a block that reads to decide and
then writes is doing those two things in **different transactions**. Almost
every write in this project has that shape: a folder is looked up before the
code chooses between an insert and an update. Two threads therefore both read
*not there*, both insert, and the second is refused by the unique index.
Measured over that exact pattern, DEFERRED gives one `ok` and one
`IntegrityError`; the block rolls back, and for a scan that is a batch of units
and files gone, surfacing as albums that never appear.

`write=True` holds the write lock across both, so the second thread waits, reads
again, finds the row and updates it. It is not the default because it would
serialize the reads too, and the reads are the tens of thousands.

Written as the complement: rather than listing the methods known to write, this
walks every `connect()` block in the tree and asks what is inside it, so a
method added later is covered when it is added.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent.parent / "src" / "diglibrary"

WRITING_VERBS = ("insert ", "update ", "delete ", "replace ", "create ", "drop ", "alter ")


def _statements_in(node: ast.AST) -> list[str]:
    """Every SQL-looking string literal inside this node."""
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = sub.value.strip().lower()
            if text.startswith(WRITING_VERBS):
                found.append(sub.value.strip().split("\n")[0][:60])
    return found


def _connect_blocks() -> list[tuple[Path, ast.With, ast.Call]]:
    """Every `with … .connect(…)` block in the package."""
    blocks = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "connect":
                    blocks.append((path, node, call))
    return blocks


def _asks_to_write(call: ast.Call) -> bool:
    return any(
        keyword.arg == "write"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def test_every_block_that_writes_opens_as_a_writer() -> None:
    """The lock. A block holding an `INSERT` and opened without `write=True` is
    one `SQLITE_BUSY_SNAPSHOT` away from discarding its batch in silence."""
    offenders = [
        (str(path.relative_to(SOURCE)), block.lineno, _statements_in(block)[0])
        for path, block, call in _connect_blocks()
        if _statements_in(block) and not _asks_to_write(call)
    ]

    assert offenders == [], (
        "these blocks write inside a DEFERRED transaction — pass write=True "
        f"where they open: {offenders}"
    )


def test_the_blocks_that_only_read_are_left_alone() -> None:
    """The other half, and the reason `write=True` is not simply the default:
    taking the write lock for a read serializes the tens of thousands of reads
    a scan makes. If this ever reaches zero, somebody has marked everything.
    """
    read_only = [
        block.lineno
        for _, block, call in _connect_blocks()
        if not _statements_in(block) and not _asks_to_write(call)
    ]

    assert len(read_only) > 20, (
        f"only {len(read_only)} read-only blocks left; write=True is being "
        "applied to blocks that do not write"
    )
