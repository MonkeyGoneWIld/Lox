"""Signing in with a username and a password.

The web interface used to be gated by one shared token pasted into compose.
That token is in your shell history, your backups and your scrollback, it
cannot be changed without editing a file and restarting, and it cannot tell one
person from another. So the first person to open a new instance creates an
account, and everyone signs in with one from then on.

The properties that matter, and that this pins:

  * a fresh instance is never briefly open -- it goes to setup, not through
  * setup closes once one account exists
  * a wrong password is refused, and so is a wrong username, in the same time
  * changing a password ends the sessions that password had, everywhere
  * the shared token still works, for scripts and the healthcheck
"""

import asyncio
import os
import sys
import time

import aiohttp

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_accounts")
PORT = 5107
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": str(PORT),
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.web.accounts import (  # noqa: E402
    AccountError,
    AccountStore,
    issue_session,
    read_session,
)

TOKEN = os.environ["LOX_AUTH_TOKEN"]
GOOD = "correct-horse-battery"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def session(jar_unsafe: bool = True) -> aiohttp.ClientSession:
    # unsafe=True because aiohttp's jar drops cookies set by an IP-address host,
    # and the session cookie is the whole mechanism under test.
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=jar_unsafe))


async def main() -> int:
    # --- the store on its own ----------------------------------------
    store = AccountStore(os.path.join(BASE, "unit"))
    check("a new instance has nobody on it", store.empty, "")

    for bad, why in (("short", "too short"), ("", "empty")):
        try:
            store.create("jack", bad)
            check(f"a {why} password is refused", False, "it was accepted")
        except AccountError:
            check(f"a {why} password is refused", True, "")

    try:
        store.create("j", GOOD)
        check("a one-character username is refused", False, "it was accepted")
    except AccountError:
        check("a one-character username is refused", True, "")

    account = store.create("Jack", GOOD)
    check("an account can be made", account.username == "Jack", account.username)
    check("and the instance is no longer empty", not store.empty, "")
    with open(store.path, encoding="utf-8") as handle:
        on_disk = handle.read()
    check("the password is not stored", GOOD not in on_disk, "")
    check("what is stored is a hash", len(account.hash) == 64 and account.hash != GOOD, account.hash[:16])
    check("with its own salt", len(account.salt) == 32, account.salt[:16])

    check("the right password verifies", store.verify("Jack", GOOD) is not None, "")
    check("and the name is case-insensitive", store.verify("jack", GOOD) is not None, "")
    check("a wrong password does not", store.verify("Jack", "not-the-password") is None, "")
    check("nor does an unknown user", store.verify("eve", GOOD) is None, "")

    # An unknown username still pays for a hash, so it cannot be told apart
    # from a known one by how long the answer takes.
    start = time.perf_counter()
    store.verify("nobody-here", GOOD)
    unknown = time.perf_counter() - start
    start = time.perf_counter()
    store.verify("Jack", "wrong-password-here")
    known = time.perf_counter() - start
    check("an unknown user takes as long as a wrong password",
          unknown > known / 4, f"{unknown * 1000:.0f}ms vs {known * 1000:.0f}ms")

    try:
        store.create("jack", GOOD)
        check("the same name cannot be taken twice", False, "it was accepted")
    except AccountError:
        check("the same name cannot be taken twice", True, "")

    # --- sessions -----------------------------------------------------
    token = issue_session(store, "Jack")
    check("a session names its account", read_session(store, token) == "Jack", str(read_session(store, token)))
    check("a tampered session is refused", read_session(store, token[:-1] + "0") is None, "")
    check("so is a malformed one", read_session(store, "nonsense") is None, "")
    check("and an expired one", read_session(store, "jack.1.deadbeef") is None, "")

    store.set_password("Jack", "a-brand-new-secret")
    check("changing the password invalidates the old session", read_session(store, token) is None, "")
    check("the old password stops working", store.verify("Jack", GOOD) is None, "")
    check("and the new one works", store.verify("Jack", "a-brand-new-secret") is not None, "")

    try:
        store.delete("Jack")
        check("the last account cannot be deleted", False, "it was")
    except AccountError:
        check("the last account cannot be deleted", True, "")

    # --- and over HTTP ------------------------------------------------
    from lox.web import create_app_async  # noqa: PLC0415

    runner = await create_app_async()
    url = f"http://127.0.0.1:{PORT}"
    app_store = runner.app["accounts"]

    try:
        async with session() as s:
            async with s.get(f"{url}/api/health") as r:
                check("health needs no credentials", r.status == 200, str(r.status))
            async with s.get(f"{url}/api/auth/state") as r:
                state = await r.json()
            check("a fresh instance asks for setup", state["setup"] is True, str(state))

            async with s.get(f"{url}/api/status") as r:
                check("and refuses everything else", r.status == 401, str(r.status))

            async with s.post(f"{url}/api/auth/setup", json={"username": "jack", "password": "short"}) as r:
                check("setup refuses a weak password", r.status == 400, str(r.status))

            async with s.post(f"{url}/api/auth/setup", json={"username": "jack", "password": GOOD}) as r:
                check("setup creates the first account", r.status == 200, str(r.status))
            async with s.get(f"{url}/api/status") as r:
                check("which signs you straight in", r.status == 200, str(r.status))

            async with s.post(f"{url}/api/auth/setup", json={"username": "eve", "password": GOOD}) as r:
                check("setup closes afterwards", r.status == 409, str(r.status))

        # A second browser, signed in as the same person.
        async with session() as mine, session() as other:
            async with mine.post(f"{url}/api/auth", json={"username": "jack", "password": GOOD}) as r:
                check("an account can sign in", r.status == 200, str(r.status))
            async with other.post(f"{url}/api/auth", json={"username": "jack", "password": GOOD}) as r:
                check("on more than one device", r.status == 200, str(r.status))

            async with mine.post(f"{url}/api/auth", json={"username": "jack", "password": "nope"}) as r:
                check("a wrong password is refused", r.status == 401, str(r.status))

            async with mine.post(
                f"{url}/api/accounts/password", json={"current": "nope", "password": "the-new-secret"}
            ) as r:
                check("so is changing it without the current one", r.status == 401, str(r.status))

            async with mine.post(
                f"{url}/api/accounts/password", json={"current": GOOD, "password": "the-new-secret"}
            ) as r:
                check("with the current one it changes", r.status == 200, str(r.status))

            async with other.get(f"{url}/api/status") as r:
                check("the other device is signed out", r.status == 401, str(r.status))
            async with mine.get(f"{url}/api/status") as r:
                check("the one that changed it is not", r.status == 200, str(r.status))
            async with other.post(
                f"{url}/api/auth", json={"username": "jack", "password": "the-new-secret"}
            ) as r:
                check("and can sign back in with the new password", r.status == 200, str(r.status))

        # Scripts and the healthcheck.
        async with session() as s:
            async with s.get(f"{url}/api/status", headers={"X-Auth-Token": TOKEN}) as r:
                check("the shared token still authenticates", r.status == 200, str(r.status))
            async with s.get(f"{url}/api/status", headers={"X-Auth-Token": "wrong"}) as r:
                check("a wrong token does not", r.status == 401, str(r.status))

        check("accounts landed in the app's own store", app_store.usernames() == ["jack"],
              str(app_store.usernames()))
    finally:
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
