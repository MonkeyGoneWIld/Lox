"""Rate-limited, on-demand access to the Gazelle trackers.

Tracker API budgets are small and the penalty for burning one is an hours-long
lockout, so nothing in the UI touches a tracker implicitly. Every tracker call
goes through this gateway, which:

* spends from a per-tracker token bucket and refuses to overdraw it,
* spaces consecutive calls by a configurable delay,
* opens a circuit breaker after repeated failures instead of hammering,
* records what it spent so the UI can show the remaining budget.

Search, Explore and download never reach a tracker. Only an explicit check does.
"""

import asyncio
import time
from collections import deque
from typing import Any
from urllib.parse import quote

import msgspec

from salmon import cfg
from salmon.errors import RequestError
from salmon.trackers import get_class, tracker_list
from salmon.trackers.base import RetryableError

# Everything a tracker call can plausibly fail with. Anything else is a bug and
# should not be swallowed into the circuit breaker.
TRACKER_ERRORS = (RequestError, RetryableError, OSError, TimeoutError)


class TrackerBudgetExceeded(Exception):
    """Raised when a tracker's request budget for the current window is spent."""


class TrackerUnavailable(Exception):
    """Raised when a tracker's circuit breaker is open."""


class TrackerStatus(msgspec.Struct):
    """Live state of one tracker, as shown in the UI."""

    code: str
    configured: bool
    budget: int
    window: int
    spent: int
    remaining: int
    cooldown_until: float | None
    consecutive_failures: int
    last_error: str | None
    last_call: float | None

    @property
    def available(self) -> bool:
        """True when a check may be started right now."""
        cooling = self.cooldown_until is not None and self.cooldown_until > time.time()
        return self.configured and self.remaining > 0 and not cooling

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        now = time.time()
        return {
            "code": self.code,
            "configured": self.configured,
            "budget": self.budget,
            "window": self.window,
            "spent": self.spent,
            "remaining": self.remaining,
            "available": self.available,
            "cooldown_seconds": max(0, int((self.cooldown_until or 0) - now)),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_call": self.last_call,
        }


class _TrackerState:
    """Bookkeeping for one tracker's budget and circuit breaker."""

    def __init__(self, code: str, budget: int, window: int, failure_threshold: int, cooldown: int) -> None:
        self.code = code
        self.budget = budget
        self.window = window
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.calls: deque[float] = deque()
        self.cooldown_until: float | None = None
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.last_call: float | None = None
        self.lock = asyncio.Lock()

    def prune(self) -> None:
        """Drop call timestamps that have fallen out of the window."""
        cutoff = time.time() - self.window
        while self.calls and self.calls[0] < cutoff:
            self.calls.popleft()

    @property
    def spent(self) -> int:
        """Calls made inside the current window."""
        self.prune()
        return len(self.calls)

    @property
    def remaining(self) -> int:
        """Calls still allowed inside the current window."""
        return max(0, self.budget - self.spent)

    def record_success(self) -> None:
        """Note a successful call and close the circuit breaker."""
        now = time.time()
        self.calls.append(now)
        self.last_call = now
        self.consecutive_failures = 0
        self.cooldown_until = None

    def record_failure(self, error: str) -> None:
        """Note a failed call, opening the breaker once the threshold is hit."""
        now = time.time()
        self.calls.append(now)
        self.last_call = now
        self.last_error = error
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.cooldown_until = now + self.cooldown


