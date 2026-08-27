"""JSON API behind the web UI.

Everything the browser does goes through here. The split that matters: search,
explore, album detail and downloads never touch a tracker, so they are safe to
call freely. Only ``/api/missing/check`` and the ``/api/requests/*`` endpoints
spend tracker budget, and each reports what it cost.
"""

import asyncio
import contextlib
import ipaddress
import os
import secrets
import shutil
import time
from typing import Any
from urllib.parse import quote

import msgspec
from aiohttp import web

from lox import cfg, debug, settings
from lox.checker import queue_rules, recheck
from lox.checker.deezer_requests import DeezerRequestChecker, age_of
from lox.checker.gateway import TrackerGateway
from lox.checker.missing import Candidate, MissingScanner
from lox.checker.request_detail import request_detail
from lox.checker.request_filters import schema as filter_schema
from lox.checker.store import CheckerStore
from lox.checker.watchlists import WatchlistManager
from lox.config.validations import ensure_dir
from lox.config.validations import problems as config_problems
from lox.deezer.download import Downloader
from lox.deezer.explore import Explorer
from lox.deezer.gw import DeezerGW, DeezerGWError
from lox.flow import FlowRegistry
from lox.notify.discord import DiscordNotifier
from lox.upload_flow import run_uploads
from lox.web.accounts import (
    MIN_PASSWORD,
    AccountError,
    AccountStore,
    issue_session,
    read_session,
)
from lox.web.jobs import JobRegistry

routes = web.RouteTableDef()

AUTH_COOKIE = "lox_token"
AUTH_HEADER = "X-Auth-Token"

# Reachable without a session: the login page itself, the endpoint that creates
# the session, and static assets, which carry nothing sensitive.
AUTH_EXEMPT_PATHS = frozenset(
    {"/login", "/api/auth", "/api/auth/state", "/api/auth/setup", "/api/health"}
)

SESSION_DAYS = 30

# Crude per-address throttle. The token guards tracker upload privileges, so an
# open port should not allow unlimited guessing.
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_WINDOW = 300.0
LOGIN_MAX_ATTEMPTS = 10
LOGIN_FAIL_DELAY = 0.5


def accounts_of(request: web.Request) -> AccountStore:
    """The account store attached to the running app."""
    return request.app["accounts"]


def auth_required() -> bool:
    """Whether anything is guarding this instance.

    False only when there are no accounts *and* no token, which is the state a
    brand-new install is in for exactly as long as it takes to make the first
    account. The middleware sends that case to setup rather than opening the
    door.
    """
    return True


def _token_ok(supplied: str | None) -> bool:
    """Compare a supplied token against the configured one in constant time.

    The token is still accepted, for scripts, the healthcheck and the ``?token=``
    bookmark. It is no longer the only way in, and it is no longer required.
    """
    expected = cfg.upload.web_interface.auth_token
    if not expected:
        return False
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def is_authenticated(request: web.Request) -> bool:
    """True when the request proves who it is, by session cookie or by token."""
    store = accounts_of(request)
    cookie = request.cookies.get(AUTH_COOKIE)
    if read_session(store, cookie):
        return True
    # The cookie may also hold the shared token itself: that is what the
    # ?token= link plants, and what a token sign-in stores. Dropping it here
    # signed out every script and bookmark that had one.
    supplied = request.headers.get(AUTH_HEADER) or request.query.get("token") or cookie
    return _token_ok(supplied)


def current_user(request: web.Request) -> str:
    """Who the request is, or "" when it authenticated with the shared token."""
    return read_session(accounts_of(request), request.cookies.get(AUTH_COOKIE)) or ""


def _set_session(response: web.Response, token: str, remember: bool = True) -> None:
    """Attach the session cookie to a response."""
    response.set_cookie(
        AUTH_COOKIE,
        token,
        httponly=True,
        samesite="Strict",
        max_age=60 * 60 * 24 * SESSION_DAYS if remember else None,
    )


