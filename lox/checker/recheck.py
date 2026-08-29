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

#: The one album answer nothing can move: present on every tracker that was
#: asked. There is no upload in it, no rule admits it, and the only way that
#: changes is somebody deleting a torrent.
#:
#: Everything else a scan writes is a state that can still change, and treating
#: the lot as settled is what made the Scan tab feel stuck. ``skipped_filter``
#: means a filter stopped it -- change the filter and it should come back, but
#: it never did. ``missing_ops`` means there is something to upload -- the
#: queue rule can widen onto it, somebody else can beat you to it, and neither
#: was ever noticed. A prefix match cannot tell those apart, because the
#: prefix does not carry which trackers said what; the stored ``found_on`` and
#: ``missing_from`` lists do.
def tracker_sets(entry: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Which trackers had it and which did not, from a stored album record.

    Falls back to reading the status when the lists are absent, which is how
    records written before those lists existed are still understood.

    Args:
        entry: A stored album record.

    Returns:
        ``(found_on, missing_from)`` as upper-case tracker codes.
    """
    found = {str(t).upper() for t in entry.get("found_on") or ()}
    missing = {str(t).upper() for t in entry.get("missing_from") or ()}
    if found or missing:
        return found, missing

    status = str(entry.get("status") or "")
    # "exists_both" is the older spelling for "on every tracker asked", and it
    # names none of them. Read as a full house rather than as nothing.
    if status == "exists_both":
        return {"*"}, set()
    for prefix, into in (("exists_", "found"), ("missing_", "missing")):
        if status.startswith(prefix):
            codes = {part.upper() for part in status[len(prefix):].split("_") if part}
            return (codes, set()) if into == "found" else (set(), codes)
    return set(), set()

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


def verdict(
    entry: dict[str, Any] | None,
    recheck_after_days: int,
    now: float | None = None,
) -> tuple[bool, str]:
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


def album_verdict(
    entry: dict[str, Any] | None,
    recheck_after_days: int,
    now: float | None = None,
    trackers: Any = (),
    confirming: bool = False,
) -> tuple[bool, str]:
    """Whether a scan should look this album up again.

    Not :func:`verdict` with a different vocabulary. A request has one answer
    and it is about the request; an album has an answer per tracker, and which
    of them said what is the whole of the decision.

    Looked up again, for reasons that are all the same reason -- nobody has
    answered yet, so there is nothing to trust:

    * no tracker ever answered. A filter stopped it, or Deezer did. Both are
      settings or facts that change, and re-deciding costs no tracker call:
      the filters run before a tracker is contacted.
    * a configured tracker was never asked, because it was out of budget when
      the scan reached it.
    * the lookup failed.

    Otherwise the answer stands until the window runs out, whatever it was.
    A release missing from a tracker used to be re-checked on every pass, on
    the reasoning that somebody might have uploaded it first -- but that made
    a scan pay a tracker call for every release already sitting in the queue,
    every time it ran. Re-scanning a playlist a second time re-checked the four
    releases the first scan had just put in the queue, which is the one set of
    albums whose answer was newest.

    Confirming the queue still holds is a real job; it is just not this one.
    The queue's own "Re-check on trackers" and the background upkeep pass both
    hand their albums straight to :meth:`MissingScanner.check`, which asks
    every time and never consults this -- so they are unaffected. ``confirming``
    states the rule for anything that does route a confirmation through the
    sweep, so that case cannot quietly inherit the wrong answer.

    Args:
        entry: The stored album record, or None.
        recheck_after_days: How long a scan's answer is trusted. 0 forever.
        now: Current epoch seconds, for tests.
        trackers: The tracker codes configured now, so a tracker that was
            never asked is not mistaken for one that answered.
        confirming: True when the caller is asking whether a queued release
            still qualifies, rather than sweeping for new work.

    Returns:
        ``(True, "")`` to look it up, or ``(False, reason)`` to skip it.
    """
    if not entry:
        return True, ""
    if str(entry.get("status") or "") in RETRY:
        return True, ""

    found, missing = tracker_sets(entry)
    if not found and not missing:
        return True, ""

    wanted = {str(t).upper() for t in trackers or ()}
    # "*" is the older "on every tracker asked", which names none of them and
    # so cannot be checked against the list of trackers configured now.
    #
    # Against everything that answered, not just what has it: a release found
    # on OPS and missing from RED has heard from both, and testing against
    # `found` alone called RED unasked and swept it up again every time.
    answered = found | missing
    if "*" not in found and wanted - answered:
        return True, ""

    # The queue asking whether its own rows still hold. That is the question
    # the button is for, so it is always worth a call.
    if missing and confirming:
        return True, ""

    if missing:
        where = f"missing from {', '.join(sorted(missing))}"
    else:
        where = "already on " + ("every tracker" if "*" in found else ", ".join(sorted(found)))
    age = age_days(entry, now)
    if age is None:
        return False, f"{where}, no date recorded"
    if recheck_after_days and age >= recheck_after_days:
        return True, ""
    when = "today" if age < 1 else f"{int(age)} day{'s' if int(age) != 1 else ''} ago"
    return False, f"{where}, checked {when}"


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
