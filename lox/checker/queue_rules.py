"""Which matched releases are worth acting on.

Every check that matches a Deezer release to a tracker is kept, because the
tracker call that produced it has already been paid for. That is not the same
question as "do I want to upload this", and for a while the answer to the
second was hardcoded: missing from at least one tracker, which put a release
that RED already has in the same list as one nobody has.

These rules answer the second question, and they are applied when the queue is
READ rather than when the check runs. That matters more than it looks:
narrowing them hides rows, widening them brings the same rows straight back,
and neither costs a tracker call. Nothing is deleted by a rule.

A rule is three parts:

* what must be true of the release on each tracker -- absent, already up, or
  not consulted at all
* whether every stated tracker has to agree, or just one of them
* what to do about releases that arrived from a request check rather than a
  scan

Between them they say things like "missing on OPS, already on RED, and there
is an OPS request for it", which is the shape of question people actually
have and which no single checkbox could express.
"""

from typing import Any, NamedTuple

#: Tracker code to the config field that carries its rule.
TRACKER_FIELDS: tuple[tuple[str, str], ...] = (
    ("RED", "queue_red"),
    ("OPS", "queue_ops"),
    ("DIC", "queue_dic"),
)

#: What each choice means on screen, so the page and the tests agree on it.
RULE_LABELS: dict[str, str] = {
    "any": "not consulted",
    "missing": "must be missing there",
    "present": "must already be there",
}

REQUEST_LABELS: dict[str, str] = {
    "any": "scan results and request fills",
    "only": "only releases that fill a request",
    "only_missing_there": "only request fills that are missing on the request's own tracker",
    "exclude": "scan results only",
}


class QueueRules(NamedTuple):
    """The admission rules, lifted out of the config."""

    trackers: tuple[tuple[str, str], ...]
    match: str
    requests: str
    require_somewhere_missing: bool

    @property
    def stated(self) -> tuple[tuple[str, str], ...]:
        """Only the trackers the user actually made a rule about."""
        return tuple((code, rule) for code, rule in self.trackers if rule != "any")

    def describe(self) -> str:
        """One line saying what is being let through, for the page to show."""
        stated = self.stated
        if stated:
            joiner = " and " if self.match == "all" else " or "
            joined = joiner.join(f"{code} {RULE_LABELS[rule]}" for code, rule in stated)
        else:
            joined = "any tracker"
        if self.requests != "any":
            joined += f"; {REQUEST_LABELS[self.requests]}"
        return joined


def rules_from(checker: Any) -> QueueRules:
    """Read the rules off the checker config.

    Args:
        checker: ``cfg.checker``.

    Returns:
        The rules as a plain value, so the predicate never touches global config.
    """
    return QueueRules(
        trackers=tuple((code, getattr(checker, field, "any") or "any") for code, field in TRACKER_FIELDS),
        match=getattr(checker, "queue_match", "all") or "all",
        requests=getattr(checker, "queue_requests", "any") or "any",
        require_somewhere_missing=bool(getattr(checker, "queue_require_somewhere_missing", True)),
    )


def admits(row: dict[str, Any], rules: QueueRules) -> tuple[bool, str]:
    """Decide whether one matched release belongs in the queue.

    Args:
        row: A queue row as :func:`lox.web.api.api_found` builds it -- ``kind``,
            ``missing_from``, ``found_on`` and, for a request, ``tracker``.
        rules: The admission rules.

    Returns:
        ``(True, "")`` to let it through, or ``(False, reason)`` where the
        reason is short enough to show beside a "12 held back" count.
    """
    missing = {str(t).upper() for t in row.get("missing_from") or ()}
    present = {str(t).upper() for t in row.get("found_on") or ()}
    is_request = row.get("kind") == "request"

    # Nothing to upload is not a matter of taste.
    if rules.require_somewhere_missing and not missing:
        # Never checked and checked-and-found are both "not missing anywhere",
        # and they want opposite things done about them.
        if not present:
            return False, "not checked against any tracker yet"
        return False, "already on every tracker it was checked against"

    if rules.requests == "exclude" and is_request:
        return False, "fills a request, and the rules ask for scan results only"
    if rules.requests in ("only", "only_missing_there") and not is_request:
        return False, "came from a scan, and the rules ask for request fills only"
    if rules.requests == "only_missing_there" and is_request:
        home = str(row.get("tracker") or "").upper()
        if not home:
            return False, "the request does not say which tracker it is on"
        if home not in missing:
            return False, f"not known to be missing on {home}, where the request is"

    stated = rules.stated
    if not stated:
        return True, ""

    def holds(code: str, rule: str) -> bool:
        return code in missing if rule == "missing" else code in present

    outcomes = [(code, rule, holds(code, rule)) for code, rule in stated]
    if rules.match == "any":
        if any(ok for _, _, ok in outcomes):
            return True, ""
        wanted = " or ".join(f"{c} {RULE_LABELS[r]}" for c, r, _ in outcomes)
        return False, f"none of the tracker rules hold ({wanted})"

    for code, rule, ok in outcomes:
        if not ok:
            # Say what is actually true there, because "RED must be missing"
            # with no second half leaves you guessing whether it was checked.
            if code in missing:
                actual = "it is missing there"
            elif code in present:
                actual = "it is already there"
            else:
                actual = "it has not been checked there"
            return False, f"{code} {RULE_LABELS[rule]}, but {actual}"
    return True, ""


def partition(rows: list[dict[str, Any]], rules: QueueRules) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into what the queue shows and what the rules held back.

    Held-back rows keep a ``held_reason`` so the page can explain itself rather
    than silently showing a shorter list.

    Args:
        rows: Every matched release.
        rules: The admission rules.

    Returns:
        ``(shown, held)``.
    """
    shown, held = [], []
    for row in rows:
        ok, reason = admits(row, rules)
        if ok:
            shown.append(row)
        else:
            held.append({**row, "held_reason": reason})
    return shown, held
