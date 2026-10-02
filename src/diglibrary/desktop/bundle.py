"""Assemble DigLibrary.app: a double-clickable, dockable macOS application.

This builds the *bundle*, not a frozen binary. It points at the interpreter that
asked for it — the checkout's `.venv` on a developer machine, the installation's
own on somebody who ran `pip install diglibrary` — so the icon launches the code
that is actually installed, with no rebuild between an edit and a run.

**This bundle is never distributed.** The launcher below writes an absolute
path taken from the machine it is built on, so on any other machine it is an
icon that launches nothing. What is published is the source.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from diglibrary.metadata.authentication import application_version

BUNDLE_ID = "org.diglibrary.app"

# `{start}` is a whole line rather than a path, because there is nothing to `cd`
# into when this is not a checkout: an installation's settings live in
# ~/.diglibrary and reaching for a source folder that is not there would be an
# invented answer. In a checkout it is still written, so a `config.toml` beside
# the code goes on winning exactly as it did.
LAUNCHER = """#!/bin/sh
# Launched from the Dock, an application inherits none of a shell's
# environment, so the credentials are read here. Keep them in
# ~/.diglibrary/env, which this file only ever sources — it never holds one.
set -e
[ -f "$HOME/.diglibrary/env" ] && . "$HOME/.diglibrary/env"
# An app launched from the Dock inherits no shell PATH, so the external
# binaries this project uses — ffmpeg, ffprobe, one day fpcalc — would be
# invisible. Homebrew's locations are added ahead of the system default.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
{start}
# No --config: the configuration file is resolved in the same order for every
# way of starting this. A config.toml beside the code still wins, because the
# line above starts in that folder; otherwise ~/.diglibrary/config.toml is used.
exec {python} -m diglibrary \\
  >>"$HOME/Library/Logs/DigLibrary.log" 2>&1
"""


def build(destination: Path) -> Path:
    """Write the bundle and return where it went."""
    bundle = destination / "DigLibrary.app"
    contents = bundle / "Contents"
    if bundle.exists():
        shutil.rmtree(bundle)
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir(parents=True)

    _put_icon(contents / "Resources" / "DigLibrary.icns")

    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleName": "DigLibrary",
                "CFBundleDisplayName": "DigLibrary",
                "CFBundleIdentifier": BUNDLE_ID,
                # The build number moves every time, and it has to: LaunchServices
                # keeps the icon and the metadata it read for a bundle id, and a
                # version that never changes is the condition under which it
                # declines to read them again. The human-facing version is the
                # project's own and stays put.
                "CFBundleVersion": str(int(time.time())),
                "CFBundleShortVersionString": application_version(),
                "CFBundleExecutable": "DigLibrary",
                "CFBundleIconFile": "DigLibrary",
                "CFBundlePackageType": "APPL",
                "LSMinimumSystemVersion": "12.0",
                # It is a window, not a background agent: it belongs in the
                # Dock and takes focus when launched.
                "LSUIElement": False,
                "NSHighResolutionCapable": True,
                "NSHumanReadableCopyright": "MIT licensed.",
            }
        )
    )

    launcher = contents / "MacOS" / "DigLibrary"
    launcher.write_text(LAUNCHER.format(start=_start_line(), python=_quote(Path(sys.executable))))
    launcher.chmod(0o755)

    # The finder caches by modification time; touching the bundle makes a
    # rebuilt icon appear without a logout.
    subprocess.run(["touch", str(bundle)], check=False)
    return bundle


ICON = Path(__file__).resolve().parent / "DigLibrary.icns"


def _put_icon(destination: Path) -> None:
    """Copy the icon this package carries. It is not drawn here, and that is a decision.

    Drawing every size takes several seconds — the mark is analytic geometry
    in pure Python and every size is redrawn rather than resized. That is
    affordable for a command typed in a terminal and not for a box ticked in
    the first-run window while it waits.

    The risk of carrying a prebuilt file is that it goes stale when the drawing
    changes and every build copies the old artwork. `iconutil` is deterministic
    (building twice gives the same bytes), so `tests/ui/test_icon.py` redraws a
    size and refuses a shipped icon that has drifted from the drawing that
    made it.
    """
    shutil.copy(ICON, destination)


def _start_line() -> str:
    """The `cd` a checkout needs, and the nothing an installation needs.

    A `config.toml` beside the code wins over `~/.diglibrary/config.toml`, and
    that is how a checkout keeps its own installation — so the bundle built from
    one has to start there or it would quietly run against different settings
    than the terminal does. An installation by name has no such folder, and
    naming one it does not have would be inventing an answer.
    """
    root = checkout_root()
    if root is None:
        return "# Installed by name: no source folder to start in."
    return f'export DIGLIBRARY_HOME={_quote(root)}\ncd "$DIGLIBRARY_HOME"'


def checkout_root() -> Path | None:
    """The repository this is running out of, or None when it is installed.

    Asked by what is actually on the disk rather than by how the import happened:
    the folder two above `diglibrary/desktop/` holds `pyproject.toml` in a
    checkout and holds `site-packages` in an installation.
    """
    root = Path(__file__).resolve().parent.parent.parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def _quote(path: Path) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def make_icon(destination: Path | None = None) -> tuple[Path, bool]:
    """Build into the requested folder, or into ~/Applications by default.

    Returns where it went and whether one was already standing there. **Both
    callers need that second answer** — the command prints `Created` or
    `Replaced`, and the window says the same two things — so it is decided here,
    once. Asked either way, an existing bundle is rebuilt rather than left alone:
    it carries the interpreter's path and this version's number inside it, so one
    from before an upgrade points at the old installation.
    """
    os.umask(0o022)
    folder = (destination or Path.home() / "Applications").expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    replaced = (folder / "DigLibrary.app").exists()
    return build(folder), replaced
