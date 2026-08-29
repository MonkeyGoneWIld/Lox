"""What a scan looks at, and what it stops looking at.

Three decisions, all of which were wrong in the same direction -- too sticky.

The filters were fixed dates in a config file. A "released after" written down
once is right on the day it is written and admits every announcement made
after it; a "released before" of 2025 keeps scanning 2025 for ever. Both are
relative to today, so both are computed, and a blank setting means the
computed one rather than no limit at all.

The filters also applied to albums nobody swept up. Ticking a release in
Search and pressing Check trackers ran it through the same track-count and
date filters as a sweep of a channel module, so an EP you had chosen by hand
was dropped with "track count 4 below minimum 5" -- a rule about what to sweep,
applied to something you had already decided about.

And an album was looked up once and then never again, whatever the answer had
been. That is right for one answer only: on every tracker there is. The rest
move -- somebody else uploads the thing you were queueing, a filter widens back
onto an album it stopped, a tracker that was out of budget never got asked --
and a scan that never asks again never notices. The last of those is also what
takes a release back OUT of the queue once somebody else has uploaded it.

Runs the real scanner against a stand-in for Deezer. No network, no tracker.
"""

import asyncio
import os
import shutil
import sys
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_scanfilters")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5113",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)
sys.path.insert(0, os.path.dirname(ROOT))

from lox import cfg  # noqa: E402
from lox.checker import recheck  # noqa: E402
from lox.checker.missing import (  # noqa: E402
    MissingScanner,
    default_max_date,
    default_min_date,
    effective_filters,
)
from lox.checker.queue_rules import QueueRules, admits  # noqa: E402
from lox.checker.store import CheckerStore  # noqa: E402
from lox.deezer.gw import TrackAvailability  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


class FakeGW:
    """One album, four tracks, out last month, all FLAC and playable."""

    def __init__(self, tracks: int = 4, released: str = "2026-07-01") -> None:
        self.tracks = tracks
        self.released = released

    async def album(self, album_id):
        return {"id": album_id, "title": "An EP", "nb_tracks": self.tracks,
                "release_date": self.released, "artist": {"name": "Somebody"}}

    async def availability(self, album_id):
        return TrackAvailability(
            total=self.tracks, flac_count=self.tracks, readable_count=self.tracks,
            all_flac=True, all_readable=True, all_have_id=True, all_have_filesize=True,
            unreadable=[], release_date=self.released, unreleased=False)


LINK = "https://www.deezer.com/album/555"


def scanner(gw=None) -> MissingScanner:
    store = CheckerStore(os.path.join(BASE, "state"))
    store.clear("albums")
    return MissingScanner(gw or FakeGW(), gateway=None, store=store)


async def main() -> int:
    # --- the defaults move with the calendar --------------------------------
    check("the oldest release a scan looks at is last January",
          default_min_date(date(2026, 8, 27)) == "2025-01-01", default_min_date(date(2026, 8, 27)))
    check("and it rolls over with the year",
          default_min_date(date(2027, 1, 15)) == "2026-01-01", default_min_date(date(2027, 1, 15)))
    check("the newest is two days out, not today",
          default_max_date(date(2026, 8, 27)) == "2026-08-29", default_max_date(date(2026, 8, 27)))
    check("which crosses a year end without special-casing it",
          default_max_date(date(2026, 12, 31)) == "2027-01-02", default_max_date(date(2026, 12, 31)))

    cfg.checker.min_date = None
    cfg.checker.max_date = None
    cfg.checker.min_tracks = 5
    active = effective_filters()
    check("a blank date means the rolling default, not 'no limit'",
          active["min_date"] == default_min_date() and active["max_date"] == default_max_date(),
          str(active))
    check("and out of the box a scan skips anything under five tracks",
          active["min_tracks"] == 5, str(active["min_tracks"]))

    cfg.checker.min_date = "2000-01-01"
    check("a date set by hand wins over the default",
          effective_filters()["min_date"] == "2000-01-01", str(effective_filters()))
    cfg.checker.min_date = None

    # --- a sweep obeys the filters ------------------------------------------
    scan = scanner()
    swept = await scan.collect([LINK])
    check("a four-track EP is swept past when the floor is five", swept == [], str(swept))
    stored = scan.store.get("albums", "555") or {}
    check("and the record says which filter did it",
          stored.get("status") == "skipped_filter" and "below minimum 5" in stored.get("reason", ""),
          str(stored.get("reason")))

    # --- an album picked by hand does not -----------------------------------
    scan = scanner()
    picked = await scan.collect([LINK], manual=True)
    check("the same EP checked by hand is not filtered out",
          [c.album_id for c in picked] == ["555"], str(picked))

    # And nor is it skipped for having been looked at before: pressing Check
    # trackers on something is asking, whatever the answer was last time.
    scan.store.put("albums", "555", {"status": "exists_ops_red",
                                     "found_on": ["RED", "OPS"], "missing_from": [],
                                     "checked_at": 9_999_999_999})
    again = await scan.collect([LINK], manual=True)
    check("nor is one that was already answered", [c.album_id for c in again] == ["555"], str(again))
    swept_again = await scan.collect([LINK])
    check("while a sweep still passes over it", swept_again == [], str(swept_again))

    # --- a widened filter brings back what the old one stopped --------------
    scan = scanner()
    await scan.collect([LINK])
    check("a filtered album is on record as filtered",
          (scan.store.get("albums", "555") or {}).get("status") == "skipped_filter", "")
    # Re-deciding costs no Deezer call while the answer is the same: the
    # filter is a function of a track count and a release date, both of which
    # are on the record. Otherwise a weekly scan of a module of four hundred
    # singles would pay two reads apiece to drop them again.
    reads = []
    scan.gw.album = lambda album_id, _log=reads: (_log.append(album_id), {})[1]
    await scan.collect([LINK])
    check("re-deciding a filtered album asks Deezer nothing", reads == [], str(reads))

    scan = scanner()
    await scan.collect([LINK])
    cfg.checker.min_tracks = 3
    back = await scan.collect([LINK])
    check("lower the floor, scan the same link, and it comes back",
          [c.album_id for c in back] == ["555"], str(back))
    cfg.checker.min_tracks = 5

    # --- and a release somebody else uploaded leaves the queue --------------
    # The chain the report was about: a queued release is looked up again,
    # the new answer overwrites the old, and the queue is drawn from the
    # answer -- so it drops out without anything having to delete it.
    rules = QueueRules(when="any", requests_too=True)
    queued = {"found_on": [], "missing_from": ["RED", "OPS"], "all_flac": True, "sources": ["scan"]}
    ok, _ = admits(queued, rules)
    check("a release missing from both trackers is in the queue", ok, "")
    # Asking again is the confirmation's job, not a sweep's. A sweep that
    # re-checked every queued release paid a tracker call apiece for the newest
    # answers it had, every time it ran.
    check("a confirmation looks at it again rather than trusting the answer",
          recheck.album_verdict({**queued, "checked_at": 1}, 365, 2, ["RED", "OPS"],
                                confirming=True)[0], "")
    check("while a sweep leaves the queue alone",
          not recheck.album_verdict({**queued, "checked_at": 1}, 365, 2, ["RED", "OPS"])[0], "")

    filled = {**queued, "found_on": ["RED", "OPS"], "missing_from": []}
    ok, why = admits(filled, rules)
    check("once both trackers have it, the same row is out of the queue", not ok, why)
    check("for a reason that says so", "already on every tracker" in why, why)
    check("and that answer is the one a scan stops asking about",
          not recheck.album_verdict({**filled, "checked_at": 2}, 365, 2, ["RED", "OPS"])[0], "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
