"""Reading the folders dropped onto the window, which is the platform's job.

Dropping an album folder onto the library is the gesture that replaces
*Choose folder… → Scan*, and several at once is the point of it.

A page can see that something was dropped, but never where it came from — a
browser hands JavaScript a name and some bytes, and this application needs a
path on disk, because it reads the audio in place and never copies it. Only the
native side knows the path, so the drop is read here and the application layer
receives a plain list of strings, the same way it receives the folder dialog.

Three things about pywebview 6.2 are worth writing down, because each is
invisible until a drop does nothing:

- The paths are collected only while something is listening.
  `performDragOperation_` checks a listener count before it reads the
  pasteboard, so that count is what switches the feature on.
- **The listener itself is never registered.** pywebview answers a DOM listener
  by serialising the whole event for the bridge, and an event carries its
  `currentTarget` — the element listened on, with every descendant and each
  one's `innerHTML`, `outerHTML` and `textContent`. Listening on `<body>` sent
  the entire window across as one JSON string on every drop — about 2.4 kB
  for each element on the page, so tens of megabytes for a shelf of a thousand
  albums — and past the engine's longest string it raised *Out of memory*
  inside pywebview's own script, so nothing reached this side and the window
  waited for an answer that was never sent. The page already knows a
  drop happened; it asks for the paths through the bridge like any other call,
  and what crosses is the paths.
- The paths are recorded before the page hears of the drop — the native
  handler reads the pasteboard and only then hands the event to the webview —
  so by the time the page asks, they are there.
"""

import os
from collections.abc import Iterable

from webview.dom import _dnd_state

__all__ = ["listen", "take"]

_listening = False


def listen() -> None:
    """Switch on the native collection of dropped paths, once."""
    global _listening
    if _listening:
        return
    _listening = True
    _dnd_state["num_listeners"] += 1


def take() -> tuple[str, ...]:
    """Return the folders dropped since the last call, and forget them.

    Taken and cleared, because the platform appends to this list and never
    empties it: left in place, the next drop would add this one's folders
    again.
    """
    paths = [str(path) for _, path in _dnd_state["paths"]]
    _dnd_state["paths"].clear()
    return _folders(paths)


def _folders(paths: Iterable[str]) -> tuple[str, ...]:
    """Return the directories among ``paths``, in the order they were dropped."""
    found: list[str] = []
    for path in paths:
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
