"""The upload carries no key, and cannot happen because somebody pushed.

Publishing to an index is the one action here that reaches every machine that
installed this application, and it is the one that cannot be undone: a version
number is never reused and a yank is a soft recall rather than an eraser. So the
workflow that does it is guarded.

Read as text rather than as YAML on purpose — this repository takes a runtime
dependency only with a reason (see `pyproject.toml`), and a parser is not needed
to ask whether a file contains a secret.
"""

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
WORKFLOW = PROJECT / ".github" / "workflows" / "publish.yml"


def _workflow() -> str:
    assert WORKFLOW.is_file(), f"{WORKFLOW} is how this is published, and it is not here"
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_publish_workflow_holds_no_secret_at_all() -> None:
    """Trusted publishing exists so that there is nothing to hold.

    The alternative keeps a long-lived token in a file, and its whole risk is
    that a token exists. A `secrets.` reference here would mean the mechanism had
    been swapped for the one with a key in it, which reads identically from the
    outside: both publish, and one of them can be stolen.
    """
    referenced = re.findall(r"secrets\.[A-Za-z_][A-Za-z0-9_]*", _workflow())

    assert referenced == [], (
        "this publishes with a stored secret, which is the mechanism trusted "
        f"publishing was chosen to remove: {sorted(set(referenced))}"
    )


def test_nothing_publishes_without_somebody_pressing_it() -> None:
    """Nothing is published unless a person starts the workflow.

    A `push` trigger turns a mistyped tag into a permanent release: the version
    number it spends can never be used again. The only way in is Run workflow,
    and the index defaults to the one that can be thrown away.
    """
    text = _workflow()
    # The `on:` block alone: from its heading to the next key at column zero.
    # Reading to the end of the file would collect the job names too.
    block = re.search(r"^on:\n(.*?)(?=^\S)", text, flags=re.MULTILINE | re.DOTALL)
    assert block is not None, "this workflow declares no triggers at all"
    triggers = re.findall(r"^  ([a-z_]+):", block.group(1), flags=re.MULTILINE)

    assert triggers == [
        "workflow_dispatch"
    ], f"something other than a person can start a release: {triggers}"
    assert re.search(r"default:\s*testpypi", text), (
        "the index this offers first should be the one a mistake can be thrown " "away on"
    )


def test_every_action_it_runs_is_pinned_to_a_commit() -> None:
    """A tag moves, and the thing it moves to runs with permission to publish.

    `@v1` is a label its author can repoint at any time, so trusting one is
    trusting whatever that repository contains on the day this runs. Pinned to a
    commit, an upgrade is a change somebody makes here on purpose. Written as
    *every* action rather than as the five in the file today.
    """
    unpinned = [
        used
        for used in re.findall(r"uses:\s*(\S+)", _workflow())
        if not re.search(r"@[0-9a-f]{40}$", used)
    ]

    assert unpinned == [], f"these run with this workflow's permissions and can move: {unpinned}"
