"""A request already looked up is not looked up again, and you can see why.

Checking one request costs a tracker call to read it and a Deezer search to
match it. Nothing stopped that being paid twice: ``check_many`` asked
``should_skip``, which compares the stored status against the *album*
scanner's final statuses -- ``exists_red``, ``skipped_no_flac`` and friends. A
request's status is never any of those, so the answer was always False and
every run started from nothing. Run the same search twice and you paid for all
of it twice.

What this covers:

  * an answered request is skipped until the recheck window is up
  * a failed check is never skipped, because it learned nothing to reuse
  * "run them anyway" overrides all of it
  * a window of 0 means an answer never goes stale
  * the skipped ones are reported rather than silently dropped
  * the history endpoint can find any of them again, by any of its filters
"""

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

BASE = os.path.join(ROOT, "_recheck")
os.environ.update({
    "LOX_HOST": "127.0.0.1",
    "LOX_PORT": "5019",
    "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
    "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
    "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
    "LOX_STATE_DIR": os.path.join(BASE, "state"),
    "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
})
for key in ("LOX_DOWNLOAD_DIR", "LOX_TORRENTS_DIR", "LOX_STATE_DIR", "LOX_SETTINGS_DIR"):
    os.makedirs(os.environ[key], exist_ok=True)

TOKEN = os.environ["LOX_AUTH_TOKEN"]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


class FakeStore:
    """Just enough store to answer ``get``."""

    def __init__(self, entries: dict) -> None:
        self.entries = entries

    def get(self, _name: str, key: str):
        return self.entries.get(key)


def policy_checks() -> None:
    from lox.checker import recheck

    now = time.time()
    day = 86400

    # --- one request at a time -------------------------------------------
    check("a request never checked is checked", recheck.verdict(None, 30, now)[0], "")

    fresh = {"status": "fillable", "checked_at": now - 2 * day}
    ok, why = recheck.verdict(fresh, 30, now)
    check("one answered two days ago is not", not ok, why)
    check("and the reason says when and what", "2 days ago" in why and "fillable" in why, why)

    check("one answered today says today",
          "today" in recheck.verdict({"status": "skipped", "checked_at": now - 3600}, 30, now)[1], "")

    stale = {"status": "fillable", "checked_at": now - 40 * day}
    check("one past the window is checked again", recheck.verdict(stale, 30, now)[0], "")
    check("exactly at the window counts as due",
          recheck.verdict({"status": "fillable", "checked_at": now - 30 * day}, 30, now)[0], "")

    # A failure answered nothing, so there is nothing to reuse.
    check("a failed check is always re-run",
          recheck.verdict({"status": "error", "checked_at": now}, 30, now)[0], "")
    check("however recent it is",
          recheck.verdict({"status": "error", "checked_at": now - 1}, 365, now)[0], "")

    # 0 means "an answer is good forever".
    ok, why = recheck.verdict({"status": "skipped", "checked_at": now - 900 * day}, 0, now)
    check("with the window off, an old answer is still trusted", not ok, why)
    check("but a failure still is not",
          recheck.verdict({"status": "error", "checked_at": now - 900 * day}, 0, now)[0], "")

    # An undated answer must not silently count as fresh forever without saying so.
    ok, why = recheck.verdict({"status": "fillable"}, 30, now)
    check("an undated answer is trusted but says it has no date",
          not ok and "no date" in why, why)

    # A status nobody recognises is not an answer.
    check("an unrecognised status is treated as unanswered",
          recheck.verdict({"status": "halfway", "checked_at": now}, 30, now)[0], "")

    check("age is reported in days", round(recheck.age_days({"checked_at": now - 3 * day}, now)) == 3, "")
    check("and is None when there is no date", recheck.age_days({}, now) is None, "")
    check("or when the date is nonsense", recheck.age_days({"checked_at": "soon"}, now) is None, "")

    # --- a batch ----------------------------------------------------------
    store = FakeStore({
        "RED:1": {"status": "fillable", "checked_at": now},
        "RED:2": {"status": "skipped", "checked_at": now - 90 * day},
        "RED:3": {"status": "error", "checked_at": now},
        "RED:4": {"status": "filled", "checked_at": now - day},
    })
    todo, skipped = recheck.plan(store, "RED", ["1", "2", "3", "4", "5"],
                                 recheck_after_days=30, now=now)
    check("a batch checks only what needs it", todo == ["2", "3", "5"], str(todo))
    check("and hands back the rest", sorted(s["id"] for s in skipped) == ["1", "4"], str(skipped))
    check("each with a reason", all(s["reason"] for s in skipped), "")
    check("and enough of the old answer to show it",
          all("status" in s and "tracker" in s for s in skipped), "")

    todo, skipped = recheck.plan(store, "RED", ["1", "2", "3", "4"],
                                 recheck_after_days=30, force=True, now=now)
    check("running them anyway runs all of them", todo == ["1", "2", "3", "4"], str(todo))
    check("and skips nothing", skipped == [], "")

    todo, _ = recheck.plan(store, "RED", ["1", "2", "3", "4"], recheck_after_days=0, now=now)
    check("with the window off only failures and unknowns are run", todo == ["3"], str(todo))