def _throttled(address: str) -> bool:
    """Record a failed attempt and report whether the caller is now locked out."""
    now = time.monotonic()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(address, []) if now - t < LOGIN_WINDOW]
    attempts.append(now)
    _LOGIN_ATTEMPTS[address] = attempts
    return len(attempts) > LOGIN_MAX_ATTEMPTS


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Gate every request on the configured shared secret.

    The API can spend tracker budget, read an authenticated Deezer session and
    launch uploads, so on any non-loopback bind it must not be open. Browsers
    get bounced to a login page; API clients get a 401 they can act on.
    """
    if request.path.startswith("/static") or request.path in AUTH_EXEMPT_PATHS:
        return await handler(request)

    # Nobody has signed up yet: everything goes to setup, so a fresh instance is
    # never briefly open while you get round to configuring it.
    if accounts_of(request).empty and not cfg.upload.web_interface.auth_token:
        if request.path.startswith("/api/"):
            return error("setup required", status=401)
        raise web.HTTPFound("/login")

    if is_authenticated(request):
        response = await handler(request)
        # A ?token= link still works, for bookmarks and scripts. Upgrade it to a
        # cookie so the SPA's own fetches do not need to carry it.
        if request.query.get("token") and not request.cookies.get(AUTH_COOKIE):
            _set_session(response, request.query["token"])
        return response

    if request.path.startswith("/api/"):
        return error("authentication required", status=401)

    target = request.path_qs
    raise web.HTTPFound(f"/login?next={quote(target, safe='')}" if target != "/" else "/login")


def json_response(data: Any, status: int = 200) -> web.Response:
    """Serialize a payload with msgspec and return it as JSON."""
    return web.Response(body=msgspec.json.encode(data), status=status, content_type="application/json")


def error(message: str, status: int = 400) -> web.Response:
    """Return a JSON error body."""
    return json_response({"error": message}, status=status)


def allowed_roots(app: web.Application) -> list[str]:
    """Directories the API is willing to read or upload from.

    Everything the UI operates on lives under the download directory or the
    seeding directory. Anything else is out of bounds.
    """
    roots = [app["downloader"].download_dir]
    if cfg.linking.link_dir:
        roots.append(cfg.linking.link_dir)
    if cfg.directory.download_directory not in roots:
        roots.append(cfg.directory.download_directory)
    return [os.path.realpath(r) for r in roots if r]


def resolve_release_path(app: web.Application, folder: str) -> str:
    """Validate a client-supplied folder path.

    The upload endpoint takes a path from the browser and hands it to a
    subprocess, so it must not be possible to walk out of the managed
    directories and point lox at, say, an SSH key directory.

    Args:
        app: The web application, for the configured roots.
        folder: Path supplied by the client.

    Returns:
        The resolved absolute path.

    Raises:
        ValueError: If the path is missing, not a directory, or outside the
            allowed roots.
    """
    if not folder:
        raise ValueError("folder is required")
    resolved = os.path.realpath(folder)
    if not os.path.isdir(resolved):
        raise ValueError(f"{folder} is not a directory")
    for root in allowed_roots(app):
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    raise ValueError("folder is outside the download and seeding directories")


def binds_publicly(host: str) -> bool:
    """True when the configured host is reachable from outside this machine."""
    if host in ("", "*"):
        return True
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname rather than an address; assume it resolves off-box.
        return host not in ("localhost",)


async def setup_services(app: web.Application) -> None:
    """Attach the shared service objects to the application."""
    debug.configure()
    gw = DeezerGW()
    gateway = TrackerGateway()
    store = CheckerStore()

    app["gw"] = gw
    app["explorer"] = Explorer(gw)
    app["downloader"] = Downloader(gw)
    app["gateway"] = gateway
    app["store"] = store
    app["scanner"] = MissingScanner(gw, gateway, store)
    app["request_checker"] = DeezerRequestChecker(gw, gateway, store)
    app["watchlists"] = WatchlistManager(gw, store)
    app["notifier"] = DiscordNotifier()
    app["jobs"] = JobRegistry()
    app["flows"] = FlowRegistry()
    # Beside settings.toml, so accounts live on the same mounted volume as
    # everything else the UI writes.
    app["accounts"] = AccountStore(os.path.dirname(settings.path))


async def teardown_services(app: web.Application) -> None:
    """Close service objects when the server stops."""
    downloader: Downloader = app["downloader"]
    await downloader.stop()
    gw: DeezerGW = app["gw"]
    await gw.close()


# ----------------------------------------------------------------------
# Status and config
# ----------------------------------------------------------------------


@routes.get("/api/status")
async def api_status(request: web.Request) -> web.Response:
    """Report Deezer auth state, tracker budgets and download counts."""
    gw: DeezerGW = request.app["gw"]
    gateway: TrackerGateway = request.app["gateway"]
    downloader: Downloader = request.app["downloader"]

    deezer_state: dict[str, Any] = {"configured": bool(gw.arl), "authenticated": gw.authenticated}
    if gw.arl and not gw.authenticated:
        try:
            await gw.login()
            deezer_state["authenticated"] = True
        except DeezerGWError as e:
            deezer_state["error"] = str(e)
    if gw.authenticated:
        deezer_state.update({"user_id": gw.user_id, "country": gw.country, "can_stream": bool(gw.license_token)})

    return json_response(
        {
            "deezer": deezer_state,
            "trackers": gateway.statuses(),
            "downloads": {
                "active": sum(1 for j in downloader.jobs.values() if j.status in ("queued", "running")),
                "total": len(downloader.jobs),
                "directory": downloader.download_dir,
                "format": downloader.preferred_format,
            },
            "notifications": {"enabled": request.app["notifier"].enabled},
            # The two upload switches worth flipping without leaving the page
            # you flip them for. Polled, so changing one on the settings page
            # moves the toggle here and vice versa -- there is one setting, not
            # a copy per page.
            "upload": {"dry_run": cfg.upload.dry_run, "yes_all": cfg.upload.yes_all},
            # Misconfiguration no longer stops the server starting, so it has to
            # be visible somewhere you will actually look.
            "problems": config_problems(),
        }
    )


@routes.get("/api/health")
async def api_health(request: web.Request) -> web.Response:
    """Liveness, with nothing in it worth authenticating for.

    The container healthcheck used to call /api/status with the shared token,
    which stopped working the moment that token became optional. This says only
    that the process is answering.
    """
    return json_response({"ok": True})


@routes.get("/api/auth/state")
async def api_auth_state(request: web.Request) -> web.Response:
    """What the login page should ask for. No authentication needed.

    A brand-new instance has no accounts, so the page offers to create the
    first one instead of asking for a password nobody has set yet.
    """
    store = accounts_of(request)
    return json_response(
        {
            "setup": store.empty,
            "token_accepted": bool(cfg.upload.web_interface.auth_token),
            "min_password": MIN_PASSWORD,
        }
    )


@routes.post("/api/auth/setup")
async def api_auth_setup(request: web.Request) -> web.Response:
    """Create the first account.

    Only while there are none: once one exists this is closed, so it cannot be
    used to add a second account from outside.
    """
    store = accounts_of(request)
    if not store.empty:
        return error("Setup is already done. Sign in instead.", status=409)

    body = await request.json()
    try:
        account = store.create(str(body.get("username") or ""), str(body.get("password") or ""))
    except AccountError as e:
        return error(str(e))

    debug.log("created the first account, username=%s", account.username, level=20)
    response = json_response({"ok": True, "username": account.username})
    _set_session(response, issue_session(store, account.username), True)
    return response


@routes.post("/api/auth")
async def api_auth(request: web.Request) -> web.Response:
    """Sign in with a username and password, or with the shared token.

    Outside the auth middleware, so it is the one place a wrong password can be
    submitted -- hence the throttle and the deliberate delay on failure.
    """
    address = request.remote or "unknown"
    body = await request.json()
    store = accounts_of(request)

    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    remember = bool(body.get("remember", True))

    if username or password:
        account = await asyncio.to_thread(store.verify, username, password)
        if account is not None:
            _LOGIN_ATTEMPTS.pop(address, None)
            response = json_response({"ok": True, "username": account.username})
            _set_session(response, issue_session(store, account.username, remember), remember)
            return response
    else:
        # The old shape: a bare token, from a script or a saved bookmark.
        token = str(body.get("token") or "")
        if _token_ok(token):
            _LOGIN_ATTEMPTS.pop(address, None)
            response = json_response({"ok": True})
            _set_session(response, token, remember)
            return response

    if _throttled(address):
        return error("Too many attempts. Wait a few minutes and try again.", status=429)
    await asyncio.sleep(LOGIN_FAIL_DELAY)
    return error("That username and password were not accepted.", status=401)


@routes.get("/api/accounts")
async def api_accounts(request: web.Request) -> web.Response:
    """Who can sign in, and who you are."""
    store = accounts_of(request)
    return json_response(
        {"accounts": store.usernames(), "you": current_user(request), "min_password": MIN_PASSWORD}
    )


@routes.post("/api/accounts")
async def api_accounts_create(request: web.Request) -> web.Response:
    """Add another account. Requires being signed in."""
    body = await request.json()
    try:
        account = accounts_of(request).create(
            str(body.get("username") or ""), str(body.get("password") or "")
        )
    except AccountError as e:
        return error(str(e))
    return json_response({"ok": True, "username": account.username})


@routes.post("/api/accounts/password")
async def api_accounts_password(request: web.Request) -> web.Response:
    """Change a password.

    Changing your own requires the current one, so a borrowed browser cannot
    lock you out of your own instance. Every existing session ends either way,
    including this one, because the signing key is derived from the hashes.
    """
    body = await request.json()
    store = accounts_of(request)
    you = current_user(request)
    target = str(body.get("username") or you)
    if not target:
        return error("Signed in with the shared token; name the account to change.")

    if target.lower() == you.lower():
        current = str(body.get("current") or "")
        if await asyncio.to_thread(store.verify, target, current) is None:
            await asyncio.sleep(LOGIN_FAIL_DELAY)
            return error("That is not your current password.", status=401)

    try:
        store.set_password(target, str(body.get("password") or ""))
    except AccountError as e:
        return error(str(e))

    response = json_response({"ok": True, "signed_out": True})
    if target.lower() == you.lower():
        response = json_response({"ok": True, "signed_out": True})
        _set_session(response, issue_session(store, target), True)
    return response


@routes.post("/api/accounts/delete")
async def api_accounts_delete(request: web.Request) -> web.Response:
    """Remove an account. The last one cannot be removed."""
    body = await request.json()
    try:
        accounts_of(request).delete(str(body.get("username") or ""))
    except AccountError as e:
        return error(str(e))
    return json_response({"ok": True})


@routes.post("/api/auth/logout")
async def api_logout(request: web.Request) -> web.Response:
    """Clear the session cookie."""
    response = json_response({"ok": True})
    response.del_cookie(AUTH_COOKIE)
    return response


@routes.get("/api/auth/status")
async def api_auth_status(request: web.Request) -> web.Response:
    """Report whether authentication is on, and whether this caller has it."""
    return json_response({"auth_required": auth_required(), "authenticated": is_authenticated(request)})


@routes.get("/api/trackers")
async def api_trackers(request: web.Request) -> web.Response:
    """Return per-tracker budget and circuit-breaker state."""
    return json_response({"trackers": request.app["gateway"].statuses()})


@routes.get("/api/config")
async def api_config(request: web.Request) -> web.Response:
    """Return the non-secret parts of the config the UI displays."""
    checker = cfg.checker
    return json_response(
        {
            "download_directory": request.app["downloader"].download_dir,
            "preferred_format": request.app["downloader"].preferred_format,
            "trackers": TrackerGateway.configured_trackers(),
            "checker": {
                "tracker_budget": checker.tracker_budget,
                "tracker_budget_window": checker.tracker_budget_window,
                "min_tracks": checker.min_tracks,
                "min_date": checker.min_date,
                "max_date": checker.max_date,
                "min_confidence": checker.min_confidence,
                "album_recheck_after_days": getattr(checker, "album_recheck_after_days", 30),
            },
            "arl_set": bool(request.app["gw"].arl),
            "discogs_set": bool(cfg.metadata.discogs_token),
        }
    )


# ----------------------------------------------------------------------
# Search and album detail
# ----------------------------------------------------------------------


@routes.get("/api/search")
async def api_search(request: web.Request) -> web.Response:
    """Search Deezer. Costs no tracker budget.

    ``type`` defaults to every kind at once, because searching for a name and
    being shown only albums hides the thing you were looking for when it was an
    artist. The three searches are independent, so they run together.
    """
    query = (request.query.get("q") or "").strip()
    if not query:
        return error("q is required")
    kind = request.query.get("type", "all")
    limit = min(int(request.query.get("limit", 30)), 100)
    gw: DeezerGW = request.app["gw"]
    explorer: Explorer = request.app["explorer"]

    async def albums() -> list[dict[str, Any]]:
        return [explorer.public_album(a) for a in await gw.search_albums(query, limit)]

    async def tracks() -> list[dict[str, Any]]:
        return [
            {
                "type": "track",
                "id": str(t.get("id")),
                "title": t.get("title", ""),
                "artist": (t.get("artist") or {}).get("name", ""),
                "image": (t.get("album") or {}).get("cover_medium"),
                "album_id": str((t.get("album") or {}).get("id") or ""),
                "duration": t.get("duration"),
            }
            for t in await gw.search_tracks(query, limit)
        ]

    async def artists() -> list[dict[str, Any]]:
        return [
            {
                "type": "artist",
                "id": str(a.get("id")),
                "title": a.get("name", ""),
                "artist": "",
                "image": a.get("picture_medium"),
                "albums": a.get("nb_album"),
            }
            for a in await gw.search_artists(query, limit)
        ]

    wanted = {"album": albums, "track": tracks, "artist": artists}
    if kind in wanted:
        wanted = {kind: wanted[kind]}

    try:
        found = await asyncio.gather(*(fn() for fn in wanted.values()))
    except DeezerGWError as e:
        return error(str(e), status=502)

    sections = dict(zip(wanted, found, strict=True))
    return json_response(
        {
            "query": query,
            "type": kind,
            "sections": sections,
            # Flat list too, so a caller that wants one kind keeps working.
            "results": [row for rows in sections.values() for row in rows],
        }
    )


async def _featured_artists(gw: DeezerGW, album_id: str, album_artist: str) -> dict[str, list[dict[str, str]]]:
    """Map each track ID to the artists credited on it besides the main one.

    The per-track artist list is preferred over the contributor roles because
    it carries Deezer IDs, which is what makes a featured credit somewhere you
    can click through to rather than just a name. The role list is the fallback
    for releases that only spell it that way, and those names come back without
    an ID rather than with a guessed one.

    Args:
        gw: Authenticated Deezer client.
        album_id: Album to read.
        album_artist: The headline artist, excluded from every track's list.

    Returns:
        Track ID to a list of ``{"id", "name"}``, the id possibly empty.
    """
    featured: dict[str, list[dict[str, str]]] = {}
    for track in await gw.album_tracks(album_id):
        track_id = str(track.get("SNG_ID") or "")
        if not track_id:
            continue

        main = {album_artist.casefold(), str(track.get("ART_NAME") or "").casefold()}
        people: list[dict[str, str]] = []
        seen: set[str] = set()
        for artist in track.get("ARTISTS") or []:
            name = str(artist.get("ART_NAME") or "")
            if not name or name.casefold() in main or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            people.append({"id": str(artist.get("ART_ID") or ""), "name": name})

        if not people:
            contributors = track.get("SNG_CONTRIBUTORS") or {}
            if isinstance(contributors, dict):
                for role in ("featured", "featuring"):
                    for value in contributors.get(role) or []:
                        name = str(value or "")
                        if name and name.casefold() not in seen:
                            seen.add(name.casefold())
                            people.append({"id": "", "name": name})

        if people:
            featured[track_id] = people
    return featured


@routes.get("/api/album/{album_id}")
async def api_album(request: web.Request) -> web.Response:
    """Return album detail plus the FLAC/streamability verdict."""
    album_id = request.match_info["album_id"]
    gw: DeezerGW = request.app["gw"]
    try:
        meta = await gw.album(album_id)
    except DeezerGWError as e:
        return error(str(e), status=502)

    availability: dict[str, Any] | None = None
    availability_error: str | None = None
    featured_by_track: dict[str, list[dict[str, str]]] = {}
    if gw.arl:
        try:
            result = await gw.availability(album_id)
            detail: dict[str, Any] = msgspec.to_builtins(result)
            detail["uploadable"] = result.uploadable
            detail["reason"] = result.reason()
            availability = detail
        except DeezerGWError as e:
            availability_error = str(e)

        # Who else is on each track. The public album endpoint names only the
        # main artist per track, so a featured credit -- the thing you check a
        # tracklist for -- is invisible there. The private records carry the
        # full cast, at the cost of one more call, and only when there is an ARL
        # to make it with.
        try:
            featured_by_track = await _featured_artists(gw, album_id, (meta.get("artist") or {}).get("name") or "")
        except DeezerGWError as e:
            debug.log("featured artists unavailable for album %s: %s", album_id, e, level=30)

    return json_response(
        {
            "id": str(meta.get("id")),
            "title": meta.get("title"),
            "artist": (meta.get("artist") or {}).get("name"),
            "artist_id": str((meta.get("artist") or {}).get("id") or ""),
            # Everyone credited on the release, which is what "featured artists"
            # actually means. Each one links through to their own page.
            "contributors": [
                {"id": str(c.get("id")), "name": c.get("name"), "role": c.get("role") or ""}
                for c in (meta.get("contributors") or [])
                if c.get("id")
            ],
            "cover": meta.get("cover_xl") or meta.get("cover_big") or meta.get("cover"),
            "release_date": meta.get("release_date"),
            "record_type": meta.get("record_type"),
            "nb_tracks": meta.get("nb_tracks"),
            "duration": meta.get("duration"),
            "label": meta.get("label"),
            "upc": meta.get("upc"),
            "explicit": meta.get("explicit_lyrics"),
            "genres": [g.get("name") for g in (meta.get("genres") or {}).get("data", [])],
            "url": meta.get("link"),
            "tracks": [
                {
                    "id": str(t.get("id")),
                    "title": t.get("title"),
                    "artist": (t.get("artist") or {}).get("name"),
                    "artist_id": str((t.get("artist") or {}).get("id") or ""),
                    "duration": t.get("duration"),
                    # An album's tracklist does not carry track_position -- that
                    # field only exists on a track fetched on its own -- so this
                    # was null for every row and the # column came out empty.
                    # The list arrives in order, so its own index is the number.
                    "number": t.get("track_position") or position,
                    "disc": t.get("disk_number") or 1,
                    "explicit": t.get("explicit_lyrics"),
                    "featured": featured_by_track.get(str(t.get("id")), []),
                }
                for position, t in enumerate((meta.get("tracks") or {}).get("data", []), 1)
            ],
            "availability": availability,
            "availability_error": availability_error,
        }
    )


#: How many held-back rows to send with the queue. The count is always exact;
#: this bounds the sample the page can show beside it.
HELD_SAMPLE = 200

#: Why a release is not in the queue, in the order the page should say it.
#: The key is matched against the reason the rule produced; ``fix`` is what the
#: user can actually do about it, and None means nothing -- which is worth
#: saying rather than implying a setting exists.
HELD_KINDS: tuple[tuple[str, str, str, str | None], ...] = (
    ("nothing_to_do", "already on every tracker",
     "are already on every tracker that was checked", None),
    ("unproven", "not checked yet",
     "have not been checked against Deezer", "recheck"),
    ("lossy", "not all FLAC",
     "have no lossless source on Deezer, and no open request accepts lossy", None),
    ("unavailable", "tracks can be downloaded",
     "cannot be downloaded in full from Deezer", None),
    ("unreleased", "not released yet",
     "are not released yet", None),
)


def _held_groups(held: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count the held rows by what is keeping them out.

    Args:
        held: Rows the rules excluded, each carrying ``held_reason``.

    Returns:
        One entry per non-empty group, plus whatever is left over, which is the
        only group the settings page can change.
    """
    counts: dict[str, int] = {}
    for row in held:
        reason = row.get("held_reason") or ""
        for key, needle, _label, _fix in HELD_KINDS:
            if needle in reason:
                counts[key] = counts.get(key, 0) + 1
                break
        else:
            counts["rules"] = counts.get("rules", 0) + 1

    groups = [
        {"key": key, "label": label, "count": counts[key], "fix": fix}
        for key, _needle, label, fix in HELD_KINDS
        if counts.get(key)
    ]
    if counts.get("rules"):
        groups.append({"key": "rules", "label": "your queue rules",
                       "count": counts["rules"], "fix": "settings"})
    return groups


