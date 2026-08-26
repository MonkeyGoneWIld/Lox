"""Settings page API: read, write, and test credentials against the real thing.

A token that is merely *saved* tells you nothing. Every section that talks to an
external service has a test that actually calls it and reports back what it
found — the Deezer account behind an ARL, the username behind a tracker session,
whether a hardlink can genuinely be made between two directories.
"""

import asyncio
import contextlib
import os
from typing import Any

import aiohttp
import msgspec
from aiohttp import web

import lox.trackers
from lox import cfg, settings
from lox import debug as debuglog
from lox.config.client_url import CLIENTS, build_client_url, split_client_url
from lox.config.schema import BOOTSTRAP_KEYS, FIELDS_BY_KEY, sections_with_fields
from lox.config.store import SettingsError, coerce, get_value, set_value
from lox.config.validations import validate as validate_config
from lox.deezer.gw import DeezerGW, DeezerGWError

routes = web.RouteTableDef()

TIMEOUT = aiohttp.ClientTimeout(total=20)


def _json(data: Any, status: int = 200) -> web.Response:
    """Serialize a payload as JSON."""
    return web.Response(body=msgspec.json.encode(data), status=status, content_type="application/json")


def ok(message: str, **detail: Any) -> web.Response:
    """A passing test result."""
    return _json({"ok": True, "message": message, "detail": detail})


def fail(message: str, **detail: Any) -> web.Response:
    """A failing test result. Still HTTP 200 — the request worked, the test did not."""
    return _json({"ok": False, "message": message, "detail": detail})


# ----------------------------------------------------------------------
# Read and write
# ----------------------------------------------------------------------


@routes.get("/api/settings")
async def api_settings(request: web.Request) -> web.Response:
    """Return the settings schema and the current effective values.

    Secrets come back as null with their key listed in ``secrets_set``, so the
    page can show "configured" without the browser ever holding the value.
    """
    values = settings.snapshot(cfg)
    secrets_set = values.pop("__secrets_set__", [])
    return _json(
        {
            "sections": sections_with_fields(),
            "values": values,
            "secrets_set": secrets_set,
            "overridden": sorted(settings.values),
            "bootstrap": list(BOOTSTRAP_KEYS),
            "config_path": request.app.get("config_path", ""),
            "problems": validate_config(cfg),
        }
    )


@routes.put("/api/settings")
async def api_settings_save(request: web.Request) -> web.Response:
    """Persist a set of changes and apply them to the running process.

    Changes take effect immediately — no restart — because the config objects
    are mutated in place and the pieces that cache from them are refreshed here.
    """
    body = await request.json()
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        return _json({"error": "changes must be a non-empty object"}, status=400)

    try:
        coerced = settings.update(changes)
    except SettingsError as e:
        return _json({"error": str(e)}, status=400)

    failed = settings.apply_to(cfg)
    lox.trackers.refresh_tracker_list()
    # The gateway copies budgets, delays and tracker credentials when it is
    # built, and it is built once at startup. Without this a saved change was
    # visible on the page and nowhere else.
    request.app["gateway"].reconfigure()
    debuglog.configure()
    # Directories can be created or corrected here, so re-run the checks that
    # were reported at startup rather than leaving a fixed problem on screen.
    remaining = validate_config(cfg)
    # The Deezer client caches its login; a new ARL has to invalidate it.
    if any(k.startswith("metadata.deezer") for k in coerced):
        gw: DeezerGW = request.app["gw"]
        gw.arl = cfg.metadata.deezer.arl
        gw.user_id = None
        gw.api_token = None
        gw.license_token = None
        await gw.close()

    return _json(
        {
            "saved": sorted(coerced),
            "unapplied": failed,
            "trackers": lox.trackers.tracker_list,
            "problems": remaining,
        }
    )


