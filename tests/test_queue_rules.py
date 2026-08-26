"""What reaches the queue, and what the page is told about what did not.

The queue used to admit anything missing from at least one tracker, which put
"nobody has this" and "RED already has it, OPS does not" in the same list with
no way to tell them apart or to ask for one and not the other.

The rules are checked twice over: once as a predicate, against every
combination that has a sensible answer, and once through the real ``/api/found``
handler with rows in the store, because the predicate being right is no use if
the endpoint never calls it.

The other half is honesty. A rule that hides rows has to say so -- a queue that
quietly got shorter looks exactly like a scan that found nothing -- so the
endpoint returns what it held and why, and that is checked here too.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_queuerules")
PORT = 5110
TOKEN = "0123456789abcdef0123456789abcdef"
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": str(PORT),
        "LOX_AUTH_TOKEN": TOKEN,
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

import aiohttp  # noqa: E402

from lox.checker.queue_rules import QueueRules, admits, partition, rules_from  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def rules(red="any", ops="any", dic="any", match="all", requests="any", floor=True) -> QueueRules:
    return QueueRules(
        trackers=(("RED", red), ("OPS", ops), ("DIC", dic)),
        match=match,
        requests=requests,
        require_somewhere_missing=floor,
    )


def row(missing=(), found=(), kind="scan", tracker=""):
    return {"kind": kind, "missing_from": list(missing), "found_on": list(found), "tracker": tracker}


def main_predicate() -> None:
    nobody_has = row(missing=["RED", "OPS"])
    red_has = row(missing=["OPS"], found=["RED"])
    ops_has = row(missing=["RED"], found=["OPS"])
    everyone_has = row(found=["RED", "OPS"])

    # --- the floor ----------------------------------------------------
    check("a release everyone has is not worth queueing",
          not admits(everyone_has, rules())[0], admits(everyone_has, rules())[1])
    check("and the reason says so",
          "every tracker" in admits(everyone_has, rules())[1], admits(everyone_has, rules())[1])
    check("unless you turn the floor off",
          admits(everyone_has, rules(floor=False))[0], "")

    # --- the default lets ordinary work through -----------------------
    for name, r in (("nobody has it", nobody_has), ("RED has it", red_has), ("OPS has it", ops_has)):
        check(f"by default, {name} is queued", admits(r, rules())[0], admits(r, rules())[1])

    # --- missing on both ----------------------------------------------
    both = rules(red="missing", ops="missing")
    check("missing on RED and OPS: takes the one nobody has", admits(nobody_has, both)[0], "")
    check("missing on RED and OPS: rejects one RED has", not admits(red_has, both)[0], "")
    check("and says which tracker disagreed",
          "RED" in admits(red_has, both)[1] and "already there" in admits(red_has, both)[1],
          admits(red_has, both)[1])

    # --- missing on OPS only, which is a rule about two trackers ------
    ops_only = rules(red="present", ops="missing")
    check("missing on OPS only: takes the one RED already has", admits(red_has, ops_only)[0], "")
    check("missing on OPS only: rejects the one nobody has", not admits(nobody_has, ops_only)[0], "")
    check("missing on OPS only: rejects the one OPS has", not admits(ops_has, ops_only)[0], "")

    # --- either one is enough -----------------------------------------
    either = rules(red="missing", ops="missing", match="any")
    check("any: one missing tracker is enough", admits(red_has, either)[0], "")
    check("any: still rejects when neither holds",
          not admits(row(missing=["DIC"], found=["RED", "OPS"]), either)[0], "")
    check("and names what it wanted",
          "none of the tracker rules hold" in admits(row(missing=["DIC"], found=["RED", "OPS"]), either)[1], "")

    # --- a tracker that was never checked is not "missing" ------------
    unchecked = row(missing=["OPS"])
    verdict = admits(unchecked, rules(red="missing", ops="missing"))
    check("an unchecked tracker does not count as missing", not verdict[0], "")
    check("and the reason says it was never checked", "not been checked" in verdict[1], verdict[1])

    # --- requests ------------------------------------------------------
    fill = row(missing=["OPS"], found=["RED"], kind="request", tracker="OPS")
    wrong_home = row(missing=["RED"], found=["OPS"], kind="request", tracker="OPS")
    check("only: a scan result is rejected", not admits(nobody_has, rules(requests="only"))[0], "")
    check("only: a request fill is taken", admits(fill, rules(requests="only"))[0], "")
    check("exclude: a request fill is rejected", not admits(fill, rules(requests="exclude"))[0], "")
    check("exclude: a scan result is taken", admits(nobody_has, rules(requests="exclude"))[0], "")
    check("only_missing_there: takes a fill missing on the request's tracker",
          admits(fill, rules(requests="only_missing_there"))[0], "")
    check("only_missing_there: rejects one already up on the request's tracker",
          not admits(wrong_home, rules(requests="only_missing_there"))[0], "")
    check("and says which tracker it meant",
          "OPS" in admits(wrong_home, rules(requests="only_missing_there"))[1],
          admits(wrong_home, rules(requests="only_missing_there"))[1])

    # --- the whole example the rules were built for --------------------
    # "missing on OPS, there is an OPS request, and RED already has it"
    exact = rules(red="present", ops="missing", requests="only_missing_there")
    check("the awkward one: OPS request, missing on OPS, RED has it", admits(fill, exact)[0], "")
    check("the awkward one: rejects the same release without a request",
          not admits(red_has, exact)[0], "")

    # --- partition and description -------------------------------------
    shown, held = partition([nobody_has, red_has, everyone_has], both)
    check("partition keeps the admitted ones", len(shown) == 1, str(len(shown)))
    check("and hands back the rest with a reason",
          len(held) == 2 and all(h["held_reason"] for h in held), str(len(held)))
    check("nothing is lost between the two", len(shown) + len(held) == 3, "")
    check("the rule describes itself in words",
          "RED must be missing there and OPS must be missing there" == both.describe(), both.describe())
    check("including the request part",
          "only releases that fill a request" in rules(requests="only").describe(),
          rules(requests="only").describe())
    check("and says so plainly when nothing is stated",
          rules().describe() == "any tracker", rules().describe())


async def main_endpoint() -> None:
    from lox import cfg
    from lox.web import create_app_async

    runner = await create_app_async()
    store = runner.app["store"]
    store.put("albums", "1", {"title": "Nobody", "artist": "A", "missing_from": ["RED", "OPS"], "found_on": []})
    store.put("albums", "2", {"title": "RedHas", "artist": "B", "missing_from": ["OPS"], "found_on": ["RED"]})
    store.put("albums", "3", {"title": "AllHave", "artist": "C", "missing_from": [], "found_on": ["RED", "OPS"]})
    store.flush()

    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    base = f"http://127.0.0.1:{PORT}"
    await session.post(f"{base}/api/auth", json={"token": TOKEN})
    try:
        async with session.get(f"{base}/api/found") as r:
            payload = await r.json()
        titles = sorted(f["title"] for f in payload["found"])
        check("the endpoint applies the floor", titles == ["Nobody", "RedHas"], str(titles))
        check("and reports the one it held", payload["held_count"] == 1, str(payload["held_count"]))
        check("with the rule in words", payload["rule"] == "any tracker", str(payload.get("rule")))

        # Narrow it the way the settings page would, and the same rows re-sort
        # themselves with no tracker call in sight.
        async with session.put(
            f"{base}/api/settings",
            json={"changes": {"checker.queue_red": "missing", "checker.queue_ops": "missing"}},
        ) as r:
            check("the rule saves", r.status == 200, str(r.status))
        check("and reaches the config", cfg.checker.queue_red == "missing", cfg.checker.queue_red)

        async with session.get(f"{base}/api/found") as r:
            payload = await r.json()
        titles = sorted(f["title"] for f in payload["found"])
        check("a narrowed rule narrows the queue", titles == ["Nobody"], str(titles))
        check("the rest are held, not dropped", payload["held_count"] == 2, str(payload["held_count"]))
        check("each held row says why",
              all(h.get("held_reason") for h in payload["held"]), "")
        check("and the page can name the rule",
              "must be missing" in payload["rule"], str(payload.get("rule")))

        # Widening brings them straight back, which is the point of deciding
        # this when the queue is read.
        async with session.put(
            f"{base}/api/settings",
            json={"changes": {"checker.queue_red": "any", "checker.queue_ops": "any"}},
        ) as r:
            await r.json()
        async with session.get(f"{base}/api/found") as r:
            payload = await r.json()
        check("widening the rule brings the rows back with no re-check",
              len(payload["found"]) == 2, str(len(payload["found"])))
    finally:
        await session.close()
        await runner.cleanup()


def main() -> int:
    main_predicate()
    asyncio.run(main_endpoint())
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