HISTORY_LIMIT = 2000
"""How many checked requests one page of history returns. The count is always
the real one; this caps what travels."""

#: Suffix to multiplier, for turning "1.49 GB" back into a number to compare.
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}


def _reached_a_tracker(entry: dict[str, Any]) -> bool:
    """Whether a scan record got as far as asking a tracker about it.

    Args:
        entry: A stored album record.

    Returns:
        True when some tracker gave a verdict -- has it, or does not.
    """
    return bool(entry.get("found_on") or entry.get("missing_from"))


def _bounty_bytes(bounty: Any) -> float:
    """A stored bounty as a number, for filtering and sorting.

    Bounties are kept as the string the tracker showed -- "1.49 GB" -- because
    that is what goes on screen. Comparing those as text puts 900 MB above 1 TB.

    Args:
        bounty: The stored bounty, e.g. ``"25.00 GB"``.

    Returns:
        Bytes, or 0.0 when there is nothing to read.
    """
    if isinstance(bounty, (int, float)):
        return float(bounty)
    parts = str(bounty or "").strip().split()
    if len(parts) != 2:
        return 0.0
    try:
        return float(parts[0]) * _UNITS.get(parts[1].upper(), 0)
    except ValueError:
        return 0.0


def _as_year(value: Any) -> float:
    """A stored year as a number, or 0 when it is missing or not one."""
    try:
        return float(str(value)[:4])
    except (TypeError, ValueError):
        return 0.0



