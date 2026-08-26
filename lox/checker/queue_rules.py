"""Which matched releases are worth acting on.

Every check that matches a Deezer release to a tracker is kept, because the
tracker call that produced it has already been paid for. That is not the same
question as "do I want to upload this", and this module answers the second one.

There is one question here and it is asked in words: **when is a release worth
queueing?** The first attempt at this asked four -- a three-way rule per
tracker, an all/any to combine them, and a separate enum for requests -- which
is a truth table with a dropdown in front of it. Nobody wants to say "RED must
already be there"; they want to say "missing from OPS, and RED already has it",
which is one sentence and is now one option.

The rules are applied when the queue is READ rather than when the check runs.
Narrowing hides rows, widening brings the same rows straight back, and neither
costs a tracker call. Nothing a rule excludes is deleted.
"""

from typing import Any, NamedTuple

from lox.config.schema import QUEUE_OPTIONS, QUEUE_TRACKERS

TRACKERS = QUEUE_TRACKERS

ANY = "any"
ALL = "all"
ONLY = "_only"

LABELS: dict[str, str] = dict(QUEUE_OPTIONS)


class QueueRules(NamedTuple):
    """The admission rules, lifted out of the config."""

    when: str
    requests_too: bool

    def describe(self) -> str:
        """One line for the page to show beside the held-back count."""
        text = LABELS.get(self.when, LABELS[ANY]).lower()
        if self.requests_too:
            text += ", plus anything that fills an open request"
        return text


def rules_from(checker: Any) -> QueueRules:
    """Read the rules off the checker config.

    Args:
        checker: ``cfg.checker``.

    Returns:
        The rules as a plain value, so the predicate never touches global config.
    """
    when = getattr(checker, "queue_when", ANY) or ANY
    return QueueRules(
        when=when if when in LABELS else ANY,
        requests_too=bool(getattr(checker, "queue_requests_too", True)),
    )


def admits(row: dict[str, Any], rules: QueueRules) -> tuple[bool, str]:
    """Decide whether one matched release belongs in the queue.

    Args:
        row: A queue row as :func:`lox.web.api.api_found` builds it --
            ``sources``, ``missing_from``, ``found_on``.
        rules: The admission rules.

    Returns:
        ``(True, "")`` to let it through, or ``(False, reason)`` where the
        reason is short enough to show beside a "12 held back" count.
    """
    missing = {str(t).upper() for t in row.get("missing_from") or ()}
    present = {str(t).upper() for t in row.get("found_on") or ()}
    sources = row.get("sources") or ([row["kind"]] if row.get("kind") else [])

    # Nothing to upload is not a matter of taste, and it comes before every
    # rule including the request one -- a request for something every tracker
    # already has is a stale request, not work.
    if not missing:
        if not present:
            return False, "not checked against any tracker yet"
        return False, "already on every tracker it was checked against"

    # An open request is a reason to upload on its own, so this is an "or"
    # around the rule below rather than a filter on top of it.
    if rules.requests_too and "request" in sources:
        return True, ""

    if rules.when == ANY:
        return True, ""

    if rules.when == ALL:
        if present:
            return False, f"{', '.join(sorted(present))} already has it"
        return True, ""

    code = rules.when[: -len(ONLY)] if rules.when.endswith(ONLY) else rules.when
    if code not in missing:
        where = "it is already there" if code in present else "it has not been checked there"
        return False, f"the rule is about {code}, and {where}"
    if rules.when.endswith(ONLY):
        others = sorted(missing - {code})
        if others:
            return False, f"also missing from {', '.join(others)}, and the rule wants only {code}"
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
