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

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
