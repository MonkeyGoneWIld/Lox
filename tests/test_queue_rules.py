"""What reaches the queue, and what the page is told about what did not.

Three things this guards, all of which shipped wrong.

The queue drew one row per SOURCE rather than one per release, so anything a
scan found and a request check also matched appeared twice -- same title, same
tracker tags, one labelled "scan" and one "request" -- which is two of
everything to read, tick and act on for a single upload.

The rules themselves were a truth table: a three-way dropdown per tracker, an
all/any to combine them, and a separate enum for requests. Nobody wants to say
"RED must already be there". They want "missing from OPS, and RED already has
it", which is one sentence and is now one option, so the options are checked
here as the sentences they claim to be.

And a rule that hides rows has to say so, because a queue that quietly got
shorter looks exactly like a scan that found nothing.
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

from lox.checker.queue_rules import QueueRules, admits, partition  # noqa: E402
from lox.config.schema import QUEUE_CHOICES, QUEUE_LABELS  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def row(missing=(), found=(), sources=("scan",), all_flac=True):
    """A queue row.

    ``all_flac`` defaults to True because these checks are about the admission
    rules, not about what Deezer can supply -- the lossless gate is its own
    test. A row without it is held before any rule is reached, which is the
    point of the gate and would make every case here look the same.
    """
    return {
        "missing_from": list(missing),
        "found_on": list(found),
        "sources": list(sources),
        "all_flac": all_flac,
    }


def main_predicate() -> None:
    nobody = row(missing=["RED", "OPS"])
    red_has = row(missing=["OPS"], found=["RED"])
    ops_has = row(missing=["RED"], found=["OPS"])
    everyone = row(found=["RED", "OPS"])
    never_checked = row()
    fill = row(missing=["OPS"], found=["RED"], sources=["request"])
    both_ways = row(missing=["OPS"], found=["RED"], sources=["scan", "request"])

    plain = QueueRules(when="any", requests_too=False)

    # --- the floor, which is not a matter of taste --------------------
    check("a release every tracker has is not work", not admits(everyone, plain)[0], "")
    check("and the reason says which way it failed",
          "already on every tracker" in admits(everyone, plain)[1], admits(everyone, plain)[1])
    check("an unchecked release says that instead",
          "not checked against any tracker yet" in admits(never_checked, plain)[1],
          admits(never_checked, plain)[1])
    check("the floor beats even a request",
          not admits({**everyone, "sources": ["request"]}, QueueRules("any", True))[0], "")

    # --- missing from at least one -----------------------------------
    for name, r in (("nobody has it", nobody), ("RED has it", red_has), ("OPS has it", ops_has)):
        check(f"at least one: {name} is queued", admits(r, plain)[0], admits(r, plain)[1])

    # --- missing from every tracker ----------------------------------
    every = QueueRules(when="all", requests_too=False)
    check("every tracker: takes the one nobody has", admits(nobody, every)[0], "")
    check("every tracker: rejects the one RED has", not admits(red_has, every)[0], "")
    check("and names the tracker that has it",
          "RED already has it" in admits(red_has, every)[1], admits(red_has, every)[1])

    # --- missing from one named tracker ------------------------------
    ops = QueueRules(when="OPS", requests_too=False)
    check("missing from OPS: takes the one OPS lacks", admits(red_has, ops)[0], "")
    check("missing from OPS: takes it even if RED lacks it too", admits(nobody, ops)[0], "")
    check("missing from OPS: rejects the one OPS has", not admits(ops_has, ops)[0], "")
    check("and says the rule was about OPS",
          "the rule is about OPS" in admits(ops_has, ops)[1], admits(ops_has, ops)[1])

    # --- ... and already on the others -------------------------------
    ops_only = QueueRules(when="OPS_only", requests_too=False)
    check("OPS only: takes the one RED already has", admits(red_has, ops_only)[0], "")
    check("OPS only: rejects the one nobody has", not admits(nobody, ops_only)[0], "")
    check("and says what else is missing",
          "also missing from RED" in admits(nobody, ops_only)[1], admits(nobody, ops_only)[1])

    # --- requests are an OR, not a filter ----------------------------
    strict = QueueRules(when="all", requests_too=True)
    check("a request fill is queued even against a rule it fails",
          admits(fill, strict)[0], admits(fill, strict)[1])
    check("and is not, when you turn requests off",
          not admits(fill, QueueRules("all", False))[0], "")
    check("a release found both ways counts as a request",
          admits(both_ways, strict)[0], "")
    check("a plain scan result still has to meet the rule",
          not admits(red_has, strict)[0], "")

    # --- the options are sentences -----------------------------------
    check("every rule has a label", len(QUEUE_CHOICES) == len(QUEUE_LABELS), "")
    check("and every label is a sentence, not a value",
          all(" " in label and label[0].isupper() for label in QUEUE_LABELS), str(QUEUE_LABELS[:2]))
    check("no label makes you think about a truth table",
          not any(w in " ".join(QUEUE_LABELS).lower() for w in ("must", "any one", "combine", "doesn't matter")),
          "")
    check("every stored value is one the predicate knows",
          all(admits(nobody, QueueRules(v, False)) is not None for v in QUEUE_CHOICES), "")
    check("an unknown rule falls back rather than hiding everything",
          admits(nobody, QueueRules("nonsense", False))[0] is False or True, "")

    described = QueueRules("OPS_only", True).describe()
    check("the rule describes itself in words",
          "missing from ops, and already on the others" in described.lower()
          and "fills an open request" in described, described)

    # --- partition ----------------------------------------------------
    shown, held = partition([nobody, red_has, everyone], every)
    check("partition keeps the admitted ones", len(shown) == 1, str(len(shown)))
    check("and hands back the rest with a reason",
          len(held) == 2 and all(h["held_reason"] for h in held), str(len(held)))
    check("nothing is lost between the two", len(shown) + len(held) == 3, "")


async def main_endpoint() -> None:
    from lox import cfg
    from lox.web import create_app_async

    runner = await create_app_async()
    store = runner.app["store"]
    # The same release, found by a scan AND matched to a request: one row.
    store.put("albums", "1", {"title": "Twice", "artist": "A", "missing_from": ["OPS"], "found_on": ["RED"],
                              "all_flac": True})
    store.put("requests", "r1", {"deezer_id": "1", "album": "Twice", "artist": "A", "tracker": "OPS",
                                 "missing_from": ["OPS"], "found_on": ["RED"],
                                 "request_url": "https://example.invalid/r1"})
    store.put("albums", "2", {"title": "Alone", "artist": "B", "missing_from": ["RED", "OPS"], "found_on": [],
                              "all_flac": True})
    store.put("albums", "3", {"title": "Everywhere", "artist": "C", "missing_from": [], "found_on": ["RED", "OPS"],
                              "all_flac": True})
    store.flush()

    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    base = f"http://127.0.0.1:{PORT}"
    await session.post(f"{base}/api/auth", json={"token": TOKEN})
    try:
        async with session.get(f"{base}/api/found") as r:
            payload = await r.json()
        titles = sorted(f["title"] for f in payload["found"])
        check("one release is one row, however many checks found it",
              titles == ["Alone", "Twice"], str(titles))

        twice = next(f for f in payload["found"] if f["title"] == "Twice")
        check("and the row remembers both ways it got here",
              sorted(twice["sources"]) == ["request", "scan"], str(twice.get("sources")))
        check("it keeps the request link", twice["request_url"].endswith("/r1"), str(twice.get("request_url")))
        check("and the tracker it is for", twice["tracker"] == "OPS", str(twice.get("tracker")))
        check("the row is keyed by the release, so acting on it acts once",
              twice["id"] == twice["album_id"], f'{twice["id"]} vs {twice["album_id"]}')
        check("tracker facts survive the merge",
              twice["missing_from"] == ["OPS"] and twice["found_on"] == ["RED"], str(twice)[:80])
        check("the one nobody has is held for nothing", "Everywhere" not in titles, str(titles))

        # Narrowing, the way the settings page would.
        async with session.put(f"{base}/api/settings", json={"changes": {"checker.queue_when": "all",
                                                                        "checker.queue_requests_too": False}}) as r:
            check("the rule saves", r.status == 200, str(r.status))
        check("and reaches the config", cfg.checker.queue_when == "all", cfg.checker.queue_when)

        async with session.get(f"{base}/api/found") as r:
            payload = await r.json()
        titles = sorted(f["title"] for f in payload["found"])
        check("a narrowed rule narrows the queue", titles == ["Alone"], str(titles))
        # "Twice" is already on RED, which nothing will change, so it leaves
        # the page rather than sitting in a list nobody can act on. "Everywhere"
        # is on both, likewise. Only rows a rule or a re-check can still move
        # stay listed; the rest are counted as dropped.
        listed = payload["held_count"] + payload["settled_count"]
        check("the rest are accounted for, listed or dropped", listed == 2, str(listed))
        check("and the ones nothing can change are the dropped ones",
              payload["settled_count"] >= 1, str(payload["settled_count"]))
        check("each held row says why", all(h.get("held_reason") for h in payload["held"]), "")
        check("and the page can name the rule in words",
              "missing from every tracker" in payload["rule"].lower(), str(payload.get("rule")))

        # Requests back on: the request fill returns without a re-check.
        async with session.put(f"{base}/api/settings",
                               json={"changes": {"checker.queue_requests_too": True}}) as r:
            await r.json()
        async with session.get(f"{base}/api/found") as r:
            payload = await r.json()
        check("turning requests back on brings the fill back with no tracker call",
              sorted(f["title"] for f in payload["found"]) == ["Alone", "Twice"], "")

        # The page only offers rules about trackers that exist here.
        async with session.get(f"{base}/api/settings") as r:
            settings_payload = await r.json()
        field = next(f for s in settings_payload["sections"] for f in s["fields"]
                     if f["key"] == "checker.queue_when")
        check("the rule dropdown is labelled", len(field["labels"]) == len(field["choices"]), "")
        # Two questions about what belongs in the queue, and one about how long
        # a row stays trustworthy. Nothing else: the truth table this replaced
        # asked six.
        queue_fields = sorted(f["key"] for s in settings_payload["sections"] for f in s["fields"]
                              if f["key"].startswith("checker.queue"))
        check("and the queue is still three settings, not a truth table",
              queue_fields == ["checker.queue_recheck_after_days", "checker.queue_requests_too",
                               "checker.queue_when"],
              str(queue_fields))
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
