"""Where HTTPS gets its trusted roots.

Every tracker call, every Deezer call and every image upload goes out over
TLS, and on Windows Python verifies them against the machine's certificate
store. A store that has not been serviced in a while still carries roots that
have since expired; OpenSSL builds a chain through one of them and reports the
site's certificate as expired. The site's certificate is fine -- it is the
root above it that ran out -- but the connection fails all the same, and the
message blames the tracker:

    Cannot connect to host <tracker>:443 ssl:True
    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    certificate has expired

Nothing about that suggests the real cause, and there is nothing the user can
do about it from inside lox, so it reads as the tracker being down.

certifi ships the current root list and is already a dependency. This *adds*
it to what is already trusted rather than replacing it, so a CA installed only
in the Windows store -- a corporate proxy's, typically -- keeps working, and
an ``SSL_CERT_FILE`` the user set themselves is left alone.

Two things have to happen, because one of them is not enough:

  * ``SSL_CERT_FILE`` covers every context built from here on, in this process
    and in anything it spawns.
  * aiohttp does not build one from here on. It builds its default context at
    *import* time (``_SSL_CONTEXT_VERIFIED`` in ``aiohttp.connector``), so if
    aiohttp was imported before this ran, the env var arrives too late and
    every session in the process quietly keeps the system-only roots. The
    roots are loaded into that context directly as well, which is additive and
    leaves everything already trusted in place.

Only setting the env var would work today and break the first time an import
lands above ``lox`` -- silently, and only on machines with a stale store.
"""

import contextlib
import os
import sys


def use_certifi_roots() -> str:
    """Trust certifi's roots in addition to the system's.

    Call before anything opens a TLS connection.

    Returns:
        The bundle now in use, or "" if the environment already named one or
        certifi is not installed.
    """
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is a hard dependency
        return ""
    bundle = certifi.where()
    if not os.path.isfile(bundle):  # pragma: no cover - a broken install
        return ""

    if os.environ.get("SSL_CERT_FILE"):
        return ""  # Deliberately set; not ours to second-guess.
    os.environ["SSL_CERT_FILE"] = bundle
    _patch_aiohttp(bundle)
    return bundle


def _patch_aiohttp(bundle: str) -> None:
    """Add the roots to aiohttp's context, if it built one before we ran.

    Best effort by design: this reaches for a private name, so a future
    aiohttp that renames or drops it should leave lox starting normally rather
    than failing at import over certificates.
    """
    connector = sys.modules.get("aiohttp.connector")
    context = getattr(connector, "_SSL_CONTEXT_VERIFIED", None)
    if context is None:
        return
    # Nothing here is worth failing startup over: without it the env var still
    # covers every context built later, which is the common case.
    with contextlib.suppress(Exception):  # pragma: no cover
        context.load_verify_locations(cafile=bundle)
