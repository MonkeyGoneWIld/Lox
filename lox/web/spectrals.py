import datetime
import os
import sqlite3
from itertools import chain
from typing import Any

from aiohttp import web
from aiohttp_jinja2 import render_template

from lox.database import DB_PATH

_active_directory = ""
"""Where the spectrals being served actually live.

They used to be reached by symlinking that directory into the package's own
static folder. That fails as soon as the package is not writable -- in a
container it is owned by the image and the process is not root -- with
``PermissionError: [Errno 1] Operation not permitted``, which killed the upload
right after the spectrals had been generated. Serving them from where they
already are needs no write access to anything.
"""


def spectral_directory() -> str:
    """The directory the active spectrals are being served from."""
    return _active_directory


async def handle_spectrals(request: web.Request) -> web.Response:
    active_spectrals: dict[str, Any] = get_active_spectrals()
    if active_spectrals.get("spectrals"):
        active_spectrals["now"] = datetime.datetime.now()
        return render_template("spectrals.html", request, active_spectrals)
    raise web.HTTPNotFound()


async def handle_spectral_file(request: web.Request) -> web.StreamResponse:
    """Serve one spectral image from the active directory.

    Args:
        request: Carries the file name.

    Returns:
        The image.

    Raises:
        HTTPNotFound: If no spectrals are active, or the name does not resolve
            to a file inside the active directory.
    """
    directory = spectral_directory()
    name = request.match_info["name"]
    if not directory or os.path.basename(name) != name:
        raise web.HTTPNotFound()
    path = os.path.realpath(os.path.join(directory, name))
    if not path.startswith(os.path.realpath(directory) + os.sep) or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


def set_active_spectrals(spectrals, directory: str = ""):
    global _active_directory  # noqa: PLW0603 - one active set per process
    _active_directory = directory
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("DELETE FROM spectrals")
        cursor.execute(
            "INSERT INTO spectrals (id, filename) VALUES " + ", ".join("(?, ?)" for _ in range(len(spectrals))),
            tuple(chain.from_iterable(list(spectrals.items()))),
        )
        conn.commit()


def get_active_spectrals():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, filename FROM spectrals ORDER BY ID ASC")
        return {"spectrals": {r["id"]: r["filename"] for r in cursor.fetchall()}}