def album_checks() -> None:
    """One album answer is worth keeping, and it is not "we looked".

    Every status a scan wrote used to count as settled, so an album was looked
    up once and then never again. That is right for exactly one of them --
    present on every tracker, which is nothing to upload under any rule -- and
    wrong for all the rest, in ways people hit:

    * a release missing from a tracker is one somebody else can upload before
      you do, and nothing ever noticed; the queue kept offering it.
    * an album a filter stopped was stopped for good. Widen the filter, scan
      the same module again, and the albums it was about stayed skipped.
    * an album checked against RED while OPS was out of budget counted as
      finished, and OPS was never asked.

    Which trackers said what is the whole of the decision, so it is read off
    the stored ``found_on`` and ``missing_from`` rather than guessed from the
    shape of a status string.
    """
    from lox.checker import recheck

    now = time.time()
    day = 86400
    both = ["RED", "OPS"]

    def album(found=(), missing=(), status="", age_days=1.0):
        return {"status": status, "found_on": list(found), "missing_from": list(missing),
                "checked_at": now - age_days * day}

    # --- the one that is finished ------------------------------------------
    ok, why = recheck.album_verdict(album(found=both, status="exists_ops_red"), 365, now, both)
    check("an album on every tracker is not looked up again", not ok, why)
    check("and the reason names them and says when",
          "RED" in why and "OPS" in why and "day" in why, why)

    # --- and everything that is not ----------------------------------------
    ok, why = recheck.album_verdict(album(found=["RED"], missing=["OPS"], status="exists_red"), 365, now, both)
    check("one missing from a tracker is looked up again", ok, why)
    ok, why = recheck.album_verdict(album(missing=both, status="missing_ops_red"), 365, now, both)
    check("and so is one missing from all of them", ok, why)
    ok, why = recheck.album_verdict({"status": "skipped_filter", "checked_at": now}, 365, now, both)
    check("an album a filter stopped is reconsidered, so widening one brings it back", ok, why)
    ok, why = recheck.album_verdict({"status": "skipped_no_flac", "checked_at": now}, 365, now, both)
    check("and so is one Deezer could not supply at the time", ok, why)
    ok, why = recheck.album_verdict(album(found=["RED"], status="exists_red"), 365, now, both)
    check("a tracker that was never asked is still a question", ok, why)
    check("a failed lookup is always retried",
          recheck.album_verdict({"status": "tracker_failed", "checked_at": now}, 365, now, both)[0], "")
    check("an album never seen is looked up", recheck.album_verdict(None, 365, now, both)[0], "")

    # --- the window is the ceiling over all of it --------------------------
    settled_but_old = album(found=both, status="exists_ops_red", age_days=400)
    check("past the window even the settled answer is asked again",
          recheck.album_verdict(settled_but_old, 365, now, both)[0], "")
    check("and with the window off it is trusted for good",
          not recheck.album_verdict(settled_but_old, 0, now, both)[0], "")

    # --- records written before the lists existed --------------------------
    check("an older 'on both trackers' record is still understood",
          not recheck.album_verdict({"status": "exists_both", "checked_at": now - day}, 365, now, both)[0], "")
    check("and an older 'missing from RED' one is read as missing",
          recheck.album_verdict({"status": "missing_red", "checked_at": now - day}, 365, now, both)[0], "")
    found, missing = recheck.tracker_sets({"status": "exists_ops_red"})
    check("a status names the trackers it was built from",
          found == {"OPS", "RED"} and not missing, f"{found} {missing}")

    # The request spelling must not leak into the album reading and vice versa.
    check("an album status is not a request answer",
          recheck.verdict({"status": "missing_red", "checked_at": now}, 30, now)[0], "")


