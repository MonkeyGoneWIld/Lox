"""Keeping the queue true while nobody is looking.

A queue row is a claim about somebody else's tracker: *nobody has uploaded this
yet*. That claim was made once, when the check ran, and then never revisited.
Meanwhile somebody uploads the release, or the request that justified the row
gets filled, and the row sits there for months looking exactly like work.

So the rows age out. Anything confirmed longer ago than
``checker.queue_recheck_after_days`` is asked about again — the oldest first,
a handful at a time — and whatever comes back is written where the queue reads
it. A release somebody has beaten you to leaves the queue by itself.

Two rules make this safe to leave running:

* **It never competes.** If a scan, a request check, an upload or a download is
  running, this tick does nothing at all and waits for the next one. The
  operator's own work has the tracker to itself.
* **It never overdraws.** The batch is small, and every tracker is asked
  whether it can afford the call before the call is made, so the budget the
  operator is saving for a real scan is still there when they want it.

Off by default in spirit as well as fact: a window of 0 disables it, and
nothing here starts a tracker call the operator did not configure.
"""

import asyncio
import contextlib
import time
from typing import Any

from lox import cfg, debug
from lox.checker.gateway import TrackerGateway
from lox.checker.missing import Candidate, MissingScanner
from lox.checker.store import CheckerStore

#: How often to look for stale rows. The window is measured in days, so there
#: is nothing to gain from looking more often than this.
TICK_SECONDS = 900.0

#: How long to wait before the first tick, so a restart does not immediately
#: start spending on a tracker while the operator is still opening the page.
FIRST_TICK_SECONDS = 120.0

#: Rows confirmed per tick. Small on purpose: this is background work, and
#: catching up over a few hours costs nothing while a burst costs the budget.
BATCH = 8

DAY = 86400.0


class QueueRecheck:
    """Background confirmation of stale queue rows."""

    def __init__(
        self,
        scanner: MissingScanner,
        gateway: TrackerGateway,
        store: CheckerStore,
        is_busy: Any,
    ) -> None:
        """Initialize the task.

        Args:
            scanner: The scanner whose check() writes the answers back.
            gateway: The tracker gateway, for budget and configured trackers.
            store: Where the queue rows live.
            is_busy: Callable returning True while the operator has something
                of their own running. This task yields to all of it.
        """
        self.scanner = scanner
        self.gateway = gateway
        self.store = store
        self.is_busy = is_busy
        self._task: asyncio.Task | None = None
        #: What the last tick did, so the UI can say the queue is being kept up
        #: to date rather than leaving it to be inferred.
        self.last_run: float | None = None
        self.last_count = 0
        self.last_note = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin ticking, if it is not already."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop ticking and wait for the current tick to unwind."""
        if not self._task:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        """Tick forever, surviving anything one tick throws."""
        await asyncio.sleep(FIRST_TICK_SECONDS)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a bad tick must not end the loop
                debug.log("queue re-check tick failed: %s", e, level=30)
            await asyncio.sleep(TICK_SECONDS)

    # ------------------------------------------------------------------
    # One tick
    # ------------------------------------------------------------------

    @staticmethod
    def window_days() -> int:
        """How old a row may get before it is confirmed again. 0 is off."""
        return int(getattr(cfg.checker, "queue_recheck_after_days", 0) or 0)

    def status(self) -> dict[str, Any]:
        """What this task is set to do and what it last did."""
        return {
            "enabled": self.window_days() > 0,
            "after_days": self.window_days(),
            "last_run": self.last_run,
            "last_count": self.last_count,
            "note": self.last_note,
        }

    async def tick(self) -> int:
        """Confirm one batch of stale rows. Returns how many were checked."""
        window = self.window_days()
        if window <= 0:
            return 0
        if self.is_busy():
            debug.log("queue re-check: something else is running, skipping this tick", level=10)
            return 0

        trackers = [code for code in self.gateway.configured_trackers() if self.gateway.can_check(code)]
        if not trackers:
            self.last_note = "no tracker had budget"
            return 0

        stale = self.stale_rows(window)
        if not stale:
            self.last_run = time.time()
            self.last_count = 0
            self.last_note = "nothing due"
            return 0

        batch = stale[:BATCH]
        debug.log("queue re-check: confirming %d of %d stale row(s) on %s",
                  len(batch), len(stale), ", ".join(trackers), level=20)
        results = await self.scanner.check(batch, trackers, stop_on_budget=True)
        self.last_run = time.time()
        self.last_count = len(results)
        self.last_note = f"confirmed {len(results)} of {len(stale)} due"
        return len(results)

    def stale_rows(self, window: int) -> list[Candidate]:
        """Queue rows that have not been confirmed inside the window.

        Oldest first, so a backlog drains in the order it aged rather than in
        whatever order the store happens to iterate.

        Args:
            window: Days after which a row is due.

        Returns:
            Candidates ready to hand to the scanner.
        """
        now = time.time()
        dismissed = self.store.load("dismissed") or {}
        due: list[tuple[float, Candidate]] = []

        for album_id, entry in (self.store.load("albums") or {}).items():
            if entry.get("uploaded_at") or album_id in dismissed:
                continue
            # Only rows that reached a tracker are queue rows. Everything else
            # in this collection is the scanner's note-to-self about an album
            # it gave up on, and re-checking those would spend the budget on
            # releases nobody can act on.
            if not (entry.get("missing_from") or entry.get("found_on")):
                continue
            # A row nothing is missing from is not in the queue and cannot come
            # back into it; the scan's own window covers those.
            if not entry.get("missing_from"):
                continue
            checked = entry.get("checked_at")
            try:
                age = (now - float(checked)) / DAY
            except (TypeError, ValueError):
                continue
            if age < window:
                continue
            due.append((
                age,
                Candidate(
                    album_id=str(album_id),
                    title=str(entry.get("title") or ""),
                    artist=str(entry.get("artist") or ""),
                    source="queue re-check",
                ),
            ))

        due.sort(key=lambda pair: pair[0], reverse=True)
        return [candidate for _age, candidate in due]
