"""Running one generated program under node, from a file rather than an argument.

The window's script is tested by extracting functions from it, pasting them
beside stubs, and running the result. That program is tens of thousands of
characters, and it was handed to node as a command-line argument — which works
on a system whose limit is megabytes and is refused outright by Windows, whose
whole command line has to fit in 32,767 characters. Ninety-nine of this suite's
tests failed that way on a Windows runner: not one of them reached node, so not
one of them said anything about the window.

Written once, here, rather than in each file that needs it. The two ways a
program is run differed only in whether node was told to read it as a module,
and a file says that by its own extension, so the flag is gone with the
argument.

The codec is named for the same reason the application names it: `text=True`
alone decodes with whatever the machine prefers, which is UTF-8 here and a
legacy codepage elsewhere, and what comes back is then a wrong string rather
than a failure anybody can see.
"""

import os
import subprocess
import tempfile
from pathlib import Path


def run_node(
    program: str,
    *,
    module: bool = True,
    check: bool = True,
    timeout: float | None = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run one program under node and return what it finished with.

    The program is written to a temporary file and the file is what node is
    given, so nothing about its length reaches the command line. `.mjs` and
    `.cjs` are how node is told which kind of program it is reading; that is
    the whole of what `--input-type=module` used to say.
    """
    handle, name = tempfile.mkstemp(suffix=".mjs" if module else ".cjs", text=True)
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(program)
        return subprocess.run(
            ["node", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=check,
        )
    finally:
        path.unlink(missing_ok=True)
