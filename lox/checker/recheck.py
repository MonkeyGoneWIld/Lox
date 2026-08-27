"""Which requests are worth looking up again, and which already have answers.

Checking a request costs a tracker call to read it and a Deezer search to match
it. Nothing stopped that being paid twice: every check went through the whole
pipeline for every id it was handed, including the ones checked an hour ago
that came back with nothing. Run the same search twice and you paid for it
twice, and the second run took just as long.

What a check learns barely moves. A request's terms are set when it is posted;
whether Deezer has the album is a fact about Deezer. So an answer is kept and
trusted for a while, and the window is a setting rather than a guess --
``checker.request_recheck_after_days``, 0 meaning an answer never goes stale.

Errors are the exception: a check that failed because the tracker was briefly
down learned nothing, and is retried on sight.

The counterpart of skipping is saying so. A run that quietly did a tenth of
what was asked looks broken, so :func:`plan` hands back both halves and the
caller reports the skipped ones, with the reason each was skipped, and offers
to run them anyway.
"""

import time
from typing import Any

#: Statuses that answered the question. A request that is filled, that has a
#: usable Deezer release, or that was looked at and had nothing worth taking is
#: settled until the recheck window is up.
SETTLED = frozenset({"filled", "fillable", "skipped"})

#: Statuses that answered nothing, because something broke on the way. These
#: are never skipped: the previous run learned nothing to reuse.
RETRY = frozenset({"error"})

#: A day, so the window setting can be written in days and compared in seconds.
DAY = 86400.0


def age_days(entry: dict[str, Any], now: float | None = None) -> float | None:
    """How long ago this request was checked, in days.

    Args:
        entry: A stored request record.
        now: Current epoch seconds, for tests.

    Returns:
        Days since the check, or None when the record does not say.
    """
    checked = entry.get("checked_at")
    try:
        checked = float(checked)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return None
    if checked <= 0:
        return None
    return max(0.0, ((now if now is not None else time.time()) - checked) / DAY)


def verdict(entry: dict[str, Any] | None, recheck_after_days: int, now: float | None = None) -> tuple[bool, str]:
    """Whether one request still needs looking up.

    Args:
        entry: The stored record, or None if it has never been checked.
        recheck_after_days: How long an answer is trusted. 0 trusts it forever.
        now: Current epoch seconds, for tests.

    Returns:
        ``(True, "")`` to check it, or ``(False, reason)`` to skip it, where the
        reason is short enough to sit in a list of skipped rows.
    """
    if not entry:
        return True, ""

    status = str(entry.get("status") or "")
    if status in RETRY:
        return True, ""
    if status not in SETTLED:
        return True, ""

    age = age_days(entry, now)
    if age is None:
        # Checked at some unknown time. Trust it, but say so -- an undated
        # answer that silently counted as fresh forever would be worse.
        return False, f"already checked ({status}), no date recorded"

    if recheck_after_days and age >= recheck_after_days:
        return True, ""

    when = "today" if age < 1 else f"{int(age)} day{'s' if int(age) != 1 else ''} ago"
    return False, f"already checked {when} ({status})"


def plan(
    store: Any,
    tracker: str,
    request_ids: list[str],
    *,
    recheck_after_days: int,
    force: bool = False,
    now: float | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Split ids into the ones to check and the ones already answered.

    Args:
        store: The checker store.
        tracker: Tracker code the ids belong to.
        request_ids: Ids to consider.
        recheck_after_days: How long an answer is trusted. 0 trusts it forever.
        force: Check everything regardless, for "run them anyway".
        now: Current epoch seconds, for tests.

    Returns:
        ``(to_check, skipped)``, where each skipped entry carries the id, why it
        was skipped, and enough of the stored answer to show in a list without
        another lookup.
    """
    to_check: list[str] = []
    skipped: list[dict[str, Any]] = []

    for request_id in request_ids:
        entry = store.get("requests", f"{tracker}:{request_id}") if not force else None
        ok, why = verdict(entry, recheck_after_days, now) if not force else (True, "")
        if ok:
            to_check.append(str(request_id))
            continue
        entry = entry or {}
        skipped.append({
            "id": str(request_id),
            "tracker": tracker,
            "reason": why,
            "status": entry.get("status") or "",
            "artist": entry.get("artist") or "",
            "album": entry.get("album") or "",
            "checked_at": entry.get("checked_at"),
            "deezer_id": entry.get("deezer_id") or "",
        })

    return to_check, skipped
