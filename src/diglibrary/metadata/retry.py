"""Retry infrastructure for transient metadata HTTP failures."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class MetadataRequestError(RuntimeError):
    """A metadata request failure with explicit retry eligibility.

    ``status`` carries the HTTP status when the failure came from a response, so
    a caller can tell "this release has no cover art" from "the archive is
    down". It is absent for failures that never reached a status at all.
    """

    message: str
    retryable: bool = False
    status: int | None = None
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        """Return the request failure message."""
        return self.message


MAXIMUM_RETRY_AFTER_SECONDS = 60.0
"""The longest this project will wait because a server asked it to.

Honouring the header is the protocol; honouring an unbounded one would let a
single response freeze a library-sized scan for as long as it likes.
"""


class RetryPolicy:
    """Retry only explicitly transient request failures, waiting as asked when told."""

    def __init__(
        self,
        max_attempts: int,
        backoff_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a retry policy with a bounded total number of attempts."""
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    def execute(self, operation: Callable[[], ResultT]) -> ResultT:
        """Run an operation, retrying only transient ``MetadataRequestError`` failures.

        A server that answers 429 usually says how long to wait, and half a
        second is not it. Scanning a large library makes that answer routine
        rather than exceptional: ignoring it spends the retries
        in a second and files the album as unidentified over a limit that would
        have cleared.
        """
        for attempt in range(1, self._max_attempts + 1):
            try:
                return operation()
            except MetadataRequestError as error:
                if not error.retryable or attempt == self._max_attempts:
                    raise
                requested = min(error.retry_after_seconds or 0.0, MAXIMUM_RETRY_AFTER_SECONDS)
                self._sleep(max(self._backoff_seconds * attempt, requested))
        raise AssertionError("Retry policy exhausted without executing an operation.")
