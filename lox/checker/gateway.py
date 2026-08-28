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
import html
import logging
import time
from collections import deque
from typing import Any
from urllib.parse import quote

import msgspec

from lox import cfg, debug
from lox.errors import RequestError
from lox.trackers import base_url, get_class, tracker_list
from lox.trackers.base import RetryableError

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
            # Where the tracker lives, so the UI can turn "OPS is missing this"
            # into a link to OPS rather than a label you have to act on by hand.
            "url": base_url(self.code),
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

    def reconfigure(self, budget: int, window: int, failure_threshold: int, cooldown: int) -> None:
        """Adopt new limits without forgetting the calls already made.

        The call history, the breaker and the failure count all survive: a
        tracker that has spent 200 calls this window has still spent them, and
        raising the ceiling mid-window must not hand out a fresh allowance.

        Args:
            budget: Calls allowed per window.
            window: Window length in seconds.
            failure_threshold: Failures in a row before the breaker opens.
            cooldown: Seconds the breaker stays open.
        """
        self.budget = budget
        self.window = window
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        # A shorter window can retire calls immediately.
        self.prune()

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


def plain(text: Any) -> str:
    """Decode the HTML entities a Gazelle response carries inside its JSON.

    The API returns strings already escaped for a web page, so an apostrophe
    arrives as ``&#39;`` and an accented letter as ``&aacute;``. Rendered as
    text -- which is the only safe way to render it -- that is what you see:
    "Live Beginnings &#39;88", "Zsoldos &Aacute;rp&aacute;d". Decoding belongs
    here, at the point the response stops being HTML and becomes data.

    Args:
        text: A field from a tracker response.

    Returns:
        The same text with entities resolved, or an empty string for None.
    """
    return html.unescape(str(text)) if text else ""


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

    def reconfigure(self) -> None:
        """Re-read the config after a settings change.

        Everything below was copied out of the config once, at startup, which
        meant the settings page could report a saved value that nothing in the
        running process was using: a raised budget was ignored until restart,
        and -- worse -- a rotated API key kept sending the old one, because
        each tracker client copies its key and cookie when it is built.

        Budget history is kept (see :meth:`_TrackerState.reconfigure`). The
        clients are dropped rather than patched: they hold no session, each
        call opens its own, so rebuilding one costs nothing and is the only
        way to be sure nothing stale is left on it. A breaker that is already
        open stays open for the cooldown it was given; the new one applies to
        the next failure.
        """
        checker = cfg.checker
        self.delay = checker.tracker_call_delay
        self.switch_delay = checker.tracker_switch_delay
        for state in self._states.values():
            state.reconfigure(
                checker.tracker_budget,
                checker.tracker_budget_window,
                checker.failure_threshold,
                checker.cooldown_seconds,
            )
        self._apis.clear()

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

    def _guard(self, code: str, state: "_TrackerState") -> None:
        """Raise if this tracker cannot be called at all right now.

        Args:
            code: Tracker code.
            state: Its budget and breaker state.

        Raises:
            TrackerBudgetExceeded: If the window's budget is spent.
            TrackerUnavailable: If the circuit breaker is open.
        """
        if state.cooldown_until and state.cooldown_until > time.time():
            wait = int(state.cooldown_until - time.time())
            raise TrackerUnavailable(f"{code} is cooling down for another {wait}s ({state.last_error})")
        if state.remaining <= 0:
            raise TrackerBudgetExceeded(
                f"{code} budget of {state.budget} requests per {state.window}s is spent; try again later"
            )

    async def _call(self, code: str, coro_factory, *, interactive: bool = False) -> Any:
        """Spend one unit of budget on a tracker call.

        Args:
            code: Tracker code.
            coro_factory: Zero-arg callable returning the coroutine to await.
            interactive: True for a single call someone is waiting on, which
                skips the queue rather than joining the back of it.

        Returns:
            Whatever the coroutine returns.

        Raises:
            TrackerBudgetExceeded: If the window's budget is spent.
            TrackerUnavailable: If the circuit breaker is open.
        """
        state = self._states[code]

        # A person clicked something and is watching a spinner.
        #
        # The lock below is held across the pacing sleep AND the HTTP call, and
        # asyncio hands it out in order, so opening one request's details while
        # a hundred-request check was running meant waiting for the hundred:
        # two or three seconds each, and the panel just sat there. The budget
        # and the breaker still apply -- this cannot be used to get around
        # either -- but one call does not need to queue behind automated work
        # to be polite, and pacing exists to protect the tracker from our
        # batches, not from a person clicking a row.
        if interactive:
            self._guard(code, state)
            try:
                result = await coro_factory()
            except TRACKER_ERRORS as e:
                state.record_failure(str(e))
                debug.log("tracker %s interactive call failed: %s", code, e, level=logging.WARNING)
                raise
            state.record_success()
            debug.event("tracker.call", tracker=code, remaining=state.remaining,
                        budget=state.budget, interactive=True)
            return result

        async with state.lock:
            self._guard(code, state)
            if state.last_call:
                elapsed = time.time() - state.last_call
                if elapsed < self.delay:
                    await asyncio.sleep(self.delay - elapsed)

            try:
                result = await coro_factory()
            except TRACKER_ERRORS as e:
                state.record_failure(str(e))
                debug.log("tracker %s call failed: %s", code, e, level=logging.WARNING)
                raise
            state.record_success()
            debug.event("tracker.call", tracker=code, remaining=state.remaining, budget=state.budget)
            return result

    async def call_action(
        self, code: str, action: str, params: dict[str, Any] | None = None, *, interactive: bool = False
    ) -> dict:
        """Run an arbitrary Gazelle ajax action against the budget.

        Args:
            code: Tracker code.
            action: The ajax action name.
            params: Query parameters.

        Returns:
            The tracker's response object.
        """
        api = self.api(code)
        return await self._call(code, lambda: api.request(action, params or {}), interactive=interactive)

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

    async def get_request(self, code: str, request_id: int, *, interactive: bool = False) -> dict:
        """Fetch one request from a tracker.

        Args:
            code: Tracker code.
            request_id: The request's id on that tracker.
            interactive: True when someone is waiting on this one call, so it
                skips the queue instead of joining the back of it.

        Returns:
            The request payload.
        """
        api = self.api(code)
        return await self._call(code, lambda: api.get_request(request_id), interactive=interactive)

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
