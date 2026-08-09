"""Durable state for the checkers.

Both scan flows are expensive and rate-limited, so results are written to disk
after every album. A rerun skips anything already resolved and retries only the
entries that failed for transient reasons.
"""

import json
import os
import threading
import time
from typing import Any

from salmon import cfg

FINAL_STATUSES = frozenset(
    {
        "exists_both",
        "exists_red",
        "exists_ops",
        "exists_dic",
        "skipped_no_flac",
        "skipped_unreadable",
        "skipped_filter",
        "skipped_missing_info",
    }
)
"""Outcomes that will never change; these albums are never re-checked."""

RETEST_STATUSES = frozenset(
    {
        "deezer_info_failed",
        "flac_check_failed",
        "skipped_track_count_mismatch",
        "skipped_no_filesize",
        "skipped_missing_track_ids",
        "tracker_failed",
        "tracker_budget_exceeded",
        "tracker_unavailable",
    }
)
"""Outcomes caused by transient failures; retried on the next run."""


class CheckerStore:
    """A small JSON-backed key/value store, one file per collection.

    Writes are atomic (temp file plus replace) so an interrupted scan cannot
    leave a truncated state file behind.
    """

    def __init__(self, directory: str | None = None) -> None:
        """Initialize the store.

        Args:
            directory: Where to keep state files. Defaults to
                ``checker.state_dir``, then ``<download_directory>/.salmon-checker``.
        """
        self.directory = directory or cfg.checker.state_dir or os.path.join(
            cfg.directory.download_directory, ".salmon-checker"
        )
        os.makedirs(self.directory, exist_ok=True)
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _path(self, name: str) -> str:
        """Return the file path backing a collection."""
        return os.path.join(self.directory, f"{name}.json")

    def load(self, name: str) -> dict[str, Any]:
        """Load a collection, caching it in memory.

        Args:
            name: Collection name, e.g. ``albums`` or ``requests``.

        Returns:
            The collection dict. Missing or corrupt files yield an empty dict.
        """
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            path = self._path(name)
            data: dict[str, Any] = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, json.JSONDecodeError):
                    data = {}
            self._cache[name] = data
            return data

    def save(self, name: str) -> None:
        """Write a collection back to disk atomically."""
        with self._lock:
            data = self._cache.get(name)
            if data is None:
                return
            path = self._path(name)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, path)

    def get(self, name: str, key: str) -> dict[str, Any] | None:
        """Return one entry from a collection."""
        return self.load(name).get(str(key))

    def put(self, name: str, key: str, value: dict[str, Any], flush: bool = True) -> None:
        """Store one entry, stamping it with the current time.

        Args:
            name: Collection name.
            key: Entry key.
            value: Entry payload.
            flush: Write to disk immediately.
        """
        collection = self.load(name)
        collection[str(key)] = {**value, "checked_at": time.time()}
        if flush:
            self.save(name)

    def delete(self, name: str, key: str, flush: bool = True) -> None:
        """Remove one entry from a collection."""
        collection = self.load(name)
        if str(key) in collection:
            del collection[str(key)]
            if flush:
                self.save(name)

    def should_skip(self, name: str, key: str) -> bool:
        """True when an entry has a final status and need not be re-checked."""
        entry = self.get(name, key)
        return bool(entry) and entry.get("status") in FINAL_STATUSES

    def needs_retest(self, name: str, key: str) -> bool:
        """True when an entry failed transiently and should be retried."""
        entry = self.get(name, key)
        return bool(entry) and entry.get("status") in RETEST_STATUSES

    def summary(self, name: str) -> dict[str, int]:
        """Count entries per status in a collection."""
        counts: dict[str, int] = {}
        for entry in self.load(name).values():
            status = entry.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def clear(self, name: str) -> int:
        """Empty a collection. Returns how many entries were removed."""
        collection = self.load(name)
        count = len(collection)
        collection.clear()
        self.save(name)
        return count
