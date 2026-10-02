"""Where an optional service key goes when the window is the one storing it.

Every test here is about something this must not do. It writes a file that a
shell sources at launch, so a mistake in it is a mistake with a shell's
authority, and it handles the two values in this project that must never be
read back out.
"""

import os
import stat
from pathlib import Path

import pytest

from diglibrary.config import credentials


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / ".diglibrary" / "env"
    monkeypatch.setattr(credentials, "ENV_FILE", path)
    for name in credentials.SETTABLE:
        monkeypatch.delenv(name, raising=False)
    return path


def test_a_key_reaches_the_file_and_this_process_at_once(env_file: Path) -> None:
    """Only the file would mean a token that works tomorrow and not today.

    Which reads, from the window, exactly like a token that was rejected.
    """
    credentials.store(credentials.DISCOGS, "abc123")

    assert 'export DIGLIBRARY_DISCOGS_TOKEN="abc123"' in env_file.read_text()
    assert os.environ[credentials.DISCOGS] == "abc123"
    assert credentials.connected()[credentials.DISCOGS] is True


def test_the_file_is_not_readable_by_anyone_else(env_file: Path) -> None:
    """A key in a world-readable file is a key on a shared machine's disk."""
    credentials.store(credentials.DISCOGS, "abc123")

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700


def test_setting_a_key_twice_leaves_one_line(env_file: Path) -> None:
    """A file with three generations of one token is one where what is in force
    depends on which line the shell read last."""
    credentials.store(credentials.DISCOGS, "first")
    credentials.store(credentials.DISCOGS, "second")

    lines = [line for line in env_file.read_text().splitlines() if line.strip()]
    assert lines == ['export DIGLIBRARY_DISCOGS_TOKEN="second"']


def test_an_empty_value_disconnects_and_keeps_the_others(env_file: Path) -> None:
    """The way to disconnect is the same field, emptied."""
    credentials.store(credentials.DISCOGS, "abc")
    credentials.store(credentials.ACOUSTID, "xyz")
    credentials.store(credentials.DISCOGS, "  ")

    text = env_file.read_text()
    assert "DIGLIBRARY_DISCOGS_TOKEN" not in text
    assert 'export DIGLIBRARY_ACOUSTID_KEY="xyz"' in text
    assert credentials.DISCOGS not in os.environ
    assert credentials.connected()[credentials.ACOUSTID] is True


def test_the_two_halves_of_a_spotify_key_are_stored_separately(env_file: Path) -> None:
    """The client-credentials flow is a pair, and half of one reaches nothing.

    Two settable names rather than one field the window would have to split:
    a secret pasted where the id goes has to fail as the wrong half, not as a
    rejected key.
    """
    credentials.store(credentials.SPOTIFY_ID, "an-id")
    credentials.store(credentials.SPOTIFY_SECRET, "a-secret")

    text = env_file.read_text()
    assert 'export DIGLIBRARY_SPOTIFY_CLIENT_ID="an-id"' in text
    assert 'export DIGLIBRARY_SPOTIFY_CLIENT_SECRET="a-secret"' in text
    held = credentials.connected()
    assert held[credentials.SPOTIFY_ID] and held[credentials.SPOTIFY_SECRET]


def test_a_value_can_never_become_shell(env_file: Path) -> None:
    """This file is `.`-sourced, so an unescaped quote stops being data.

    No real Discogs or AcoustID key carries any of these four characters, and
    that is exactly why it is handled here: the day one does, nobody will be
    looking at this function.
    """
    credentials.store(credentials.DISCOGS, 'a"; rm -rf $HOME; echo "b')

    line = env_file.read_text().strip()
    assert line.startswith('export DIGLIBRARY_DISCOGS_TOKEN="')
    assert line.endswith('"')
    # Every quote inside the value is escaped, so the string never closes early.
    assert line.count('"') - line.count('\\"') == 2


def test_only_the_names_this_application_owns_can_be_written(env_file: Path) -> None:
    """A name arriving from the page must not be able to write any line at all."""
    with pytest.raises(ValueError, match="Not a key"):
        credentials.store("PATH", "/tmp/evil")
    assert not env_file.exists()


def test_a_key_written_here_reaches_a_process_no_shell_prepared(env_file: Path) -> None:
    """The file is read back by the application itself, whatever launched it.

    The app bundle's launcher `.`-sources this file, and a terminal does not. If
    only the launcher read it, a key pasted into Settings would work when the
    application is opened by its icon and be invisible when it is opened from a
    terminal, over the same file on the same disk.
    """
    credentials.store(credentials.DISCOGS, "a-token")
    os.environ.pop(credentials.DISCOGS, None)
    assert not credentials.connected()[credentials.DISCOGS], "no shell prepared this process"

    assert credentials.load() == 1
    assert credentials.connected()[credentials.DISCOGS]
    assert os.environ[credentials.DISCOGS] == "a-token"


def test_a_name_the_shell_already_exported_is_left_alone(env_file: Path) -> None:
    """An explicit export in the shell outranks the stored convenience.

    It is also what makes this free under the icon, where the launcher has
    already sourced this very file into the environment.
    """
    credentials.store(credentials.DISCOGS, "the-file-one")
    os.environ[credentials.DISCOGS] = "the-shell-one"

    assert credentials.load() == 0
    assert os.environ[credentials.DISCOGS] == "the-shell-one"


def test_every_assignment_is_read_and_not_the_five_this_application_writes(
    env_file: Path,
) -> None:
    """Written as the complement, or the asymmetry moves to another variable.

    The launcher sources the whole file. A loader that read only `SETTABLE`
    would leave the README's own MusicBrainz contact address working by one
    route and not the other — the same defect, one name over. `SETTABLE` bounds
    what the *window* may write, because that name arrives from a page.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "# a comment, and a blank line follow\n\n"
        'export DIGLIBRARY_MUSICBRAINZ_CONTACT="someone@example.com"\n'
        "DIGLIBRARY_SOMETHING_UNQUOTED=plain\n",
        encoding="utf-8",
    )
    for name in ("DIGLIBRARY_MUSICBRAINZ_CONTACT", "DIGLIBRARY_SOMETHING_UNQUOTED"):
        os.environ.pop(name, None)

    assert credentials.load() == 2
    assert os.environ["DIGLIBRARY_MUSICBRAINZ_CONTACT"] == "someone@example.com"
    assert os.environ["DIGLIBRARY_SOMETHING_UNQUOTED"] == "plain"


def test_what_is_read_back_is_exactly_what_a_shell_would_read(env_file: Path) -> None:
    """The inverse of `_escaped`, proved against `sh` itself rather than by eye.

    Anything this reads differently from the shell that sources the same file is
    a key that works by one route and not the other. So the value goes in
    through `store`, comes back out through `load`, and comes back out again
    through a real `sh` reading the real file — and the three have to agree.
    """
    import subprocess

    for value in (
        'a"; rm -rf $HOME; echo "b',
        "back\\slash",
        "a $DOLLAR and a `tick`",
        "plain-token-123",
        'trailing space and quote" ',
    ):
        credentials.store(credentials.DISCOGS, value)
        os.environ.pop(credentials.DISCOGS, None)
        credentials.load()

        from_the_shell = subprocess.run(
            ["/bin/sh", "-c", f'. "{env_file}"; printf %s "${credentials.DISCOGS}"'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        assert os.environ[credentials.DISCOGS] == value.strip(), f"round trip: {value!r}"
        assert from_the_shell == value.strip(), f"the shell reads it the same: {value!r}"