async def endpoint_checks() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from lox.web import create_app_async

    runner = await create_app_async()
    app = runner.app
    store = app["store"]
    now = time.time()

    store.put("requests", "RED:1", {"status": "fillable", "artist": "Aphex Twin", "album": "SAW",
                                    "year": "1992", "bounty": "25.00 GB", "deezer_id": "9"})
    store.put("requests", "RED:2", {"status": "skipped", "reason": "no match", "artist": "Nobody",
                                    "album": "Lost", "year": "1978", "bounty": "100.00 MB"})
    store.put("requests", "OPS:3", {"status": "error", "reason": "timed out", "artist": "Someone",
                                    "album": "Broke", "year": "2015", "bounty": "1.00 TB"})
    # A key from before the "TRACKER:ID" convention.
    store.put("requests", "legacy", {"status": "skipped", "artist": "Old", "album": "Row"}, flush=True)
    entries = store.load("requests")
    entries["RED:2"]["checked_at"] = now - 60 * 86400
    store.save("requests")

    client = TestClient(TestServer(app))
    await client.start_server()
    headers = {"X-Auth-Token": TOKEN}

    async def history(query: str = "") -> dict:
        response = await client.get("/api/requests/history" + query, headers=headers)
        return await response.json()

    everything = await history()
    ids = [r["id"] for r in everything["requests"]]
    check("every checked request is listed", everything["total"] == 4, str(everything["total"]))
    check("newest first", ids[0] in ("1", "3", "legacy"), str(ids))
    check("the outcomes on offer come from the rows",
          everything["statuses"] == ["error", "fillable", "skipped"], str(everything["statuses"]))
    check("and the recheck window is reported so the page can flag what is due",
          isinstance(everything["recheck_after_days"], int), "")

    # A key with no colon must not become a tracker with no request.
    legacy = next(r for r in everything["requests"] if r["key"] == "legacy")
    check("a key from before the convention keeps its id", legacy["id"] == "legacy", str(legacy["id"]))
    check("and claims no tracker", legacy["tracker"] == "", repr(legacy["tracker"]))

    check("filter by tracker", [r["id"] for r in (await history("?tracker=OPS"))["requests"]] == ["3"], "")
    check("filter by outcome",
          [r["id"] for r in (await history("?status=fillable"))["requests"]] == ["1"], "")
    check("filter by two outcomes at once",
          len((await history("?status=fillable&status=error"))["requests"]) == 2, "")
    check("filter by text, on artist or album",
          [r["id"] for r in (await history("?q=aphex"))["requests"]] == ["1"], "")
    check("and on request id", [r["id"] for r in (await history("?q=3"))["requests"]] == ["3"], "")

    # Bounties are stored as the tracker's own string, so comparing them as
    # text puts 900 MB above 1 TB.
    big = [r["id"] for r in (await history("?min_bounty=1073741824"))["requests"]]
    check("bounty is compared as a size, not as text", sorted(big) == ["1", "3"], str(big))

    check("filter by year from",
          sorted(r["id"] for r in (await history("?min_year=2000"))["requests"]) == ["3"], "")
    dated = sorted(r["id"] for r in (await history("?max_year=1995"))["requests"])
    check("filter by year to", dated == ["1", "2"], str(dated))
    check("and a row with no year stays out of year filters either way",
          "legacy" not in dated
          and "legacy" not in [r["id"] for r in (await history("?min_year=1000"))["requests"]], "")

    recent = [r["id"] for r in (await history("?checked_within=7"))["requests"]]
    check("filter by checked recently", "2" not in recent and "1" in recent, str(recent))
    overdue = [r["id"] for r in (await history("?checked_before=30"))["requests"]]
    check("filter by not checked for a while", overdue == ["2"], str(overdue))

    check("filters combine", (await history("?tracker=RED&status=skipped"))["total"] == 1, "")
    check("a filter matching nothing is empty rather than everything",
          (await history("?q=nothingmatchesthis"))["total"] == 0, "")
    check("a nonsense number is ignored rather than fatal",
          (await history("?min_bounty=abc"))["total"] == 4, "")

    # --- the scan's own history ------------------------------------------
    store.put("albums", "a1", {"status": "exists_ops_red", "title": "Everyone Has It",
                               "artist": "A", "source": "playlist",
                               "found_on": ["RED", "OPS"], "missing_from": []})
    store.put("albums", "a2", {"status": "missing_red", "title": "Worth Uploading",
                               "artist": "B", "source": "genre",
                               "missing_from": ["RED"], "found_on": []})
    store.put("albums", "a3", {"status": "skipped_filter", "title": "Ruled Out",
                               "artist": "C", "source": "playlist",
                               "reason": "released 2019-01-01, before 2025-01-01"}, flush=True)

    response = await client.get("/api/scan/history", headers=headers)
    scan = await response.json()
    by_id = {row["id"]: row for row in scan["albums"]}
    check("the scan history lists what a scan looked up", scan["total"] >= 3, str(scan["total"]))
    check("with the tracker verdict read as a phrase",
          by_id["a1"]["outcome"] == "Already on every tracker", by_id["a1"]["outcome"])
    check("whichever trackers the status names",
          by_id["a2"]["outcome"] == "Missing from a tracker", by_id["a2"]["outcome"])
    check("and a filtered album says so",
          by_id["a3"]["outcome"] == "Ruled out by a scan filter", by_id["a3"]["outcome"])
    check("it carries the reason", "before 2025-01-01" in by_id["a3"]["reason"], "")
    check("where the album came from", by_id["a2"]["source"] == "genre", by_id["a2"]["source"])
    check("when it was added and when it was last looked at",
          by_id["a2"]["added_at"] and by_id["a2"]["checked_at"], "")
    check("and the window a scan will trust it for",
          isinstance(scan["recheck_after_days"], int), "")
    check("plus the filters a scan applies, so the page can show them",
          {"min_tracks", "min_date", "max_date"} <= set(scan["filters"]),
          str(sorted(scan["filters"])))
    # Both dates default to something relative to today, so a blank one is not
    # "no limit" and the page cannot work out what a scan will do from the
    # stored value alone.
    check("and what a blank one currently means",
          scan["filters"]["min_date_effective"] and scan["filters"]["max_date_effective"],
          str(scan["filters"]))
    check("which is last January, and two days out",
          scan["filters"]["min_date_default"].endswith("-01-01")
          and scan["filters"]["max_date_default"] > scan["filters"]["min_date_default"],
          str(scan["filters"]))

    await client.close()