@routes.get("/api/found")
async def api_found(request: web.Request) -> web.Response:
    """Everything a check has matched to a Deezer release. No tracker calls.

    Scans and request checks both end up knowing "this release exists on Deezer
    and is not on that tracker", and both threw it away as soon as you left the
    tab. Kept here so the work already paid for is somewhere you can act on.
    """
    store: CheckerStore = request.app["store"]
    # Keyed by Deezer album id, because a release found by a scan and matched
    # to a request is ONE release. It used to be two rows -- identical title,
    # identical tracker tags, one saying "scan" and one saying "request" --
    # which is two of everything to read, tick and act on for one upload.
    by_album: dict[str, dict[str, Any]] = {}
    dismissed = store.load("dismissed") or {}

    def merge(album_id: str, row: dict[str, Any]) -> None:
        """Fold a row into whatever is already known about that release."""
        existing = by_album.get(album_id)
        if existing is None:
            by_album[album_id] = row
            return
        # Tracker facts are unions: a scan that checked RED and a request check
        # that checked OPS between them know about both.
        for key in ("missing_from", "found_on"):
            existing[key] = sorted({*existing.get(key, ()), *row.get(key, ())})
        # A release that fills a request is a request row, whichever arrived
        # first: the request is the more useful thing to say about it, and it
        # carries the link and the bounty.
        if row.get("kind") == "request":
            for key in ("tracker", "bounty", "request_url", "confidence", "request_id"):
                if row.get(key):
                    existing[key] = row[key]
            existing["kind"] = "request"
        existing["sources"] = sorted({*existing.get("sources", ()), *row.get("sources", ())})
        # Deezer facts are the same release's facts whichever check found them,
        # so whichever row actually looked wins over the one that did not.
        for key in ("all_flac", "flac_count", "deezer_tracks"):
            if existing.get(key) is None and row.get(key) is not None:
                existing[key] = row[key]
        for key in ("deezer_unavailable", "release_date", "blocked"):
            if not existing.get(key) and row.get(key):
                existing[key] = row[key]
        # A request's terms only ever come from the request row.
        for key in ("request_formats", "request_encodings"):
            if row.get(key):
                existing[key] = row[key]
        existing["title"] = existing.get("title") or row.get("title") or ""
        existing["artist"] = existing.get("artist") or row.get("artist") or ""
        # The newest check is the one the "last checked" column should quote,
        # and the oldest sighting is the one "added" should: a release a scan
        # found in June and a request check matched today has been waiting
        # since June.
        if (row.get("checked_at") or 0) > (existing.get("checked_at") or 0):
            existing["checked_at"] = row.get("checked_at")
        if row.get("added_at") and (
            not existing.get("added_at") or row["added_at"] < existing["added_at"]
        ):
            existing["added_at"] = row["added_at"]

    for album_id, entry in (store.load("albums") or {}).items():
        # Whether "missing from nothing" is worth showing is a queue rule now,
        # not a fact of this loop. Deciding it here made the setting that turns
        # that floor off unable to do anything.
        if entry.get("uploaded_at") or album_id in dismissed:
            continue
        # The album collection is two things wearing one name: releases that
        # were checked against a tracker, and a note-to-self for every album
        # the scanner gave up on so it does not try again. The second kind has
        # no title, no artist and no verdict -- an album Deezer answered
        # DATA_ERROR for is not a release anybody can act on -- and it was
        # being listed here anyway, as a row with an em dash where the name
        # goes and "not checked against any tracker yet" as the explanation.
        #
        # Two thirds of one real queue was that. Those records belong to the
        # scan, which reports them; a release reaches this page when a tracker
        # has actually answered about it.
        if not _reached_a_tracker(entry):
            continue
        merge(
            album_id,
            {
                "kind": "scan",
                "id": album_id,
                "album_id": album_id,
                "sources": ["scan"],
                "title": entry.get("title") or "",
                "artist": entry.get("artist") or "",
                "missing_from": entry.get("missing_from") or [],
                "found_on": entry.get("found_on") or [],
                "checked_at": entry.get("checked_at"),
                "added_at": entry.get("first_seen") or entry.get("checked_at"),
                "url": f"https://www.deezer.com/album/{album_id}",
                # What Deezer can actually supply. None means nobody looked,
                # which the queue treats as unproven rather than as fine.
                "all_flac": entry.get("all_flac"),
                "flac_count": entry.get("flac_count"),
                "deezer_tracks": entry.get("deezer_tracks"),
                # What Deezer will not supply, and why. Named rather than
                # counted, so the page can say which tracks are missing.
                "deezer_unavailable": entry.get("deezer_unavailable") or [],
                "release_date": entry.get("release_date") or "",
                "blocked": entry.get("blocked") or "",
            },
        )

    for request_id, entry in (store.load("requests") or {}).items():
        # A match the tracker already has is not something to upload, and
        # neither is a request that was already filled -- which is how filled
        # requests were reaching this page: the check ran the whole Deezer
        # pipeline against them, and when the "is it on the tracker" search
        # missed, they were filed here as worth uploading.
        if not entry.get("deezer_id") or entry.get("already_on_tracker") or entry.get("filled"):
            continue
        if entry.get("uploaded_at") or str(entry.get("deezer_id")) in dismissed or request_id in dismissed:
            continue
        album_id = str(entry.get("deezer_id"))
        merge(
            album_id,
            {
                "kind": "request",
                # The row is identified by the release, so acting on it acts on
                # the release. The request id rides along for the link.
                "id": album_id,
                "request_id": request_id,
                "album_id": album_id,
                "sources": ["request"],
                "title": entry.get("album") or entry.get("deezer_title") or "",
                "artist": entry.get("artist") or entry.get("deezer_artist") or "",
                "tracker": entry.get("tracker") or "",
                "bounty": entry.get("bounty") or "",
                # Which trackers have it and which do not, so the row says what
                # the last check actually found rather than only that it exists.
                "found_on": entry.get("found_on") or [],
                "missing_from": entry.get("missing_from") or [],
                "confidence": entry.get("confidence"),
                "request_url": entry.get("request_url") or "",
                "checked_at": entry.get("checked_at"),
                "added_at": entry.get("first_seen") or entry.get("checked_at"),
                "url": entry.get("deezer_url") or "",
                "all_flac": entry.get("all_flac"),
                "deezer_tracks": entry.get("deezer_tracks"),
                # What this request will accept. A release that is not all
                # FLAC is only queueable when one of these says so.
                "request_formats": entry.get("request_formats") or [],
                "request_encodings": entry.get("request_encodings") or [],
                "deezer_unavailable": entry.get("deezer_unavailable") or [],
                "release_date": entry.get("release_date") or "",
                "blocked": entry.get("blocked") or "",
            },
        )

    rows = sorted(by_album.values(), key=lambda r: r.get("checked_at") or 0, reverse=True)

    # The rules are applied here rather than when the check ran, so widening
    # them brings rows straight back instead of needing the tracker calls
    # again. Held rows are counted and returned rather than dropped, because a
    # queue that quietly got shorter is indistinguishable from a scan that
    # found nothing.
    rules = queue_rules.rules_from(cfg.checker)
    shown, held = queue_rules.partition(rows, rules)

    # A row excluded for something that will never change -- every tracker has
    # it, Deezer cannot supply it, it is not out yet -- is not waiting for
    # anything. Keeping it on the page produced a list of things nobody could
    # act on, which a re-check could not clear either: re-checking confirmed
    # the same answer and put the row straight back.
    #
    # Only what can still move stays: releases nobody has checked against
    # Deezer, and releases a queue rule is holding, which the rule can release.
    settled = [row for row in held if queue_rules.is_settled(row["held_reason"])]
    held = [row for row in held if not queue_rules.is_settled(row["held_reason"])]
    # Every album a scan ever looked at is a held row once the floor is on, so
    # the count is the honest number and the list is a sample of it. Sending
    # ten thousand rows to explain why they are not on screen would be its own
    # kind of rude.
    return json_response(
        {
            "found": shown,
            "held": held[:HELD_SAMPLE],
            "held_count": len(held),
            "held_shown": min(len(held), HELD_SAMPLE),
            # Counted but not listed: the page says how many were dropped for
            # good so the number is not simply missing.
            "settled_count": len(settled),
            "settled_groups": _held_groups(settled),
            # Split out, because "held back by your queue rules" is the wrong
            # thing to tell someone whose rows are held because nobody has
            # checked what Deezer can supply. One is a setting they chose; the
            # other is work waiting to be done, and they read very differently.
            # Grouped by what is actually keeping each one out, because only
            # one of these groups is a setting. Calling all of them "your queue
            # rules" sent people to Settings to widen a rule that had nothing
            # to do with it -- and for most of them there is no setting at all.
            "held_groups": _held_groups(held),
            "rule": rules.describe(),
            "blacklisted": sum(1 for d in dismissed.values() if d.get("blacklist")),
        }
    )


