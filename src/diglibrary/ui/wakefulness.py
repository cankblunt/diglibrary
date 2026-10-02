"""Keeping the machine awake while something is still coming in.

A download is the one thing this application does that it cannot finish on its
own: the bytes come from a stranger's machine over a network that stops existing
when this one sleeps. A machine that sleeps mid-album fails every transfer in
flight.

So while transfers are moving, an assertion is held. `caffeinate -i` asserts
against *idle* sleep only: the display still dims, the lid still works, and
closing it still sleeps the machine. That is the limit of this: it stops the
machine dozing off while nobody is touching it, and it cannot stop the lid being
shut.

macOS-only, and every failure is a no-op: a platform without `caffeinate` loses
the assertion and keeps the application.
"""

import logging
import subprocess
import sys
import threading

__all__ = ["Wakefulness"]


class Wakefulness:
    """Hold, and release, one assertion that the machine should not doze off."""

    def __init__(self, logger: logging.Logger) -> None:
        """Create a released assertion."""
        self._logger = logger
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def set(self, wanted: bool) -> None:
        """Hold the assertion, or release it. Asking for what is already so is free."""
        with self._lock:
            if wanted:
                self._hold()
            else:
                self._release()

    def _hold(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if sys.platform != "darwin":
            return
        try:
            # No timeout and no waiting: this outlives the call and is ended by
            # being killed. `-i` is idle sleep only — see the module docstring
            # for what that deliberately does not cover.
            self._process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            # A machine that will not be kept awake still downloads; it just
            # stops when it sleeps.
            self._logger.info(
                "This machine could not be asked to stay awake.",
                extra={"operation": "ui.wakefulness"},
            )
            self._process = None
            return
        self._logger.info(
            "Holding the machine awake while transfers are moving.",
            extra={"operation": "ui.wakefulness"},
        )

    def _release(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except Exception:
            return
        self._logger.info(
            "Nothing is moving; the machine may sleep again.",
            extra={"operation": "ui.wakefulness"},
        )
