"""Settings page API: read, write, and test credentials against the real thing.

A token that is merely *saved* tells you nothing. Every section that talks to an
external service has a test that actually calls it and reports back what it
found — the Deezer account behind an ARL, the username behind a tracker session,
whether a hardlink can genuinely be made between two directories.
"""

import contextlib
import os
from typing import Any

import aiohttp
import msgspec
from aiohttp import web

import lox.trackers
from lox import cfg, settings
from lox import debug as debuglog
from lox.config.schema import BOOTSTRAP_KEYS, sections_with_fields
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

    Tests run against whatever is currently saved, so save before testing.
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
    """Fetch a known Discogs release to prove the token works."""
    token = cfg.metadata.discogs_token
    if not token:
        return fail("No Discogs token set. Request track-count verification will be weaker without it.")
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session, session.get(
        "https://api.discogs.com/releases/249504",
        headers={"Authorization": f"Discogs token={token}", "User-Agent": "lox/1.0"},
    ) as resp:
        if resp.status == 401:
            return fail("Discogs rejected the token.")
        if resp.status != 200:
            return fail(f"Discogs returned HTTP {resp.status}.")
        data = msgspec.json.decode(await resp.read())
    return ok(f"Token works. Test lookup returned {len(data.get('tracklist') or [])} tracks.")


async def _test_torrent_client(request: web.Request) -> web.Response:
    """Connect to every configured torrent client and report its version."""
    from lox.uploader.torrent_client import TorrentClientGenerator

    entries = [s for s in cfg.seedbox if s.torrent_client]
    if not entries:
        return fail("No seedbox entry has a torrent_client URL.")

    results = []
    for entry in entries:
        try:
            client = TorrentClientGenerator.parse_libtc_url(entry.torrent_client)
            client.login()
            results.append({"name": entry.name or entry.type, "ok": True})
        except Exception as e:  # noqa: BLE001 - reported per client
            results.append({"name": entry.name or entry.type, "ok": False, "error": str(e)})

    good = [r for r in results if r["ok"]]
    if not good:
        return fail("Could not connect to any torrent client.", clients=results)
    if len(good) < len(results):
        return fail(f"Connected to {len(good)} of {len(results)} clients.", clients=results)
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
    "ptpimg": "ptpimg_key",
    "ptscreens": "ptscreens_key",
    "oeimg": "oeimg_key",
    "imgbb": "imgbb_key",
}


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


async def _test_image_host(host: str) -> web.Response:
    """Check one image host's key against the host itself.

    A real request, not a presence check: a key that is set but wrong fails at
    the moment a release is being uploaded, which is the worst time to find out.
    Nothing is uploaded -- these are read-only endpoints -- because a settings
    test should not quietly put a file on a public host.
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
            if host == "ptpimg":
                # ptpimg has no whoami; the upload endpoint answers 400 for a
                # good key with no file and 403 for a bad one.
                async with session.post("https://ptpimg.me/upload.php", data={"api_key": key}) as resp:
                    if resp.status in (401, 403):
                        return fail(f"ptpimg rejected the key. {used_for}")
                    return ok(f"ptpimg accepted the key. {used_for}")
            if host == "imgbb":
                async with session.get("https://api.imgbb.com/1/upload", params={"key": key}) as resp:
                    body = (await resp.text())[:200]
                    if resp.status in (400, 415):
                        # "no image" rather than "invalid key" means the key passed.
                        if "key" in body.lower() and "invalid" in body.lower():
                            return fail(f"imgbb rejected the key. {used_for}")
                        return ok(f"imgbb accepted the key. {used_for}")
                    if resp.status in (401, 403):
                        return fail(f"imgbb rejected the key. {used_for}")
                    return ok(f"imgbb answered HTTP {resp.status}. {used_for}")
            base = "https://ptscreens.com" if host == "ptscreens" else "https://oeimg.com"
            async with session.get(f"{base}/api/1/upload", params={"key": key}) as resp:
                if resp.status in (401, 403):
                    return fail(f"{host} rejected the key. {used_for}")
                return ok(f"{host} accepted the key. {used_for}")
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