# The rest of a torrent-client entry -- everything that is not the connection
# itself, which the page renders from CLIENTS instead. Described here so the
# page can draw it without knowing the struct.
#
# "when" hides a field until another one has a particular value. The rclone
# settings are meaningless for a client that can already see the files, and
# three dead boxes on every entry is how a settings page turns into a copy of
# the config file.
SEEDBOX_FIELDS = [
    {"key": "name", "label": "Name", "kind": "text", "help": "What this client is called on this page."},
    {"key": "enabled", "label": "Send uploads here", "kind": "bool"},
    {"key": "directory", "label": "Save path", "kind": "text",
     "help": "The folder the client should load the release from, spelled the way the client sees it — which "
             "is not the way lox sees it if either of you is in a container. Blank uses the seeding "
             "directory from Seeding layout. {tracker} becomes RED, OPS or DIC."},
    {"key": "label", "label": "Category or label", "kind": "text",
     "help": "A category in qBittorrent, a label everywhere else. {tracker} works here too."},
    {"key": "tracker", "label": "Handles", "kind": "choice", "choices": ["", "RED", "OPS", "DIC"],
     "labels": {"": "Every tracker"}},
    {"key": "flac_only", "label": "FLAC only", "kind": "bool",
     "help": "Skip this client for MP3 downconversions."},
    {"key": "add_paused", "label": "Add paused", "kind": "bool",
     "help": "For when you would rather check the files before it starts announcing."},
    {"key": "type", "label": "The files are", "kind": "choice", "choices": ["local", "rclone"],
     "labels": {"local": "Already where this client can read them",
                "rclone": "On another machine — copy them there first"}},
    {"key": "url", "label": "rclone remote", "kind": "text", "when": {"key": "type", "value": "rclone"},
     "help": "The remote's name as rclone knows it, without the colon."},
    {"key": "extra_args", "label": "Extra rclone arguments", "kind": "list",
     "when": {"key": "type", "value": "rclone"}, "placeholder": "--checksum",
     "help": "Passed straight to rclone copy, separated by spaces."},
]


def _connection_of(box: Any) -> dict[str, Any]:
    """The connection fields of one entry, with the password withheld.

    The page is told a password is set, never what it is -- the same treatment
    every other secret on the settings page gets.
    """
    parts = split_client_url(getattr(box, "torrent_client", "") or "")
    password = parts.pop("password", "")
    parts["password_set"] = bool(password)
    return parts


@routes.get("/api/settings/seedboxes")
async def api_seedboxes(request: web.Request) -> web.Response:
    """The torrent clients finished uploads are handed to.

    Separate from the rest of the settings because this is a list of
    connections, not a set of single values -- which is why it was missing from
    the page entirely, leaving "inject into the torrent client" with nothing to
    inject into.
    """
    entries = []
    for box in cfg.seedbox:
        entry = msgspec.to_builtins(box)
        entry.pop("torrent_client", None)
        entry["connection"] = _connection_of(box)
        entries.append(entry)
    return _json(
        {
            "seedboxes": entries,
            "fields": SEEDBOX_FIELDS,
            "clients": [c.as_dict() for c in CLIENTS],
        }
    )


def _saved_password(connection: dict[str, Any], name: str) -> str:
    """The password already on file for a connection the page sent back blank.

    Matched on where it connects rather than on the entry's name: the name is
    a label you are free to change, and looking it up by name meant renaming a
    client while leaving its password untouched silently blanked the password
    -- which stops seeding without saying anything. Falls back to the name for
    an entry whose address is being corrected at the same time.

    Args:
        connection: The connection fields as the page sent them.
        name: The entry's name.

    Returns:
        The stored password, or an empty string when there is nothing to reuse.
    """
    def where(parts: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(parts.get("client") or ""),
            str(parts.get("host") or "").lower(),
            str(parts.get("port") or ""),
            str(parts.get("username") or ""),
        )

    target = where(connection)
    by_name = ""
    for box in cfg.seedbox:
        parts = split_client_url(box.torrent_client or "")
        if not parts.get("password"):
            continue
        if where(parts) == target:
            return parts["password"]
        if box.name and box.name == name:
            by_name = parts["password"]
    return by_name


