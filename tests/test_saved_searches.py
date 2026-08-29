"""A saved search is a link, and several of them are one scan.

Saving one used to take three answers: a name you invented, a kind picked from
a dropdown of six, and an id you had to go and dig out of a URL by hand. All
three describe something Deezer already knows about the link that was on the
clipboard, so the link is now the whole of the input and the rest is asked for.

Running them was one at a time, and each run overwrote the box the last one had
filled -- so "check everything I follow" was six presses and only the last one
survived. They are scanned together now, and a link-backed search hands the
scan its own link, so the albums it finds are labelled with the playlist or the
artist they came from rather than all reading "Direct link".

The other half is what is *not* here any more. Whether an album already looked
up gets paid for again is the recheck window's business and nothing else's;
there was a tickbox beside the Scan button saying the same thing in fewer
words, so one decision had two controls that could contradict each other.

Everything below runs the real classes against a stand-in for Deezer. Nothing
here touches the network or a tracker.
"""

import asyncio
import inspect
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_savedsearches")
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
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)
sys.path.insert(0, os.path.dirname(ROOT))

from lox.checker.missing import MissingScanner  # noqa: E402
from lox.checker.store import CheckerStore  # noqa: E402
from lox.checker.watchlists import WatchlistManager  # noqa: E402
from lox.deezer.gw import DeezerGWError, parse_artist_id  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


MODULE_ID = "8fc2bf46-1111-2222-3333-444455556666"


class FakeGW:
    """The handful of Deezer reads a saved search needs, answered locally."""

    async def playlist(self, playlist_id):
        return {"id": playlist_id, "title": "Weekly Rap Drops", "nb_tracks": 312}

    async def album(self, album_id):
        return {"id": album_id, "title": "Selected Ambient Works", "artist": {"name": "Aphex Twin"}}

    async def public(self, path, params=None):
        if path.startswith("/artist/") and path.count("/") == 2:
            return {"id": "27", "name": "Aphex Twin", "nb_album": 38}
        if path.endswith("/albums"):
            return {"data": [
                {"id": 1, "title": "One", "record_type": "album", "release_date": "2001-01-01",
                 "artist": {"id": 27, "name": "Aphex Twin"}},
                {"id": 2, "title": "Two", "record_type": "ep", "release_date": "2003-01-01",
                 "artist": {"id": 27, "name": "Aphex Twin"}},
            ]}
        if path.endswith("/top"):
            return {"data": []}
        return {}


def manager() -> WatchlistManager:
    """A manager on a clean store, with the page-scraping surfaces stubbed."""
    store = CheckerStore(os.path.join(BASE, "state"))
    store.clear("watchlists")
    m = WatchlistManager(FakeGW(), store)

    async def module(module_id):
        return {"id": module_id, "title": "Hip Hop Novelties",
                "items": [{"id": "1", "type": "album"}, {"id": "2", "type": "album"},
                          {"id": "3", "type": "playlist"}]}

    async def chart(genre_id, limit=50):
        return {"albums": [{"id": "900"}, {"id": "901"}]}

    m.explorer.module = module
    m.explorer.chart = chart
    return m


