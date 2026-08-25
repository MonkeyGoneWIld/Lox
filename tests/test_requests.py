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


class IgnoresShowFilled(FakeGateway):
    """OPS, as it actually behaved.

    Asked for unfilled requests it answers with filled ones anyway. Measured on
    a real four-page fetch: 73 of 100 rows came back carrying isFilled, a
    fillerName and a torrentId. Every third row here is unfilled, so a page of
    25 yields 8 usable ones and the parameter is provably being ignored.
    """

    async def call_action(self, tracker: str, action: str, params: dict) -> dict:
        data = await FakeGateway.call_action(self, tracker, action, params)
        for i, row in enumerate(data["results"]):
            if i % 3:
                row.update(
                    {
                        "isFilled": True,
                        "fillerId": 38096,
                        "fillerName": "someone",
                        "torrentId": 3730745,
                        "timeFilled": "2026-08-20 11:14:26",
                    }
                )
        return data


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
    # Not "false". show_filled is a checkbox, and an unticked checkbox sends
    # nothing; PHP reads the non-empty string "false" as true, so spelling the
    # refusal out asked for the opposite of it.
    check("filled requests are excluded by saying nothing",
          "show_filled" not in sent, str(sent.get("show_filled")))

    gw = FakeGateway(total=25)
    await checker_for(gw).collect_requests("RED", show_filled=True, limit=25)
    check("filled requests are asked for the way the form asks",
          gw.calls[0].get("show_filled") == "on", str(gw.calls[0].get("show_filled")))

    # --- a tracker that ignores show_filled -------------------------------
    #
    # OPS does. Asking is not the same as being obeyed, and the answer carries
    # isFilled on every row, so the answer is what gets believed. Without this,
    # three quarters of a paid-for fetch was Deezer lookups against requests
    # that had already been closed.
    gw = IgnoresShowFilled(total=500)
    found = await checker_for(gw).collect_requests("OPS", limit=100)
    check("a tracker sending filled rows anyway was not asked for them",
          "show_filled" not in gw.calls[0], str(gw.calls[0].get("show_filled")))
    kept = found["requests"]
    check("but the filled rows it sends back are dropped",
          len(kept) == 36, f"{len(kept)} kept")
    check("and how many were dropped is reported",
          found["filtered"] == 64, str(found.get("filtered")))
    check("the fetch still costs only the pages asked for",
          found["calls"] == 4, f"{found['calls']} calls")
    check("a short list after filtering is not called complete",
          found["complete"] is False, str(found["complete"]))

    # A page that is entirely filled must not look like the end of the results.
    class AllFilledFirstPage(IgnoresShowFilled):
        async def call_action(self, tracker, action, params):
            data = await IgnoresShowFilled.call_action(self, tracker, action, params)
            if int(params.get("page", 1)) == 1:
                for row in data["results"]:
                    row["isFilled"] = True
            return data

    gw = AllFilledFirstPage(total=500)
    found = await checker_for(gw).collect_requests("OPS", limit=75)
    check("a page of nothing but filled requests does not end the fetch",
          found["calls"] == 3, f"{found['calls']} calls")
    check("and the later pages still contribute",
          len(found["requests"]) > 0, str(len(found["requests"])))

    # Asking for filled ones keeps them.
    gw = IgnoresShowFilled(total=100)
    found = await checker_for(gw).collect_requests("OPS", show_filled=True, limit=25)
    check("asking for filled requests keeps them",
          len(found["requests"]) == 25 and found["filtered"] == 0,
          f"{len(found['requests'])} kept, {found['filtered']} dropped")

    # --- a filled request is finished, and costs nothing to learn it ------
    #
    # Checking one used to run the whole Deezer pipeline against it -- a search,
    # an availability lookup, sometimes a second tracker call -- and when the
    # "is it already up" search then missed, the release was filed under Found
    # as worth uploading. Both halves stop at the tracker's own isFilled.
    class OneRequest:
        """A tracker serving a single request, filled or not."""

        def __init__(self, filled: bool) -> None:
            self.filled = filled
            self.actions: list[str] = []

        async def call_action(self, tracker, action, params):
            self.actions.append(action)
            row = {
                "requestId": 8811, "categoryName": "Music", "title": "Eden Sauvage",
                "year": 2025, "artists": [[{"name": "Los Eclipses"}]],
                "formatList": ["FLAC"], "mediaList": ["WEB"], "bitrateList": ["Lossless"],
                "totalBounty": 1024 ** 3, "timeAdded": "2026-08-20 09:23:07",
                "description": "",
            }
            if self.filled:
                row.update({"isFilled": True, "fillerName": "someone",
                            "torrentId": 3730745, "timeFilled": "2026-08-20 11:14:26"})
            return row if action == "request" else {"results": [row], "pages": 1}

        def can_check(self, tracker):
            return True

        async def get_request(self, tracker, request_id):
            return await self.call_action(tracker, "request", {"id": request_id})

        def request_url(self, tracker, request_id):
            return f"https://example.invalid/requests.php?action=view&id={request_id}"

    class ExplodingDeezer:
        """Any Deezer call at all is the bug this guards against."""

        async def search_albums(self, *_a, **_k):
            raise AssertionError("reached Deezer")

        async def availability(self, *_a, **_k):
            raise AssertionError("reached Deezer availability")

    from lox.checker.deezer_requests import DeezerRequestChecker as DRC  # noqa: PLC0415

    gw = OneRequest(filled=True)
    checker = DRC(gw=ExplodingDeezer(), gateway=gw, store=None)  # type: ignore[arg-type]
    matches = await checker.check_many("OPS", ["8811"], skip_known=False)
    got = matches[0]
    check("a filled request is reported as filled", got.status == "filled", got.status)
    check("and says who filled it", "someone" in (got.reason or ""), str(got.reason))
    check("it never reaches Deezer", gw.actions == ["request"], str(gw.actions))
    check("it is not offered as fillable", got.fillable is False, str(got.fillable))
    check("and counts as already on the tracker",
          got.already_on_tracker is True, str(got.already_on_tracker))
    check("the age it has sat open is reported", got.created == "2026-08-20 09:23:07", got.created)

    # An open one still goes the whole way.
    gw = OneRequest(filled=False)
    checker = DRC(gw=ExplodingDeezer(), gateway=gw, store=None)  # type: ignore[arg-type]
    matches = await checker.check_many("OPS", ["8811"], skip_known=False)
    check("an open request is still checked against Deezer",
          matches[0].status == "error" and "reached Deezer" in str(matches[0].reason),
          f"{matches[0].status}: {matches[0].reason}")

    # The listing carries both new columns.
    gw = OneRequest(filled=True)
    rows, _pages, _dropped = await DRC(gw=None, gateway=gw, store=None).search_requests(  # type: ignore[arg-type]
        "OPS", show_filled=True)
    check("a listed request says whether it is filled", rows[0]["filled"] is True, str(rows[0].get("filled")))
    check("and how long it has been open", rows[0]["age"].endswith(("day", "days")), rows[0]["age"])

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

    # "Only specified" is per group and off by default, exactly as the tracker
    # has it. A request states what its author will accept, so one that accepts
    # any media names no media at all -- switching this on hides all of those
    # rather than narrowing the list. On a real OPS search the same ticks return
    # 48 requests with it on and 413 with it off, which is the whole bug.
    check("strict is off unless asked for",
          not any(k.endswith("_strict") for k in red) and not any(k.endswith("_strict") for k in ops),
          str([k for k in list(red) + list(ops) if k.endswith("_strict")]))

    strict_red = build_params("RED", media=["WEB"], encodings=["Lossless"], formats=["FLAC"],
                              strict_media=True)
    check("RED spells the strict flag bitrate_strict",
          "bitrate_strict" in build_params("RED", encodings=["Lossless"], strict_encodings=True))
    check("OPS spells it bitrates_strict",
          "bitrates_strict" in build_params("OPS", encodings=["Lossless"], strict_encodings=True))
    check("each group's strict flag is independent",
          "media_strict" in strict_red and "formats_strict" not in strict_red
          and "bitrate_strict" not in strict_red,
          str(sorted(k for k in strict_red if "strict" in k)))

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
    # --- tracker text arrives HTML-escaped -------------------------------
    # Gazelle escapes its JSON strings for a web page, so rendering them as
    # text -- the only safe way -- showed the entities themselves.
    from lox.checker.gateway import plain

    check("an escaped apostrophe decodes",
          plain("Live Beginnings &#39;88") == "Live Beginnings '88", plain("Live Beginnings &#39;88"))
    check("named entities decode",
          plain("Zsoldos &Aacute;rp&aacute;d") == "Zsoldos Árpád", plain("Zsoldos &Aacute;rp&aacute;d"))
    check("numeric entities decode",
          plain("Erd&#337;k") == "Erdők", plain("Erd&#337;k"))
    check("nothing in, empty string out", plain(None) == "" and plain("") == "")

    # --- a group's artist comes from either of two shapes -----------------
    # browse returns a flat "artist"; torrentgroup, which an album check reads,
    # returns musicInfo with no flat field at all. Missing the second left the
    # artist blank, so a group rendered as "— Bedtime Stories (1994)".
    from lox.checker.missing import _group_artist, _release_type_name

    check("a flat artist field is used", _group_artist({"artist": "Madonna"}) == "Madonna")
    check("musicInfo is read when there is no flat field",
          _group_artist({"musicInfo": {"artists": [{"name": "Madonna"}]}}) == "Madonna")
    check("several artists are joined",
          _group_artist({"musicInfo": {"artists": [{"name": "A"}, {"name": "B"}]}}) == "A & B")
    check("entities decode here too",
          _group_artist({"musicInfo": {"artists": [{"name": "Zsoldos &Aacute;rp&aacute;d"}]}})
          == "Zsoldos Árpád")
    check("an unknown cast yields nothing, not a separator", _group_artist({}) == "")

    # Release-type numbers differ per tracker, so they resolve per tracker.
    check("release type resolves on RED", _release_type_name("RED", 1) == "Album")
    check("17 is Demo on RED but DJ Mix on OPS",
          _release_type_name("RED", 17) == "Demo" and _release_type_name("OPS", 17) == "DJ Mix")
    check("an unmapped tracker names nothing", _release_type_name("DIC", 1) == "")
    check("a missing release type names nothing", _release_type_name("RED", None) == "")

    check("rows carry an id, title and link",
          row["id"] == "0" and row["title"] == "Album 0" and "id=0" in row["url"], str(row))
    check("bounty is human readable", row["bounty"] == "1.00 GB", row["bounty"])

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
