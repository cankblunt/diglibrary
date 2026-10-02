"""Asking the platform which folder to work on, in a window wide enough to read.

The folder dialog pywebview opens is the system's own, created with no size and
no memory, so it arrives at the width macOS chose and forgets whatever the user
does to it. An album folder is named `Artist - Album (Year) [FORMAT]`, which is
easily sixty characters before the format, and a narrow column truncates every
name in the middle — a list of albums none of which can be told apart.

So the panel is opened here instead, with two differences and nothing else: it
starts wide, and it is given an autosave name, which is what makes macOS
remember the size the panel is dragged to.

Everything here is macOS-only and every failure is a ``None`` — the caller falls
back to the dialog pywebview would have opened, so a machine without AppKit, or
a future Windows build, loses the width and keeps the feature.
"""

import sys
import threading

_PANEL_NAME = "DigLibraryFolderPanel"
"""Where macOS keeps the size this panel was left at, between launches."""

_FIRST_RUN_WIDTH = 1180
_FIRST_RUN_HEIGHT = 720
"""The size before anything was resized: enough for a full album folder name.

Only ever applied when macOS has no remembered frame — a size the user set is
never overridden.
"""

_SCREEN_SHARE = 0.7
"""The most of a screen the first-run panel may take, so it fits small displays."""


def available() -> bool:
    """Report whether this platform can be asked to open the panel described here.

    Asked separately from asking, because ``None`` from the ask means *the
    panel was cancelled* — and a caller that read a cancel as "unavailable" would answer
    it by opening a second dialog.
    """
    if sys.platform != "darwin":
        return False
    try:
        import AppKit  # noqa: F401
        import Foundation  # noqa: F401
        from PyObjCTools import AppHelper  # noqa: F401
    except ImportError:
        return False
    return True


def choose_folder(start: str | None = None) -> str | None:
    """Return the folder that was picked, or ``None`` when none was.

    Only call this when :func:`available` is true.
    """
    import AppKit
    import Foundation
    from PyObjCTools import AppHelper

    chosen: list[str | None] = []
    finished = threading.Semaphore(0)

    def show() -> None:
        try:
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setCanCreateDirectories_(True)
            panel.setAllowsMultipleSelection_(False)
            panel.setTitle_("Choose the folder to scan")
            if start:
                panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(start))
            _size(AppKit, panel)
            ok = panel.runModal() == AppKit.NSFileHandlingPanelOKButton
            urls = panel.URLs() if ok else None
            chosen.append(str(urls[0].path()) if urls else None)
        except Exception:
            chosen.append(None)
        finally:
            finished.release()

    # The panel is modal and belongs to the main thread; this is called from the
    # bridge's thread. Handing it over and waiting is what pywebview does for
    # the same reason.
    AppHelper.callAfter(show)
    finished.acquire()
    return chosen[0] if chosen else None


def _size(appkit, panel) -> None:
    """Give the panel a readable first-run size, and let macOS remember the next."""
    panel.setFrameAutosaveName_(_PANEL_NAME)
    if panel.setFrameUsingName_(_PANEL_NAME):
        # A remembered frame exists: the panel was resized before, and that
        # size outranks the default.
        return
    screen = appkit.NSScreen.mainScreen()
    if screen is None:
        return
    visible = screen.visibleFrame()
    width = min(_FIRST_RUN_WIDTH, visible.size.width * _SCREEN_SHARE)
    height = min(_FIRST_RUN_HEIGHT, visible.size.height * _SCREEN_SHARE)
    origin_x = visible.origin.x + (visible.size.width - width) / 2
    origin_y = visible.origin.y + (visible.size.height - height) / 2
    panel.setFrame_display_(((origin_x, origin_y), (width, height)), False)
