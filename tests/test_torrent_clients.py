"""Setting up a torrent client without knowing what a libtc URL is.

The settings page used to ask for the connection as one free-text box --
``qbittorrent+http://user:pass@host:8080`` -- with the other three clients'
shapes listed underneath as a hint. Everything about that is a config file with
a border drawn round it: the scheme is two schemes joined by a plus, which one
depends on the client, the password sits in the middle of the host, and every
way of getting it wrong comes back as "could not connect".

Now the page asks which client, where it is and which account to use, and the
server composes the URL. What that has to get right, and what this pins:

  * every client's URL comes out in the shape its own parser reads back
  * a password with an @ or a : in it survives the round trip
  * the browser is never sent a password, and never has to retype one
  * a bad port is refused with a sentence, not a stack trace
  * a hand-written config.toml still fills the form in
  * an unreachable client fails the test rather than passing quietly
"""

import asyncio
import contextlib
import os
import sys

import aiohttp

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_clients")
PORT = 5108
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

from lox.config.client_url import (  # noqa: E402
    CLIENT_BY_ID,
    CLIENTS,
    build_client_url,
    split_client_url,
)

NASTY = "p@ss:w/rd #1"
"""A password that exercises every character the URL format gives a meaning to."""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


@contextlib.contextmanager
def parser_only():
    """The real libtc parser, with the connecting part taken out.

    Constructing a client signs in, which opens a socket. The composition tests
    are about the URL, not the network, so for their duration every client
    becomes one that records what it was handed and connects to nothing. Put
    back afterwards -- the HTTP tests below need a client that really fails to
    connect, and a stub that always succeeds would hide that.
    """
    import lox.clients as module  # noqa: PLC0415

    class Recorder(module.TorrentClient):
        def login(self):
            return "connected"

    original = dict(module.TORRENT_CLIENT_MAPPING)
    module.TORRENT_CLIENT_MAPPING.update(dict.fromkeys(original, Recorder))
    try:
        yield module
    finally:
        module.TORRENT_CLIENT_MAPPING.update(original)


