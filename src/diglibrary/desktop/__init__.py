"""The application icon, for whichever installation is running.

It is not a Dock icon and this module never puts one there: what it writes is
`~/Applications/DigLibrary.app`, and macOS only lets a person drag something
into a Dock.

The code lives inside the package rather than in `tools/`, because `tools/` is
not part of the wheel: an installation by name could not otherwise create an
icon and would have to be started from a terminal every time. There is one
implementation, used by a checkout and by an installation alike.

The bundle is assembled **on the machine that will run it**, out of the
interpreter that is running at that moment, and it is not distributed.
"""
