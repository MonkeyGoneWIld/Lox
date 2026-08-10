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
import shlex
import shutil
import sys
import time
from typing import Any
from urllib.parse import quote

import msgspec
from aiohttp import web

from lox import cfg, debug
from lox.checker.deezer_requests import DeezerRequestChecker
from lox.checker.gateway import TrackerGateway
from lox.checker.missing import Candidate, MissingScanner
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
from lox.web.jobs import JobRegistry

routes = web.RouteTableDef()

AUTH_COOKIE = "lox_token"
AUTH_HEADER = "X-Auth-Token"

# Reachable without a session: the login page itself, the endpoint that creates
# the session, and static assets, which carry nothing sensitive.
AUTH_EXEMPT_PATHS = frozenset({"/login", "/api/auth"})

SESSION_DAYS = 30

# Crude per-address throttle. The token guards tracker upload privileges, so an
# open port should not allow unlimited guessing.
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_WINDOW = 300.0
LOGIN_MAX_ATTEMPTS = 10
LOGIN_FAIL_DELAY = 0.5


def auth_required() -> bool:
    """True when an auth token is configured."""
    return bool(cfg.upload.web_interface.auth_token)


def _token_ok(supplied: str | None) -> bool:
    """Compare a supplied token against the configured one in constant time."""
    expected = cfg.upload.web_interface.auth_token
    if not expected:
        return True
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def is_authenticated(request: web.Request) -> bool:
    """True when the request carries a valid token by any accepted means."""
    supplied = request.headers.get(AUTH_HEADER) or request.cookies.get(AUTH_COOKIE) or request.query.get("token")
    return _token_ok(supplied)


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
    if not auth_required() or request.path.startswith("/static") or request.path in AUTH_EXEMPT_PATHS:
        return await handler(request)

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


@routes.post("/api/auth")
async def api_auth(request: web.Request) -> web.Response:
    """Exchange the configured token for a session cookie.

    This is what the login page posts to. It stays outside the auth middleware,
    so it is the one place a wrong token can be submitted — hence the throttle.
    """
    if not auth_required():
        return json_response({"ok": True, "auth_required": False})

    address = request.remote or "unknown"
    body = await request.json()
    token = str(body.get("token") or "")

    if _token_ok(token):
        _LOGIN_ATTEMPTS.pop(address, None)
        response = json_response({"ok": True})
        _set_session(response, token, bool(body.get("remember", True)))
        return response

    if _throttled(address):
        return error("Too many attempts. Wait a few minutes and try again.", status=429)
    # Slow down guessing without holding a worker for long.
    await asyncio.sleep(LOGIN_FAIL_DELAY)
    return error("That token was not accepted.", status=401)


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


async def _featured_artists(gw: DeezerGW, album_id: str, album_artist: str) -> dict[str, list[str]]:
    """Map each track ID to the artists credited on it besides the main one.

    Deezer's private records spell this two ways depending on the release, so
    both are read: an explicit ``featured`` contributor list, and failing that
    the full artist list minus whoever is billed as the main artist. Anything
    unrecognised yields nothing rather than a guess.

    Args:
        gw: Authenticated Deezer client.
        album_id: Album to read.
        album_artist: The headline artist, excluded from every track's list.

    Returns:
        Track ID to featured artist names.
    """
    featured: dict[str, list[str]] = {}
    for track in await gw.album_tracks(album_id):
        track_id = str(track.get("SNG_ID") or "")
        if not track_id:
            continue

        contributors = track.get("SNG_CONTRIBUTORS") or {}
        names: list[str] = []
        if isinstance(contributors, dict):
            for role in ("featured", "featuring"):
                value = contributors.get(role)
                if isinstance(value, list):
                    names.extend(str(v) for v in value if v)

        if not names:
            main = {album_artist.casefold(), str(track.get("ART_NAME") or "").casefold()}
            for artist in track.get("ARTISTS") or []:
                name = str(artist.get("ART_NAME") or "")
                if name and name.casefold() not in main and name not in names:
                    names.append(name)

        if names:
            featured[track_id] = names
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
    featured_by_track: dict[str, list[str]] = {}
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
                    "number": t.get("track_position"),
                    "disc": t.get("disk_number"),
                    "explicit": t.get("explicit_lyrics"),
                    "featured": featured_by_track.get(str(t.get("id")), []),
                }
                for t in (meta.get("tracks") or {}).get("data", [])
            ],
            "availability": availability,
            "availability_error": availability_error,
        }
    )


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
        limit = max(1, min(500, int(request.query.get("limit", 25))))
    except ValueError:
        return error("limit must be a number")

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
        )
    except Exception as e:  # noqa: BLE001 - budget and transport errors both surface here
        return error(str(e), status=502)
    return json_response(found)


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
    return json_response(filter_schema(tracker))


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

    async def run(job) -> None:
        matches = await checker.check_many(tracker, request_ids, progress=job.emit)
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


def _cli_command() -> list[str]:
    """Return the argv prefix that invokes this package's CLI."""
    console_script = shutil.which("lox")
    if console_script:
        return [console_script]
    return [sys.executable, "-u", "-m", "lox"]


async def _run_cli(job, args: list[str]) -> int:
    """Run the CLI as a subprocess, streaming its output into a job.

    Args:
        job: The job to stream into. Its ``stdin`` is wired to the process so
            the browser can answer prompts.
        args: CLI arguments after the program name.

    Returns:
        The process exit code.
    """
    command = _cli_command()
    job.write_log(f"$ {os.path.basename(command[0])} {shlex.join(args)}")
    process = await asyncio.create_subprocess_exec(
        *command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "TERM": "dumb"},
    )
    job.stdin = process.stdin
    try:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            job.write_log(line.decode(errors="replace").rstrip("\n"))
        return await process.wait()
    finally:
        job.stdin = None


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

    label = f"{os.path.basename(folder)} to {', '.join(trackers)}"
    flow = flows.start(
        "upload",
        f"{'Dry run of ' if cfg.upload.dry_run else 'Uploading '}{label}",
        lambda f: run_uploads(f, folder, trackers, source=source, auto_rename=auto_rename),
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
