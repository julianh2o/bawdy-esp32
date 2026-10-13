"""API values: key/value pairs set over REST (e.g. live audio levels) that patterns read through ``type = "api"`` params.

Kept in memory only: a restart clears them, and patterns fall back to their params' defaults until values come in.
Writes replace the whole dict (copy on write), so the render loop reads a consistent snapshot without locking.
"""

from __future__ import annotations
import re
import threading
import time
from typing import Any

KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_KEYS = 1000


def check_key(key: Any) -> str:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ValueError(f"Bad key {key!r}: use 1-64 letters, digits, '_', '.', ':' or '-'")
    return key


class ValueStore:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._updated: dict[str, float] = {}
        self.version = 0  # bumped on every change; patterns re-read their values when it moves
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[dict[str, Any], int]:
        """The current values (don't modify) and their version."""
        with self._lock:
            return self._values, self.version

    def get(self, key: str) -> dict[str, Any]:
        """``{key, value, updated}``. Raises KeyError."""
        with self._lock:
            values, updated = self._values, self._updated
        if key not in values:
            raise KeyError(key)
        return {"key": key, "value": values[key], "updated": updated[key]}

    def all(self) -> tuple[dict[str, Any], dict[str, float]]:
        """Every value, and when each was last set (Unix time), sorted by key."""
        with self._lock:
            values, updated = self._values, self._updated
        return dict(sorted(values.items())), dict(sorted(updated.items()))

    def update(self, changes: Any, replace: bool = False) -> None:
        """Set several keys at once; a null value deletes its key. ``replace`` first drops every key not in ``changes``."""
        if not isinstance(changes, dict):
            raise ValueError("'values' must be an object of key: value")
        for key in changes:
            check_key(key)
        now = time.time()
        with self._lock:
            values = {} if replace else dict(self._values)
            updated = {} if replace else dict(self._updated)
            for key, value in changes.items():
                if value is None:
                    values.pop(key, None)
                    updated.pop(key, None)
                else:
                    values[key], updated[key] = value, now
            if len(values) > MAX_KEYS:
                raise ValueError(f"Too many keys (at most {MAX_KEYS})")
            self._values, self._updated = values, updated
            self.version += 1

    def set(self, key: str, value: Any) -> None:
        if value is None:
            raise ValueError("'value' can't be null; DELETE the key to remove it")
        self.update({check_key(key): value})

    def delete(self, key: str) -> None:
        """Raises KeyError if the key isn't set."""
        with self._lock:
            if key not in self._values:
                raise KeyError(key)
        self.update({key: None})

    def clear(self) -> None:
        self.update({}, replace=True)