async def main() -> int:
    # --- a link is the whole of the input -----------------------------------
    m = manager()

    kind, target, name, count = await m.resolve("https://www.deezer.com/playlist/2228601362")
    check("a playlist link resolves to a playlist",
          (kind, target) == ("playlist", "2228601362"), f"{kind} {target}")
    check("named from Deezer, not from you", name == "Weekly Rap Drops", name)
    check("and sized, so the list can say which playlist this is", count == 312, str(count))

    kind, target, name, count = await m.resolve(
        f"https://www.deezer.com/en/channels/module/{MODULE_ID}")
    check("a channel module link resolves to the module",
          (kind, target, name) == ("module", MODULE_ID, "Hip Hop Novelties"), f"{kind} {name}")
    check("counting the albums on it, not everything on it", count == 2, str(count))

    kind, target, name, count = await m.resolve("https://www.deezer.com/artist/27")
    check("an artist link resolves to the artist",
          (kind, target, name, count) == ("artist", "27", "Aphex Twin", 38), f"{kind} {name} {count}")

    kind, target, name, _ = await m.resolve("https://www.deezer.com/album/12345")
    check("an album link is named with its artist, which is how you would say it",
          (kind, target) == ("album", "12345") and name.startswith("Aphex Twin"), name)

    try:
        await m.resolve("https://example.com/something")
        check("a link Deezer does not know is refused", False, "no error raised")
    except DeezerGWError as e:
        check("a link Deezer does not know is refused", True)
        check("and the message names what would work",
              all(w in str(e) for w in ("playlist", "module", "artist", "album")), str(e))

    # --- saving the same link twice is one saved search ---------------------
    first, already = await m.save_link("https://www.deezer.com/playlist/2228601362")
    check("saving a link keeps it", not already and first.name == "Weekly Rap Drops", first.name)
    again, already = await m.save_link("https://www.deezer.com/playlist/2228601362")
    check("saving it again is the same one, not a second",
          already and again.id == first.id, f"{already} {again.id} {first.id}")
    check("so the list does not fill up with duplicates", len(m.saved()) == 1, str(len(m.saved())))

    # --- a name you are stuck with is a name you can change -----------------
    renamed = m.rename(first.id, "Tuesday drops")
    check("a saved search can be renamed", renamed is not None and renamed.name == "Tuesday drops",
          renamed.name if renamed else "None")
    check("renaming something that is gone says so", m.rename("nope", "x") is None)

    # --- several at once, and each one hands over its own link --------------
    artist, _ = await m.save_link("https://www.deezer.com/artist/27")
    module_watch, _ = await m.save_link(f"https://www.deezer.com/en/channels/module/{MODULE_ID}")

    sources, problems = await m.scan_sources([first.id, artist.id, module_watch.id])
    check("several saved searches scan as one set of sources", len(sources) == 3, str(sources))
    check("each contributing its own link, so albums keep the name of where they came from",
          sources == ["https://www.deezer.com/playlist/2228601362",
                      "https://www.deezer.com/artist/27",
                      f"https://www.deezer.com/en/channels/module/{MODULE_ID}"], str(sources))
    check("and nothing went wrong", problems == [], str(problems))
    check("using one marks it used, so the list can say when it last ran",
          (m.get(first.id) or first).last_run, "")

    sources, _ = await m.scan_sources([first.id, first.id])
    check("the same search twice is one source, not two", len(sources) == 1, str(sources))

    _, problems = await m.scan_sources(["gone"])
    check("a search that is no longer there is reported rather than ignored",
          len(problems) == 1 and "no longer saved" in problems[0]["error"], str(problems))

    # A genre chart is not a link, so it is run and its albums become the
    # sources. Kept working because saved searches predate this change.
    chart_watch = m.create("Pop chart", "chart", "132")
    sources, problems = await m.scan_sources([chart_watch.id])
    check("a saved search that is not a link still scans",
          sources == ["https://www.deezer.com/album/900", "https://www.deezer.com/album/901"],
          str(sources))
    check("with nothing reported against it", problems == [], str(problems))

    # --- an artist link is a scan source in its own right -------------------
    check("an artist URL is recognised at all", parse_artist_id("https://www.deezer.com/artist/27") == "27")
    check("and a bare word is not", parse_artist_id("artist 27") is None)

    scanner = MissingScanner(FakeGW(), gateway=None, store=CheckerStore(os.path.join(BASE, "state")))
    album_sources: dict[str, str] = {}
    events: list[tuple] = []
    await scanner._expand_source(  # noqa: SLF001 - the unit under test
        "https://www.deezer.com/artist/27", album_sources, lambda e, p: events.append((e, p)))
    check("a scan expands an artist link into their releases",
          sorted(album_sources) == ["1", "2"], str(album_sources))
    check("labelled with the artist, not with 'Direct link'",
          set(album_sources.values()) == {"Aphex Twin"}, str(set(album_sources.values())))
    check("and says so as it goes",
          events and events[0][0] == "source_done" and events[0][1]["kind"] == "artist", str(events))

    album_sources = {}
    events = []
    await scanner._expand_source(  # noqa: SLF001
        "https://example.com/nope", album_sources, lambda e, p: events.append((e, p)))
    check("an unusable link names the four that would work",
          events and all(w in events[0][1]["error"] for w in ("playlist", "module", "artist", "album")),
          str(events))

    # --- one control decides whether an answer is paid for twice ------------
    # The tickbox beside the Scan button said the same thing as the recheck
    # window sitting above it, so the two could disagree about one decision.
    signature = inspect.signature(MissingScanner.collect)
    check("collect has no skip_known knob any more",
          "skip_known" not in signature.parameters, str(signature))
    check("the recheck window is what decides it",
          "album_recheck_after_days" in inspect.getsource(MissingScanner._recheck_window), "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
