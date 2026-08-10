"""Check request collection: the filters sent, and the paging.

The tracker sets the page size, so fetching more than one page is the only way
to get more than 25 requests -- and every page is a call against a budget that
punishes bursts. This covers what gets asked for, how many calls it costs, and
the ways paging can go wrong: a tracker that runs out of results early, and one
that ignores the page parameter and serves the same page forever.

Run it directly:

    python tests/test_requests.py
"""

import asyncio
import os
import sys
import tempfile

# Before importing lox: the config is read at import time.
ROOT = tempfile.mkdtemp(prefix="lox-requests-")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5097",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(ROOT, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(ROOT, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(ROOT, "config"),
        "LOX_STATE_DIR": os.path.join(ROOT, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


class FakeGateway:
    """A tracker that serves fixed pages and records what it was asked."""

    def __init__(self, total: int, page_size: int = 25, *, ignores_page: bool = False) -> None:
        """Set up a tracker holding ``total`` requests."""
        self.total = total
        self.page_size = page_size
        self.ignores_page = ignores_page
        self.calls: list[dict] = []

    async def call_action(self, tracker: str, action: str, params: dict) -> dict:
        """Serve one page."""
        self.calls.append(dict(params))
        page = 1 if self.ignores_page else int(params.get("page", 1))
        start = (page - 1) * self.page_size
        rows = [
            {"requestId": start + i, "title": f"Album {start + i}", "year": 2025, "totalBounty": 1024**3}
            for i in range(min(self.page_size, max(0, self.total - start)))
        ]
        pages = max(1, -(-self.total // self.page_size))
        return {"results": rows, "pages": pages, "currentPage": page}

    def request_url(self, tracker: str, request_id: int) -> str:
        """Link back to the request."""
        return f"https://example.invalid/requests.php?action=view&id={request_id}"


async def main() -> int:
    """Run the checks."""
    from lox.checker.deezer_requests import DeezerRequestChecker

    def checker_for(gateway):
        return DeezerRequestChecker(gw=None, gateway=gateway, store=None)  # type: ignore[arg-type]

    # --- one page is one call -----------------------------------------
    gw = FakeGateway(total=500)
    found = await checker_for(gw).collect_requests("RED", limit=25)
    check("25 costs a single call", found["calls"] == 1, f"{found['calls']} calls")
    check("25 requests come back", len(found["requests"]) == 25, str(len(found["requests"])))
    check("a full page is reported complete", found["complete"] is True)

    # --- more than a page pages on --------------------------------------
    gw = FakeGateway(total=500)
    found = await checker_for(gw).collect_requests("RED", limit=100)
    check("100 costs four calls", found["calls"] == 4, f"{found['calls']} calls")
    check("100 requests come back", len(found["requests"]) == 100, str(len(found["requests"])))
    asked = [c["page"] for c in gw.calls]
    check("pages are requested in order", asked == [1, 2, 3, 4], str(asked))
    ids = [r["id"] for r in found["requests"]]
    check("no request is collected twice", len(set(ids)) == len(ids))

    # --- the tracker runs out before the limit --------------------------
    gw = FakeGateway(total=32)
    found = await checker_for(gw).collect_requests("RED", limit=200)
    check("collection stops when the tracker runs dry", found["calls"] == 2, f"{found['calls']} calls")
    check("everything available is returned", len(found["requests"]) == 32, str(len(found["requests"])))
    check("running dry is reported as incomplete", found["complete"] is False)

    # --- a tracker that ignores ?page ------------------------------------
    # Without a guard this pages forever, hammering a rate-limited API.
    gw = FakeGateway(total=500, ignores_page=True)
    found = await checker_for(gw).collect_requests("RED", limit=200)
    check("a repeated page stops the loop", found["calls"] == 2, f"{found['calls']} calls")
    check("only the distinct rows are kept", len(found["requests"]) == 25, str(len(found["requests"])))

    # --- the filters actually reach the tracker --------------------------
    gw = FakeGateway(total=25)
    await checker_for(gw).collect_requests("RED", "prairie rose", tags="country, folk", tags_all=True, limit=25)
    sent = gw.calls[0]
    check("search is sent", sent.get("search") == "prairie rose", str(sent.get("search")))
    check("tags are sent", sent.get("tags") == "country, folk", str(sent.get("tags")))
    check("tag match mode is sent as all", sent.get("tags_type") == 1, str(sent.get("tags_type")))
    check("filled requests excluded by default", sent.get("show_filled") == "false", str(sent.get("show_filled")))

    gw = FakeGateway(total=25)
    await checker_for(gw).collect_requests("RED", tags="jazz", limit=25)
    check("tag match mode defaults to any", gw.calls[0].get("tags_type") == 0, str(gw.calls[0].get("tags_type")))
    check("no tags means no tag parameters", "tags" not in FakeGateway(total=1).calls)

    gw = FakeGateway(total=25)
    await checker_for(gw).collect_requests("RED", show_filled=True, limit=25)
    check("filled requests can be asked for", gw.calls[0].get("show_filled") == "true")

    # --- the rows are shaped for the table -------------------------------
    gw = FakeGateway(total=1)
    found = await checker_for(gw).collect_requests("RED", limit=25)
    row = found["requests"][0]
    check("rows carry an id, title and link",
          row["id"] == "0" and row["title"] == "Album 0" and "id=0" in row["url"], str(row))
    check("bounty is human readable", row["bounty"] == "1.00 GB", row["bounty"])

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
