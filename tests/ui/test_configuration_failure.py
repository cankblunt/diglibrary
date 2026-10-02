"""What the application shows when it cannot read its own settings."""

from pathlib import Path

import pytest

from diglibrary.config.loader import ConfigurationError
from diglibrary.ui import app


def test_settings_it_cannot_read_open_a_window_instead_of_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    """A `ConfigurationError` left uncaught in `run` is, from the Dock, an icon
    that bounces and stops.

    No window, nothing on any screen, and the reason written only to
    `~/Library/Logs/DigLibrary.log` — the last place someone whose application
    will not open goes looking. The message names the file and the key, so the
    window shows it.
    """
    opened: list[dict[str, object]] = []
    started: list[bool] = []
    monkeypatch.setattr(app.webview, "create_window", lambda *args, **kwargs: opened.append(kwargs))
    monkeypatch.setattr(app.webview, "start", lambda *args, **kwargs: started.append(True))
    broken = tmp_path / "config.toml"
    # Valid TOML, valid sections, and one value nobody can use — which is the
    # ordinary way this fails, rather than a file that is missing.
    broken.write_text(
        "[database]\npath = 'library.sqlite3'\n\n"
        "[logging]\nlevel = 'LOUD'\ndirectory = 'logs'\nfilename = 'app.jsonl'\n",
        encoding="utf-8",
    )

    app.run(broken)

    assert started == [True], "nothing was ever put on screen"
    assert len(opened) == 1
    page = str(opened[0]["html"])
    assert str(broken) in page, "the window does not say which file"
    assert "LOUD" in page, "the window does not say what is wrong with it"


def test_a_failure_that_is_not_about_settings_still_fails_loudly(
    tmp_path: Path, monkeypatch
) -> None:
    """Only the one someone can act on is answered with a window.

    Swallowing the rest would turn every start-up failure into the same polite
    page about a settings file, which names a cause nobody measured.
    """
    monkeypatch.setattr(
        app, "create_application", lambda _: (_ for _ in ()).throw(RuntimeError("disk on fire"))
    )

    with pytest.raises(RuntimeError, match="disk on fire"):
        app.run(tmp_path / "config.toml")


def test_the_page_cannot_be_ended_early_by_a_path_or_a_message() -> None:
    """A folder with a `<` in its name is legal, and neither of these two values
    is written by this project."""
    page = app._configuration_failure_page(
        Path("/Users/someone/<b>weird</b>/config.toml"), "Unsupported logging level: <script>"
    )

    assert "<b>weird</b>" not in page
    assert "<script>" not in page
    assert "&lt;b&gt;weird&lt;/b&gt;" in page and "&lt;script&gt;" in page


def test_the_error_it_answers_is_the_one_the_loader_raises() -> None:
    """Named by import rather than by string, so a rename cannot leave this
    catching nothing."""
    assert issubclass(ConfigurationError, ValueError)
    assert app.ConfigurationError is ConfigurationError
