"""A key that is wrong has to test as wrong.

Every image-host test passed for a made-up key. The check failed only on HTTP
401 or 403, and none of these hosts uses those for a bad key -- they answer 400
with a JSON body naming the reason, so every refusal was read as a pass. The
one credential the settings page exists to verify was the one it could not.

Underneath that were three more:

  * the oeimg check called oeimg.com, which does not resolve, while uploads
    went to imgoe.download -- a different Chevereto instance that rejects an
    OnlyImage key, because it was never issued one
  * ptscreens was checked with GET, which that API answers with an HTML error
    page rather than JSON, so there was nothing to read even had it looked
  * ptpimg.me answers HTTP 500 to everything, its own upload endpoint
    included, and "not 401" counted that as the key being accepted

The bodies below are the real ones, captured from the live hosts with a key of
thirty-four zeroes. Nothing here touches the network.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_imagehosts")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5112",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

import msgspec  # noqa: E402

from lox import cfg  # noqa: E402
from lox.images import HOSTS, imgbb, oeimg, ptscreens  # noqa: E402
from lox.web.settings_api import _IMAGE_KEYS, _image_verdict  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def verdict(host: str, status: int, body: str) -> dict:
    """Run the real verdict function and read its answer."""
    return msgspec.json.decode(_image_verdict(host, status, body, "Not selected.").body)


# What the hosts actually say. Captured live, key of thirty-four zeroes.
BAD_KEY = '{"status_code":400,"error":{"message":"Invalid API key.","code":100},"status_txt":"Bad Request"}'
BAD_KEY_IMGBB = ('{"status_code":400,"error":{"message":"Invalid API v1 key.","code":100},'
                 '"status_txt":"Bad Request"}')
# What a good key with no file attached looks like: the key got through and only
# the upload is missing, which is exactly what this test sends.
NO_FILE = ('{"status_code":400,"error":{"message":"Empty upload source.","code":130},'
           '"status_txt":"Bad Request"}')
PTSCREENS_HTML = '<!DOCTYPE html>\n<html lang="en"><head><title>Bad request · PTScreens</title>'


def main() -> int:
    # --- a wrong key is wrong ----------------------------------------
    for host, body in (("oeimg", BAD_KEY), ("ptscreens", BAD_KEY), ("imgbb", BAD_KEY_IMGBB)):
        got = verdict(host, 400, body)
        check(f"{host} rejects a key it did not issue", got["ok"] is False, got["message"][:80])
        check(f"and repeats what {host} said about it",
              "invalid api" in got["message"].lower(), got["message"][:80])

    # --- a right key is right ----------------------------------------
    #
    # The distinguishing fact is the error code, not the status: both of these
    # are HTTP 400. 100 is "invalid API key"; anything else means the key was
    # accepted and only the file was missing.
    for host in ("oeimg", "ptscreens", "imgbb"):
        got = verdict(host, 400, NO_FILE)
        check(f"{host} passes a key when the only complaint is the missing file",
              got["ok"] is True, got["message"][:80])
    check("a plain 200 passes too", verdict("oeimg", 200, "{}")["ok"] is True, "")

    # --- and everything else is reported, not guessed ----------------
    dead = verdict("ptscreens", 500, "")
    check("a host answering 500 is a failure, not a pass", dead["ok"] is False, dead["message"][:80])
    check("which is what hid ptpimg being dead", "not answering properly" in dead["message"], "")

    for status in (401, 403):
        got = verdict("oeimg", status, "")
        check(f"HTTP {status} is still a rejection", got["ok"] is False, got["message"][:60])

    # An answer that cannot be read is not an answer. This is the shape the old
    # check turned into a pass, so it must not be a pass now either.
    html = verdict("ptscreens", 400, PTSCREENS_HTML)
    check("an unreadable refusal is not mistaken for approval", html["ok"] is False, html["message"][:90])
    check("and it says it could not tell",
          "could not tell" in html["message"], html["message"][:90])

    # --- the test and the uploader cannot drift apart ----------------
    #
    # They were three different domains: the check called oeimg.com, the
    # uploader posted to imgoe.download, and the account is on onlyimage.org.
    check("oeimg uploads to OnlyImage", oeimg.API_URL.startswith("https://onlyimage.org/"), oeimg.API_URL)
    check("and not to the domain that does not resolve", "oeimg.com" not in oeimg.API_URL, oeimg.API_URL)
    check("nor to the instance that never issued the key",
          "imgoe.download" not in oeimg.API_URL, oeimg.API_URL)
    check("ptscreens has one endpoint too",
          ptscreens.API_URL == "https://ptscreens.com/api/1/upload", ptscreens.API_URL)
    check("and imgbb", imgbb.API_URL == "https://api.imgbb.com/1/upload", imgbb.API_URL)

    # --- a key changed on the settings page reaches the next upload --
    #
    # These were module-level dicts built at import: the settings page mutates
    # cfg in place, but never the dict, so a corrected key did nothing until the
    # process was restarted.
    for module, attribute in ((oeimg, "oeimg_key"), (ptscreens, "ptscreens_key")):
        setattr(cfg.image, attribute, "first-key")
        before = module.headers()["X-API-Key"]
        setattr(cfg.image, attribute, "second-key")
        after = module.headers()["X-API-Key"]
        check(f"{attribute} is read when the request is made, not at import",
              before == "first-key" and after == "second-key", f"{before} -> {after}")

    # --- ptpimg is gone ----------------------------------------------
    check("ptpimg is not an upload target", "ptpimg" not in HOSTS, str(sorted(HOSTS)))
    check("nor a key the settings page tests", "ptpimg" not in _IMAGE_KEYS, str(sorted(_IMAGE_KEYS)))
    check("nor a choice you can pick", "ptpimg" not in str(cfg.image.image_uploader), "")
    check("and its module is deleted",
          not os.path.exists(os.path.join(os.path.dirname(ROOT), "lox", "images", "ptpimg.py")), "")

    # Every host that takes a key has an uploader, and vice versa.
    check("every key belongs to a host that exists",
          set(_IMAGE_KEYS) <= set(HOSTS), str(set(_IMAGE_KEYS) - set(HOSTS)))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
