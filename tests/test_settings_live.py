"""A saved setting has to reach the running process, not just the page.

The settings page was honest and useless: it wrote settings.toml, mutated the
config object, and read the new value straight back, so every change looked
like it had taken. Nothing that actually does the work was told.

Two things were copied out of the config once, at startup, and never read
again:

  * the tracker budget, window, delays and breaker thresholds, which the
    gateway snapshots into per-tracker state -- so raising a budget did
    nothing, and the sidebar's calls-left readout kept quoting the old ceiling
  * the tracker API clients, which copy their API key and session cookie when
    they are built and are then cached on the gateway forever -- so rotating a
    key left every call still sending the old one, with no sign on the page
    that anything was wrong

Both are checked here against the real HTTP handler, because both looked
correct in the config and only came apart at the object doing the work.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_settingslive")
PORT = 5109
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

from lox import cfg  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def main() -> int:
    from lox.web import create_app_async

    runner = await create_app_async()
    app = runner.app
    gateway = app["gateway"]
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    base = f"http://127.0.0.1:{PORT}"

    async def save(changes: dict) -> dict:
        async with session.put(f"{base}/api/settings", json=changes and {"changes": changes}) as r:
            return await r.json()

    async with session.post(f"{base}/api/auth", json={"token": TOKEN}) as r:
        check("signed in", r.status == 200, str(r.status))

    try:
        # --- the budget reaches the thing that spends it ------------------
        body = await save({"checker.tracker_budget": 777, "checker.tracker_budget_window": 600})
        check("the budget saves", "checker.tracker_budget" in body.get("saved", []), str(body)[:90])
        check("and nothing failed to apply", not body.get("unapplied"), str(body.get("unapplied")))
        check("the config has it", cfg.checker.tracker_budget == 777, str(cfg.checker.tracker_budget))
        for code in ("RED", "OPS", "DIC"):
            state = gateway._states[code]
            check(f"{code}'s gateway state has it", state.budget == 777, str(state.budget))
        check("and the window with it",
              gateway._states["RED"].window == 600, str(gateway._states["RED"].window))

        # The sidebar reads this, which is why a stale budget was visible.
        check("the reported status quotes the new ceiling",
              gateway.status("RED").budget == 777, str(gateway.status("RED").budget))

        # --- raising it mid-window is not a fresh allowance ---------------
        state = gateway._states["RED"]
        state.record_success()
        state.record_success()
        spent = state.spent
        await save({"checker.tracker_budget": 900})
        check("calls already made survive a budget change", state.spent == spent, f"{state.spent} vs {spent}")
        check("and the new ceiling applies to what is left",
              state.remaining == 900 - spent, str(state.remaining))

        # --- delays are re-read too ---------------------------------------
        await save({"checker.tracker_call_delay": 9})
        check("the call delay is re-read", gateway.delay == 9, str(gateway.delay))

        # --- a rotated key reaches the client ------------------------------
        await save({"tracker.red.api_key": "AAAA1111", "tracker.red.session": "sess-one"})
        first = gateway.api("RED")
        check("a configured tracker hands back a client", first.api_key == "AAAA1111", str(first.api_key))

        await save({"tracker.red.api_key": "BBBB2222"})
        second = gateway.api("RED")
        check("a rotated key is not served from cache", second is not first, "same object")
        check("and the client sends the new key", second.api_key == "BBBB2222", str(second.api_key))

        await save({"tracker.red.session": "sess-two"})
        check("a new session cookie reaches it too",
              gateway.api("RED").cookie == "sess-two", str(gateway.api("RED").cookie))

        # --- and the page still agrees with the process --------------------
        async with session.get(f"{base}/api/settings") as r:
            payload = await r.json()
        check("the page shows what the gateway is using",
              payload["values"].get("checker.tracker_budget") == 900,
              str(payload["values"].get("checker.tracker_budget")))
    finally:
        await session.close()
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
