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

    # --- a dropdown reads as English ---------------------------------
    # Stored values want to be short and stable; "only_missing_there" is not a
    # thing to put in front of someone. Labels are parallel to choices, so the
    # one way to get this wrong is to let them fall out of step.
    mismatched = [f.key for f in FIELDS if f.labels and len(f.labels) != len(f.choices)]
    check("every labelled choice has one label per option", not mismatched, ", ".join(mismatched))
    labelled = [f for f in FIELDS if f.kind == "choice" and f.labels]
    check("the queue rule is labelled", any(f.key == "checker.queue_when" for f in labelled), "")
    check("and no label is left as the raw value",
          all(shown != stored
              for f in labelled
              for shown, stored in zip(f.labels, f.choices, strict=True)), "")

    # --- what reaches the queue is two questions, not six -------------
    # The first version asked for a three-way rule per tracker, an all/any to
    # combine them, and an enum for requests: a truth table with dropdowns in
    # front of it. Nobody wants to say "RED must already be there".
    queue_keys = {f.key for f in FIELDS if f.section == "queue"}
    check("the queue is configured by a rule and two switches",
          queue_keys == {"checker.queue_when", "checker.queue_requests_too",
                         "checker.request_recheck_after_days"},
          str(sorted(queue_keys)))

    # How long a request check is trusted. The same setting is offered on the
    # Requests page, beside the search it governs, because a setting reachable
    # only from another page is a setting nobody changes.
    window = next(f for f in FIELDS if f.key == "checker.request_recheck_after_days")
    check("the recheck window is a number of days", window.kind == "int", window.kind)
    check("and cannot be negative", window.minimum == 0, str(window.minimum))
    for dead in ("checker.queue_red", "checker.queue_ops", "checker.queue_dic",
                 "checker.queue_match", "checker.queue_requests",
                 "checker.queue_require_somewhere_missing"):
        check(f"{dead} is gone", dead not in keys, "")

    rule = next(f for f in FIELDS if f.key == "checker.queue_when")
    check("every rule option is a sentence about a situation",
          all(label.startswith("Missing from") for label in rule.labels), str(rule.labels[:2]))
    check("and none of them asks you to think in truth tables",
          not any(w in " ".join(rule.labels).lower()
                  for w in ("must", "any one", "combine", "doesn't matter")), "")

    # --- Qobuz: 401 is not a bad app ID ------------------------------
    # Measured against the live API. 400 means the app ID is wrong; 401 means
    # the app ID got through and the request is not authenticated, which Qobuz
    # answers to every catalogue endpoint when no user token is attached. This
    # test read 401 as "bad app ID" and never sent the token at all, so a
    # correct app ID and a saved token reported as a rejected app ID.
    from lox.web.settings_api import _qobuz_verdict  # noqa: PLC0415

    for status, has_token, want_ok, must_say in (
        (200, True, True, "both work"),
        (200, False, True, "App ID works"),
        (400, False, False, "rejected the app ID"),
        (400, True, False, "rejected the app ID"),
        (401, True, False, "rejected the auth token"),
        (401, False, False, "without a user auth token"),
        (403, True, False, "rejected the auth token"),
        (503, True, False, "HTTP 503"),
    ):
        passed, message = _qobuz_verdict(status, has_token)
        label = f"{status} with{'' if has_token else 'out'} a token"
        check(f"{label} -> {'pass' if want_ok else 'fail'}", passed is want_ok, message)
        check(f"  and says so: {must_say!r}", must_say.lower() in message.lower(), message)

    check("a 401 never blames the app ID",
          "app id" not in _qobuz_verdict(401, True)[1].lower().split("--")[1], _qobuz_verdict(401, True)[1])

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

        # --- a stored secret can be looked at, on request ------------
        #
        # "•••••••• (saved)" cannot answer which key is in there, which is the
        # only question anyone has about one. It is revealed one at a time and
        # never in the page payload, so an unrevealed secret is not sitting in
        # the DOM of every open tab.
        async with session.put(
            f"http://127.0.0.1:{PORT}/api/settings",
            json={"changes": {"metadata.discogs_token": "DISCOGS-TOKEN-abc123"}},
        ) as resp:
            check("a secret can be saved", resp.status == 200, str(resp.status))

        async with session.get(f"http://127.0.0.1:{PORT}/api/settings") as resp:
            payload = await resp.json()
        check("the payload still withholds it",
              payload["values"].get("metadata.discogs_token") is None,
              repr(payload["values"].get("metadata.discogs_token")))
        check("while saying it is set",
              "metadata.discogs_token" in payload["secrets_set"], "")

        async with session.get(
            f"http://127.0.0.1:{PORT}/api/settings/secret?key=metadata.discogs_token"
        ) as resp:
            revealed = await resp.json()
        check("and it can be revealed by name",
              revealed.get("value") == "DISCOGS-TOKEN-abc123", str(revealed)[:80])

        # Only secrets. Anything else is a bug in the caller and says so.
        async with session.get(
            f"http://127.0.0.1:{PORT}/api/settings/secret?key=upload.dry_run"
        ) as resp:
            body = await resp.json()
            check("a setting that is not a secret is refused",
                  resp.status == 400 and "not a secret" in body.get("error", ""), str(body)[:80])
        async with session.get(f"http://127.0.0.1:{PORT}/api/settings/secret?key=made.up") as resp:
            check("and so is one that does not exist", resp.status == 400, str(resp.status))

        # Behind the same door as everything else.
        async with aiohttp.ClientSession() as anon, anon.get(
            f"http://127.0.0.1:{PORT}/api/settings/secret?key=metadata.discogs_token"
        ) as resp:
            check("revealing needs a signed-in session", resp.status == 401, str(resp.status))

        # The page itself still serves, with the new shape.
        async with session.get(f"http://127.0.0.1:{PORT}/api/settings") as resp:
            payload = await resp.json()
        check("the settings payload carries categories",
              all("category" in s for s in payload["sections"]), "")
        check("and per-field tests",
              any(f.get("test") for s in payload["sections"] for f in s["fields"]), "")
        listed = {f["key"] for s in payload["sections"] for f in s["fields"]}
        # Every field except the ones edited on the screen they govern. The
        # scan filters are declared here so the settings API validates and
        # saves them, but they belong on the Scan tab: sat in a list beside
        # the tracker budget they read as rules the whole app obeys, which is
        # how they came to be understood as one.
        elsewhere = {f.key for f in FIELDS if f.on_page}
        check("the scan filters are edited on the Scan tab",
              elsewhere >= {"checker.min_tracks", "checker.min_date", "checker.max_date"},
              str(sorted(elsewhere)))
        check("every other field is served", listed == keys - elsewhere,
              str(sorted((keys - elsewhere) ^ listed)))
        check("and none of them leaks onto the settings page",
              not (listed & elsewhere), str(sorted(listed & elsewhere)))
    finally:
        await session.close()
        await runner.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
