"""Thread-safe request pacing for external metadata services."""

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Reserve request slots so calls do not exceed a configured average rate."""

    def __init__(
        self,
        requests_per_second: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a limiter that spaces requests by the inverse of the configured rate."""
        self._interval = 1.0 / requests_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Wait until this caller's next request slot is available."""
        with self._lock:
            now = self._monotonic()
            scheduled = max(now, self._next_slot)
            self._next_slot = scheduled + self._interval
        delay = scheduled - now
        if delay > 0:
            self._sleep(delay)