def _compose_entries(entries: list[Any]) -> list[dict[str, Any]]:
    """Turn what the page sends back into entries the config understands.

    The page edits the connection as parts; the config stores it as one URL.
    A password left blank means "the one already saved", so changing a category
    does not mean retyping a password the browser was never given.

    Args:
        entries: Raw entries from the request body.

    Returns:
        Entries with ``torrent_client`` composed and ``connection`` removed.

    Raises:
        SettingsError: If an entry is not a set of fields, or its connection
            cannot be made into a URL.
    """
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries or [], 1):
        if not isinstance(entry, dict):
            raise SettingsError(f"Torrent client {index} is not a set of fields.")
        entry = dict(entry)
        connection = entry.pop("connection", None)
        if isinstance(connection, dict):
            connection = dict(connection)
            if not connection.get("password"):
                connection["password"] = _saved_password(connection, str(entry.get("name") or ""))
            try:
                entry["torrent_client"] = build_client_url(connection)
            except ValueError as e:
                raise SettingsError(f"Torrent client {index}: {e}") from e
        out.append(entry)
    return out


@routes.get("/api/settings/seedboxes/secret")
async def api_seedbox_secret(request: web.Request) -> web.Response:
    """The stored password for one torrent client.

    Same reasoning as the settings reveal: the page is told a password is set,
    never what it is, and "which password is in there" is a fair question when a
    client will not connect. Looked up by the entry's name, one at a time.
    """
    name = request.query.get("name", "")
    for box in cfg.seedbox:
        if box.name == name:
            password = split_client_url(box.torrent_client or "").get("password", "")
            debuglog.log("settings: revealed the password for torrent client %s", name, level=20)
            return _json({"name": name, "value": password, "set": bool(password)})
    return _json({"error": f"no torrent client called {name or '(unnamed)'}"}, status=400)


