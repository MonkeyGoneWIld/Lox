import asyncio
import hashlib
from os.path import dirname, join

import aiohttp_jinja2
import asyncclick as click
import jinja2
from aiohttp import web
from aiohttp_jinja2 import render_template

from lox import cfg
from lox.common import commandgroup
from lox.config import find_config_path
from lox.database import run_migrations
from lox.errors import WebServerIsAlreadyRunning
from lox.web import api, settings_api, spectrals

web_cfg = cfg.upload.web_interface


@commandgroup.command()
async def web_cmd() -> None:
    """Start the lox web server."""
    click.secho(f"Running webserver on http://{web_cfg.host}:{web_cfg.port}", fg="cyan")
    runner = await create_app_async()
    try:
        # Keep the server running
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


@commandgroup.command(name="ui")
async def ui_cmd() -> None:
    """Start the web UI: Deezer search, explore, download, check and upload."""
    url = f"http://{web_cfg.host}:{web_cfg.port}/"
    click.secho(f"lox UI on {url}", fg="cyan", bold=True)

    # No shell in a container to run `lox migrate` in, and the database is only
    # lox's own bookkeeping.
    if run_migrations():
        click.secho("Applied pending database migrations.", fg="green")

    if api.binds_publicly(web_cfg.host) and not api.auth_required():
        click.secho(
            "\nWARNING: the UI is bound to a non-loopback address with no auth_token set.\n"
            "Anyone who can reach this port can spend your tracker API budget, read your\n"
            "Deezer session and start uploads to your tracker accounts.\n"
            "Set upload.web_interface.auth_token, or bind host to 127.0.0.1.\n",
            fg="red",
            bold=True,
        )
    elif api.auth_required():
        click.secho("Auth token required. The UI will ask for it on first load.", fg="cyan")

    if not cfg.metadata.deezer.arl:
        click.secho(
            "No Deezer ARL configured. Search and charts will work; downloads, "
            "channels and availability checks will not. Set metadata.deezer.arl in your config.",
            fg="yellow",
        )
    click.secho("Trackers are only contacted when you press a check button.", fg="green")
    runner = await create_app_async()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


async def create_app_async() -> web.AppRunner:
    """Create and start the aiohttp web application.

    Returns:
        The AppRunner instance for the web server.

    Raises:
        WebServerIsAlreadyRunning: If the port is already in use.
    """
    app = web.Application(middlewares=[api.auth_middleware])
    add_routes(app)
    app.on_startup.append(api.setup_services)
    app.on_cleanup.append(api.teardown_services)
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(join(dirname(__file__), "templates")))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, web_cfg.host, web_cfg.port)
    try:
        await site.start()
    except OSError as err:
        raise WebServerIsAlreadyRunning from err
    return runner


#: Every address the single-page UI can show, so the server answers each of
#: them with the shell and the script picks the view back up from the path.
#: The sub-tab paths are here too -- Requests and Scan each have a second tab,
#: and returning to the first one on reload is the same bug in miniature.
APP_PATHS: tuple[str, ...] = (
    "/search",
    "/browse",
    "/scan",
    "/scan/history",
    "/requests",
    "/requests/history",
    "/queue",
    "/downloading",
    "/uploading",
    "/settings",
)


def add_routes(app: web.Application) -> None:
    """Add routes to the web application.

    Args:
        app: The aiohttp web application.
    """
    # follow_symlinks is no longer needed here: spectrals used to be symlinked
    # into this directory, which the package cannot write to in a container.
    app.router.add_static("/static", join(dirname(__file__), "static"))
    # Assets are cache-busted by content. Without this a browser can hold the
    # previous app.js against a newer server -- and the two halves disagreeing
    # about a payload's shape renders as a blank form rather than an error, so
    # an upgrade looked like the feature had been removed.
    app["asset_version"] = _asset_version()
    app.router.add_route("GET", "/", handle_app)
    # The same shell at every address the app navigates to, so reloading on a
    # page keeps you on it and Back means what it means everywhere else. The
    # app never changed the address bar at all, so a reload always landed on
    # Search and the browser's own buttons did nothing.
    #
    # Listed rather than a catch-all: a typo should still be a 404, and an
    # /api path that does not exist must not come back as a page of HTML.
    for path in APP_PATHS:
        app.router.add_route("GET", path, handle_app)
    app.router.add_route("GET", "/album/{album_id}", handle_app)
    app.router.add_route("GET", "/artist/{artist_id}", handle_app)
    app.router.add_route("GET", "/login", handle_login)
    app.router.add_route("GET", "/legacy", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app.router.add_route("GET", "/spectral-file/{name}", spectrals.handle_spectral_file)
    app.router.add_routes(api.routes)
    app.router.add_routes(settings_api.routes)
    app["static_root_url"] = web_cfg.static_root_url
    # There may be no config file at all when the bootstrap came from the
    # environment; the settings page only shows this as provenance.
    try:
        app["config_path"] = find_config_path()
    except FileNotFoundError:
        app["config_path"] = "environment (LOX_* variables)"


def _asset_version() -> str:
    """A short fingerprint of the front-end assets.

    Changes whenever the script, the stylesheet or the icon changes, and only
    then, so a new build is fetched fresh while an unchanged one still caches.
    """
    digest = hashlib.sha1()
    static = join(dirname(__file__), "static")
    # fonts.css names the vendored faces; a change to it has to bust the cache
    # like any other, or a swapped font is served against a stale stylesheet.
    # The icon is in here for a blunter reason: browsers cache a favicon harder
    # than anything else on the page, and a new mark that nobody sees is not a
    # new mark.
    for relative in (
        "scripts/app.js",
        "css/app.css",
        "css/fonts.css",
        "images/favicon.ico",
        "images/logo.png",
    ):
        path = join(static, relative)
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            digest.update(relative.encode())
    return digest.hexdigest()[:10]


async def handle_app(request: web.Request) -> web.Response:
    """Serve the single-page UI shell.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered application shell.
    """
    return render_template(
        "app.html",
        request,
        {"static_root_url": web_cfg.static_root_url, "asset_version": request.app.get("asset_version", "")},
    )


async def handle_login(request: web.Request) -> web.Response:
    """Serve the sign-in page.

    Anyone already holding a valid session is sent straight to the app rather
    than being asked again.

    Args:
        request: The aiohttp request object.

    Returns:
        The login page, or a redirect when a session already exists.
    """
    if not api.auth_required() or api.is_authenticated(request):
        raise web.HTTPFound("/")
    return render_template(
        "login.html",
        request,
        {"static_root_url": web_cfg.static_root_url, "asset_version": request.app.get("asset_version", "")},
    )


async def handle_index(request: web.Request) -> web.Response:
    """Handle the original spectrals index page.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered index page response.
    """
    return render_template("index.html", request, {})
