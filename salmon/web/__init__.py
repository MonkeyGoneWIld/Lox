import asyncio
from os.path import dirname, join

import aiohttp_jinja2
import asyncclick as click
import jinja2
from aiohttp import web
from aiohttp_jinja2 import render_template

from salmon import cfg
from salmon.config import find_config_path
from salmon.common import commandgroup
from salmon.errors import WebServerIsAlreadyRunning
from salmon.web import api, settings_api, spectrals

web_cfg = cfg.upload.web_interface


@commandgroup.command()
async def web_cmd() -> None:
    """Start the salmon web server."""
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


def add_routes(app: web.Application) -> None:
    """Add routes to the web application.

    Args:
        app: The aiohttp web application.
    """
    app.router.add_static("/static", join(dirname(__file__), "static"), follow_symlinks=True)
    app.router.add_route("GET", "/", handle_app)
    app.router.add_route("GET", "/login", handle_login)
    app.router.add_route("GET", "/legacy", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app.router.add_routes(api.routes)
    app.router.add_routes(settings_api.routes)
    app["static_root_url"] = web_cfg.static_root_url
    app["config_path"] = find_config_path()


async def handle_app(request: web.Request) -> web.Response:
    """Serve the single-page UI shell.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered application shell.
    """
    return render_template("app.html", request, {"static_root_url": web_cfg.static_root_url})


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
    return render_template("login.html", request, {"static_root_url": web_cfg.static_root_url})


async def handle_index(request: web.Request) -> web.Response:
    """Handle the original spectrals index page.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered index page response.
    """
    return render_template("index.html", request, {})
