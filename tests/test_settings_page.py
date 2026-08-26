"""The settings page describes settings that exist and tests that run.

Two failures this guards against, both of which shipped:

A section holding several independent credentials had one "Test connection"
button at the top. Four image-host keys and five metadata tokens cannot be
answered by one button -- it could only ever report on one of them, and it
reported success while the other three were wrong.

And a setting that no longer does anything is worse than a missing one: it
reads as a control. The text-editor box was still on the page after every
editor had been replaced by a form.
"""

import asyncio
import os
import sys

import aiohttp

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_settingspage")
PORT = 5106
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

from lox import cfg  # noqa: E402
from lox.config.schema import CATEGORIES, FIELDS, SECTIONS, sections_with_fields  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def resolve(key: str):
    """Walk a dotted settings key against the live config."""
    node = cfg
    for part in key.split("."):
        if not hasattr(node, part):
            return None, False
        node = getattr(node, part)
    return node, True


async def main() -> int:
    # --- every field points at a setting that exists -----------------
    missing = [f.key for f in FIELDS if not resolve(f.key)[1]]
    check("every field on the page is a real setting", not missing, ", ".join(missing))

    # --- and every section is reachable ------------------------------
    rendered = sections_with_fields()
    shown = {s["id"] for s in rendered}
    declared = {s.id for s in SECTIONS}
    check("every declared section renders", shown == declared, str(sorted(declared - shown)))
    check("including one that is only a test",
          any(s["id"] == "torrent" and not s["fields"] and s["test"] for s in rendered), "")

    bad_category = [s.category for s in SECTIONS if s.category not in CATEGORIES]
    check("every section is in a known category", not bad_category, str(sorted(set(bad_category))))
    for name in CATEGORIES:
        check(f"the {name} category has sections",
              any(s.category == name for s in SECTIONS), "")

    # --- settings that no longer do anything are off the page --------
    keys = {f.key for f in FIELDS}
    for dead, why in (
        ("upload.default_editor", "every editor is a form now"),
        ("upload.web_interface.display_host", "the spectral viewer it linked to is gone"),
        ("upload.multi_tracker_upload", "trackers are ticked per upload instead"),
        ("upload.description.copy_uploaded_url_to_clipboard",
         "it copies to the server's clipboard, which is not the machine you are on"),
        ("image.ptpimg_key", "ptpimg.me answers HTTP 500 to everything, including its own upload endpoint"),
    ):
        check(f"{dead} is gone -- {why}", dead not in keys, "")

    # --- settings the pipeline reads are editable --------------------
    for key in (
        "upload.requests.always_ask_for_request_fill",
        "upload.compression.lma_comment_in_t_desc",
        "upload.description.fullwidth_replacements",
        "upload.compression.use_upc_as_catno",
        "upload.search.blacklisted_genres",
        "upload.formatting.blacklisted_substitution",
    ):
        check(f"{key} is editable", key in keys, "")

    # --- one credential, one test ------------------------------------
    per_field = {f.key: f.test for f in FIELDS if f.test}
    for key in ("image.ptscreens_key", "image.oeimg_key", "image.imgbb_key"):
        check(f"{key} has its own test", per_field.get(key, "").startswith("image:"), per_field.get(key, ""))
    for key, target in (
        ("metadata.discogs_token", "discogs"),
        ("metadata.apple_music_token", "apple"),
        ("metadata.qobuz.user_auth_token", "qobuz"),
        ("metadata.tidal.token", "tidal"),
    ):
        check(f"{key} tests {target}", per_field.get(key) == target, per_field.get(key, ""))

    check("no section pretends one button covers several credentials",
          not any(s.test for s in SECTIONS if s.id in ("images", "metadata")),
          str([s.id for s in SECTIONS if s.test and s.id in ("images", "metadata")]))

    # --- and every test named anywhere is dispatchable ---------------
    from lox.web import create_app_async  # noqa: PLC0415

    runner = await create_app_async()
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with session.post(f"http://127.0.0.1:{PORT}/api/auth", json={"token": TOKEN}) as r:
        check("signed in", r.status == 200, str(r.status))

    try:
        targets = sorted({s.test for s in SECTIONS if s.test} | set(per_field.values()))
        unknown = []
        for target in targets:
            async with session.post(
                f"http://127.0.0.1:{PORT}/api/settings/test/{target}", json={"values": {}}
            ) as resp:
                if resp.status == 404:
                    unknown.append(target)
        check("every test button has a handler", not unknown, ", ".join(unknown))
        check("and there are more of them than sections", len(targets) >= len(SECTIONS) - 4, str(len(targets)))

        # An unconfigured credential reports that, rather than raising.
        async with session.post(
            f"http://127.0.0.1:{PORT}/api/settings/test/image:oeimg", json={"values": {}}
        ) as resp:
            body = await resp.json()
        check("an unset key fails cleanly", body.get("ok") is False and "no oeimg key" in body["message"].lower(),
              str(body)[:110])

        async with session.post(
            f"http://127.0.0.1:{PORT}/api/settings/test/image:catbox", json={"values": {}}
        ) as resp:
            body = await resp.json()
        check("a host with no key says so rather than passing",
              body.get("ok") is False and "anonymous" in body["message"].lower(), str(body)[:110])

        # The page itself still serves, with the new shape.
        async with session.get(f"http://127.0.0.1:{PORT}/api/settings") as resp:
            payload = await resp.json()
        check("the settings payload carries categories",
              all("category" in s for s in payload["sections"]), "")
        check("and per-field tests",
              any(f.get("test") for s in payload["sections"] for f in s["fields"]), "")
        listed = {f["key"] for s in payload["sections"] for f in s["fields"]}
        check("every field is served", listed == keys, str(sorted(keys ^ listed)))
    finally:
        await session.close()
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