def _mark_uploaded(store: CheckerStore, album_id: str, folder: str, trackers: list[str]) -> None:
    """Record that a release has been uploaded, so Found stops offering it.

    Matched by Deezer id when the upload came from a Found row, and otherwise
    by folder name against the stored title -- an upload started from the
    Uploads tab has no id attached to it, and it is still the same release.
    """
    stamp = {"uploaded_at": time.time(), "uploaded_to": trackers}
    if album_id:
        for name in ("albums", "requests"):
            entry = store.get(name, album_id)
            if entry is not None:
                store.put(name, album_id, {**entry, **stamp}, flush=False)
        store.flush()
        return

    basename = os.path.basename(folder).lower()
    if not basename:
        return
    for name in ("albums", "requests"):
        for key, entry in list((store.load(name) or {}).items()):
            title = str(entry.get("title") or entry.get("album") or "").strip().lower()
            artist = str(entry.get("artist") or "").strip().lower()
            # Both have to be long enough to mean something. A one-letter
            # artist matches almost any folder name, and marking the wrong
            # release as uploaded is worse than missing one.
            if len(title) < 3 or len(artist) < 3:
                continue
            if title in basename and artist in basename:
                store.put(name, key, {**entry, **stamp}, flush=False)
    store.flush()


@routes.post("/api/found/dismiss")
async def api_found_dismiss(request: web.Request) -> web.Response:
    """Take a row off the Found list. No tracker calls.

    Two ways, because they answer different questions. "Not now" forgets the
    row, and a later scan that finds the release again puts it back. "Never"
    blacklists it, and no scan will list it again until it is un-blacklisted --
    which is what you want for a release you have decided you are not uploading.
    """
    body = await request.json()
    keys = [str(k) for k in (body.get("ids") or []) if str(k)]
    if not keys:
        return error("ids is required")
    blacklist = bool(body.get("blacklist"))

    store: CheckerStore = request.app["store"]

    # A row on this page is a RELEASE, identified by its Deezer album id, and
    # what is known about it can live in either collection or both -- a scan
    # found it, a request check matched it, one row. The requests collection is
    # keyed "OPS:80755" though, so deleting by album id deleted the scan half
    # and left the request half behind: the release came straight back as a row
    # with one source and a different reason, and removing it again did the
    # same thing. This maps the release back to whatever is filed under it.
    request_keys: dict[str, list[str]] = {}
    for request_key, entry in (store.load("requests") or {}).items():
        deezer_id = str(entry.get("deezer_id") or "")
        if deezer_id:
            request_keys.setdefault(deezer_id, []).append(request_key)

    for key in keys:
        if blacklist:
            store.put("dismissed", key, {"blacklist": True, "at": time.time()}, flush=False)
        else:
            # Forgotten rather than remembered as unwanted: dropping the check
            # result is what lets the next scan surface it again.
            store.delete("albums", key, flush=False)
            # Both spellings: the id as given, in case the caller had the
            # request's own key, and every request that matched this release.
            store.delete("requests", key, flush=False)
            for request_key in request_keys.get(key, ()):
                store.delete("requests", request_key, flush=False)
    store.flush()
    return json_response({"dismissed": len(keys), "blacklisted": blacklist})


@routes.post("/api/found/restore")
async def api_found_restore(request: web.Request) -> web.Response:
    """Un-blacklist releases so scans can list them again. No tracker calls."""
    body = await request.json()
    keys = [str(k) for k in (body.get("ids") or []) if str(k)]
    store: CheckerStore = request.app["store"]
    if not keys:
        removed = store.clear("dismissed")
    else:
        removed = 0
        for key in keys:
            if store.get("dismissed", key):
                store.delete("dismissed", key, flush=False)
                removed += 1
        store.flush()
    return json_response({"restored": removed})


@routes.get("/api/album/{album_id}/check")
async def api_album_check_saved(request: web.Request) -> web.Response:
    """Return the last stored check for an album. Contacts no tracker.

    A check costs budget, and the answer to "is this already on RED" does not
    change minute to minute, so once it has been asked the answer is kept and
    shown again for free. Asking again stays a deliberate press.
    """
    scanner: MissingScanner = request.app["scanner"]
    return json_response({"check": scanner.saved_album_check(request.match_info["album_id"])})


@routes.post("/api/album/{album_id}/check")
async def api_album_check(request: web.Request) -> web.Response:
    """Check one album against the trackers, keeping every group it inspected.

    This is the per-album flow: check, read the links it found, then decide
    whether to upload. It costs more budget than the batch scanner because it
    does not stop at the first match — the near misses are the point.
    """
    album_id = request.match_info["album_id"]
    body = await request.json() if request.can_read_body else {}
    trackers = body.get("trackers") or TrackerGateway.configured_trackers()
    if not trackers:
        return error("no trackers configured")

    scanner: MissingScanner = request.app["scanner"]
    jobs: JobRegistry = request.app["jobs"]

    async def run(job) -> None:
        check = await scanner.check_album(album_id, trackers)
        job.results.append(check.as_dict())

    job = jobs.spawn("album_check", f"Checking album {album_id} on {', '.join(trackers)}", run)
    return json_response({"job_id": job.id})


@routes.get("/api/artist/{artist_id}")
async def api_artist(request: web.Request) -> web.Response:
    """Return an artist with their discography grouped by release type."""
    try:
        return json_response(await request.app["explorer"].artist(request.match_info["artist_id"]))
    except DeezerGWError as e:
        return error(str(e), status=502)


@routes.get("/api/artist/{artist_id}/albums")
async def api_artist_albums(request: web.Request) -> web.Response:
    """List an artist's discography."""
    try:
        albums = await request.app["explorer"].artist_albums(request.match_info["artist_id"])
    except DeezerGWError as e:
        return error(str(e), status=502)
    return json_response({"results": albums})


@routes.get("/api/playlist/{playlist_id}/albums")
async def api_playlist_albums(request: web.Request) -> web.Response:
    """Collapse a playlist into the distinct albums it references."""
    try:
        albums = await request.app["explorer"].playlist_albums(request.match_info["playlist_id"])
    except DeezerGWError as e:
        return error(str(e), status=502)
    return json_response({"results": albums})


# ----------------------------------------------------------------------
# Explore
# ----------------------------------------------------------------------


@routes.get("/api/explore/channels")
async def api_channels(request: web.Request) -> web.Response:
    """List Deezer's channels. Requires an ARL."""
    try:
        return json_response({"channels": await request.app["explorer"].channels()})
    except DeezerGWError as e:
        return error(str(e), status=502)


@routes.get("/api/explore/channel/{slug}")
async def api_channel(request: web.Request) -> web.Response:
    """Fetch one channel and its modules."""
    try:
        return json_response(await request.app["explorer"].channel(request.match_info["slug"]))
    except DeezerGWError as e:
        return error(str(e), status=502)


@routes.get("/api/explore/module/{module_id}")
async def api_module(request: web.Request) -> web.Response:
    """Fetch one channel module by ID."""
    try:
        return json_response(await request.app["explorer"].module(request.match_info["module_id"]))
    except DeezerGWError as e:
        return error(str(e), status=502)


@routes.get("/api/explore/genres")
async def api_genres(request: web.Request) -> web.Response:
    """List editorial genres."""
    try:
        return json_response({"genres": await request.app["explorer"].genres()})
    except DeezerGWError as e:
        return error(str(e), status=502)


@routes.get("/api/explore/charts")
async def api_charts(request: web.Request) -> web.Response:
    """Fetch the chart for a genre (0 for the global chart)."""
    genre = request.query.get("genre", "0")
    try:
        return json_response(await request.app["explorer"].chart(genre))
    except DeezerGWError as e:
        return error(str(e), status=502)


@routes.get("/api/explore/releases")
async def api_releases(request: web.Request) -> web.Response:
    """Fetch editorial new releases for a genre."""
    genre = request.query.get("genre", "0")
    try:
        return json_response(await request.app["explorer"].new_releases(genre))
    except DeezerGWError as e:
        return error(str(e), status=502)


# ----------------------------------------------------------------------
# Saved searches
# ----------------------------------------------------------------------


