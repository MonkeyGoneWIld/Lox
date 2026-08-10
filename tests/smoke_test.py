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
                check("/login serves the sign-in page", r.status == 200 and "Access token" in body)

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