async def main() -> int:
    with parser_only() as libtc:
        # --- every client, composed and read back by its own parser --
        #
        # The password is deliberately horrible: an @ ends the auth section and
        # a colon separates the username from the password, so both have to
        # survive being percent-encoded and come back out unchanged.
        for spec in CLIENTS:
            url = build_client_url(
                {
                    "client": spec.id,
                    "host": "10.0.0.4",
                    "port": 4321,
                    "username": "admin",
                    "password": NASTY,
                    "secure": spec.secure,
                }
            )
            check(f"{spec.label} composes a URL", bool(url), url.replace(NASTY, "…"))

            parsed = libtc.TorrentClientGenerator.parse_libtc_url(url)
            check(f"{spec.label}'s own parser reads the password back",
                  parsed.password == NASTY, repr(parsed.password))
            check(f"{spec.label} keeps the username", parsed.username == "admin", repr(parsed.username))
            # qBittorrent and ruTorrent are addressed by URL; the other two by
            # host and port. Either way the address has to survive.
            where = parsed.url or f"{parsed.host}:{parsed.port}"
            check(f"{spec.label} keeps the address", "10.0.0.4" in where and "4321" in where, where)

            back = split_client_url(url)
            check(f"{spec.label} fills its own form back in",
                  back["client"] == spec.id and back["host"] == "10.0.0.4"
                  and back["port"] == 4321 and back["password"] == NASTY,
                  str({k: v for k, v in back.items() if k != "password"}))
            check(f"{spec.label} remembers whether it was HTTPS",
                  back["secure"] == spec.secure, str(back["secure"]))

        check("a username with no password still parses",
              libtc.TorrentClientGenerator.parse_libtc_url(
                  build_client_url({"client": "transmission", "host": "h", "username": "u"})).username == "u", "")

    # --- the defaults do the tedious part ----------------------------
    check("a blank port becomes the client's own default",
          split_client_url(build_client_url({"client": "deluge", "host": "h"}))["port"]
          == CLIENT_BY_ID["deluge"].port, "")
    check("ruTorrent fills in the path to rpc.php",
          build_client_url({"client": "rutorrent", "host": "h"}).endswith("/plugins/rpc/rpc.php"), "")
    check("a pasted address is reduced to a host and a port",
          build_client_url({"client": "qbittorrent", "host": "http://10.0.0.9:9090/"})
          == "qbittorrent+http://10.0.0.9:9090", "")
    check("an IPv6 literal keeps its brackets",
          split_client_url(build_client_url(
              {"client": "qbittorrent", "host": "[2001:db8::1]", "port": 8080}))["host"] == "[2001:db8::1]", "")

    # --- half-filled is not an error, wrong is -----------------------
    check("an entry with no client yet is not an error", build_client_url({"client": ""}) == "", "")
    check("nor is one with no host yet", build_client_url({"client": "deluge", "host": ""}) == "", "")
    for parts, expect in (
        ({"client": "utorrent", "host": "h"}, "not a torrent client"),
        ({"client": "deluge", "host": "h", "port": "eight"}, "not a port"),
        ({"client": "deluge", "host": "h", "port": 70000}, "1 to 65535"),
    ):
        try:
            build_client_url(parts)
            check(f"{parts} is refused", False, "it was accepted")
        except ValueError as e:
            check(f"{parts.get('client')}/{parts.get('port')} is refused in words",
                  expect in str(e), str(e))

    # --- a config.toml written by hand still opens -------------------
    for url, expect in (
        ("qbittorrent+http://user:pw@nas:8080", "qbittorrent"),
        ("deluge://user:pw@10.0.0.1:58846", "deluge"),
        ("rutorrent+https://rt.example.com/plugins/rpc/rpc.php", "rutorrent"),
        ("transmission+http://127.0.0.1:9091", "transmission"),
    ):
        check(f"{expect} written by hand fills the form", split_client_url(url)["client"] == expect, url)
    for junk in ("", "nonsense", "ftp://host", "utorrent+http://h:1"):
        check(f"{junk!r} leaves an empty form rather than raising",
              split_client_url(junk)["client"] == "", "")

    # --- and over HTTP -----------------------------------------------
    from lox.web import create_app_async  # noqa: PLC0415

    runner = await create_app_async()
    url = f"http://127.0.0.1:{PORT}"
    headers = {"X-Auth-Token": TOKEN}

    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(f"{url}/api/settings/seedboxes") as r:
                payload = await r.json()
            check("the page is told which clients exist",
                  {c["id"] for c in payload["clients"]} == {c.id for c in CLIENTS},
                  str([c["id"] for c in payload["clients"]]))
            check("each client says which port it uses",
                  all(c["port"] for c in payload["clients"]), "")
            check("no field asks for a connection URL any more",
                  not any(f["key"] == "torrent_client" for f in payload["fields"]),
                  str([f["key"] for f in payload["fields"]]))
            check("the rclone fields are hidden until they apply",
                  all(f.get("when") == {"key": "type", "value": "rclone"}
                      for f in payload["fields"] if f["key"] in ("url", "extra_args")), "")
            # The dropdown's own blank entry reads "Every tracker", so a help
            # line underneath explaining what blank means was telling you what
            # you had just read.
            check("the tracker choice labels its blank option instead of explaining it",
                  next(f for f in payload["fields"] if f["key"] == "tracker")["labels"][""] == "Every tracker"
                  and not any("every tracker" in (f.get("help") or "").lower() for f in payload["fields"]), "")

            entry = {
                "name": "seedbox",
                "enabled": True,
                "type": "local",
                "directory": "/downloads/{tracker}",
                "connection": {
                    "client": "qbittorrent",
                    "host": "10.0.0.4",
                    "port": 8080,
                    "username": "admin",
                    "password": NASTY,
                    "secure": False,
                },
            }
            async with s.put(f"{url}/api/settings/seedboxes", json={"seedboxes": [entry]}) as r:
                saved = await r.json()
            check("a client saves", r.status == 200 and saved.get("saved") == 1, str(saved))

            async with s.get(f"{url}/api/settings/seedboxes") as r:
                payload = await r.json()
            stored = payload["seedboxes"][0]
            check("and comes back as parts, not as a URL", "torrent_client" not in stored, str(stored.keys()))
            check("with the password withheld",
                  "password" not in stored["connection"] and stored["connection"]["password_set"] is True,
                  str(stored["connection"]))
            check("and everything else intact",
                  stored["connection"]["host"] == "10.0.0.4"
                  and stored["connection"]["port"] == 8080
                  and stored["connection"]["username"] == "admin"
                  and stored["directory"] == "/downloads/{tracker}", str(stored))

            from lox import cfg, settings  # noqa: PLC0415

            check("what is stored is a URL the pipeline can use",
                  cfg.seedbox[0].torrent_client.startswith("qbittorrent+http://admin:"),
                  cfg.seedbox[0].torrent_client.split("@")[-1])

            # Editing anything else must not require retyping the password --
            # the browser was never given it, so it has nothing to retype.
            edited = {**stored, "label": "music"}
            edited["connection"] = {**stored["connection"], "password": ""}
            async with s.put(f"{url}/api/settings/seedboxes", json={"seedboxes": [edited]}) as r:
                check("editing a label keeps the saved password", r.status == 200, str(r.status))
            check("really keeps it", split_client_url(cfg.seedbox[0].torrent_client)["password"] == NASTY, "")
            check("and applied the edit", cfg.seedbox[0].label == "music", cfg.seedbox[0].label)

            # A password that is typed replaces the one on file.
            replaced = {**stored, "connection": {**stored["connection"], "password": "brand-new"}}
            async with s.put(f"{url}/api/settings/seedboxes", json={"seedboxes": [replaced]}) as r:
                await r.json()
            check("a typed password replaces the old one",
                  split_client_url(cfg.seedbox[0].torrent_client)["password"] == "brand-new", "")

            bad = {**stored, "connection": {**stored["connection"], "port": 99999}}
            async with s.put(f"{url}/api/settings/seedboxes", json={"seedboxes": [bad]}) as r:
                body = await r.json()
            check("a bad port is refused with a sentence",
                  r.status == 400 and "65535" in body.get("error", ""), str(body))

            on_but_empty = {"name": "empty", "enabled": True, "type": "local", "connection": {"client": ""}}
            async with s.put(f"{url}/api/settings/seedboxes", json={"seedboxes": [on_but_empty]}) as r:
                body = await r.json()
            check("a client switched on with nowhere to connect is refused",
                  r.status == 400 and "address" in body.get("error", ""), str(body))

            # The test button reads the editor, so a connection can be checked
            # before it is saved. Nothing is listening on this port, so this
            # has to come back as a failure rather than as a pass or a 500.
            async with s.post(
                f"{url}/api/settings/test/qbittorrent",
                json={
                    "values": {},
                    "seedboxes": [
                        {
                            "name": "unsaved",
                            "enabled": True,
                            "connection": {
                                "client": "transmission",
                                "host": "127.0.0.1",
                                "port": 1,
                                "username": "u",
                                "password": "p",
                            },
                        }
                    ],
                },
            ) as r:
                body = await r.json()
            tested = (body.get("detail") or {}).get("clients") or []
            check("testing an unsaved client reaches it rather than the saved one",
                  r.status == 200 and body["ok"] is False
                  and [c["name"] for c in tested] == ["unsaved"], str(body)[:220])

            async with s.post(f"{url}/api/settings/test/qbittorrent",
                              json={"values": {}, "seedboxes": []}) as r:
                body = await r.json()
            check("with nothing set up it says so instead of failing",
                  body["ok"] is False and "no torrent client" in body["message"].lower(), str(body))

            settings.set_seedboxes([])
            settings.apply_to(cfg)
    finally:
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