@routes.get("/api/watchlists")
async def api_watchlists(request: web.Request) -> web.Response:
    """List saved Deezer searches."""
    manager: WatchlistManager = request.app["watchlists"]
    return json_response({"watchlists": [w.as_dict() for w in manager.saved()]})


@routes.post("/api/watchlists")
async def api_watchlist_create(request: web.Request) -> web.Response:
    """Save a new Deezer search."""
    body = await request.json()
    kind = body.get("kind")
    if kind not in ("new_releases", "chart", "search", "artist", "playlist", "module"):
        return error("kind must be one of new_releases, chart, search, artist, playlist, module")
    manager: WatchlistManager = request.app["watchlists"]
    watch = manager.create(body.get("name", ""), kind, body.get("target", "0"), int(body.get("limit", 50)))
    return json_response(watch.as_dict())


@routes.delete("/api/watchlists/{watch_id}")
async def api_watchlist_delete(request: web.Request) -> web.Response:
    """Delete a saved search."""
    return json_response({"deleted": request.app["watchlists"].delete(request.match_info["watch_id"])})


@routes.post("/api/watchlists/{watch_id}/run")
async def api_watchlist_run(request: web.Request) -> web.Response:
    """Run a saved search. Deezer only, no tracker budget spent."""
    manager: WatchlistManager = request.app["watchlists"]
    try:
        albums = await manager.run(request.match_info["watch_id"])
    except KeyError:
        return error("no such watchlist", status=404)
    except DeezerGWError as e:
        return error(str(e), status=502)
    return json_response({"results": albums})


# ----------------------------------------------------------------------
# Downloads
# ----------------------------------------------------------------------


@routes.post("/api/download")
async def api_download(request: web.Request) -> web.Response:
    """Queue one or more albums for download."""
    body = await request.json()
    album_ids = body.get("album_ids") or ([body["album_id"]] if body.get("album_id") else [])
    if not album_ids:
        return error("album_id or album_ids is required")

    downloader: Downloader = request.app["downloader"]
    # Recreated here as well as at startup, so a volume that came back after a
    # remount does not need a restart. Every track would otherwise fail one at a
    # time with an errno that says nothing about which setting is wrong.
    problem = ensure_dir(downloader.download_dir, "Download directory")
    if problem:
        return error(f"{problem} Fix it under Settings → Paths, or check the volume mount.")

    queued, failed = [], []
    for album_id in album_ids:
        try:
            job = await downloader.enqueue(album_id)
            queued.append(job.as_dict())
        except Exception as e:  # noqa: BLE001 - reported per album
            failed.append({"album_id": str(album_id), "error": str(e)})
    return json_response({"queued": queued, "failed": failed})


@routes.get("/api/downloads")
async def api_downloads(request: web.Request) -> web.Response:
    """List every download job, newest first."""
    downloader: Downloader = request.app["downloader"]
    jobs = sorted(downloader.jobs.values(), key=lambda j: j.started or 0, reverse=True)
    return json_response({"jobs": [j.as_dict() for j in jobs]})


@routes.post("/api/downloads/{job_id}/cancel")
async def api_download_cancel(request: web.Request) -> web.Response:
    """Withdraw a download that has not started yet."""
    cancelled = request.app["downloader"].cancel(request.match_info["job_id"])
    return json_response({"cancelled": cancelled})


@routes.post("/api/downloads/clear")
async def api_downloads_clear(request: web.Request) -> web.Response:
    """Drop finished download jobs from the list."""
    return json_response({"cleared": request.app["downloader"].clear_finished()})


# ----------------------------------------------------------------------
# Missing-album scanning
# ----------------------------------------------------------------------


@routes.post("/api/missing/collect")
async def api_missing_collect(request: web.Request) -> web.Response:
    """Expand playlists and modules into filtered candidates. No tracker calls."""
    body = await request.json()
    sources = [s for s in (body.get("sources") or []) if s.strip()]
    if not sources:
        return error("sources is required")
    skip_known = bool(body.get("skip_known", True))

    scanner: MissingScanner = request.app["scanner"]
    jobs: JobRegistry = request.app["jobs"]

    async def run(job) -> None:
        candidates = await scanner.collect(sources, progress=job.emit, skip_known=skip_known)
        job.results.extend(c.as_dict() for c in candidates)

    job = jobs.spawn("missing_collect", f"Collecting from {len(sources)} source(s)", run)
    return json_response({"job_id": job.id})


@routes.post("/api/missing/check")
async def api_missing_check(request: web.Request) -> web.Response:
    """Check collected candidates against the trackers. Spends tracker budget."""
    body = await request.json()
    raw_candidates = body.get("candidates") or []
    trackers = body.get("trackers") or TrackerGateway.configured_trackers()
    if not raw_candidates:
        return error("candidates is required")

    gateway: TrackerGateway = request.app["gateway"]
    unavailable = [t for t in trackers if not gateway.can_check(t)]
    if unavailable and not body.get("force"):
        return error(
            f"No budget available for {', '.join(unavailable)}. Wait for the window to roll over, or pass force.",
            status=429,
        )

    try:
        candidates = [msgspec.convert(c, Candidate) for c in raw_candidates]
    except msgspec.ValidationError as e:
        return error(f"bad candidate payload: {e}")

    scanner: MissingScanner = request.app["scanner"]
    notifier: DiscordNotifier = request.app["notifier"]
    jobs: JobRegistry = request.app["jobs"]
    by_id = {c.album_id: c for c in candidates}

    async def run(job) -> None:
        results = await scanner.check(candidates, trackers, progress=job.emit)
        if cfg.notifications.notify_missing and notifier.enabled:
            for result in results:
                if result.missing_from:
                    candidate = by_id.get(result.album_id)
                    if candidate:
                        await notifier.missing_album(result.as_dict(), candidate.as_dict())

    job = jobs.spawn("missing_check", f"Checking {len(candidates)} album(s) on {', '.join(trackers)}", run)
    return json_response({"job_id": job.id, "estimated_calls": scanner.estimate(candidates, trackers)})


# ----------------------------------------------------------------------
# Request checking
# ----------------------------------------------------------------------


@routes.get("/api/requests/list")
async def api_requests_list(request: web.Request) -> web.Response:
    """List open requests on a tracker.

    Costs one tracker call per page. The tracker sets the page size (25 on both
    RED and OPS), so asking for more than that costs more than one.
    """
    tracker = request.query.get("tracker", "")
    if not tracker:
        return error("tracker is required")

    try:
        # No small ceiling here: the number of pages is the user's call and the
        # tracker budget is what actually limits it. 500 was exactly 20 pages,
        # which silently capped anyone who asked for more.
        limit = max(1, min(25_000, int(request.query.get("limit", 25))))
        # The browser reads one page per call so it can show progress and be
        # stopped; this is which page it is asking for.
        start_page = max(1, int(request.query.get("start_page") or 1))
    except ValueError:
        return error("limit and start_page must be numbers")

    def flag(name: str) -> bool:
        return request.query.get(name, "") in ("1", "true", "yes", "on")

    def labels(name: str) -> list[str]:
        return [v for v in request.query.getall(name, []) if v]

    checker: DeezerRequestChecker = request.app["request_checker"]
    try:
        found = await checker.collect_requests(
            tracker,
            request.query.get("search", ""),
            limit=limit,
            tags=request.query.get("tags", ""),
            tags_all=flag("tags_all"),
            show_filled=flag("show_filled"),
            include_old=flag("include_old"),
            search_descriptions=flag("descriptions"),
            formats=labels("format"),
            media=labels("media"),
            encodings=labels("encoding"),
            release_types=labels("release_type"),
            strict_formats=flag("strict_format"),
            strict_media=flag("strict_media"),
            strict_encodings=flag("strict_encoding"),
            bounty_min=request.query.get("bounty_min", ""),
            bounty_max=request.query.get("bounty_max", ""),
            categories=labels("category"),
            start_page=start_page,
        )
    except Exception as e:  # noqa: BLE001 - budget and transport errors both surface here
        return error(str(e), status=502)
    return json_response(found)


@routes.get("/api/requests/detail")
async def api_request_detail(request: web.Request) -> web.Response:
    """Everything one request says, for the half of the split that shows it.

    Costs one tracker call. The tracker's own page would be the obvious thing
    to show and cannot be: RED and OPS both send ``X-Frame-Options``, so a
    browser will not render it inside another page no matter what we do. This
    returns the same record that page is built from instead.
    """
    tracker = request.query.get("tracker", "")
    raw_id = request.query.get("id", "")
    if not tracker or not raw_id:
        return error("tracker and id are required")
    try:
        request_id = int(raw_id)
    except ValueError:
        return error("id must be a number")

    gateway: TrackerGateway = request.app["gateway"]
    try:
        return json_response(await request_detail(gateway, tracker, request_id))
    except Exception as e:  # noqa: BLE001 - budget and transport errors both surface here
        return error(str(e), status=502)


