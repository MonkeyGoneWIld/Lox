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


#: Formats that are not lossless. Deezer serves FLAC or MP3, but a request can
#: name any of these and the question is the same one: will lossy do?
LOSSY_FORMATS = frozenset({"MP3", "AAC", "AC3", "DTS", "Ogg Vorbis"})

#: The encodings that are not lossy. Everything else on either tracker's list
#: -- V0, V2, 320, 256, APS, q8.x -- is.
LOSSLESS_ENCODINGS = frozenset({"Lossless", "24bit Lossless"})

#: What a Gazelle request says when it does not mind. Deliberately not ``ANY``:
#: that name is taken by the queue rule meaning "queue anything", and defining
#: it twice in one module silently rebound the rule's own constant -- every
#: "queue anything" rule then fell through to the per-tracker branch and held
#: back the entire queue.
ANY_FORMAT = "Any"


def request_allows_lossy(formats: Any, encodings: Any) -> bool:
    """Whether a request said, in as many words, that lossy will do.

    Silence is not consent here. A request that names no format and no encoding
    has not "specifically mentioned" anything, so it is treated as wanting the
    lossless upload everyone assumes a request is for -- which is the
    conservative answer, and the recoverable one: a release held back for this
    is one re-check away from the queue, where a lossy upload against a
    lossless request is a trumped torrent and someone else's problem.

    Args:
        formats: The request's ``formatList``, e.g. ``["FLAC", "MP3"]``.
        encodings: Its ``bitrateList``, e.g. ``["V0 (VBR)", "320"]``.

    Returns:
        True when a not-all-FLAC source could legitimately fill it.
    """
    wanted_formats = [str(f) for f in formats or []]
    wanted_encodings = [str(e) for e in encodings or []]
    if not wanted_formats and not wanted_encodings:
        return False

    # A request naming only lossless formats wants lossless, whatever else it
    # says about bitrates.
    if wanted_formats and ANY_FORMAT not in wanted_formats and not (LOSSY_FORMATS & set(wanted_formats)):
        return False

    # And one naming only lossless encodings wants lossless, whatever formats
    # it listed -- "MP3, Lossless" is a contradiction, and the safe reading of
    # a contradiction is the strict one.
    if wanted_encodings and ANY_FORMAT not in wanted_encodings:
        return bool(set(wanted_encodings) - LOSSLESS_ENCODINGS)

    # Left over: the encodings said Any, or named nothing. A stated format has
    # already been checked and allows lossy, and a request that named no format
    # but any bitrate has still said it does not mind.
    return bool(wanted_formats) or ANY_FORMAT in wanted_encodings


#: Reasons a release will never become uploadable. A row held for one of these
#: is not waiting for anything -- no setting changes it, no re-check changes
#: it -- so it is dropped rather than parked in a list of things to look at.
#: Everything else is a state that can still move: unchecked, or excluded by a
#: rule the user can widen.
SETTLED = (
    "already on every tracker",
    "not released yet",
    "tracks can be downloaded",
    "not all FLAC on Deezer",
    "no song ID",
    "no filesize",
    "no tracks returned",
)


def is_settled(reason: str) -> bool:
    """Whether an exclusion is final rather than something still to resolve.

    Args:
        reason: The text :func:`admits` produced.

    Returns:
        True when nothing the user does will change the answer.
    """
    return any(needle in reason for needle in SETTLED)


def lossless_gate(row: dict[str, Any]) -> tuple[bool, str]:
    """Whether Deezer can actually produce an upload worth making.

    Deezer serves some albums as FLAC throughout and others with tracks that
    are MP3 only. A release that is not all FLAC is not an upload -- not a
    worse upload, not one -- unless a request is open that says lossy is
    acceptable. Nothing checked this outside the request path, so an album
    checked from Search or Browse went into the queue on the strength of "no
    tracker has it", with no idea whether there was anything to give them.

    Args:
        row: A queue row, carrying ``all_flac`` and, for a request, the
            formats and encodings that request will accept.

    Returns:
        ``(True, "")`` to let it through, or ``(False, reason)``.
    """
    # Whatever the availability check decided Deezer cannot supply: not
    # released yet, tracks that will not download, no song ids. It is a fact
    # about the source, so no request and no rule gets past it.
    blocked = str(row.get("blocked") or "")
    if blocked:
        return False, blocked

    all_flac = row.get("all_flac")
    if all_flac is True:
        return True, ""

    if all_flac is None:
        # Never looked. Held rather than hidden: the row stays on the page with
        # this as its reason, and a re-check answers it.
        return False, "Deezer formats not checked yet — re-check it to see if it is all FLAC"

    if "request" in (row.get("sources") or ()) and request_allows_lossy(
        row.get("request_formats"), row.get("request_encodings")
    ):
        return True, ""

    # The count is worth saying when we have it -- "4 of 11" is a different
    # release from "10 of 11" -- but only then. It came out as "only None/9"
    # for a row that knew the total and not the tally.
    have = row.get("flac_count")
    total = row.get("deezer_tracks")
    detail = f" (only {have} of {total} tracks are FLAC)" if have is not None and total else ""
    return False, f"not all FLAC on Deezer{detail}, and no open request accepts lossy"


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

    # Before any question of taste: is there a release here at all? A source
    # that is not all FLAC cannot fill a normal request and is not worth an
    # upload, so this runs ahead of the request shortcut below -- which would
    # otherwise wave through exactly the rows this is about.
    ok, why = lossless_gate(row)
    if not ok:
        return False, why

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
