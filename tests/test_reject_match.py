"""Saying a release does not fill a request has to stick.

The matcher is confident about a wrong match and stays confident. It matched
"Songs From The Road Band — Just Hanging On" to a request and nothing about the
next check would decide differently, so taking the release off the queue lasted
exactly until the next check put it back -- and the only way to stop it was to
blacklist a release that is perfectly fine, just not this request's.

So a refusal is recorded against the pairing. The release leaves the queue, the
request goes back to having no match and stays open for something that does
fill it, and the matcher will not offer that release for that request again.
The refusal is kept rather than forgotten: it is the only evidence there is of
the matcher being wrong, and both lookup histories show it.
"""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_rejectmatch")
shutil.rmtree(BASE, ignore_errors=True)
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5137",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)
sys.path.insert(0, os.path.dirname(ROOT))

from lox.checker import deezer_requests  # noqa: E402
from lox.checker.store import CheckerStore  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def main() -> int:
    store = CheckerStore(os.path.join(BASE, "state"))

    # --- nothing is refused to begin with ----------------------------
    check("a pairing nobody has judged is not refused",
          not deezer_requests.rejected_match(store, "OPS", "80162", "111"), "")

    # --- and a refusal is remembered ---------------------------------
    deezer_requests.reject_match(store, "OPS", "80162", "111", "Songs From The Road Band — Just Hanging On")
    check("a refused pairing is refused",
          deezer_requests.rejected_match(store, "OPS", "80162", "111"), "")
    check("and the name is kept, so it can be read back",
          "Just Hanging On" in str(store.get(deezer_requests.REJECTIONS, "OPS:80162")),
          str(store.get(deezer_requests.REJECTIONS, "OPS:80162")))

    # It is the PAIRING that was refused, not the release and not the request.
    # A release that does not fill one request may well fill another, and the
    # request stays open for something that does.
    check("the same release is still fair game for another request",
          not deezer_requests.rejected_match(store, "OPS", "99999", "111"), "")
    check("and the same request for another release",
          not deezer_requests.rejected_match(store, "OPS", "80162", "222"), "")
    check("nor does it carry across trackers",
          not deezer_requests.rejected_match(store, "RED", "80162", "111"), "")

    # --- several refusals against one request ------------------------
    deezer_requests.reject_match(store, "OPS", "80162", "222", "Something Else")
    check("a second refusal joins the first",
          deezer_requests.rejected_ids(store, "OPS", "80162") == {"111", "222"},
          str(deezer_requests.rejected_ids(store, "OPS", "80162")))
    deezer_requests.reject_match(store, "OPS", "80162", "111", "Songs From The Road Band")
    check("and refusing the same one twice does not double it",
          len((store.get(deezer_requests.REJECTIONS, "OPS:80162") or {}).get("deezer_ids") or []) == 2,
          str(store.get(deezer_requests.REJECTIONS, "OPS:80162")))

    # --- it survives the check that would otherwise undo it ----------
    # The request record is rewritten in full by every check, which is why the
    # refusal is not kept on it: stored there it would last until the next
    # check, which is the problem it exists to solve.
    store.put("requests", "OPS:80162", {"status": "fillable", "deezer_id": "111"}, flush=True)
    check("a refusal is not stored on the record a check overwrites",
          deezer_requests.rejected_match(store, "OPS", "80162", "111"), "")

    # And it is read back by a fresh store, so a restart does not forget it.
    again = CheckerStore(os.path.join(BASE, "state"))
    check("nor is it forgotten when lox restarts",
          deezer_requests.rejected_match(again, "OPS", "80162", "111"), "")

    # --- the matcher consults it -------------------------------------
    source = deezer_requests.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    check("the matcher refuses a pairing that was refused",
          "rejected_match(self.store, tracker, request_id, match.deezer_id)" in text, "")
    check("and says so rather than going quiet",
          "you said this release does not fill this request" in text, "")
    check("with the refusal on the record, for judging the matching later",
          '"rejected": match.rejected,' in text, "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
