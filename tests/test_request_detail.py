"""Check the request renderer against the shape a real Gazelle request has.

The split view used to frame the tracker's own page and could not: RED and OPS
both send X-Frame-Options. So the request is fetched and drawn here instead,
which means this code is now the only thing standing between the tracker's
BBCode and the page.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5098",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(ROOT, "_detail", "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(ROOT, "_detail", "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(ROOT, "_detail", "config"),
        "LOX_STATE_DIR": os.path.join(ROOT, "_detail", "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.checker.bbcode import render, strip  # noqa: E402
from lox.checker.html_clean import looks_like_html, sanitize  # noqa: E402
from lox.checker.request_detail import request_detail  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


# A request as the ajax API returns one. The values follow the page structure
# Gazelle renders -- header, terms, votes, description, comments -- with no real
# request's identifiers in them.
RAW = {
    "requestId": 41688,
    "requestorId": 1087,
    "requestorName": "someone",
    "timeAdded": "2016-10-13 18:26:39",
    "canVote": True,
    "minimumVote": 20971520,
    "voteCount": 73,
    "lastVote": "2026-07-27 11:02:00",
    "topContributors": [
        {"userId": 1087, "userName": "someone", "bounty": 751619276800},
        {"userId": 3347, "userName": "another", "bounty": 94371840000},
    ],
    "totalBounty": 862650567227,
    "categoryId": 1,
    "categoryName": "Music",
    "title": "Divadlo Archa &acute;97",
    "year": 1997,
    "image": "https://example.invalid/cover.jpg",
    "description": (
        "[color=#ff0000][b]Notes: unfillable, the CD is an audience recording. "
        "[u]Do not fill without contacting staff.[/u][/b][/color]\n\n"
        "[size=5][b][artist]Some Artist[/artist] - Divadlo Archa[/b][/size]\n"
        "[b]Country:[/b] Czech Republic\n"
        "[b]Format:[/b] CDr, Unofficial Release\n\n"
        "[size=4][b]Tracklist[/b][/size]\n[b]1.[/b] Divadlo Archa [i](01:14:09)[/i]\n\n"
        "More information: https://www.discogs.com/release/9190366"
    ),
    "musicInfo": {
        "composers": [],
        "dj": [],
        "artists": [{"id": 32169, "name": "Some Artist"}],
        "with": [{"id": 5, "name": "A Guest"}],
        "conductor": [],
        "remixedBy": [],
        "producer": [],
    },
    "catalogueNumber": "",
    "recordLabel": "Not On Label",
    "releaseType": "Bootleg",
    "bitrateList": ["Lossless"],
    "formatList": ["FLAC"],
    "mediaList": ["CD", "WEB"],
    "logCue": "",
    "isFilled": False,
    "tags": {"1": "electronic", "2": "ambient", "3": "drone"},
    "comments": [
        {
            "postId": 351974,
            "authorId": 1087,
            "name": "someone",
            "addedTime": "2019-07-14 09:12:00",
            "comment": "FLACs around the net appear to be [b]lossy transcodes[/b].",
        },
        {
            "postId": 753046,
            "authorId": 34191,
            "name": "another",
            "addedTime": "2023-01-02 20:00:00",
            "comment": "[quote=someone]I'm on this, nobody move.[/quote]\nYayyy!",
            "editedUsername": "another",
            "editedTime": "2023-01-03 08:00:00",
        },
    ],
    "commentPage": 1,
    "commentPages": 1,
    # A field neither tracker documents, to prove nothing gets dropped.
    "someNewField": "surprise",
}


class FakeApi:
    base_url = "https://tracker.invalid"


class FakeGateway:
    """Stands in for the real gateway; records what it was asked for."""

    def __init__(self, raw):
        self.raw = raw
        self.asked = None

    async def get_request(self, code, request_id):
        self.asked = (code, request_id)
        return self.raw

    def api(self, _code):
        return FakeApi()

    def request_url(self, _code, request_id):
        return f"https://tracker.invalid/requests.php?action=view&id={request_id}"


async def main() -> int:
    # --- BBCode is escaped before anything is turned back into markup ---
    hostile = render('<script>alert(1)</script>[b]bold[/b]')
    check("markup in the source is inert", "<script>" not in hostile, hostile[:60])
    check("the escaped form is what shows", "&lt;script&gt;" in hostile, hostile[:40])
    check("known tags still render", "<strong>bold</strong>" in hostile, hostile[-30:])

    check("javascript urls are not linked",
          "javascript" not in render("[url=javascript:alert(1)]click[/url]").lower(),
          render("[url=javascript:alert(1)]click[/url]"))
    # Left as the literal text it was, which is the point: it never reaches a
    # style attribute, so there is nothing for the browser to interpret.
    check("a colour that is not a colour never becomes a style",
          "style=" not in render("[color=expression(alert(1))]x[/color]"),
          render("[color=expression(alert(1))]x[/color]"))
    check("a url in an image src is checked the same way",
          "<img" not in render("[img]javascript:alert(1)[/img]"),
          render("[img]javascript:alert(1)[/img]"))
    check("an http url becomes a link",
          '<a href="https://example.invalid/a"' in render("[url=https://example.invalid/a]see[/url]"),
          render("[url=https://example.invalid/a]see[/url]"))
    check("a bare url is linked too",
          "<a href=" in render("see https://example.invalid/x for more"),
          render("see https://example.invalid/x for more"))
    check("code blocks are not reprocessed",
          "[b]" in render("[code][b]not bold[/b][/code]"), render("[code][b]not bold[/b][/code]"))
    check("quotes carry who said it",
          "<cite>someone</cite>" in render("[quote=someone]hi[/quote]"), render("[quote=someone]hi[/quote]"))
    check("lists become lists", "<li>one</li>" in render("[list][*]one[*]two[/list]"),
          render("[list][*]one[*]two[/list]"))
    check("newlines survive as breaks", render("a\nb") == "a<br>b", render("a\nb"))
    check("strip leaves the words only", strip("[b]Hello[/b] [i]there[/i]") == "Hello there",
          strip("[b]Hello[/b] [i]there[/i]"))

    # --- HTML the tracker already rendered is cleaned, not escaped ---
    # RED renders the description before sending it. Escaping that showed the
    # reader "<a rel=... href=...>" as text instead of a link.
    pre = '<a rel="noreferrer" target="_blank" href="https://www.deezer.com/en/album/880644682">an album</a>'
    check("a tracker's own markup is recognised", looks_like_html(pre), pre[:30])
    cleaned = sanitize(pre)
    check("its link survives as a link", cleaned.startswith("<a ") and "an album</a>" in cleaned, cleaned)
    check("and it is not shown as text", "&lt;a" not in cleaned, cleaned[:40])
    check("links are forced to open safely",
          'target="_blank"' in cleaned and 'rel="noopener noreferrer"' in cleaned, cleaned)

    check("scripts go, contents and all",
          sanitize("before<script>alert(1)</script>after") == "beforeafter",
          sanitize("before<script>alert(1)</script>after"))
    check("so do iframes", "<iframe" not in sanitize('<iframe src="https://x.invalid"></iframe>'),
          sanitize('<iframe src="https://x.invalid"></iframe>'))
    handler = sanitize('<img src="https://a.invalid/x.png" onerror="alert(1)">')
    check("event handlers are stripped", "onerror" not in handler, handler)
    check("a javascript href is dropped", "javascript:" not in sanitize('<a href="javascript:alert(1)">x</a>'),
          sanitize('<a href="javascript:alert(1)">x</a>'))
    check("url() in a style is dropped",
          "url(" not in sanitize('<span style="background-color: url(javascript:x)">y</span>'),
          sanitize('<span style="background-color: url(javascript:x)">y</span>'))
    check("a colour in a style is kept",
          'style="color: #ff0000"' in sanitize('<span style="color: #ff0000">red</span>'),
          sanitize('<span style="color: #ff0000">red</span>'))
    check("stray end tags cannot break out",
          sanitize("</div></div>text") == "text", sanitize("</div></div>text"))
    check("unclosed tags are closed", sanitize("<b>bold") == "<b>bold</b>", sanitize("<b>bold"))
    check("plain BBCode is not mistaken for html", not looks_like_html("[b]hi[/b] see [url]x[/url]"), "")

    # --- the whole record, laid out ---
    gateway = FakeGateway(RAW)
    d = await request_detail(gateway, "RED", 41688)

    check("the tracker was asked for that request", gateway.asked == ("RED", 41688), str(gateway.asked))
    check("the title is unescaped", d["title"] == "Divadlo Archa ´97", d["title"])
    check("the artist is pulled from musicInfo", d["artist"] == "Some Artist", d["artist"])
    check("the year is kept", d["year"] == "1997", d["year"])
    check("it links back to the tracker page",
          d["url"].endswith("requests.php?action=view&id=41688"), d["url"])

    check("the terms are all there",
          (d["bitrates"], d["formats"], d["media"]) == (["Lossless"], ["FLAC"], ["CD", "WEB"]),
          str([d["bitrates"], d["formats"], d["media"]]))
    check("release type and label survive",
          (d["release_type"], d["record_label"]) == ("Bootleg", "Not On Label"),
          str([d["release_type"], d["record_label"]]))
    check("the bounty is readable", d["bounty"] == "803.41 GB", d["bounty"])
    check("so is the vote cost", d["minimum_vote"] == "20.00 MB", d["minimum_vote"])
    check("votes are counted", d["votes"] == 73, str(d["votes"]))
    check("contributors keep their names and shares",
          [c["name"] for c in d["contributors"]] == ["someone", "another"]
          and d["contributors"][0]["bounty"] == "700.00 GB",
          str(d["contributors"]))
    check("tags come out of the id map", d["tags"] == ["electronic", "ambient", "drone"], str(d["tags"]))
    check("the cast is grouped by role",
          [g["role"] for g in d["people"]] == ["Artists", "With"], str(d["people"]))
    check("an unfilled request says so", d["filled"] is False and d["torrent_url"] == "", str(d["filled"]))

    # --- description and comments, rendered ---
    body = d["description_html"]
    check("the description is rendered, not raw", "[b]" not in body and "<strong>" in body, body[:70])
    check("the warning keeps its colour", 'style="color: #ff0000"' in body, body[:90])
    check("sizes become sizes", "font-size:" in body, body[:120])
    check("the artist tag links to the tracker",
          "https://tracker.invalid/artist.php?artistname=Some%20Artist" in body, body[:200])
    check("the discogs link is a link", '<a href="https://www.discogs.com/release/9190366"' in body,
          body[-160:])

    check("every comment is kept", len(d["comments"]) == 2, str(len(d["comments"])))
    check("comments are rendered too", "<strong>lossy transcodes</strong>" in d["comments"][0]["html"],
          d["comments"][0]["html"])
    check("a quoted comment keeps the quote", "<blockquote" in d["comments"][1]["html"],
          d["comments"][1]["html"][:60])
    check("an edit is attributed",
          d["comments"][1]["edited_by"] == "another" and d["comments"][1]["edited"] != "",
          str([d["comments"][1]["edited_by"], d["comments"][1]["edited"]]))

    # --- nothing the tracker sent is thrown away ---
    check("unknown fields are carried through", d["extra"].get("someNewField") == "surprise", str(d["extra"]))

    # --- RED's shape: rendered description, BBCode alongside, numeric type ---
    red = await request_detail(
        FakeGateway({
            **RAW,
            "releaseType": 11,
            "releaseName": "Live album",
            "description": '<a rel="noreferrer" href="https://www.deezer.com/en/album/880644682">link</a>',
            "bbDescription": "released July 31, 2026",
            "groupId": 0,
            "comments": [{"postId": 1, "name": "someone", "addedTime": "2026-01-01 00:00:00",
                          "comment": "<strong>already</strong> html"}],
        }),
        "RED", 41688,
    )
    check("the release type is a name, not a number", red["release_type"] == "Live album", red["release_type"])
    check("the BBCode form wins when both are sent",
          red["description_html"] == "released July 31, 2026", red["description_html"])
    check("a comment that is already html stays html",
          red["comments"][0]["html"] == "<strong>already</strong> html", red["comments"][0]["html"])
    check("internal ids do not become facts",
          "groupId" not in red["extra"] and "bbDescription" not in red["extra"], str(red["extra"]))

    # A tracker that sends only rendered HTML, with no BBCode to prefer.
    html_only = await request_detail(
        FakeGateway({**RAW, "description": '<b>bold</b> and <a href="https://x.invalid">a link</a>',
                     "bbDescription": ""}),
        "OPS", 1,
    )
    check("html-only descriptions are cleaned and kept",
          "<b>bold</b>" in html_only["description_html"] and "&lt;b&gt;" not in html_only["description_html"],
          html_only["description_html"])

    # And one that sends BBCode in the field RED puts HTML in.
    bb_only = await request_detail(
        FakeGateway({**RAW, "description": "[b]bold[/b]", "bbDescription": ""}), "OPS", 1)
    check("BBCode in the same field still renders",
          bb_only["description_html"] == "<strong>bold</strong>", bb_only["description_html"])

    # A numeric release type with no name beside it resolves from the tracker's
    # own table, where the same number means different things.
    numeric = await request_detail(FakeGateway({**RAW, "releaseType": 11, "releaseName": ""}), "RED", 1)
    check("a bare release type number is resolved", numeric["release_type"] not in ("", "11"),
          numeric["release_type"])

    # --- a filled request ---
    filled = await request_detail(
        FakeGateway({**RAW, "isFilled": True, "fillerName": "filler", "torrentId": 12345,
                     "timeFilled": "2026-01-01 00:00:00"}),
        "OPS", 41688,
    )
    check("a filled request names who filled it", filled["filled_by"] == "filler", filled["filled_by"])
    check("and links the torrent that did",
          filled["torrent_url"] == "https://tracker.invalid/torrents.php?torrentid=12345",
          filled["torrent_url"])

    # --- an empty response does not explode ---
    blank = await request_detail(FakeGateway({}), "RED", 7)
    check("an empty response still returns a shape",
          blank["id"] == "7" and blank["comments"] == [] and blank["description_html"] == "",
          str([blank["id"], blank["comments"]]))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
