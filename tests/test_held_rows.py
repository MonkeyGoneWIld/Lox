"""The queue page lists releases, not the scanner's notes to itself.

The albums collection is two things wearing one name. Some entries are releases
that were checked against a tracker. The rest are the scanner's memory of
albums it gave up on -- Deezer answered DATA_ERROR, or returned no info, or the
track count disagreed -- kept so a rescan does not try them again. They have no
title, no artist and no verdict.

Both were being listed on the queue page. On a real install that was 24 rows
out of 37: an em dash where the name goes, "not checked on any tracker" in the
trackers column, and "not checked against any tracker yet" as the explanation,
under a heading that said "Held back by your queue rules". None of it was held
by a rule, none of it could be acted on, and no setting on the settings page
would have brought any of it back.

What this covers:

  * a scan record that never reached a tracker is not a queue row
  * one that did is, whichever way the verdict went
  * the rows that are out are grouped by what is actually keeping them out,
    with the queue rules as one group among several rather than the label on
    all of them
  * removing a release removes all of it -- the requests collection is keyed
    by tracker and request id, so deleting by album id used to delete the scan
    half and leave the request half behind, and the row came straight back
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

BASE = os.path.join(ROOT, "_heldrows")
os.environ.update({
    "LOX_HOST": "127.0.0.1",
    "LOX_PORT": "5020",
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


async def run() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from lox.web import create_app_async

    runner = await create_app_async()
    app = runner.app
    store = app["store"]

    # --- the scanner's notes to itself, verbatim from a real install ------
    store.put("albums", "1006449551", {
        "status": "flac_check_failed", "source": "nu_rock",
        "error": "gw-light error for deezer.pageAlbum: {'DATA_ERROR': 'album::getData'}",
    })
    store.put("albums", "2000000001", {"status": "skipped_missing_info", "source": "nu_rock"})
    store.put("albums", "2000000002", {
        "status": "skipped_track_count_mismatch", "title": "Counted Wrong", "artist": "X",
        "expected": 12, "actual": 11, "source": "nu_rock",
    })
    store.put("albums", "2000000003", {"status": "deezer_info_failed", "error": "boom", "source": "nu_rock"})

    # --- releases a tracker actually answered about -----------------------
    store.put("albums", "3000000001", {
        "status": "missing_red", "title": "Worth Uploading", "artist": "A",
        "missing_from": ["RED"], "found_on": [], "all_flac": True,
    })
    store.put("albums", "3000000002", {
        "status": "exists_both", "title": "Everyone Has It", "artist": "B",
        "missing_from": [], "found_on": ["RED", "OPS"], "all_flac": True,
    })
    store.put("albums", "3000000003", {
        "status": "missing_ops", "title": "Never Looked At Deezer", "artist": "C",
        "missing_from": ["OPS"], "found_on": [],
    })
    store.put("albums", "3000000004", {
        "status": "missing_ops", "title": "Lossy Only", "artist": "D",
        "missing_from": ["OPS"], "found_on": [], "all_flac": False,
        "flac_count": 2, "deezer_tracks": 10,
    })
    # A release both halves know about, which is the one removal got wrong.
    store.put("albums", "4000000001", {
        "status": "missing_ops", "title": "Both Halves", "artist": "E",
        "missing_from": ["OPS"], "found_on": [], "all_flac": True,
    }, flush=True)
    store.put("requests", "OPS:80755", {
        "status": "fillable", "deezer_id": "4000000001", "album": "Both Halves", "artist": "E",
        "tracker": "OPS", "missing_from": ["OPS"], "found_on": [], "all_flac": True,
        "request_url": "https://orpheus.network/requests.php?action=view&id=80755",
    }, flush=True)

    client = TestClient(TestServer(app))
    await client.start_server()
    headers = {"X-Auth-Token": TOKEN}
    # Starting the test server runs the app's startup again, which builds a
    # second store and rebinds app["store"]. The seeding above reached this one
    # through the files it flushed; anything read back has to come from the
    # store the handlers are actually using, not the one seeded through.
    store = app["store"]

    async def found() -> dict:
        response = await client.get("/api/found", headers=headers)
        return await response.json()

    data = await found()
    listed = {r["album_id"] for r in data["found"] + data["held"]}

    # --- the notes stay out -----------------------------------------------
    for album_id, why in (
        ("1006449551", "Deezer refused the album"),
        ("2000000001", "Deezer returned no info"),
        ("2000000003", "the Deezer lookup failed"),
    ):
        check(f"a record where {why} is not a queue row", album_id not in listed, album_id)
    check("nor is one dropped before any tracker was asked",
          "2000000002" not in listed, "")
    check("so nothing on the page is nameless",
          all(r.get("title") or r.get("artist") for r in data["found"] + data["held"]),
          str([r["album_id"] for r in data["found"] + data["held"]
               if not (r.get("title") or r.get("artist"))]))

    # --- the ones a tracker answered about stay in ------------------------
    check("a release missing from a tracker is a queue row", "3000000001" in listed, "")
    check("and it is in the queue",
          "3000000001" in {r["album_id"] for r in data["found"]}, "")

    # A release every tracker already has is not waiting for anything: no
    # setting admits it and no re-check changes the answer. Listing it
    # produced a page of things nobody could act on, and a re-check put every
    # one of them straight back. It is counted and dropped.
    check("one every tracker already has is not listed at all",
          "3000000002" not in listed, "")
    check("but it is counted rather than silently missing",
          data["settled_count"] >= 1, str(data["settled_count"]))

    # --- grouped by what is really keeping them out -----------------------
    groups = {g["key"]: g for g in data["held_groups"]}
    dropped = {g["key"]: g for g in data["settled_groups"]}

    # What is still listed is what can still move.
    check("one nobody has checked against Deezer is still listed",
          groups.get("unproven", {}).get("count") == 1, str(sorted(groups)))
    check("and says a re-check is what fixes it",
          groups.get("unproven", {}).get("fix") == "recheck", "")
    check("the queue rules are one group among these, not the label on all of them",
          "rules" not in groups, str(sorted(groups)))

    # What is dropped is what cannot.
    check("a release every tracker has is dropped",
          dropped.get("nothing_to_do", {}).get("count") == 1, str(sorted(dropped)))
    check("and so is one with no lossless source",
          dropped.get("lossy", {}).get("count") == 1, str(sorted(dropped)))

    check("every listed row is counted exactly once",
          sum(g["count"] for g in data["held_groups"]) == data["held_count"],
          f'{sum(g["count"] for g in data["held_groups"])} vs {data["held_count"]}')
    check("and so is every dropped one",
          sum(g["count"] for g in data["settled_groups"]) == data["settled_count"],
          f'{sum(g["count"] for g in data["settled_groups"])} vs {data["settled_count"]}')

    # --- removing a release removes all of it -----------------------------
    both = next(r for r in data["found"] + data["held"] if r["album_id"] == "4000000001")  # noqa: E501
    check("a release known to a scan and a request is one row",
          sorted(both.get("sources", [])) == ["request", "scan"], str(both.get("sources")))

    response = await client.post("/api/found/dismiss", headers=headers,
                                 json={"ids": ["4000000001"], "blacklist": False})
    check("removing it is accepted", (await response.json()).get("dismissed") == 1, "")

    after = await found()
    still = [r for r in after["found"] + after["held"] if r["album_id"] == "4000000001"]
    check("and it does not come back as the half that was keyed differently",
          not still, str([r.get("sources") for r in still]))
    check("the request behind it is gone too",
          store.get("requests", "OPS:80755") is None, "")

    # Blacklisting reaches both halves as well, by a different route.
    store.put("albums", "5000000001", {
        "status": "missing_ops", "title": "Blacklist Me", "artist": "F",
        "missing_from": ["OPS"], "found_on": [], "all_flac": True,
    }, flush=True)
    store.put("requests", "OPS:80756", {
        "status": "fillable", "deezer_id": "5000000001", "album": "Blacklist Me", "artist": "F",
        "tracker": "OPS", "missing_from": ["OPS"], "found_on": [], "all_flac": True,
    }, flush=True)
    await client.post("/api/found/dismiss", headers=headers,
                      json={"ids": ["5000000001"], "blacklist": True})
    after = await found()
    check("a blacklisted release is gone from both halves",
          not [r for r in after["found"] + after["held"] if r["album_id"] == "5000000001"], "")
    check("but is remembered, so a rescan does not list it again",
          store.get("dismissed", "5000000001") is not None, "")

    await client.close()


def store_checks() -> None:
    """When a release was first seen survives every later write."""
    import time

    from lox.checker.store import CheckerStore

    store = CheckerStore(os.path.join(BASE, "state-firstseen"))
    store.put("albums", "a", {"title": "First"}, flush=True)
    first = store.get("albums", "a")["first_seen"]
    check("a new entry records when it was first seen", first > 0, str(first))

    time.sleep(0.01)
    store.put("albums", "a", {"title": "First", "status": "missing_red"}, flush=True)
    again = store.get("albums", "a")
    check("a later write keeps it", again["first_seen"] == first, str(again["first_seen"]))
    check("while the checked time moves on", again["checked_at"] > first, "")


def page_checks() -> None:
    """What did not reach the queue is a count and a reason, not a list.

    It used to be a table you could open under the queue, and everything in it
    was something nobody wanted: releases every tracker already has, releases
    Deezer cannot supply, releases a rule the operator set deliberately keeps
    out. Offering all of that as work to get through made the queue look like
    it was hiding things, and re-checking could not clear it -- the answer came
    back the same and the row went straight back.

    The count and the reasons stay, because a list that quietly got shorter is
    worse than one that says why. The table does not.
    """
    with open(os.path.join(os.path.dirname(ROOT), "lox", "web", "static", "scripts", "app.js"),
              encoding="utf-8") as handle:
        js = handle.read()

    check("the heading no longer blames the queue rules for all of it",
          "Held back by your queue rules (${" not in js, "")
    check("the excluded rows are not a second list under the queue",
          "Excluded from the queue" not in js and "function heldTable" not in js, "")
    check("nor a set of actions on rows nobody wanted",
          "function heldAct" not in js and "function heldPick" not in js, "")
    # Read against the script with its comments taken out: why the list was
    # removed is worth explaining to whoever maintains this, and worth not
    # saying to somebody looking at their queue.
    import re  # noqa: PLC0415

    spoken = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    spoken = re.sub(r"^\s*//.*$", "", spoken, flags=re.MULTILINE)
    check("the queue does not report what it kept out either",
          "excluded" not in spoken and "state.foundRule" not in spoken, "")
    check("a re-check is still available from the queue itself",
          "function recheckReleases" in js and "function recheckFound" in js, "")


def main() -> int:
    asyncio.run(run())
    store_checks()
    page_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