@routes.put("/api/settings/seedboxes")
async def api_seedboxes_save(request: web.Request) -> web.Response:
    """Replace the torrent-client list."""
    body = await request.json()
    entries = body.get("seedboxes")
    if not isinstance(entries, list):
        return _json({"error": "seedboxes must be a list"}, status=400)

    try:
        stored = settings.set_seedboxes(_compose_entries(entries))
    except SettingsError as e:
        return _json({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001 - a bad entry must say so, not 500
        return _json({"error": f"Could not save: {type(e).__name__}: {e}"}, status=400)
    settings.apply_to(cfg)
    return _json({"saved": len(stored)})


@routes.get("/api/settings/secret")
async def api_settings_secret(request: web.Request) -> web.Response:
    """Hand back one stored secret, so it can be read on the page that set it.

    Secrets are deliberately not in the settings payload: the page is told a key
    is set, never what it is, so a value nobody asked for is not sitting in the
    DOM of every open tab. But "•••••••• (saved)" cannot answer the question
    people actually have, which is *which* key is in there -- the one from the
    right account, or the one pasted from the wrong tab an hour ago.

    So it is revealed on request, one key at a time, and never in bulk. This
    grants no access that the page did not already have: anyone who can reach
    it can already spend the credential by pressing Test or starting an upload.
    It is logged for the same reason a password reveal is logged anywhere --
    reading a secret is an event worth having a record of.

    Args:
        request: Carries ``key``, a dotted settings key.

    Returns:
        The key and its value, or 400 if the key is not a secret this page owns.
    """
    key = request.query.get("key", "")
    field = FIELDS_BY_KEY.get(key)
    if field is None or field.kind != "secret":
        # Named rather than silently empty: asking for a non-secret is a bug in
        # the caller, and asking for one that does not exist is worth saying.
        return _json({"error": f"{key or 'that'} is not a secret this page stores"}, status=400)

    value = get_value(cfg, key)
    debuglog.log("settings: revealed %s to the browser", key, level=20)
    return _json({"key": key, "value": value or "", "set": bool(value)})


@routes.post("/api/settings/reset")
async def api_settings_reset(request: web.Request) -> web.Response:
    """Drop UI overrides for the given keys, reverting to config.toml."""
    body = await request.json()
    keys = body.get("keys") or []
    for key in keys:
        settings.values.pop(key, None)
        settings._values.pop(key, None)  # noqa: SLF001 - same package, no public delete yet
    settings.save()
    return _json({"reset": keys, "restart_required": True})


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@contextlib.contextmanager
def _temporarily(values: dict[str, Any]):
    """Apply unsaved settings for the duration of a test, then put them back.

    Args:
        values: Dotted keys to raw values, straight from the settings page.

    Yields:
        Nothing; the config is mutated for the body of the block.
    """
    if not values:
        yield
        return
    previous: dict[str, Any] = {}
    applied: list[str] = []
    try:
        for key, raw in values.items():
            coerced = coerce(key, raw)
            previous[key] = get_value(cfg, key)
            set_value(cfg, key, coerced)
            applied.append(key)
        yield
    finally:
        for key in applied:
            with contextlib.suppress(SettingsError):
                set_value(cfg, key, previous[key])


@routes.post("/api/settings/test/{target}")
async def api_settings_test(request: web.Request) -> web.Response:
    """Run a live check for one settings section.

    Anything typed but not yet saved is applied for the duration of the call
    and rolled back afterwards, so a credential can be tested before it is
    committed and a failing test never leaves the config half-changed.
    """
    target = request.match_info["target"]
    body = await request.json() if request.can_read_body else {}
    pending = body.get("values") or {}
    handler = {
        "deezer": _test_deezer,
        "red": _test_red,
        "ops": _test_ops,
        "dic": _test_dic,
        "discogs": _test_discogs,
        "apple": _test_apple_music,
        "qobuz": _test_qobuz,
        "tidal": _test_tidal,
        "qbittorrent": _test_torrent_client,
        "discord": _test_discord,
        "linking": _test_linking,
        "paths": _test_paths,
        "images": _test_images,
    }.get(target)
    # One key, one test. A section holding four independent image-host
    # credentials cannot be answered by a single button, so each key names its
    # own host here.
    if handler is None and target.startswith("image:"):
        host = target.split(":", 1)[1]

        async def image_handler(_request: web.Request) -> web.Response:
            return await _test_image_host(host)

        handler = image_handler

    if handler is None:
        return _json({"error": f"no test named {target}"}, status=404)
    try:
        # Test what is on screen, not what was last saved. Anything typed but
        # unsaved is applied for the call and rolled back afterwards, so a
        # failed test never leaves the running config half-changed.
        with _temporarily(pending):
            return await handler(request)
    except SettingsError as e:
        return fail(str(e))
    except Exception as e:  # noqa: BLE001 - a failing test must report, not 500
        return fail(f"Test raised {type(e).__name__}: {e}")


async def _test_deezer(request: web.Request) -> web.Response:
    """Log in to Deezer with the configured ARL."""
    arl = cfg.metadata.deezer.arl
    if not arl:
        return fail("No ARL set.")

    probe = DeezerGW(arl=arl)
    try:
        await probe.login(force=True)
        detail = {
            "user_id": probe.user_id,
            "country": probe.country,
            "can_stream": bool(probe.license_token),
        }
        if not probe.license_token:
            return fail(
                "ARL is valid but the account has no streaming licence, so downloads will not work.", **detail
            )
        return ok(f"Signed in as user {probe.user_id} ({probe.country}). Downloads available.", **detail)
    except DeezerGWError as e:
        return fail(str(e))
    finally:
        await probe.close()


async def _test_tracker(code: str) -> web.Response:
    """Call a tracker's index endpoint and report who we are."""
    if code not in cfg.tracker.configured():
        return fail(f"{code} has no session cookie or API key.")
    try:
        api = lox.trackers.get_class(code)()
        data = await api.request("index")
    except Exception as e:  # noqa: BLE001 - surfaced as a test failure
        return fail(f"{code} rejected the credentials: {e}")

    username = (data or {}).get("username") or "?"
    detail = {
        "username": username,
        "user_id": (data or {}).get("id"),
        "uploaded": (data or {}).get("userstats", {}).get("uploaded"),
        "ratio": (data or {}).get("userstats", {}).get("ratio"),
        "auth_method": "API key" if getattr(api, "api_key", None) else "session cookie",
    }
    return ok(f"Authenticated on {code} as {username} via {detail['auth_method']}.", **detail)


async def _test_red(request: web.Request) -> web.Response:
    """Check RED credentials."""
    return await _test_tracker("RED")


async def _test_ops(request: web.Request) -> web.Response:
    """Check OPS credentials."""
    return await _test_tracker("OPS")


async def _test_dic(request: web.Request) -> web.Response:
    """Check DIC credentials."""
    return await _test_tracker("DIC")


async def _test_discogs(request: web.Request) -> web.Response:
    """Ask Discogs who the token belongs to.

    Against /oauth/identity, which requires authentication. It used to fetch a
    release instead -- and a release is public, so Discogs answered 200 with the
    full record for a token of ten zeroes. The test passed for any string at
    all, including none of the right ones.
    """
    token = cfg.metadata.discogs_token
    if not token:
        return fail("No Discogs token set. Request track-count verification will be weaker without it.")
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session, session.get(
        "https://api.discogs.com/oauth/identity",
        headers={"Authorization": f"Discogs token={token}", "User-Agent": "lox/1.0"},
    ) as resp:
        body = await resp.text()
        if resp.status in (401, 403):
            return fail("Discogs rejected the token.")
        if resp.status != 200:
            return fail(f"Discogs returned HTTP {resp.status}.")
        try:
            who = msgspec.json.decode(body.encode("utf-8", "replace"))
        except (msgspec.DecodeError, ValueError):
            return fail("Discogs answered with something this test could not read.")
    username = (who or {}).get("username") or "?"
    return ok(f"Authenticated with Discogs as {username}.", username=username,
              id=(who or {}).get("id"))


def _connect(name: str, url: str) -> dict[str, Any]:
    """Sign in to one torrent client. Blocking, so it is called in a thread."""
    from lox.clients import TorrentClientGenerator  # noqa: PLC0415

    try:
        client = TorrentClientGenerator.parse_libtc_url(url)
    except Exception as e:  # noqa: BLE001 - reported per client
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
    # Every client here answers a failed login with None rather than raising,
    # so "it did not throw" is not the same as "it connected".
    if client.client is None:
        return {"name": name, "ok": False, "error": "refused the connection or the credentials"}
    return {"name": name, "ok": True}


async def _test_torrent_client(request: web.Request) -> web.Response:
    """Sign in to each configured torrent client and report which answered.

    Tests whatever is currently in the editor when the page sends it, so a
    connection can be checked before it is saved -- which is the order anyone
    types one in.
    """
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()

    pending = body.get("seedboxes")
    if isinstance(pending, list):
        try:
            entries = [
                (str(e.get("name") or f"client {i}"), str(e.get("torrent_client") or ""))
                for i, e in enumerate(_compose_entries(pending), 1)
            ]
        except SettingsError as e:
            return fail(str(e))
    else:
        entries = [(s.name or s.type, s.torrent_client) for s in cfg.seedbox]

    entries = [(name, url) for name, url in entries if url]
    if not entries:
        return fail("No torrent client has an address yet. Add one below, then test.")

    # login() opens a socket and waits on it. On the event loop that stalls
    # every other request in the process, including the page you are on.
    results = [await asyncio.to_thread(_connect, name, url) for name, url in entries]

    good = [r for r in results if r["ok"]]
    if not good:
        return fail("Could not connect to any torrent client.", clients=results)
    if len(good) < len(results):
        return fail(f"Connected to {len(good)} of {len(results)}.", clients=results)
    return ok(f"Connected to {len(good)} torrent client(s).", clients=results)


async def _test_discord(request: web.Request) -> web.Response:
    """Post a test message to the configured webhook."""
    webhook = cfg.notifications.discord_webhook
    if not webhook:
        return fail("No webhook URL set.")
    async with (
        aiohttp.ClientSession(timeout=TIMEOUT) as session,
        session.post(webhook, json={"content": "lox test message — your webhook works."}) as resp,
    ):
            if 200 <= resp.status < 300:
                return ok("Test message delivered. Check your Discord channel.")
            return fail(f"Discord returned HTTP {resp.status}.")


async def _test_linking(request: web.Request) -> web.Response:
    """Prove a hardlink can actually be made from downloads to the link dir."""
    if not cfg.linking.enabled:
        return fail("Linking is off, so uploads will seed from the download directory.")
    link_dir = cfg.linking.link_dir
    if not link_dir:
        return fail("No seeding directory set.")
    if not os.path.isdir(link_dir):
        return fail(f"{link_dir} does not exist.")

    download_dir = cfg.metadata.deezer.download_dir or cfg.directory.download_directory
    if not os.path.isdir(download_dir):
        return fail(f"Download directory {download_dir} does not exist.")

    source_path = os.path.join(download_dir, f".lox-linktest-{os.getpid()}")
    with open(source_path, "wb") as handle:
        handle.write(b"lox link test")
    target = os.path.join(link_dir, f".lox-linktest-{os.getpid()}")
    try:
        if cfg.linking.method == "hardlink":
            os.link(source_path, target)
            same_inode = os.stat(source_path).st_ino == os.stat(target).st_ino
            message = (
                "Hardlinks work between the download and seeding directories. Uploading to two "
                "trackers will not cost extra disk."
                if same_inode
                else "A link was created but it is not the same inode — this will duplicate data."
            )
            return ok(message) if same_inode else fail(message)
        os.symlink(source_path, target) if cfg.linking.method == "symlink" else None
        return ok(f"Method is '{cfg.linking.method}', which always works but does not save disk space.")
    except OSError as e:
        same_fs = os.stat(download_dir).st_dev == os.stat(link_dir).st_dev
        hint = (
            " The two directories are on different filesystems — mount them under a common parent."
            if not same_fs
            else ""
        )
        return fail(f"Could not link: {e}.{hint}")
    finally:
        for path in (target, source_path):
            with contextlib.suppress(OSError):
                os.unlink(path)


async def _test_paths(request: web.Request) -> web.Response:
    """Check every configured directory exists and is writable."""
    checks = {
        "download_directory": cfg.directory.download_directory,
        "dottorrents_dir": cfg.directory.dottorrents_dir,
        "tmp_dir": cfg.directory.tmp_dir,
        "linking.link_dir": cfg.linking.link_dir,
        "checker.state_dir": cfg.checker.state_dir,
        "deezer.download_dir": cfg.metadata.deezer.download_dir,
    }
    results = {}
    problems = []
    for name, path in checks.items():
        if not path:
            results[name] = "unset"
            continue
        if not os.path.isdir(path):
            results[name] = "missing"
            problems.append(f"{name} ({path}) does not exist")
        elif not os.access(path, os.W_OK):
            results[name] = "read-only"
            problems.append(f"{name} ({path}) is not writable")
        else:
            results[name] = "ok"

    if problems:
        return fail("; ".join(problems), paths=results)
    return ok("Every configured directory exists and is writable.", paths=results)


# The hosts that need a key, and where that key lives. catbox and imgbox take
# anonymous uploads, so they have nothing to test.
_IMAGE_KEYS = {
    "ptscreens": "ptscreens_key",
    "oeimg": "oeimg_key",
    "imgbb": "imgbb_key",
}
"""The hosts that take a key. ptpimg is gone: ptpimg.me answers HTTP 500 to
everything, including its own upload endpoint, and a test that only failed on
401 or 403 read that as the key being accepted."""


def _image_roles(host: str) -> list[str]:
    """Which of the three uploader slots a host is currently selected for."""
    return [
        role
        for role, chosen in (
            ("general images", cfg.image.image_uploader),
            ("cover art", cfg.image.cover_uploader),
            ("spectrals", cfg.image.specs_uploader),
        )
        if chosen == host
    ]


def _image_verdict(host: str, status: int, body: str, used_for: str) -> web.Response:
    """Read an image host's answer to a key with no file attached.

    None of these hosts uses 401 for a bad key. They answer HTTP 400 with a JSON
    body naming the reason, so a check that only failed on 401 or 403 read every
    refusal as a pass -- which is why a made-up key tested green.

    What separates them is the error code. 100 is "invalid API key" on all of
    them. Any other code -- 130, "empty upload source" -- means the key got
    through and only the file was missing, which is exactly what a test that
    uploads nothing should produce.

    Args:
        host: Host name, for the message.
        status: HTTP status.
        body: Response body.
        used_for: What this host is currently selected for.

    Returns:
        A passing or failing test result.
    """
    code: int | None = None
    detail = ""
    try:
        parsed = msgspec.json.decode(body.encode("utf-8", "replace"))
        error = parsed.get("error") or {} if isinstance(parsed, dict) else {}
        if isinstance(error, dict):
            code = error.get("code") if isinstance(error.get("code"), int) else None
            detail = str(error.get("message") or "")
    except (msgspec.DecodeError, ValueError, AttributeError):
        pass

    lowered = detail.lower()
    if code == 100 or "invalid api" in lowered or "invalid key" in lowered:
        return fail(f"{host} rejected the key: {detail or 'invalid API key'}. {used_for}")
    if status in (401, 403):
        return fail(f"{host} rejected the key with HTTP {status}. {used_for}")
    if status >= 500:
        return fail(f"{host} is not answering properly -- HTTP {status}. {used_for}")
    if code is not None:
        # An error that names a reason other than the key: it got through.
        return ok(f"{host} accepted the key.", used_for=used_for.strip(), answered=detail or f"code {code}")
    if status == 200:
        return ok(f"{host} accepted the key.", used_for=used_for.strip(), answered="HTTP 200")
    # A refusal whose reason could not be read. Not a pass: the whole failure
    # this replaced was reading an answer it did not understand as approval.
    return fail(
        f"{host} answered HTTP {status} and this test could not tell whether the key was the problem. "
        f"{used_for}",
        answered=(body or "")[:160],
    )


async def _test_image_host(host: str) -> web.Response:
    """Check one image host's key against the host itself.

    A real request, not a presence check: a key that is set but wrong fails at
    the moment a release is being uploaded, which is the worst time to find out.

    Sent exactly the way the uploader sends it -- same URL, same header -- and
    against the same module-level constant, so the test cannot drift onto a
    different endpoint from the one that does the work. It did: the check for
    oeimg pointed at oeimg.com, which does not resolve, while uploads went to a
    third domain again.

    Nothing is uploaded. No file is attached, which is a request these hosts
    answer with "empty upload source" once the key has been accepted.
    """
    attribute = _IMAGE_KEYS.get(host)
    if attribute is None:
        return fail(f"{host} takes anonymous uploads, so there is no key to test.")

    key = getattr(cfg.image, attribute, "")
    roles = _image_roles(host)
    used_for = f"Selected for {', '.join(roles)}." if roles else "Not currently selected for anything."
    if not key:
        return fail(f"No {host} key set. {used_for}")

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            if host == "imgbb":
                # imgbb takes the key as a query parameter, not a header.
                from lox.images import imgbb  # noqa: PLC0415

                url = getattr(imgbb, "API_URL", "https://api.imgbb.com/1/upload")
                async with session.get(url, params={"key": key}) as resp:
                    return _image_verdict(host, resp.status, await resp.text(), used_for)

            module = "oeimg" if host == "oeimg" else "ptscreens"
            from importlib import import_module  # noqa: PLC0415

            uploader = import_module(f"lox.images.{module}")
            # POST, because a GET to a Chevereto API answers with an HTML error
            # page rather than JSON -- unreadable, and read as a pass.
            async with session.post(uploader.API_URL, headers=uploader.headers()) as resp:
                return _image_verdict(host, resp.status, await resp.text(), used_for)
    except TimeoutError:
        return fail(f"{host} did not answer in time.")
    except aiohttp.ClientError as e:
        return fail(f"Could not reach {host}: {e}")


async def _test_images(request: web.Request) -> web.Response:
    """Report whether the three selected hosts each have what they need."""
    selected = {cfg.image.image_uploader, cfg.image.cover_uploader, cfg.image.specs_uploader}
    missing = [
        host for host in selected
        if host in _IMAGE_KEYS and not getattr(cfg.image, _IMAGE_KEYS[host], "")
    ]
    if missing:
        return fail(f"Selected but missing an API key: {', '.join(sorted(missing))}.")
    return ok(f"Using {', '.join(sorted(selected))}. Test each key for whether it actually works.")


async def _test_apple_music(request: web.Request) -> web.Response:
    """Check the Apple Music developer token against the catalog API."""
    token = cfg.metadata.apple_music_token
    if not token:
        return fail("No Apple Music token set. Request verification works without it, just less well.")
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session, session.get(
        "https://api.music.apple.com/v1/catalog/us/albums/1440857781",
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        if resp.status in (401, 403):
            return fail("Apple Music rejected the token. Developer tokens expire — six months at most.")
        if resp.status != 200:
            return fail(f"Apple Music returned HTTP {resp.status}.")
    return ok("Token works.")


async def _test_qobuz(request: web.Request) -> web.Response:
    """Check the Qobuz app ID, and the auth token if one is set."""
    app_id = cfg.metadata.qobuz.app_id
    if not app_id:
        return fail("No Qobuz app ID set.")
    params = {"album_id": "0060254764852", "app_id": app_id}
    headers = {}
    if cfg.metadata.qobuz.user_auth_token:
        headers["X-User-Auth-Token"] = cfg.metadata.qobuz.user_auth_token
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session, session.get(
        "https://www.qobuz.com/api.json/0.2/album/get", params=params, headers=headers
    ) as resp:
        if resp.status in (400, 401):
            return fail("Qobuz rejected the app ID or the auth token.")
        if resp.status != 200:
            return fail(f"Qobuz returned HTTP {resp.status}.")
    return ok("App ID works." + (" Auth token accepted." if headers else " No auth token set, which is fine."))


async def _test_tidal(request: web.Request) -> web.Response:
    """Check the Tidal token against the album endpoint."""
    token = cfg.metadata.tidal.token
    if not token:
        return fail("No Tidal token set.")
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session, session.get(
        "https://api.tidal.com/v1/albums/77646169",
        params={"countryCode": "US"},
        headers={"x-tidal-token": token},
    ) as resp:
        if resp.status in (401, 403):
            return fail("Tidal rejected the token.")
        if resp.status != 200:
            return fail(f"Tidal returned HTTP {resp.status}.")
    return ok("Token works.")


# ----------------------------------------------------------------------
# Debug
# ----------------------------------------------------------------------


@routes.get("/api/debug")
async def api_debug(request: web.Request) -> web.Response:
    """Return debug state, the diagnostics summary and the recent log."""
    limit = min(int(request.query.get("limit", 300)), 2000)
    path = debuglog.log_path()
    return _json(
        {
            "enabled": debuglog.enabled(),
            "diagnostics": debuglog.diagnostics(),
            "log": debuglog.recent(limit),
            "logfile": {
                "path": path,
                "bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
                "max_file_bytes": cfg.logging.max_file_bytes,
                "max_total_bytes": cfg.logging.max_total_bytes,
            },
        }
    )


@routes.post("/api/debug/clear")
async def api_debug_clear(request: web.Request) -> web.Response:
    """Empty the in-memory debug log."""
    debuglog.clear()
    return _json({"ok": True})


@routes.get("/api/debug/logfile")
async def api_debug_logfile(request: web.Request) -> web.StreamResponse:
    """Download the active rolling log file.

    Already redacted on the way in, so it is safe to share as-is.
    """
    path = debuglog.log_path()
    if not os.path.isfile(path):
        return _json({"error": "no log file yet"}, status=404)
    return web.FileResponse(
        path, headers={"Content-Disposition": 'attachment; filename="lox.log"'}
    )


@routes.get("/api/debug/bundle")
async def api_debug_bundle(request: web.Request) -> web.Response:
    """Download diagnostics plus the recent log as a text file.

    Everything in it has been through the redactor, so it is safe to paste into
    an issue: secrets are reported as set or unset, never by value.
    """
    return web.Response(
        text=debuglog.diagnostics_text(),
        content_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="lox-diagnostics.txt"'},
    )
