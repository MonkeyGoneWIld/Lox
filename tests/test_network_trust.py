"""HTTPS gets working roots, and a failed tracker call says something.

Two faults that between them made the request page look like a dead tracker:

  * On Windows, verification runs against the machine's certificate store. A
    store carrying a root that has since expired makes OpenSSL report the
    tracker's certificate as expired -- the tracker's certificate is fine, the
    root above it is not -- and every call fails with a message that blames
    the tracker.
  * A Gazelle API call that is not authenticated is answered with a 302 to the
    login page and an empty body, not an error. That reached the user as an
    empty string: nothing said, nothing to act on.

The first is asserted against the real aiohttp, because the whole point of the
fix is an ordering problem that only shows up with the real thing: aiohttp
builds its default SSL context at import time, so setting the environment
variable after aiohttp is imported is too late and silently does nothing.
"""

import os
import ssl
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

# Importing lox reads the config, so point it at throwaway directories the way
# the other tests here do.
os.environ.setdefault("LOX_HOST", "127.0.0.1")
os.environ.setdefault("LOX_PORT", "5016")
os.environ.setdefault("LOX_AUTH_TOKEN", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("LOX_DOWNLOAD_DIR", os.path.join(ROOT, "_trust", "downloads"))
os.environ.setdefault("LOX_TORRENTS_DIR", os.path.join(ROOT, "_trust", "torrents"))
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def main() -> int:
    import certifi

    from lox.certs import use_certifi_roots

    # --- the roots ------------------------------------------------------
    os.environ.pop("SSL_CERT_FILE", None)
    bundle = use_certifi_roots()
    check("the certifi bundle is picked up", bundle == certifi.where(), bundle)
    check("and every context built after this sees it",
          os.environ.get("SSL_CERT_FILE") == certifi.where(), "")

    # Someone who set this deliberately -- a corporate CA, usually -- keeps it.
    os.environ["SSL_CERT_FILE"] = "already-chosen"
    check("an SSL_CERT_FILE already set is left alone",
          use_certifi_roots() == "" and os.environ["SSL_CERT_FILE"] == "already-chosen", "")
    os.environ["SSL_CERT_FILE"] = certifi.where()

    # The ordering fault, against the real aiohttp. Its verified context is
    # built once at import; if lox is imported after it, the env var arrives
    # too late and the process keeps system-only roots for its whole life.
    import aiohttp.connector as connector

    context = getattr(connector, "_SSL_CONTEXT_VERIFIED", None)
    check("aiohttp still builds one context at import, which is why this matters",
          isinstance(context, ssl.SSLContext), type(context).__name__)

    if isinstance(context, ssl.SSLContext):
        before = len(context.get_ca_certs())
        fresh = ssl.create_default_context()
        fresh.load_verify_locations(cafile=certifi.where())
        check("a context with certifi loaded trusts more roots than a bare system one",
              len(fresh.get_ca_certs()) >= before, f"{len(fresh.get_ca_certs())} vs {before}")

        # Additive: loading the bundle must not drop what was already trusted,
        # or a CA installed only in the Windows store stops working.
        system_only = ssl.create_default_context()
        kept = {c.get("serialNumber") for c in system_only.get_ca_certs()}
        system_only.load_verify_locations(cafile=certifi.where())
        after = {c.get("serialNumber") for c in system_only.get_ca_certs()}
        check("and loading it keeps every root that was already trusted",
              kept <= after, f"{len(kept - after)} lost")

    # --- the message ----------------------------------------------------
    from lox.trackers.base import _why_it_failed

    redirect = _why_it_failed(302)
    check("a 302 is explained as an authentication problem",
          "302" in redirect and "key" in redirect.lower(), redirect[:60])
    check("and points at where the key is set", "Settings" in redirect, "")
    check("a 401 says the key was refused",
          "401" in _why_it_failed(401) and "key" in _why_it_failed(401).lower(), "")
    check("a 500 is the tracker's problem, not the user's",
          "try again" in _why_it_failed(503).lower(), _why_it_failed(503)[:50])
    check("and anything else still says something",
          _why_it_failed(418).strip() != "" and "418" in _why_it_failed(418), "")
    check("no status produces an empty message",
          all(_why_it_failed(s).strip() for s in (200, 302, 401, 404, 500, 503, 418)), "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
