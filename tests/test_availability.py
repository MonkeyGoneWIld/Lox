"""Deezer has to actually be able to supply a release before it counts as one.

BIG NOTER — SONGS IN THE KEY OF WRESTLING reached the queue, and lox reported
it as "All FLAC, all streamable, 11/11 FLAC". Deezer's own answer for that
album is that four of its eleven tracks are readable and it is not out until
2026-09-18: it is a pre-release, and Deezer lists the whole tracklist with FLAC
sizes while only the released singles play.

The streamable half of the check had never done anything. It read
``track.get("readable", True)`` off the gw-light song records, which are
upper-case — SNG_ID, SNG_TITLE, FILESIZE_FLAC — and carry no ``readable`` key
at all, so the default won on every track of every album ever checked.

So the check now asks the public API, which answers per track, and refuses a
release whose date has not arrived. What it cannot supply it names, rather than
counting.
"""

import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

BASE = os.path.join(ROOT, "_availability")
os.environ.setdefault("LOX_HOST", "127.0.0.1")
os.environ.setdefault("LOX_PORT", "5021")
os.environ.setdefault("LOX_AUTH_TOKEN", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("LOX_DOWNLOAD_DIR", os.path.join(BASE, "downloads"))
os.environ.setdefault("LOX_TORRENTS_DIR", os.path.join(BASE, "torrents"))
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def make(**kw):
    from lox.deezer.gw import TrackAvailability

    base = dict(
        total=11, flac_count=11, readable_count=11,
        all_flac=True, all_readable=True, all_have_id=True, all_have_filesize=True,
        unreadable=[], release_date="2020-01-01", unreleased=False,
    )
    base.update(kw)
    return TrackAvailability(**base)


class AliasGW:
    """A DeezerGW whose two lookups are decided rather than made."""

    def __init__(self, known: set[str], public_answer: dict) -> None:
        from lox.deezer.gw import DeezerGW

        self.real = DeezerGW(arl="x")
        self.known = known
        self.public_answer = public_answer
        self.asked: list[str] = []
        self.public_calls: list[str] = []

        async def call(_method, payload=None, **_kw):
            from lox.deezer.gw import DeezerGWError

            alb = str((payload or {}).get("alb_id"))
            self.asked.append(alb)
            if alb not in self.known:
                raise DeezerGWError(
                    "gw-light error for deezer.pageAlbum: {'DATA_ERROR': 'album::getData'}"
                )
            return {"SONGS": {"data": [{"SNG_ID": "1"}]}}

        async def public(path, _params=None):
            self.public_calls.append(path)
            return self.public_answer

        self.real.call = call  # pyright: ignore[reportAttributeAccessIssue]
        self.real.public = public  # pyright: ignore[reportAttributeAccessIssue]


def alias_checks() -> None:
    """An alias album id must not sink every private lookup that follows.

    Deezer hands out alias ids. The public API answers /album/1025281552 with
    the record for 927219761, following the redirect without saying so, while
    the private gateway only knows canonical ids and replies
    {'DATA_ERROR': 'album::getData'}. A release found through the public API
    was therefore checked against both trackers and then failed every private
    lookup after it -- its availability, and its featured artists mid-upload --
    for a release that is perfectly available under its real id.
    """
    import asyncio
    import contextlib

    from lox.deezer.gw import DeezerGWError

    # The reported case, both ids as Deezer actually returned them.
    gw = AliasGW(known={"927219761"}, public_answer={"id": 927219761, "title": "Mount Zero"})
    page = asyncio.run(gw.real.album_page("1025281552"))
    check("an alias id still returns the album", bool(page.get("SONGS")), str(page)[:60])
    check("by asking the private gateway for the canonical id",
          gw.asked == ["1025281552", "927219761"], str(gw.asked))
    check("which it learned from the public API", gw.public_calls == ["/album/1025281552"],
          str(gw.public_calls))

    # And it is learned once, not once per lookup.
    asyncio.run(gw.real.album_page("1025281552"))
    check("the alias is remembered rather than looked up again",
          len(gw.public_calls) == 1, str(gw.public_calls))

    # A canonical id costs nothing extra.
    plain = AliasGW(known={"927219761"}, public_answer={"id": 927219761})
    asyncio.run(plain.real.album_page("927219761"))
    check("an id that works is not resolved at all",
          plain.asked == ["927219761"] and plain.public_calls == [], str(plain.public_calls))

    # An id nobody has heard of reports the gateway's own error, not a
    # confusing second one from the public API.
    dead = AliasGW(known=set(), public_answer={"error": {"type": "DataException"}})
    try:
        asyncio.run(dead.real.album_page("1"))
        raised = ""
    except DeezerGWError as e:
        raised = str(e)
    check("a dead id still raises, with the reason Deezer gave",
          "album::getData" in raised, raised[:70])
    check("and it is not asked about twice", dead.asked == ["1"], str(dead.asked))

    # An id the public API resolves to itself is not worth a second call.
    same = AliasGW(known=set(), public_answer={"id": 55})
    with contextlib.suppress(DeezerGWError):
        asyncio.run(same.real.album_page("55"))
    check("nor is one that resolves to itself", same.asked == ["55"], str(same.asked))

    # Anything that is not this error is passed straight through: a token
    # failure is not an alias problem and must not cost a public call.
    other = AliasGW(known={"1"}, public_answer={})

    async def boom(_method, payload=None, **_kw):
        raise DeezerGWError("gw-light error for deezer.pageAlbum: {'VALID_TOKEN_REQUIRED': 1}")

    other.real.call = boom  # pyright: ignore[reportAttributeAccessIssue]
    with contextlib.suppress(DeezerGWError):
        asyncio.run(other.real.album_page("1"))
    check("an unrelated failure is not treated as an alias",
          other.public_calls == [], str(other.public_calls))


def main() -> int:
    from lox.deezer.gw import _is_future

    # --- the date --------------------------------------------------------
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    check("a date in the future is future", _is_future(tomorrow), tomorrow)
    check("a date in the past is not", not _is_future(yesterday), yesterday)
    check("today is not future", not _is_future(date.today().isoformat()), "")
    # Plenty of real releases carry no date, and refusing all of them would be
    # worse than the problem.
    check("a missing date is not treated as future", not _is_future(""), "")
    check("nor is an unreadable one", not _is_future("soon"), "")
    check("nor a truncated one", not _is_future("2026"), "")

    # --- the album that started this -------------------------------------
    # Four of eleven readable, out on 2026-09-18.
    wrestling = make(
        readable_count=4, all_readable=False,
        unreadable=["MY GENOCIDE", "IT'S ALL GOOD TIL IT'S NOT", "A MESSAGE FROM CHUCK",
                    "BY THE TIME I GET TO YORTA YORTA WOKA", "WHAT YA SWINGIN' AT?",
                    "B.M.F.", "WHAT'S THE MATTER WITH ADAM?"],
        release_date=tomorrow, unreleased=True,
    )
    check("a pre-release is not uploadable", not wrestling.uploadable, "")
    check("and says so by its date", "not released yet" in (wrestling.reason() or ""), wrestling.reason() or "")
    check("even though every track reports a FLAC size", wrestling.all_flac, "")

    # The same album once its date arrives is still unusable, for the other
    # reason -- which is the one that used to go unnoticed entirely.
    out_now = make(
        readable_count=4, all_readable=False,
        unreadable=wrestling.unreadable, release_date=yesterday, unreleased=False,
    )
    check("a released album with unplayable tracks is still not uploadable",
          not out_now.uploadable, "")
    reason = out_now.reason() or ""
    check("and the reason counts what can actually be fetched",
          "only 4 of 11 tracks can be downloaded" in reason, reason)
    check("and names the missing ones rather than only counting",
          "MY GENOCIDE" in reason, reason)
    check("without listing all seven", "and 4 more" in reason, reason)
    # The "and N more" counts the names held, not the shortfall: a payload
    # naming two while excluding seven produced "One, Two and 4 more".
    short = make(total=11, readable_count=4, all_readable=False, unreadable=["One", "Two"])
    check("and the overflow counts the names it has",
          "(One, Two)" in (short.reason() or ""), short.reason() or "")

    # --- the ordinary cases ----------------------------------------------
    check("a complete, released album passes", make().uploadable, "")
    check("and has nothing to report", make().reason() is None, "")

    lossy = make(flac_count=6, all_flac=False)
    check("a partly-lossy album is not uploadable", not lossy.uploadable, "")
    check("and says which count is short", "6/11" in (lossy.reason() or ""), lossy.reason() or "")

    # Unplayable tracks are the bigger complaint: an album with FLAC sizes for
    # tracks nobody can fetch is not a FLAC problem.
    both = make(flac_count=6, all_flac=False, readable_count=4, all_readable=False,
                unreadable=["One", "Two"])
    check("with both problems, the one that stops a download is reported",
          "can be downloaded" in (both.reason() or ""), both.reason() or "")

    check("no tracks at all is its own answer",
          make(total=0).reason() == "no tracks returned", "")
    check("missing song ids too",
          "no song ID" in (make(all_have_id=False).reason() or ""), "")

    # --- the check is wired to the paths ---------------------------------
    import inspect

    from lox.checker import deezer_requests, missing, queue_rules
    from lox.deezer import gw

    source = inspect.getsource(gw.DeezerGW.availability)
    check("availability asks the public API which tracks play",
          "readable_by_id" in source, "")
    check("consulting it first, with the record's own field only as a fallback",
          "playable = readable_by_id.get(track_id)" in source
          and "if playable is None:" in source, "")
    check("and records the release date", "release_date" in source, "")

    check("the scanner files an unreleased album as such",
          "skipped_unreleased" in inspect.getsource(missing), "")
    check("the request check uses the one verdict",
          "availability.reason()" in inspect.getsource(deezer_requests), "")
    check("and a request that takes MP3 is not refused for FLAC alone",
          "not availability.all_flac and availability.all_readable"
          in inspect.getsource(deezer_requests), "")
    check("the queue refuses whatever Deezer cannot supply",
          'row.get("blocked")' in inspect.getsource(queue_rules), "")

    alias_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
