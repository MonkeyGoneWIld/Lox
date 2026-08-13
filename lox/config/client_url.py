"""A torrent client connection, as parts rather than as one URL.

What gets stored is a libtc-style URL --
``qbittorrent+http://user:pass@10.0.0.4:8080`` -- because that is what
``config.toml`` has always held and what
:meth:`~lox.clients.TorrentClientGenerator.parse_libtc_url`
reads. It is a reasonable thing to store and a bad thing to ask anyone to type:
the scheme is two schemes joined by a plus, which one depends on the client, the
password sits in the middle of the host, and every way of getting it wrong
produces a connection error rather than a syntax error. The settings page asked
for it as a single free-text box with the four shapes listed underneath as a
hint, which is a config file with a border drawn round it.

So the page edits the parts -- which client, which host, which port, and the
account -- and this composes them into the URL that gets stored and takes it
apart again to fill the form back in.
"""

from typing import Any, NamedTuple
from urllib.parse import quote, unquote, urlparse


class ClientKind(NamedTuple):
    """One kind of torrent client, and what connecting to it needs."""

    id: str
    label: str
    port: int
    """Default port, filled in when the port box is left empty."""
    secure: bool
    """Whether it is reached over HTTP, and so can be reached over HTTPS."""
    path: str | None
    """Default URL path, or None for a client that has no path in its address."""
    path_required: bool
    help: str

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the settings page."""
        return {
            "id": self.id,
            "label": self.label,
            "port": self.port,
            "secure": self.secure,
            "path": self.path,
            "path_required": self.path_required,
            "help": self.help,
        }


CLIENTS: tuple[ClientKind, ...] = (
    ClientKind(
        "qbittorrent",
        "qBittorrent",
        8080,
        secure=True,
        path="",
        path_required=False,
        help="The Web UI address and the account you sign into it with.",
    ),
    ClientKind(
        "transmission",
        "Transmission",
        9091,
        secure=True,
        path=None,
        path_required=False,
        help="The RPC port — 9091 unless you changed it. Blank credentials are fine if RPC auth is off.",
    ),
    ClientKind(
        "deluge",
        "Deluge",
        58846,
        secure=False,
        path=None,
        path_required=False,
        help="The daemon's port, not the Web UI's, and the account from Deluge's auth file.",
    ),
    ClientKind(
        "rutorrent",
        "ruTorrent",
        80,
        secure=True,
        path="/plugins/rpc/rpc.php",
        path_required=True,
        help="The address of the RPC plugin's rpc.php, not of the ruTorrent page.",
    ),
)

CLIENT_BY_ID: dict[str, ClientKind] = {c.id: c for c in CLIENTS}


def _clean_host(host: str) -> tuple[str, int | None]:
    """Reduce whatever was typed in the host box to a host, and maybe a port.

    People paste the address bar. ``http://10.0.0.4:8080/`` is not a host, but
    it is obvious what was meant, and rejecting it teaches nothing.

    Args:
        host: Whatever was typed.

    Returns:
        Tuple of (host, port or None).
    """
    text = (host or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip("/").strip()
    # An IPv6 literal is bracketed, and its colons are not port separators.
    if text.startswith("["):
        closing = text.find("]")
        if closing != -1:
            rest = text[closing + 1 :]
            port = rest[1:] if rest.startswith(":") else ""
            return text[: closing + 1], int(port) if port.isdigit() else None
    if ":" in text:
        text, _, port = text.rpartition(":")
        return text.strip(), int(port) if port.isdigit() else None
    return text, None


def _auth(username: str, password: str) -> str:
    """The ``user:pass@`` part of the URL, percent-encoded, or an empty string.

    Both halves are always emitted when either is set: the parser on the other
    side splits on the first colon and raises when there is none, so a username
    with no password has to be written ``user:@host``.
    """
    if not username and not password:
        return ""
    return f"{quote(username, safe='')}:{quote(password, safe='')}@"


def build_client_url(parts: dict[str, Any]) -> str:
    """Compose a libtc URL from the fields the settings page collects.

    Args:
        parts: ``client``, ``host``, ``port``, ``username``, ``password``,
            ``secure`` and ``path``. Everything but the client and the host has
            a sensible default.

    Returns:
        The URL to store, or an empty string when no host was given -- an entry
        being filled in is not an error, it is just not connectable yet.

    Raises:
        ValueError: If the client is unknown, or the port is not a port.
    """
    kind = str(parts.get("client") or "").strip().lower()
    if not kind:
        return ""
    spec = CLIENT_BY_ID.get(kind)
    if spec is None:
        known = ", ".join(c.label for c in CLIENTS)
        raise ValueError(f"{kind} is not a torrent client lox can talk to. Pick one of: {known}.")

    host, embedded_port = _clean_host(str(parts.get("host") or ""))
    if not host:
        return ""

    raw_port = parts.get("port")
    port = embedded_port or spec.port
    if raw_port not in (None, ""):
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{raw_port!r} is not a port number.") from e
    if not 1 <= port <= 65535:
        raise ValueError(f"{port} is not a port number; ports run from 1 to 65535.")

    auth = _auth(str(parts.get("username") or ""), str(parts.get("password") or ""))
    scheme = ("https" if parts.get("secure") else "http") if spec.secure else ""

    path = ""
    if spec.path is not None:
        path = str(parts.get("path") or "").strip()
        if not path and spec.path_required:
            path = spec.path
        if path and not path.startswith("/"):
            path = f"/{path}"

    if not scheme:
        return f"{spec.id}://{auth}{host}:{port}{path}"
    return f"{spec.id}+{scheme}://{auth}{host}:{port}{path}"


def split_client_url(url: str) -> dict[str, Any]:
    """Take a stored libtc URL apart, so the form can be filled in from it.

    Anything unrecognisable comes back as the empty form rather than raising:
    a hand-edited ``config.toml`` should leave the page usable, not blank.

    Args:
        url: The stored connection URL.

    Returns:
        The same keys :func:`build_client_url` accepts, plus ``password``.
    """
    empty: dict[str, Any] = {
        "client": "",
        "host": "",
        "port": None,
        "username": "",
        "password": "",
        "secure": False,
        "path": "",
    }
    if not url or "://" not in url:
        return empty

    try:
        parsed = urlparse(url)
    except ValueError:
        return empty

    scheme_parts = parsed.scheme.split("+")
    spec = CLIENT_BY_ID.get(scheme_parts[0].lower())
    if spec is None:
        return empty

    netloc = parsed.netloc
    username = password = ""
    if "@" in netloc:
        auth, netloc = netloc.rsplit("@", 1)
        username, _, password = auth.partition(":")
        username, password = unquote(username), unquote(password)

    host, port = _clean_host(netloc)
    return {
        "client": spec.id,
        "host": host,
        "port": port if port is not None else spec.port,
        "username": username,
        "password": password,
        "secure": spec.secure and scheme_parts[-1].lower() == "https",
        "path": parsed.path if spec.path is not None else "",
    }