def source_checks() -> None:
    import inspect

    from lox.checker import deezer_requests
    from lox.checker.gateway import TrackerGateway
    from lox.checker.request_detail import request_detail

    source = inspect.getsource(deezer_requests.DeezerRequestChecker.check_many)
    check("the batch check asks the recheck policy", "recheck.plan" in source, "")

    from lox.checker import missing as missing_mod

    scan = inspect.getsource(missing_mod.MissingScanner.collect)
    check("and a scan asks the album half of it",
          "recheck.album_verdict" in scan, "")
    check("rather than the status list that missed exists_ops_red",
          "self.store.should_skip" not in scan, "")
    check("saying what it passed over before it spends anything",
          'emit("skipped"' in scan, "")

    # The scan filters govern scanning and nothing else.
    from lox.checker import deezer_requests as dr_mod

    check("only the scan consults the scan filters",
          "_filter_reason" in inspect.getsource(missing_mod)
          and "min_tracks" not in inspect.getsource(dr_mod), "")

    from lox.config.schema import FIELDS

    hidden = {f.key for f in FIELDS if f.on_page == "scan"}
    check("the scan filters are declared, so they can still be saved",
          {"checker.min_tracks", "checker.min_date", "checker.max_date"} <= hidden,
          str(sorted(hidden)))
    from lox.config.schema import sections_with_fields

    on_page = {f["key"] for s_ in sections_with_fields() for f in s_["fields"]}
    check("but not offered on the settings page", not (hidden & on_page),
          str(sorted(hidden & on_page)))
    check("while the request threshold is",
          "checker.min_confidence" in on_page, "")
    # should_skip compares against the album scanner's final statuses, which a
    # request's status is never one of -- which is why nothing was skipped.
    check("rather than the album scanner's statuses",
          "self.store.should_skip" not in source, "")
    check("and says what it skipped before spending anything",
          'emit("skipped"' in source, "")

    # A person clicking a row must not queue behind a running batch.
    check("opening one request is an interactive call",
          "interactive=True" in inspect.getsource(request_detail), "")
    call = inspect.getsource(TrackerGateway._call)
    check("an interactive call skips the pacing queue",
          "if interactive:" in call and "async with state.lock" in call, "")
    check("but still obeys the budget and the breaker",
          call.index("self._guard(code, state)") < call.index("async with state.lock"), "")


def main() -> int:
    policy_checks()
    album_checks()
    asyncio.run(endpoint_checks())
    source_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
