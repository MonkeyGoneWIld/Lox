"""Found stops offering what has been uploaded, and the bar only goes forwards.

A release that has been uploaded is not one that is missing, so leaving it on
the Found list made that list less true after every successful upload. And the
download bar was a percentage of total bytes, but a track's size is only known
once its download starts -- so the total grew as tracks began and the bar slid
backwards, 7/10 becoming 7/14.
"""

import asyncio
import os
import sys

import aiohttp

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_foundprog")
PORT = 5104
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

from lox.deezer.download import DownloadJob, TrackDownload  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def track(number: int, size: int = 0, downloaded: int = 0, status: str = "queued") -> TrackDownload:
    return TrackDownload(
        id=str(number), title=f"Track {number}", artist="Someone",
        number=number, disc=1, status=status, size=size, downloaded=downloaded,
    )


async def main() -> int:
    # --- the bar never goes backwards --------------------------------
    # Ten tracks, and the sizes arrive one at a time as each starts. The
    # byte-based total grew every time one did.
    job = DownloadJob(id="j", album_id="1", title="Album", artist="Someone",
                      tracks=[track(i) for i in range(1, 11)])
    seen = []
    for index, t in enumerate(job.tracks):
        # A track starts: its size becomes known, then it downloads and finishes.
        t.size = 5_000_000 + index * 1_000_000
        seen.append(job.percent)
        t.downloaded = t.size // 2
        seen.append(job.percent)
        t.downloaded, t.status = t.size, "done"
        seen.append(job.percent)

    monotonic = all(b >= a for a, b in zip(seen, seen[1:], strict=False))
    check("the percentage only ever goes up", monotonic,
          " ".join(f"{p:.0f}" for p in seen) if not monotonic else "")
    check("it starts at zero", seen[0] == 0.0, str(seen[0]))
    check("and ends at a hundred", seen[-1] == 100.0, str(seen[-1]))

    half = DownloadJob(id="h", album_id="1", title="A", artist="B",
                       tracks=[track(1, status="done"), track(2)])
    check("half the tracks done is half the bar", half.percent == 50.0, str(half.percent))

    empty = DownloadJob(id="e", album_id="1", title="A", artist="B", tracks=[], status="done")
    check("a job with no tracks does not divide by zero", empty.percent == 100.0, str(empty.percent))

    # --- Found drops what has been uploaded, and what you dismiss -----
    # The server's own store, not a second one pointed at the same directory:
    # each instance caches, so two of them would overwrite each other's writes
    # and the test would be measuring its own copy rather than the app's.
    from lox.web import create_app_async  # noqa: PLC0415

    runner = await create_app_async()
    store = runner.app["store"]

    store.put("albums", "111", {"title": "Kept", "artist": "A", "missing_from": ["RED"],
                                "all_flac": True})
    store.put("albums", "222", {"title": "Uploaded", "artist": "B", "missing_from": ["RED"],
                                "all_flac": True})
    store.put("albums", "333", {"title": "Dismissed", "artist": "C", "missing_from": ["RED"],
                                "all_flac": True})
    store.put("albums", "444", {"title": "Blacklisted", "artist": "D", "missing_from": ["RED"],
                                "all_flac": True})
    store.flush()

    from lox.web.api import _mark_uploaded  # noqa: PLC0415

    _mark_uploaded(store, "222", "", ["RED"])
    check("an uploaded release is stamped", store.get("albums", "222").get("uploaded_at"), "")
    check("and says where it went", store.get("albums", "222").get("uploaded_to") == ["RED"], "")

    _mark_uploaded(store, "", "Someone Else - A Different Record (2026) [WEB FLAC]", ["RED"])
    check("a folder that matches nothing stamps nothing",
          not store.get("albums", "111").get("uploaded_at"), str(store.get("albums", "111")))

    store.put("albums", "555", {"title": "Sammaouny", "artist": "Mohamed Hamaki",
                                "missing_from": ["RED"], "all_flac": True}, flush=True)
    _mark_uploaded(store, "", "Mohamed Hamaki - Sammaouny (2026) [WEB FLAC]", ["RED"])
    check("but a folder that names the release does stamp it",
          store.get("albums", "555").get("uploaded_at"), str(store.get("albums", "555")))

    # --- a check writes its answer where Found will read it ---------------
    #
    # A request row is keyed by tracker and request id; an album check writes
    # under the Deezer album id. Nothing joined the two, so checking a release
    # from its own page, finding it on every tracker, and returning to Found
    # still showed it as worth uploading -- the answer had been recorded
    # somewhere that row does not read.
    store.put("requests", "OPS:80001", {
        "status": "fillable", "tracker": "OPS", "deezer_id": "999",
        "album": "Eden Sauvage", "artist": "Los Eclipses",
        "request_url": "https://example.invalid/requests.php?action=view&id=80001",
    }, flush=True)

    class Verdict:
        def __init__(self, found, missing):
            self.found_on = found
            self.missing_from = missing

    scanner = runner.app["scanner"]
    scanner._mirror_to_requests("999", Verdict(["RED"], ["OPS"]))  # noqa: SLF001
    row = store.get("requests", "OPS:80001")
    check("a partial find is recorded on the request",
          row.get("found_on") == ["RED"] and row.get("missing_from") == ["OPS"], str(row.get("found_on")))
    check("and it is still worth uploading somewhere",
          row.get("already_on_tracker") is False, str(row.get("already_on_tracker")))

    scanner._mirror_to_requests("999", Verdict(["RED", "OPS"], []))  # noqa: SLF001
    row = store.get("requests", "OPS:80001")
    check("found on every tracker closes it",
          row.get("already_on_tracker") is True, str(row.get("already_on_tracker")))
    check("with both trackers named", row.get("found_on") == ["RED", "OPS"], str(row.get("found_on")))

    scanner._mirror_to_requests("no-such-album", Verdict([], ["RED"]))  # noqa: SLF001
    check("an album nothing requests changes nothing",
          store.get("requests", "OPS:80001").get("already_on_tracker") is True, "")

    # aiohttp, not urllib: a blocking request from inside the loop the server
    # runs on waits for a reply that cannot be produced until it yields.
    # unsafe=True because aiohttp's cookie jar discards cookies set by an
    # IP-address host, and the session cookie is how this app authenticates.
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    # The app authenticates with a cookie set by /api/auth, not a bearer header.
    async with session.post(f"http://127.0.0.1:{PORT}/api/auth", json={"token": TOKEN}) as r:
        check("the test session is signed in", r.status == 200, str(r.status))

    async def call(path, body=None):
        url = f"http://127.0.0.1:{PORT}{path}"
        if body is None:
            async with session.get(url) as r:
                return await r.json()
        async with session.post(url, json=body) as r:
            return await r.json()

    try:
        listed = {row["id"] for row in (await call("/api/found"))["found"]}
        check("an uploaded release is off the list", "222" not in listed, str(sorted(listed)))
        check("a request found on every tracker is off it too",
              "999" not in listed, str(sorted(listed)))

        store.put("requests", "OPS:80002", {
            "status": "fillable", "tracker": "OPS", "deezer_id": "998",
            "album": "Still Missing", "artist": "Someone",
            "found_on": ["RED"], "missing_from": ["OPS"], "already_on_tracker": False,
            "all_flac": True,
        }, flush=True)
        rows = (await call("/api/found"))["found"]
        still = next((r for r in rows if r["album_id"] == "998"), None)
        check("one still missing somewhere stays on the list", still is not None, str(sorted(listed)))
        check("and says which tracker has it",
              still and still["found_on"] == ["RED"], str(still and still.get("found_on")))
        check("and which one does not",
              still and still["missing_from"] == ["OPS"], str(still and still.get("missing_from")))
        check("and when it was last checked", bool(still and still.get("checked_at")), "")
        check("the others are still on it", {"111", "333", "444"} <= listed, str(sorted(listed)))

        await call("/api/found/dismiss", {"ids": ["333"], "blacklist": False})
        check("a plain removal forgets the check result",
              store.get("albums", "333") is None, str(store.get("albums", "333")))

        await call("/api/found/dismiss", {"ids": ["444"], "blacklist": True})
        check("a blacklist remembers it instead",
              store.get("albums", "444") is not None and store.get("dismissed", "444"), "")

        after = await call("/api/found")
        listed = {row["id"] for row in after["found"]}
        check("both are off the list", not ({"333", "444"} & listed), str(sorted(listed)))
        check("and the blacklist is counted", after["blacklisted"] == 1, str(after["blacklisted"]))

        # A rescan puts a removed release back; a blacklisted one stays off.
        store.put("albums", "333", {"title": "Dismissed", "artist": "C", "missing_from": ["RED"],
                                    "all_flac": True}, flush=True)
        listed = {row["id"] for row in (await call("/api/found"))["found"]}
        check("a rescan can bring a removed release back", "333" in listed, str(sorted(listed)))
        check("but not a blacklisted one", "444" not in listed, str(sorted(listed)))

        restored = await call("/api/found/restore", {"ids": ["444"]})
        check("the blacklist can be cleared", restored["restored"] == 1, str(restored))
        listed = {row["id"] for row in (await call("/api/found"))["found"]}
        check("and the release comes back", "444" in listed, str(sorted(listed)))
    finally:
        await session.close()
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
