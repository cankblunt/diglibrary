"""The by-hand answer to the changelog check, which must cost something to use.

`check_every_commit_is_accounted_for` refuses a commit that changed what a user
meets and wrote no changelog line. Often the answer is that a neighbour wrote
the line — a change made over three commits and described once — and
`internal/changelog_accounted_for.toml` is where that is said.

Everything here is about the one way that file could go wrong: becoming free.
An entry accepted with a placeholder reason, or naming a commit this repository
does not have, is coverage nobody paid for. The real file is read too, where
there is one, because a guard that only sees its own fixtures agrees with
itself.
"""

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

import preflight_release  # noqa: E402

REAL = PROJECT / "internal" / "changelog_accounted_for.toml"
LONG_ENOUGH = "Described in the entry of this release, written by the commit that closed it."


def _read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str):
    """Read one forged file through the real parser."""
    folder = tmp_path / "internal"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "changelog_accounted_for.toml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(preflight_release, "PROJECT", tmp_path)
    # Commits still resolve against the real repository, which is the point: an
    # entry is only an answer if it names a commit that exists.
    monkeypatch.setattr(
        preflight_release,
        "_git",
        lambda *arguments: subprocess.run(
            ["git", "-C", str(PROJECT), *arguments],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
    )
    return preflight_release._accounted_for()


def test_a_placeholder_reason_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The cost of using this file is having to write the reason, or it is free."""
    answers, complaints = _read(
        monkeypatch,
        tmp_path,
        '[[accounted]]\ncommit = "HEAD"\nwhere = "covered"\n',
    )

    assert answers == {}, "nothing is accounted for by a word"
    assert complaints == ["HEAD — the reason is too short to be one"]


def test_a_commit_this_repository_does_not_have_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hash naming nothing would sit here for ever looking like coverage."""
    answers, complaints = _read(
        monkeypatch,
        tmp_path,
        f'[[accounted]]\ncommit = "deadbee"\nwhere = "{LONG_ENOUGH}"\n',
    )

    assert answers == {}
    assert complaints == ["deadbee — no such commit in this repository"]


def test_an_answer_is_filed_under_the_whole_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git's abbreviation is not a fixed width — it grows with the repository.

    An entry written as seven characters today and compared against whatever git
    prints tomorrow would silently stop answering for its own commit, and the
    release would be refused for a reason that had already been given.
    """
    head = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    answers, complaints = _read(
        monkeypatch,
        tmp_path,
        f'[[accounted]]\ncommit = "{head[:7]}"\nwhere = "{LONG_ENOUGH}"\n',
    )

    assert complaints == []
    assert list(answers) == [head], "the short form answers for the full hash"


def test_a_missing_file_is_simply_no_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No file is not a failure: it is a release where nobody needed to say anything."""
    monkeypatch.setattr(preflight_release, "PROJECT", tmp_path)

    assert preflight_release._accounted_for() == ({}, [])


def test_the_file_this_repository_ships_answers_for_real_commits() -> None:
    """The real file, read the real way.

    Every entry must name a commit that exists and carry a reason somebody wrote,
    which is what the preflight demands of it at release time. A hash mistyped
    into the file is caught here rather than on the day of a release.
    """
    answers, complaints = preflight_release._accounted_for()

    assert complaints == [], "the shipped file is refused by its own parser"
    if REAL.exists():
        assert answers, "the file exists and accounts for nothing, which is not a state"