@routes.get("/api/scan/history")
async def api_scan_history(request: web.Request) -> web.Response:
    """Every album a scan has looked up, and what it found. No tracker calls.

    A scan skips what is in here, so this is also the answer to "why did that
    scan do so little": the work was already paid for. Selecting rows and
    re-checking is how you ask again before the recheck window is up.
    """
    store: CheckerStore = request.app["store"]
    now = time.time()

    rows = []
    for album_id, entry in (store.load("albums") or {}).items():
        status = str(entry.get("status") or "")
        rows.append({
            "id": str(album_id),
            "album_id": str(album_id),
            "title": entry.get("title") or "",
            "artist": entry.get("artist") or "",
            "status": status,
            "outcome": _scan_outcome(entry),
            "reason": entry.get("reason") or entry.get("error") or "",
            "source": entry.get("source") or "",
            "found_on": entry.get("found_on") or [],
            "missing_from": entry.get("missing_from") or [],
            "all_flac": entry.get("all_flac"),
            "deezer_tracks": entry.get("deezer_tracks"),
            "release_date": entry.get("release_date") or "",
            "added_at": entry.get("first_seen") or entry.get("checked_at"),
            "checked_at": entry.get("checked_at"),
            "checked_days_ago": recheck.age_days(entry, now),
            "url": f"https://www.deezer.com/album/{album_id}",
        })

    rows.sort(key=lambda r: r.get("checked_at") or 0, reverse=True)
    return json_response({
        "albums": rows[:HISTORY_LIMIT],
        "total": len(rows),
        "shown": min(len(rows), HISTORY_LIMIT),
        "recheck_after_days": int(getattr(cfg.checker, "album_recheck_after_days", 30) or 0),
        "filters": {
            "min_tracks": cfg.checker.min_tracks,
            "min_date": cfg.checker.min_date or "",
            "max_date": cfg.checker.max_date or "",
        },
    })


#: What a stored album status means, in one phrase. The status itself carries
#: whichever trackers were configured -- "missing_ops_red" -- which is precise
#: and unreadable.
def _scan_outcome(entry: dict[str, Any]) -> str:
    """One phrase for what a scan concluded about an album.

    Args:
        entry: The stored album record.

    Returns:
        Something a column can group by.
    """
    status = str(entry.get("status") or "")
    if status.startswith("missing_"):
        return "Missing from a tracker"
    if status.startswith("exists_"):
        return "Already on every tracker"
    if status == "skipped_filter":
        return "Ruled out by a scan filter"
    if status == "skipped_no_flac":
        return "No lossless source"
    if status == "skipped_unreleased":
        return "Not released yet"
    if status.startswith("skipped_"):
        return "Nothing usable on Deezer"
    if status:
        return "Lookup failed"
    return "Unknown"


@routes.get("/api/requests/history")
async def api_requests_history(request: web.Request) -> web.Response:
    """Every request that has been checked, and what came of it. No tracker calls.

    The answers were already being stored -- they are what stops a second run
    paying for the same lookups -- but there was nowhere to read them. So a
    request checked last week was invisible: you could not see what it said, or
    that it had been checked at all, only that a new run went quiet about it.

    Filtering happens here rather than in the browser because the collection is
    every request ever checked and most of it is not what you are looking at.
    """
    store: CheckerStore = request.app["store"]

    def number(name: str, default: float | None = None) -> float | None:
        raw = request.query.get(name, "")
        try:
            return float(raw) if raw != "" else default
        except ValueError:
            return default

    want_status = {s for s in request.query.getall("status", []) if s}
    want_tracker = request.query.get("tracker", "")
    text = request.query.get("q", "").strip().lower()
    min_bounty = number("min_bounty")
    min_year, max_year = number("min_year"), number("max_year")
    checked_within = number("checked_within")   # days
    checked_before = number("checked_before")   # days
    now = time.time()

    rows = []
    for key, entry in (store.load("requests") or {}).items():
        # Keys are "TRACKER:ID". A key written before that convention has no
        # colon, and partition would hand back the whole thing as the tracker
        # and an empty id -- a row claiming to be tracker "r1" with no request
        # behind it, which cannot be re-run and reads as corruption.
        raw_key = str(key)
        tracker, sep, request_id = raw_key.partition(":")
        if not sep:
            tracker, request_id = "", raw_key
        age = recheck.age_days(entry, now)
        row = {
            "key": raw_key,
            "id": request_id,
            "tracker": tracker,
            "status": entry.get("status") or "",
            "reason": entry.get("reason") or "",
            "artist": entry.get("artist") or entry.get("deezer_artist") or "",
            "album": entry.get("album") or entry.get("deezer_title") or "",
            "year": entry.get("year") or "",
            "bounty": entry.get("bounty") or "",
            "bounty_bytes": _bounty_bytes(entry.get("bounty")),
            # When the request was posted on the tracker, and how long ago
            # that was. A request open for two years and one opened yesterday
            # are different propositions, and the page had no way to tell them
            # apart -- it only ever said when *we* last looked at it.
            "created": entry.get("created") or "",
            "created_age": age_of(entry.get("created")),
            "request_url": entry.get("request_url") or "",
            "deezer_id": entry.get("deezer_id") or "",
            "deezer_url": entry.get("deezer_url") or "",
            "confidence": entry.get("confidence"),
            "filled": bool(entry.get("filled")),
            "already_on_tracker": entry.get("already_on_tracker"),
            "all_flac": entry.get("all_flac"),
            "checked_at": entry.get("checked_at"),
            "checked_days_ago": age,
        }
        if want_status and row["status"] not in want_status:
            continue
        if want_tracker and row["tracker"] != want_tracker:
            continue
        if text and text not in f"{row['artist']} {row['album']} {row['id']}".lower():
            continue
        if min_bounty is not None and row["bounty_bytes"] < min_bounty:
            continue
        # A row with no year does not match a year filter either way. Letting
        # it through read as year 0, which is below every "from" and under
        # every "to", so undated rows turned up in every year search.
        if min_year is not None or max_year is not None:
            year = _as_year(row["year"])
            if not year:
                continue
            if min_year is not None and year < min_year:
                continue
            if max_year is not None and year > max_year:
                continue
        # "checked in the last N days" and "not checked for N days" are the two
        # ways anyone asks about this, so both are offered rather than one
        # range control that has to be reasoned about.
        if checked_within is not None and (age is None or age > checked_within):
            continue
        if checked_before is not None and (age is None or age < checked_before):
            continue
        rows.append(row)

    rows.sort(key=lambda r: r.get("checked_at") or 0, reverse=True)
    window = int(getattr(cfg.checker, "request_recheck_after_days", 30) or 0)
    return json_response({
        "requests": rows[:HISTORY_LIMIT],
        "total": len(rows),
        "shown": min(len(rows), HISTORY_LIMIT),
        "statuses": sorted({r["status"] for r in rows if r["status"]}),
        "recheck_after_days": window,
    })


@routes.get("/api/requests/filters")
async def api_requests_filters(request: web.Request) -> web.Response:
    """Describe the filters a tracker's request search takes. No tracker calls.

    The UI renders itself from this rather than from a fixed list, because the
    two trackers do not offer the same filters and do not agree on the IDs
    behind the ones they share.
    """
    tracker = request.query.get("tracker", "")
    if not tracker:
        return error("tracker is required")
    return json_response(filter_schema(
        tracker,
        recheck_after_days=int(getattr(cfg.checker, "request_recheck_after_days", 30) or 0),
    ))


@routes.post("/api/requests/check")
async def api_requests_check(request: web.Request) -> web.Response:
    """Check requests for a fillable Deezer source. Spends tracker budget."""
    body = await request.json()
    tracker = body.get("tracker")
    request_ids = [str(r).strip() for r in (body.get("request_ids") or []) if str(r).strip()]
    if not tracker or not request_ids:
        return error("tracker and request_ids are required")

    gateway: TrackerGateway = request.app["gateway"]
    if not gateway.can_check(tracker) and not body.get("force"):
        status = gateway.status(tracker).as_dict()
        return error(f"No budget available for {tracker} ({status['remaining']} left)", status=429)

    checker: DeezerRequestChecker = request.app["request_checker"]
    notifier: DiscordNotifier = request.app["notifier"]
    jobs: JobRegistry = request.app["jobs"]

    # "Run them anyway" -- the button offered next to the list of requests that
    # were skipped because they already had an answer.
    recheck_all = bool(body.get("recheck"))

    async def run(job) -> None:
        matches = await checker.check_many(tracker, request_ids, progress=job.emit, force=recheck_all)
        if cfg.notifications.notify_fillable and notifier.enabled:
            for match in matches:
                if match.fillable:
                    await notifier.fillable_request(match.as_dict())

    job = jobs.spawn("requests_check", f"Checking {len(request_ids)} {tracker} request(s)", run)
    return json_response({"job_id": job.id})