class TrackerGateway:
    """Single entry point for every tracker request the checker makes."""

    def __init__(self) -> None:
        """Initialize the gateway from the checker config."""
        checker = cfg.checker
        self.delay = checker.tracker_call_delay
        self.switch_delay = checker.tracker_switch_delay
        self._apis: dict[str, Any] = {}
        self._states: dict[str, _TrackerState] = {
            code: _TrackerState(
                code,
                checker.tracker_budget,
                checker.tracker_budget_window,
                checker.failure_threshold,
                checker.cooldown_seconds,
            )
            for code in ("RED", "OPS", "DIC")
        }

    @staticmethod
    def configured_trackers() -> list[str]:
        """Tracker codes that have credentials in the config."""
        return list(tracker_list)

    def api(self, code: str):
        """Return (and cache) the Gazelle API client for a tracker code.

        Args:
            code: Tracker code, e.g. ``RED``.

        Returns:
            The tracker's API instance.

        Raises:
            TrackerUnavailable: If the tracker is not configured.
        """
        if code not in tracker_list:
            raise TrackerUnavailable(f"{code} is not configured")
        if code not in self._apis:
            self._apis[code] = get_class(code)()
        return self._apis[code]

    def status(self, code: str) -> TrackerStatus:
        """Return the current budget and breaker state for one tracker."""
        state = self._states[code]
        return TrackerStatus(
            code=code,
            configured=code in tracker_list,
            budget=state.budget,
            window=state.window,
            spent=state.spent,
            remaining=state.remaining,
            cooldown_until=state.cooldown_until,
            consecutive_failures=state.consecutive_failures,
            last_error=state.last_error,
            last_call=state.last_call,
        )

    def statuses(self) -> list[dict[str, Any]]:
        """Return serialized status for every known tracker."""
        return [self.status(code).as_dict() for code in ("RED", "OPS", "DIC")]

    def can_check(self, code: str, needed: int = 1) -> bool:
        """True when a tracker has budget and its breaker is closed."""
        status = self.status(code)
        return status.available and status.remaining >= needed

    async def _call(self, code: str, coro_factory) -> Any:
        """Spend one unit of budget on a tracker call.

        Args:
            code: Tracker code.
            coro_factory: Zero-arg callable returning the coroutine to await.

        Returns:
            Whatever the coroutine returns.

        Raises:
            TrackerBudgetExceeded: If the window's budget is spent.
            TrackerUnavailable: If the circuit breaker is open.
        """
        state = self._states[code]
        async with state.lock:
            if state.cooldown_until and state.cooldown_until > time.time():
                wait = int(state.cooldown_until - time.time())
                raise TrackerUnavailable(f"{code} is cooling down for another {wait}s ({state.last_error})")
            if state.remaining <= 0:
                raise TrackerBudgetExceeded(
                    f"{code} budget of {state.budget} requests per {state.window}s is spent; try again later"
                )
            if state.last_call:
                elapsed = time.time() - state.last_call
                if elapsed < self.delay:
                    await asyncio.sleep(self.delay - elapsed)

            try:
                result = await coro_factory()
            except TRACKER_ERRORS as e:
                state.record_failure(str(e))
                raise
            state.record_success()
            return result

    async def browse(self, code: str, searchstr: str) -> list[dict]:
        """Run a ``browse`` search on a tracker.

        Args:
            code: Tracker code.
            searchstr: The search string.

        Returns:
            The result rows, or an empty list if the tracker reported failure.
        """
        api = self.api(code)
        data = await self._call(code, lambda: api.request("browse", {"searchstr": searchstr}))
        if not isinstance(data, dict):
            return []
        if isinstance(data.get("results"), list):
            return data["results"]
        return data.get("results") or []

    async def torrentgroup(self, code: str, group_id: int) -> dict:
        """Fetch one torrent group from a tracker."""
        api = self.api(code)
        return await self._call(code, lambda: api.torrentgroup(group_id))

    async def get_request(self, code: str, request_id: int) -> dict:
        """Fetch one request from a tracker."""
        api = self.api(code)
        return await self._call(code, lambda: api.get_request(request_id))

    async def artist_id(self, code: str, artist_name: str) -> int | None:
        """Look up a tracker's internal artist ID, or None if not found."""
        api = self.api(code)
        try:
            data = await self._call(code, lambda: api.request("artist", {"artistname": artist_name}))
        except (*TRACKER_ERRORS, TrackerBudgetExceeded, TrackerUnavailable):
            return None
        return (data or {}).get("id")

    def artist_url(self, code: str, artist_name: str, artist_id: int | None = None) -> str:
        """Build a browsable artist URL for a tracker."""
        base = self.api(code).base_url
        if artist_id:
            return f"{base}/artist.php?id={artist_id}"
        return f"{base}/torrents.php?artistname={quote(artist_name)}"

    def request_url(self, code: str, request_id: int) -> str:
        """Build a browsable request URL for a tracker."""
        return f"{self.api(code).base_url}/requests.php?action=view&id={request_id}"
