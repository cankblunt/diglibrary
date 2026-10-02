"""How soon a tooltip appears, which only the platform can be asked.

Every tooltip in the window is the system's own, drawn for a `title`, and a
page has no say over when it opens: the wait belongs to the platform. Left at
the system's default it is about a second and a half, measured in this window's
own engine, which is long enough that a caveat living in a tooltip is a caveat
nobody waits for.

The value is *registered*, not written. A registered default is the lowest
layer the system reads, so a delay somebody has set for the whole machine still
wins, nothing is stored on disk, and it ends with the process.

macOS-only, and every failure is a no-op: a platform that cannot be asked keeps
its own delay and loses nothing else.
"""

import sys
from typing import Protocol

__all__ = ["DELAY_MILLISECONDS", "appear_sooner"]

DELAY_MILLISECONDS = 300
"""Soon, and still not for a pointer that is only passing over on its way."""

_SETTING = "NSInitialToolTipDelay"


class _Defaults(Protocol):
    def registerDefaults_(self, values: dict[str, int]) -> None: ...  # noqa: N802


def appear_sooner(defaults: _Defaults | None = None) -> bool:
    """Ask the platform to open tooltips sooner, and say whether it was asked.

    Call before the window starts. ``defaults`` is the store to register with,
    injected so this can be driven without the platform.
    """
    if defaults is None:
        if sys.platform != "darwin":
            return False
        try:
            import Foundation
        except ImportError:
            return False
        defaults = Foundation.NSUserDefaults.standardUserDefaults()
    defaults.registerDefaults_({_SETTING: DELAY_MILLISECONDS})
    return True
