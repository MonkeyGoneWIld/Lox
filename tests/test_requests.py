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
    check("filled requests excluded by default", sent.get("show_filled") == "false", str(sent.get("show_filled")))

    gw = FakeGateway(total=25)
    await checker_for(gw).collect_requests("RED", show_filled=True, limit=25)
    check("filled requests can be asked for", gw.calls[0].get("show_filled") == "true")

    # --- the two trackers do not speak the same language ------------------
    # Transcribed from the live search forms. Getting these wrong does not
    # error, it searches for something else -- RED's WEB is OPS's CD-adjacent
    # DAT -- so each one is pinned.
    from lox.checker.request_filters import build_params, schema

    red = build_params("RED", tags="jazz", tags_all=True, media=["WEB"], encodings=["Lossless"], formats=["FLAC"])
    ops = build_params("OPS", tags="jazz", tags_all=True, media=["WEB"], encodings=["Lossless"], formats=["FLAC"])

    check("RED names the tag mode tags_type", red.get("tags_type") == "1", str(red.get("tags_type")))
    check("OPS names it tag_mode", ops.get("tag_mode") == "all", str(ops.get("tag_mode")))
    check("RED is not sent OPS's tag parameter", "tag_mode" not in red)
    check("OPS is not sent RED's tag parameter", "tags_type" not in ops)

    check("WEB is 7 on RED", red.get("media[]") == [7], str(red.get("media[]")))
    check("WEB is 1 on OPS", ops.get("media[]") == [1], str(ops.get("media[]")))
    check("Lossless is 8 on RED", red.get("bitrates[]") == [8], str(red.get("bitrates[]")))
    check("Lossless is 0 on OPS", ops.get("bitrates[]") == [0], str(ops.get("bitrates[]")))
    check("FLAC happens to agree at 1", red.get("formats[]") == [1] and ops.get("formats[]") == [1])

    check("RED spells the strict flag bitrate_strict", "bitrate_strict" in red, str(sorted(red)))
    check("OPS spells it bitrates_strict", "bitrates_strict" in ops, str(sorted(ops)))

    check("RED indexes the music category", red.get("filter_cat[1]") == 1, str(red.get("filter_cat[1]")))
    check("OPS lists it", ops.get("filter_cat[]") == 0, str(ops.get("filter_cat[]")))

    # Filters one tracker has and the other does not.
    red_extra = build_params("RED", include_old=True, search_descriptions=True, bounty_min="5")
    ops_extra = build_params("OPS", include_old=True, search_descriptions=True, bounty_min="5")
    check("include-old is RED only", "showall" in red_extra and "showall" not in ops_extra)
    check("description search is RED only",
          "include_descriptions" in red_extra and "include_descriptions" not in ops_extra)
    check("a bounty floor is OPS only", ops_extra.get("bounty_min") == "5" and "bounty_min" not in red_extra)

    # A label the tracker does not have must be dropped, never translated.
    check("an unknown label is dropped rather than mistranslated",
          "media[]" not in build_params("RED", media=["BD"]), str(build_params("RED", media=["BD"])))

    # --- an unmapped tracker gets only what needs no IDs ------------------
    unknown = build_params("DIC", tags="jazz", tags_all=True, media=["WEB"], encodings=["Lossless"])
    check("an unmapped tracker sends no ID filters", "media[]" not in unknown and "bitrates[]" not in unknown,
          str(sorted(unknown)))
    check("an unmapped tracker gets both tag spellings",
          unknown.get("tags_type") == 1 and unknown.get("tag_mode") == "all", str(unknown))
    check("the schema says so rather than pretending", schema("DIC")["mapped"] is False)
    check("the schema explains why", "verified" in schema("DIC")["note"])
    check("a mapped schema offers its own options",
          "WEB" in schema("RED")["media"] and schema("RED")["bounty"] is False)
    check("OPS advertises its bounty filter", schema("OPS")["bounty"] is True)

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
