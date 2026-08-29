"""Start the real lox web app and exercise it over HTTP.

Point the LOX_* bootstrap at throwaway directories and run it directly:

    python tests/smoke_test.py

See the Checks workflow for the full set of variables it expects.

It writes to the settings file it is pointed at, so do not aim it at a real
deployment.

This is the end-to-end check that has been missing: the aiohttp app is created
for real, the auth middleware runs, and the settings/debug endpoints are hit the
way the browser hits them.
"""

import asyncio
import json
import sys

import aiohttp

BASE = "http://127.0.0.1:5015"
TOKEN = "0123456789abcdef0123456789abcdef"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def _rows(kind: str, count: int) -> list[dict]:
    """Deezer-shaped search rows, for stubbing the client."""
    return [
        {
            "id": i,
            "title": f"{kind} {i}",
            "name": f"{kind} {i}",
            "artist": {"name": "Taylor Swift"},
            "album": {"id": 5, "cover_medium": ""},
        }
        for i in range(count)
    ]


async def main() -> int:
    from lox.database import run_migrations
    from lox.web import create_app_async

    if run_migrations():
        print("applied pending database migrations")

    runner = await create_app_async()
    print("server started\n")

    try:
        async with aiohttp.ClientSession() as s:
            # --- auth gate ---------------------------------------------
            async with s.get(f"{BASE}/api/status") as r:
                check("unauthenticated /api/status -> 401", r.status == 401, f"got {r.status}")

            async with s.get(f"{BASE}/", allow_redirects=False) as r:
                check("unauthenticated / -> redirect to /login",
                      r.status == 302 and "/login" in r.headers.get("Location", ""),
                      f"got {r.status} {r.headers.get('Location', '')}")

            async with s.get(f"{BASE}/login") as r:
                body = await r.text()
                check("/login serves the sign-in page", r.status == 200 and "Password" in body, body[:80])

            async with s.post(f"{BASE}/api/auth", json={"token": "wrong"}) as r:
                check("wrong token rejected", r.status == 401, f"got {r.status}")

            async with s.post(f"{BASE}/api/auth", json={"token": TOKEN}) as r:
                check("correct token accepted + cookie set",
                      r.status == 200 and "lox_token" in r.headers.get("Set-Cookie", ""))

            # --- authenticated surface ---------------------------------
            h = {"X-Auth-Token": TOKEN}

            async with s.get(f"{BASE}/api/status", headers=h) as r:
                data = await r.json()
                check("/api/status", r.status == 200,
                      f"deezer_configured={data['deezer']['configured']} trackers={len(data['trackers'])}")

            async with s.get(f"{BASE}/api/settings", headers=h) as r:
                data = await r.json()
                fields = sum(len(sec["fields"]) for sec in data["sections"])
                check("/api/settings schema", r.status == 200 and fields > 60,
                      f"{len(data['sections'])} sections, {fields} fields")
                # Only what the server needs before it can serve the page that
                # would edit it. Directories are deliberately not in here: a
                # wrong path has to be fixable from the UI, not just compose.
                check("bootstrap keys cover bind address and auth",
                      set(data["bootstrap"]) == {"upload.web_interface.host",
                                                 "upload.web_interface.port",
                                                 "upload.web_interface.auth_token"},
                      str(sorted(data["bootstrap"])))
                editable = {f["key"] for sec in data["sections"] for f in sec["fields"]}
                check("download directory is editable in the UI",
                      "directory.download_directory" in editable)

            # --- a real settings write, applied live --------------------
            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"checker.tracker_budget": 150,
                                               "upload.dry_run": True}}) as r:
                data = await r.json()
                check("PUT /api/settings", r.status == 200, f"saved={data.get('saved')}")

            import lox
            check("setting applied to live config", lox.cfg.checker.tracker_budget == 150,
                  f"tracker_budget={lox.cfg.checker.tracker_budget}")
            check("dry_run applied", lox.cfg.upload.dry_run is True)

            # The uploads page carries its own toggles for these two. They have
            # to be the same setting, not a copy, so the page reads them back
            # from here rather than remembering what it last sent.
            async with s.get(f"{BASE}/api/status", headers=h) as r:
                data = await r.json()
            check("status reports the upload switches",
                  data.get("upload", {}).get("dry_run") is True, str(data.get("upload")))

            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"upload.dry_run": False, "upload.yes_all": True}}) as r:
                check("upload switches are writable", r.status == 200)
            async with s.get(f"{BASE}/api/status", headers=h) as r:
                data = await r.json()
            check("a change on one page is visible to the other",
                  data["upload"] == {"dry_run": False, "yes_all": True}, str(data.get("upload")))

            # --- search returns every kind at once ----------------------
            # Stubbed at the Deezer client so this runs without an ARL. The
            # point is the response shape the page renders sections from.
            gw = runner.app["gw"]
            gw.search_albums = lambda q, limit=30: _rows("album", 3)
            gw.search_tracks = lambda q, limit=30: _rows("track", 2)
            gw.search_artists = lambda q, limit=30: _rows("artist", 1)

            async with s.get(f"{BASE}/api/search?q=taylor", headers=h) as r:
                data = await r.json()
            check("search defaults to every kind", data.get("type") == "all", str(data.get("type")))
            check("each kind comes back in its own section",
                  {k: len(v) for k, v in data.get("sections", {}).items()}
                  == {"album": 3, "track": 2, "artist": 1},
                  str({k: len(v) for k, v in data.get("sections", {}).items()}))
            check("a flat list is still there for callers that want one",
                  len(data.get("results", [])) == 6, str(len(data.get("results", []))))

            async with s.get(f"{BASE}/api/search?q=taylor&type=artist", headers=h) as r:
                data = await r.json()
            check("filtering to one kind returns only that kind",
                  list(data.get("sections", {})) == ["artist"] and len(data["results"]) == 1,
                  str(list(data.get("sections", {}))))

            # --- a saved album check is readable for free ----------------
            # Checking costs tracker budget, so the answer is kept and shown
            # again on the album page without asking a tracker anything.
            scanner = runner.app["scanner"]

            async with s.get(f"{BASE}/api/album/999/check", headers=h) as r:
                data = await r.json()
            check("an unchecked album has no stored result",
                  r.status == 200 and data.get("check") is None, str(data))

            scanner.store.put(
                "albums",
                "999",
                {
                    "status": "exists_red",
                    "title": "Bedtime Stories",
                    "artist": "Madonna",
                    "found_on": ["RED"],
                    "missing_from": [],
                    "verdicts": [{"tracker": "RED", "status": "found", "calls_used": 2,
                                  "match": {"name": "Bedtime Stories", "artist": "Madonna",
                                            "year": 1994, "url": "https://example.invalid/1"},
                                  "inspected": [], "queries": []}],
                },
                flush=True,
            )

            async with s.get(f"{BASE}/api/album/999/check", headers=h) as r:
                data = await r.json()
            stored = data.get("check") or {}
            check("a stored result comes back", stored.get("found_on") == ["RED"], str(stored.get("found_on")))
            check("the verdicts come back with it, not just the summary",
                  len(stored.get("verdicts") or []) == 1
                  and stored["verdicts"][0]["match"]["url"] == "https://example.invalid/1",
                  str(stored.get("verdicts")))
            check("it says when it was checked", isinstance(stored.get("checked_at"), int | float),
                  str(stored.get("checked_at")))

            # --- cancelling work that is already under way ---------------
            import asyncio

            jobs = runner.app["jobs"]

            started = asyncio.Event()

            async def slow(job):
                started.set()
                await asyncio.sleep(30)

            running = jobs.spawn("test", "Something slow", slow)
            await asyncio.wait_for(started.wait(), timeout=2)

            async with s.post(f"{BASE}/api/jobs/{running.id}/cancel", headers=h) as r:
                data = await r.json()
                check("a running job can be cancelled", r.status == 200 and data.get("cancelled") is True, str(data))
            await asyncio.sleep(0.1)
            check("it stops and says so", running.status == "cancelled", running.status)

            async with s.post(f"{BASE}/api/jobs/{running.id}/cancel", headers=h) as r:
                data = await r.json()
                check("cancelling a stopped job is a no-op", data.get("cancelled") is False, str(data))

            # A download already in flight, not merely queued.
            downloader = runner.app["downloader"]
            live = asyncio.Event()

            async def never_ends(job):
                job.status = "running"
                live.set()
                await asyncio.sleep(30)

            downloader._run_job = never_ends  # noqa: SLF001 - standing in for a real transfer
            await downloader.start()
            job = await downloader.enqueue("1")
            await asyncio.wait_for(live.wait(), timeout=3)
            check("the download is running", job.status == "running", job.status)
            check("cancelling a running download reports success", downloader.cancel(job.id) is True)
            await asyncio.sleep(0.2)
            check("the running download stops", job.status == "cancelled", job.status)

            # --- deleting a release folder ------------------------------
            import os

            import lox
            root = lox.cfg.directory.download_directory
            victim = os.path.join(root, "Deletable - Release (2026) [WEB FLAC]")
            os.makedirs(victim, exist_ok=True)
            with open(os.path.join(victim, "01.flac"), "wb") as f:
                f.write(b"x")

            # The download directory itself must never be removable in one call.
            async with s.post(f"{BASE}/api/folders/delete", headers=h, json={"folder": root}) as r:
                body = await r.json()
                check("refuses to delete the download directory itself",
                      r.status == 400 and "itself" in body.get("error", ""), str(body))

            # Nor anything outside the directories lox manages.
            async with s.post(f"{BASE}/api/folders/delete", headers=h,
                              json={"folder": os.path.dirname(root)}) as r:
                check("refuses a path outside the managed roots", r.status == 400, f"got {r.status}")

            async with s.post(f"{BASE}/api/folders/delete", headers=h, json={"folder": victim}) as r:
                check("deletes a release folder", r.status == 200, f"got {r.status}")
            check("the files are gone", not os.path.exists(victim))

            # --- a download that failed partway -------------------------
            # The folder is created before the first track is fetched, so a
            # download that fails halfway leaves a real folder behind -- nine
            # of ten tracks, still named [WEB FLAC]. Delete was shown only for
            # status "done", so that folder had no way off the page: Cancel had
            # gone, Delete never appeared, and Clear finished drops the row and
            # leaves the files.
            broken = os.path.join(root, "Halfway - Release (2026) [WEB FLAC]")
            os.makedirs(broken, exist_ok=True)
            with open(os.path.join(broken, "01.flac"), "wb") as f:
                f.write(b"x")
            job.status = "failed"
            job.error = "1 of 10 track(s) failed"
            job.folder = broken
            check("a failed download still knows its folder",
                  downloader.jobs[job.id].folder == broken, str(job.folder))

            async with s.post(f"{BASE}/api/folders/delete", headers=h, json={"folder": broken}) as r:
                check("a half-finished folder can be deleted", r.status == 200, f"got {r.status}")
            check("and the files really go", not os.path.exists(broken))
            # Otherwise the row stays, offering Delete for a path that has gone.
            check("the download it came from is forgotten with it",
                  job.id not in downloader.jobs, str(list(downloader.jobs)))

            async with s.get(f"{BASE}/api/settings", headers=h) as r:
                data = await r.json()
                check("setting persisted and read back",
                      data["values"]["checker.tracker_budget"] == 150)

            # --- validation is enforced server-side ---------------------
            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"linking.method": "teleport"}}) as r:
                data = await r.json()
                check("invalid choice rejected", r.status == 400, data.get("error", "")[:60])

            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"upload.web_interface.port": 9999}}) as r:
                check("bootstrap key refused", r.status == 400)

            # --- sizes are stored as bytes but spoken about in units ----
            async with s.get(f"{BASE}/api/settings", headers=h) as r:
                data = await r.json()
                kinds = {f["key"]: f["kind"] for sec in data["sections"] for f in sec["fields"]}
                check("log sizes are edited as a number and a unit",
                      kinds.get("logging.max_file_bytes") == "bytes"
                      and kinds.get("logging.max_total_bytes") == "bytes",
                      str(kinds.get("logging.max_file_bytes")))

            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"logging.max_file_bytes": 512 * 1024}}) as r:
                data = await r.json()
                check("a size saves as a plain byte count", r.status == 200, str(data.get("error", "")))
            check("size applied to live config", lox.cfg.logging.max_file_bytes == 524288,
                  str(lox.cfg.logging.max_file_bytes))

            # The bound is a byte count in the schema; the complaint should not be.
            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"logging.max_file_bytes": 1024}}) as r:
                data = await r.json()
                check("an out-of-range size is refused in the same units it is entered",
                      r.status == 400 and "64 KB" in data.get("error", ""), data.get("error", ""))

            async with s.put(f"{BASE}/api/settings", headers=h,
                             json={"changes": {"logging.max_file_bytes": 8 * 1024 * 1024}}) as r:
                check("size restored", r.status == 200)

            # --- tests that need no network -----------------------------
            async with s.post(f"{BASE}/api/settings/test/paths", headers=h) as r:
                data = await r.json()
                check("paths test runs", r.status == 200, f"ok={data['ok']} {data['message'][:60]}")

            async with s.post(f"{BASE}/api/settings/test/deezer", headers=h) as r:
                data = await r.json()
                check("deezer test reports no ARL", r.status == 200 and data["ok"] is False,
                      data["message"][:50])

            # --- debug ---------------------------------------------------
            async with s.get(f"{BASE}/api/debug", headers=h) as r:
                data = await r.json()
                creds = data["diagnostics"]["credentials"]
                leaked = TOKEN in json.dumps(data)
                check("/api/debug", r.status == 200, f"auth_token={creds['auth_token']}")
                check("diagnostics leak no secrets", not leaked)

            # --- other read-only surfaces --------------------------------
            for path in ("/api/trackers", "/api/watchlists", "/api/downloads", "/api/jobs", "/api/folders"):
                async with s.get(f"{BASE}{path}", headers=h) as r:
                    check(f"GET {path}", r.status == 200, f"got {r.status}")

            async with s.get(f"{BASE}/", headers=h) as r:
                body = await r.text()
                check("authenticated / serves the app shell",
                      r.status == 200 and 'id="sidebar"' in body)

            # Every screen has an address, so reloading on one keeps you on
            # it. The app never changed the address bar, so a reload always
            # landed on Search and the browser's own buttons did nothing.
            from lox.web import APP_PATHS

            for path in APP_PATHS:
                async with s.get(f"{BASE}{path}", headers=h) as r:
                    body = await r.text()
                    check(f"{path} serves the app shell",
                          r.status == 200 and 'id="sidebar"' in body, f"got {r.status}")

            # The four that name something rather than somewhere: a release, an
            # artist, a Deezer channel and one request on one tracker.
            for path in (
                "/album/1000982941",
                "/artist/12345",
                "/browse/channel/rap-fr",
                "/requests/red/80755",
            ):
                async with s.get(f"{BASE}{path}", headers=h) as r:
                    check(f"{path} serves the app shell", r.status == 200, f"got {r.status}")

            # A place on the settings page is a place you can be sent to. The
            # names come from the schema, so a section added there gets an
            # address without anybody remembering to add one.
            for path in ("/settings/accounts", "/settings/torrent", "/settings/users"):
                check(f"{path} is an address", path in APP_PATHS, "not in APP_PATHS")

            # Listed rather than a catch-all: a typo is still a typo, and an
            # /api path that does not exist must not come back as HTML.
            for path in ("/nonsense", "/settings/nonsense", "/browse/nonsense"):
                async with s.get(f"{BASE}{path}", headers=h) as r:
                    check(f"{path} is still a 404", r.status == 404, f"got {r.status}")
            async with s.get(f"{BASE}/api/nonsense", headers=h) as r:
                check("and an unknown API path does not serve a page",
                      r.status == 404, f"got {r.status}")

            async with s.get(f"{BASE}/static/scripts/app.js") as r:
                check("static assets served", r.status == 200)
    finally:
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