# ----------------------------------------------------------------------
# Uploading
# ----------------------------------------------------------------------


@routes.get("/spectral-image/{folder}/{name}")
async def api_spectral_image(request: web.Request) -> web.StreamResponse:
    """Serve one generated spectral image.

    Spectrals live in the scratch directory and are what the lossy-master
    question is actually about, so the browser has to be able to see them
    rather than being sent to a separate page.
    """
    folder = request.match_info["folder"]
    name = request.match_info["name"]
    if "/" in folder or "\\" in folder or os.path.basename(name) != name:
        return error("bad path", status=400)

    root = cfg.directory.tmp_dir or cfg.directory.download_directory
    path = os.path.realpath(os.path.join(root, folder, name))
    if not path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(path):
        return error("not found", status=404)
    return web.FileResponse(path)


@routes.post("/api/folders/delete")
async def api_folder_delete(request: web.Request) -> web.Response:
    """Delete a release folder.

    Irreversible, so it is gated the same way uploading is: the path has to
    resolve inside the download or seeding directories, which stops a crafted
    request from pointing lox at something else. It refuses to delete a root
    itself -- emptying your whole library should never be one request.
    """
    body = await request.json()
    try:
        path = resolve_release_path(request.app, body.get("folder", ""))
    except ValueError as e:
        return error(str(e))

    if path in allowed_roots(request.app):
        return error("that is the download directory itself, not a release in it")

    try:
        await asyncio.to_thread(shutil.rmtree, path)
    except OSError as e:
        return error(f"could not delete {path}: {e}", status=500)

    debug.log("deleted release folder %s", path, level=20)
    return json_response({"deleted": path})


@routes.get("/api/folders")
async def api_folders(request: web.Request) -> web.Response:
    """List release folders sitting in the download directory."""
    directory = request.app["downloader"].download_dir
    problem = ensure_dir(directory, "Download directory")
    if problem:
        # An empty list here would read as "nothing to upload" when the truth is
        # "lox cannot see your library".
        return json_response(
            {
                "directory": directory,
                "folders": [],
                "error": f"{problem} Check it under Settings → Paths.",
            }
        )

    def scan() -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                audio = 0
                size = 0
                for root, _dirs, files in os.walk(entry.path):
                    for name in files:
                        if name.lower().endswith((".flac", ".mp3")):
                            audio += 1
                            with contextlib.suppress(OSError):
                                size += os.path.getsize(os.path.join(root, name))
                found.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "tracks": audio,
                        "bytes": size,
                        "modified": entry.stat().st_mtime,
                    }
                )
        found.sort(key=lambda f: f["modified"], reverse=True)
        return found

    # Walking a large music directory is slow enough to stall the event loop.
    folders = await asyncio.to_thread(scan)
    return json_response({"directory": directory, "folders": folders, "linking": cfg.linking.enabled})


@routes.post("/api/upload")
async def api_upload(request: web.Request) -> web.Response:
    """Start an upload as an interactive flow.

    The pipeline runs in this process with its prompts redirected into the
    flow, so the browser answers questions with real controls instead of typing
    into a terminal.
    """
    body = await request.json()
    try:
        folder = resolve_release_path(request.app, body.get("folder", ""))
    except ValueError as e:
        return error(str(e))

    trackers = body.get("trackers") or ([body["tracker"]] if body.get("tracker") else [])
    trackers = [t for t in trackers if t in TrackerGateway.configured_trackers()]
    if not trackers:
        return error("at least one configured tracker is required")

    source = body.get("source", "WEB")
    auto_rename = bool(body.get("auto_rename", False))
    flows: FlowRegistry = request.app["flows"]
    store: CheckerStore = request.app["store"]
    album_id = str(body.get("album_id") or "")

    async def run(f):
        result = await run_uploads(f, folder, trackers, source=source, auto_rename=auto_rename)
        # A release that has been uploaded is not one that is missing any more.
        # Leaving it on the Found list meant every successful upload made that
        # list slightly less true than it was before.
        if result.get("succeeded") and not cfg.upload.dry_run:
            _mark_uploaded(store, album_id, folder, result["succeeded"])
        return result

    label = f"{os.path.basename(folder)} to {', '.join(trackers)}"
    flow = flows.start(
        "upload",
        f"{'Dry run of ' if cfg.upload.dry_run else 'Uploading '}{label}",
        run,
    )
    return json_response(
        {"flow_id": flow.id, "trackers": trackers, "linking": cfg.linking.enabled, "dry_run": cfg.upload.dry_run}
    )


# ----------------------------------------------------------------------
# Flows
# ----------------------------------------------------------------------


@routes.get("/api/flows")
async def api_flows(request: web.Request) -> web.Response:
    """List flows, newest first."""
    return json_response({"flows": request.app["flows"].summaries(request.query.get("kind"))})


@routes.get("/api/flows/{flow_id}")
async def api_flow(request: web.Request) -> web.Response:
    """Fetch one flow, including the question it is waiting on."""
    flow = request.app["flows"].get(request.match_info["flow_id"])
    if not flow:
        return error("no such flow", status=404)
    return json_response(flow.as_dict())


@routes.post("/api/flows/{flow_id}/answer")
async def api_flow_answer(request: web.Request) -> web.Response:
    """Answer the question a flow is waiting on."""
    flow = request.app["flows"].get(request.match_info["flow_id"])
    if not flow:
        return error("no such flow", status=404)
    body = await request.json()
    accepted = flow.answer(body.get("step_id", ""), body.get("value"))
    if not accepted:
        return error("that question has already been answered", status=409)
    return json_response({"accepted": True})


@routes.post("/api/flows/{flow_id}/cancel")
async def api_flow_cancel(request: web.Request) -> web.Response:
    """Abandon a flow."""
    flow = request.app["flows"].get(request.match_info["flow_id"])
    if not flow:
        return error("no such flow", status=404)
    return json_response({"cancelled": flow.cancel()})


@routes.post("/api/flows/clear")
async def api_flows_clear(request: web.Request) -> web.Response:
    """Drop finished flows."""
    return json_response({"cleared": request.app["flows"].clear_finished()})


@routes.post("/api/jobs/{job_id}/input")
async def api_job_input(request: web.Request) -> web.Response:
    """Send a line of stdin to a running upload job."""
    job = request.app["jobs"].get(request.match_info["job_id"])
    if not job or not job.stdin:
        return error("job is not accepting input", status=409)
    body = await request.json()
    line = (body.get("line") or "") + "\n"
    job.stdin.write(line.encode())
    await job.stdin.drain()
    job.write_log(f"> {body.get('line', '')}")
    return json_response({"sent": True})


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------


@routes.get("/api/jobs")
async def api_jobs(request: web.Request) -> web.Response:
    """List background jobs, optionally filtered by kind."""
    return json_response({"jobs": request.app["jobs"].summaries(request.query.get("kind"))})


@routes.get("/api/jobs/{job_id}")
async def api_job(request: web.Request) -> web.Response:
    """Fetch one job, returning only results the client has not seen."""
    job = request.app["jobs"].get(request.match_info["job_id"])
    if not job:
        return error("no such job", status=404)
    return json_response(job.as_dict(since=int(request.query.get("since", 0))))


@routes.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(request: web.Request) -> web.Response:
    """Cancel a running job."""
    return json_response({"cancelled": request.app["jobs"].cancel(request.match_info["job_id"])})


@routes.post("/api/jobs/clear")
async def api_jobs_clear(request: web.Request) -> web.Response:
    """Drop finished jobs."""
    return json_response({"cleared": request.app["jobs"].clear_finished()})


# ----------------------------------------------------------------------
# Stored scan history
# ----------------------------------------------------------------------


@routes.get("/api/history/{collection}")
async def api_history(request: web.Request) -> web.Response:
    """Return stored results and a status breakdown for a collection."""
    collection = request.match_info["collection"]
    if collection not in ("albums", "requests"):
        return error("collection must be albums or requests", status=404)
    store: CheckerStore = request.app["store"]
    return json_response({"summary": store.summary(collection), "entries": store.load(collection)})


@routes.post("/api/history/{collection}/clear")
async def api_history_clear(request: web.Request) -> web.Response:
    """Empty a stored collection so everything is re-checked next run."""
    collection = request.match_info["collection"]
    if collection not in ("albums", "requests"):
        return error("collection must be albums or requests", status=404)
    return json_response({"cleared": request.app["store"].clear(collection)})
