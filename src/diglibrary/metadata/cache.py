"""Filesystem-backed JSON cache for metadata API responses."""

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from diglibrary.cachedir import MEGABYTE, clear_folder, enforce_folder_limit, folder_bytes, touch

SUFFIXES = (".json",)
"""What this cache writes, and therefore all it is ever allowed to delete."""

DEFAULT_LIMIT_BYTES = 250 * MEGABYTE
"""The default ceiling for this cache.

Every entry here is one HTTP answer that can be asked for again; the cost of
discarding one is a request against a rate limit, never a fact lost. Set high
enough that a full library scan does not evict its own work mid-run.
"""


class JsonMetadataCache:
    """Persist JSON-compatible API payloads with a time-to-live policy."""

    def __init__(
        self,
        directory: Path,
        ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a cache rooted at ``directory`` without reading or writing it yet."""
        self._directory = directory
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def get(self, key: str) -> Mapping[str, Any] | None:
        """Return an unexpired cached payload, or ``None`` when no valid entry exists.

        An entry that has expired is removed as it is found, so that reading
        the cache is also what clears what it can no longer serve.
        """
        path = self._path_for(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("expires_at"), (int, float)):
            return None
        value = payload.get("value")
        if payload["expires_at"] < self._clock():
            path.unlink(missing_ok=True)
            return None
        if not isinstance(value, dict):
            return None
        # A hit says so on the file itself, because the folder's ceiling
        # discards by modification time and would otherwise throw away exactly
        # the releases being asked for most (see `diglibrary.cachedir`).
        touch(path)
        return value

    def held_bytes(self) -> int:
        """Return what this cache holds on disk, so the window can say it."""
        return folder_bytes([self._directory], SUFFIXES)

    def enforce_limit(self, limit_bytes: int, logger: logging.Logger) -> int:
        """Discard the least recently used entries until the folder fits.

        Expiry alone never bounded this: an entry is only checked when it is
        read, so a release nobody asks for again is a file that outlives the
        installation.
        """
        return enforce_folder_limit(
            [self._directory], limit_bytes, SUFFIXES, logger, "metadata.cache.evicted"
        )

    def clear(self) -> int:
        """Delete every cached response here and return the bytes freed."""
        return clear_folder([self._directory], SUFFIXES)

    def set(self, key: str, value: Mapping[str, Any]) -> None:
        """Atomically store a JSON-compatible payload under ``key``.

        The temporary file is named for this write rather than for the key: a
        scan and a search can ask the same source for the same release at the
        same moment, and two writers sharing one temporary path interleave their
        bytes into it before either replaces the entry.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._path_for(key)
        payload = {"expires_at": self._clock() + self._ttl_seconds, "value": value}
        handle, temporary_name = tempfile.mkstemp(
            dir=self._directory, prefix=destination.stem, suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file, sort_keys=True)
            os.replace(temporary, destination)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.json"
