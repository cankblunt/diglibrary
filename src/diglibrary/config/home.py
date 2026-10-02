"""Where DigLibrary keeps its own files when no configuration file is named.

The database, the logs, the caches and the backups are all resolved against
the folder the configuration file is in. Defaulting to `config.toml` in the
current directory suits a checkout, where everything sits beside the code, and
fails on an installed package, which ships no such file in the working
directory.

An explicit `--config` wins, and a `config.toml` in the current directory is
used when it is there, which keeps a checkout and any existing installation
working unchanged. Only when neither is true does this fall back to
`~/.diglibrary`, writing the template there the first time.

**Nothing is moved.** An installation that already has files somewhere goes on
using them; this only decides where a *new* one starts. Relocating a database
and its caches is a decision for whoever runs the application, not a default.
"""

import shutil
from pathlib import Path

from diglibrary.config.loader import ConfigurationError

HOME = Path("~/.diglibrary").expanduser()
"""The folder that also holds the credentials file and the spectrogram cache.
One place, so there is one thing to back up and one to delete."""

TEMPLATE = Path(__file__).parent / "default.toml"


def resolve(requested: Path | None) -> Path:
    """Return the configuration file to read, writing the default one if needed.

    Order: what was asked for, then a `config.toml` beside the working
    directory, then `~/.diglibrary/config.toml`. Only the last of the three is
    ever created, and only when it is absent.
    """
    if requested is not None:
        return requested
    here = Path("config.toml")
    if here.is_file():
        return here
    _refuse_to_start_beside_an_orphaned_database(here)
    settled = HOME / "config.toml"
    if not settled.is_file():
        HOME.mkdir(parents=True, exist_ok=True)
        # `copy` rather than a write of read text: the template is a file the
        # project ships, and copying it keeps the two identical byte for byte
        # so a diff against a later version says only what actually changed.
        shutil.copy(TEMPLATE, settled)
    return settled


def _refuse_to_start_beside_an_orphaned_database(expected: Path) -> None:
    """Stop rather than open an empty library next to a full one.

    The one shape where falling through is worse than failing: a directory that
    holds a database and no configuration. Everything a configuration names is
    resolved beside it, so the fresh template written to `~/.diglibrary` opens a
    *new, empty* database — and the screen that follows is a library of nothing,
    which is indistinguishable from a library that was lost.

    This is reachable because an installation's settings file is the one thing in
    such a folder that is not itself a database or a cache: it can be moved,
    renamed, or swept up by a `git clean -fdx` in a checkout, and the folders
    around it survive. The error names both files rather than replacing one.
    """
    databases = sorted(Path().glob("*.sqlite3"))
    if not databases:
        return
    raise ConfigurationError(
        f"Found {databases[0].name} here but no {expected.name}. This looks like an "
        f"installation whose settings file is missing, and starting without it would "
        f"open a new, empty library instead of that one. Restore {expected.name} "
        f"beside the database, or name a file with --config."
    )
