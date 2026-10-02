"""Reading the folders dropped onto the window, which is the platform's job.

Dropping an album folder onto the library is the gesture that replaces
*Choose folder… → Scan*, and several at once is the point of it.

A page can see that something was dropped, but never where it came from — a
browser hands JavaScript a name and some bytes, and this application needs a
path on disk, because it reads the audio in place and never copies it. Only the
native side knows the path, so the drop is read here and the application layer
receives a plain list of strings, the same way it receives the folder dialog.

Two things about pywebview 6.2 are worth writing down, because both are
invisible until a drop silently does nothing:

- The paths are collected only while something is listening. `performDragOperation_`
  checks a listener count before it reads the pasteboard, so registering the
  handler is what switches the feature on, not just what consumes it.
- `pywebviewFullPath` is attached by matching a recorded path against the name
  the page reports, and for a **directory** the recorded key is the name of its
  *parent* — so for a dropped folder the two never match and the path is never
  attached. The paths are still collected, so they are read from where they are
  collected. Both routes are honoured here: the attached one when it is there,
  the collected list when it is not.
"""

import os
from collections.abc import Callable, Iterable

from webview.dom import _dnd_state

__all__ = ["listen"]


def listen(window: object, deliver: Callable[[tuple[str, ...]], None]) -> bool:
    """Call ``deliver`` with the folders dropped anywhere on ``window``.

    Returns whether the listener could be registered. A platform that does not
    carry paths through a drop loses the gesture and keeps the application:
    every other way of adding a folder is untouched.
    """
    try:
        body = window.dom.body  # type: ignore[attr-defined]
    except Exception:
        return False

    def dropped(event: dict[str, object]) -> None:
        deliver(_folders(event))

    try:
        body.events.drop += dropped
    except Exception:
        return False
    return True


def _folders(event: dict[str, object]) -> tuple[str, ...]:
    """Return the directories in one drop, in the order they were dropped."""
    found: list[str] = []
    for path in _paths(event):
        # A folder is what this application organises. Dropping the tracks
        # themselves means the same album, so the folder holding them is what
        # is taken — and dropping ten tracks of one album adds it once.
        #
        # The parent is taken only for something that is really there. A path
        # that has gone has a parent that has not, and that parent is whatever
        # folder it sat in — possibly the whole music root. Reading a vanished
        # path as its parent is how a gesture meant to add one album ends up
        # scanning a whole library.
        if os.path.isdir(path):
            folder = path
        elif os.path.isfile(path):
            folder = os.path.dirname(path)
        else:
            continue
        if folder and folder not in found:
            found.append(folder)
    return tuple(found)


def _paths(event: dict[str, object]) -> Iterable[str]:
    """Read every real path this drop carried, by whichever route carried it."""
    transfer = event.get("dataTransfer") or {}
    files = transfer.get("files", []) if isinstance(transfer, dict) else []
    attached = [
        str(entry["pywebviewFullPath"])
        for entry in files
        if isinstance(entry, dict) and entry.get("pywebviewFullPath")
    ]
    # Whatever was not matched to a name is still recorded, and for a dropped
    # folder nothing ever is. Taken and cleared, so the next drop starts empty
    # rather than re-adding this one.
    collected = [str(path) for _, path in _dnd_state["paths"]]
    _dnd_state["paths"].clear()
    return [*attached, *collected]
