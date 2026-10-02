"""Where the application keeps its files when nobody has said.

No configuration file is shipped, so a default that is a relative path ends a
first run in `Configuration file does not exist: config.toml`. The other half
matters as much: an installation that already has files must go on using them,
because moving a database and its caches is a decision and not a default.
"""

import re
from pathlib import Path

import pytest

from diglibrary.config import home
from diglibrary.config.loader import ConfigurationError, load_configuration


def test_a_first_run_writes_the_template_and_everything_lands_beside_it(
    tmp_path: Path, monkeypatch
) -> None:
    """No config.toml here, none in the home folder: one is made, and it works."""
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)

    settled = home.resolve(None)

    assert settled == tmp_path / ".diglibrary" / "config.toml"
    assert settled.is_file(), "the template has to be written, or there is nothing to read"
    config = load_configuration(settled)
    # Relative paths resolve against the file, so the whole installation is one
    # folder — one thing to back up, one thing to delete.
    for path in (
        config.database.path,
        config.logging.directory,
        config.metadata.cache_directory,
        config.artwork.backup_directory,
    ):
        assert path.is_relative_to((tmp_path / ".diglibrary").resolve()), path


def test_a_config_beside_the_working_directory_still_wins(tmp_path: Path, monkeypatch) -> None:
    """A checkout, and an installation run from one, stay where they are."""
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[database]\npath = 'x.sqlite3'\n")

    assert home.resolve(None) == Path("config.toml")
    assert not (tmp_path / ".diglibrary").exists(), "nothing is created when nothing is needed"


def test_what_was_asked_for_always_wins(tmp_path: Path, monkeypatch) -> None:
    """An explicit --config is an explicit answer, and no default overrides it."""
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[database]\npath = 'x.sqlite3'\n")

    asked = tmp_path / "elsewhere.toml"
    assert home.resolve(asked) == asked


def test_an_existing_settled_config_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    """After the first run the file is the user's; the template is a starting point."""
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".diglibrary").mkdir()
    mine = tmp_path / ".diglibrary" / "config.toml"
    mine.write_text("[database]\npath = 'the one I edited.sqlite3'\n")

    assert home.resolve(None) == mine
    assert "the one I edited" in mine.read_text()


def test_a_database_with_no_settings_beside_it_stops_instead_of_opening_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """The one shape where falling through is worse than failing.

    Everything a configuration names is resolved beside it, so the fresh template
    written to `~/.diglibrary` opens a *new, empty* database — and a library of
    nothing is indistinguishable from a library that was lost. An installation
    kept in a working directory can lose its settings file and keep its
    database, its backups and its caches: whatever removes ignored files by
    pattern takes the one and leaves the folders.
    """
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "diglibrary.sqlite3").write_bytes(b"not really a database")

    with pytest.raises(ConfigurationError, match=re.escape("diglibrary.sqlite3")):
        home.resolve(None)

    assert not (tmp_path / ".diglibrary").exists(), "an empty library was set up anyway"


def test_an_empty_directory_still_gets_its_first_run(tmp_path: Path, monkeypatch) -> None:
    """The refusal is about an installation missing its settings, not about a
    first run — which is every clone, and has to keep working."""
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)

    assert home.resolve(None) == tmp_path / ".diglibrary" / "config.toml"
    assert (tmp_path / ".diglibrary" / "config.toml").is_file()


def test_naming_a_file_is_always_obeyed_even_beside_an_orphaned_database(
    tmp_path: Path, monkeypatch
) -> None:
    """`--config` is an explicit answer, and it is also the way out of the
    refusal above."""
    monkeypatch.setattr("diglibrary.config.home.HOME", tmp_path / ".diglibrary")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "diglibrary.sqlite3").write_bytes(b"not really a database")
    asked = tmp_path / "elsewhere.toml"

    assert home.resolve(asked) == asked
