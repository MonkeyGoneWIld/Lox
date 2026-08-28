"""Browsing that leads somewhere, and a download that says what it actually is.

Four things this pins, all of which shipped broken.

**Browse led nowhere.** The channel list asked the private gateway for its page
with the argument in the request body; ``page.get`` reads it out of the query
string under ``gateway_input``, with an upper-case ``PAGE`` inside. Every
channel request was therefore answered ``MISSING_PARAMETER_PAGE``, dropped
through to the editorial genres, and handed the page a slug of the form
``genre:132`` -- which :meth:`channel` then rejected as invalid, so every card
in the fallback led to an error message. Both halves are checked here.

**New releases was empty.** ``/editorial/<genre>/releases`` is region-dependent
and returns nothing for most of the world, and it was the only source. There
are three now, tried in order, and the payload names whichever answered so a
chart is never quietly passed off as this week's records.

**A lossy download looked exactly like a lossless one.** The folder was named
``[WEB FLAC]`` before a single stream URL had been resolved, so a release
Deezer serves as MP3 landed in a folder claiming to be lossless -- the one
mistake in this pipeline a tracker will not forgive.

**And a retired setting complained for ever.** ``image.ptpimg_key`` was written
by an older lox; ptpimg has since been removed, and every save afterwards
reported "Could not apply: image.ptpimg_key" with no way to clear it short of
editing settings.toml by hand.
"""

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_browse")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5123",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)
os.makedirs(os.environ["LOX_SETTINGS_DIR"], exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


# ----------------------------------------------------------------------
# A Deezer that answers the way the real one does
# ----------------------------------------------------------------------


class FakeGW:
    """Stands in for DeezerGW, recording what was asked for."""

    def __init__(self, pages=None, public=None):
        self.pages = pages or {}
        self.public_data = public or {}
        self.calls: list[tuple[str, dict, dict]] = []
        self.public_calls: list[str] = []

    async def call(self, method, payload=None, retries=2, query=None):
        from lox.deezer.gw import DeezerGWError

        self.calls.append((method, payload or {}, query or {}))
        blob = json.loads((query or {}).get("gateway_input") or "{}")
        page = blob.get("PAGE")
        if page is None:
            raise DeezerGWError("gw-light error for page.get: {'MISSING_PARAMETER_PAGE': ...}")
        if page not in self.pages:
            raise DeezerGWError(f"REQUEST_ERROR: Channel identifier format {page!r} is incorrect")
        return self.pages[page]

    async def public(self, path, params=None):
        from lox.deezer.gw import DeezerGWError

        self.public_calls.append(path)
        if path in self.public_data:
            return self.public_data[path]
        raise DeezerGWError(f"nothing at {path}")


def channel_page(*channels):
    """A gateway channels page carrying the given channel cards."""
    return {
        "title": "All Channels",
        "sections": [
            {
                "title": "Genres",
                "module_id": "83718b7b",
                "items": [
                    {
                        "type": "channel",
                        "background_color": "#3448FC",
                        "data": {
                            "type": "channel",
                            "id": f"uuid-{slug}",
                            "title": title,
                            "slug": slug,
                            "background_color": "#3448FC",
                            "pictures": [{"md5": "abc123", "type": "misc"}],
                            "__TYPE__": "channel",
                        },
                    }
                    for slug, title in channels
                ],
            }
        ],
    }


def album_item(album_id, title, date):
    return {
        "type": "album",
        "data": {
            "ALB_ID": album_id,
            "ALB_TITLE": title,
            "ALB_PICTURE": "cover123",
            "ART_ID": "77",
            "ART_NAME": "Somebody",
            "ORIGINAL_RELEASE_DATE": date,
            "__TYPE__": "album",
        },
    }


def browse_checks() -> None:
    from lox.deezer.explore import Explorer, genre_of

    # --- the gateway is asked the way it wants to be asked -------------
    gw = FakeGW(pages={"channels/explore": channel_page(("pop", "Pop"), ("rap", "Rap"))})
    ex = Explorer(gw)  # type: ignore[arg-type]
    channels = asyncio.run(ex.channels())

    _method, payload, query = gw.calls[0]
    check("page.get sends its argument in the query string", "gateway_input" in query, str(sorted(query)))
    check("and nothing in the body, which is what it ignores", payload == {}, str(payload))
    blob = json.loads(query["gateway_input"])
    check("with an upper-case PAGE, which is the key it looks for",
          blob.get("PAGE") == "channels/explore", str(sorted(blob)))
    check("and a SUPPORT map, or the modules come back empty",
          isinstance(blob.get("SUPPORT"), dict) and "grid" in blob["SUPPORT"], "")

    check("real channels come back", [c["slug"] for c in channels] == ["pop", "rap"],
          str([c["slug"] for c in channels]))
    check("each with its artwork, which lives in a pictures list",
          all(c["image"] and "abc123" in c["image"] for c in channels), str(channels[0]["image"]))
    check("and its colour, which is all some of them have",
          channels[0]["colour"] == "#3448FC", str(channels[0]["colour"]))
    check("and the strip it came from, so ninety-eight cards are not one grid",
          all(c["group"] == "Genres" for c in channels), str(channels[0].get("group")))

    # --- an album card carries what a list of them needs ---------------
    gw = FakeGW(pages={
        "channels/explore": channel_page(("pop", "Pop")),
        "channels/rock": {
            "title": "Rock",
            "sections": [{"title": "New", "module_id": "m1",
                          "items": [album_item("11", "Newer", "2026-08-01"),
                                    album_item("22", "Older", "1998-01-01")]}],
        },
    })
    ex = Explorer(gw)  # type: ignore[arg-type]
    page = asyncio.run(ex.channel("rock"))
    items = page["sections"][0]["items"]
    check("a channel page renders its modules", len(items) == 2, str(len(items)))
    check("and each album says when it came out",
          [i["date"] for i in items] == ["2026-08-01", "1998-01-01"], str([i["date"] for i in items]))
    check("and who made it, by id as well as by name",
          items[0]["artist_id"] == "77", items[0].get("artist_id", ""))
    check("the module keeps its id, so a scan can re-run it later",
          page["sections"][0]["id"] == "m1", str(page["sections"][0]["id"]))

    # --- a genre is a page, not a slug that cannot resolve -------------
    check("a genre slug is recognised as one", genre_of("genre:132") == "132", str(genre_of("genre:132")))
    check("and a real slug is not", genre_of("rap") is None, str(genre_of("rap")))

    gw = FakeGW(
        pages={},
        public={
            "/genre/132": {"id": 132, "name": "Pop"},
            "/editorial/132/releases": {"data": []},
            "/chart/132/albums": {"data": [
                {"id": 1, "title": "Fresh", "artist": {"id": 9, "name": "A"}, "release_date": None},
            ]},
            "/album/1": {"release_date": "2026-08-01", "nb_tracks": 10},
            "/chart/132": {"albums": {"data": [
                {"id": 5, "title": "Charting", "artist": {"id": 9, "name": "A"}}]},
                "tracks": {"data": []},
                "artists": {"data": [{"id": 9, "name": "A"}]}},
        },
    )
    ex = Explorer(gw)  # type: ignore[arg-type]
    genre_page = asyncio.run(ex.channel("genre:132"))
    check("a genre:<id> slug opens a page rather than raising",
          genre_page["title"] == "Pop", str(genre_page.get("title")))
    check("built out of what is new and what is charting in it",
          [s["title"] for s in genre_page["sections"]][:1] == ["Recent, from the chart"]
          or "Top albums" in [s["title"] for s in genre_page["sections"]],
          str([s["title"] for s in genre_page["sections"]]))

    # --- new releases has three sources, and names the one that answered
    editorial = FakeGW(public={"/editorial/0/releases": {"data": [
        {"id": 1, "title": "Brand new", "artist": {"id": 2, "name": "A"}, "release_date": "2026-08-20"}]}})
    got = asyncio.run(Explorer(editorial).new_releases(0, 10))  # type: ignore[arg-type]
    check("the editorial feed answers first", got["source"] == "editorial", got["source"])
    check("and says nothing, because nothing needs explaining", got["note"] == "", got["note"])

    channelled = FakeGW(
        pages={"channels/new": {"title": "New releases", "sections": [
            {"title": "Popular", "module_id": "m", "items": [album_item("3", "Out now", "2026-08-25")]}]}},
        public={"/editorial/0/releases": {"data": []}},
    )
    got = asyncio.run(Explorer(channelled).new_releases(0, 10))  # type: ignore[arg-type]
    check("the new-releases channel stands in when it is empty", got["source"] == "channel", got["source"])
    check("and says so", "editorial feed" in got["note"], got["note"])
    check("with the releases it found", [a["title"] for a in got["results"]] == ["Out now"], "")

    charted = FakeGW(
        pages={},
        public={
            "/editorial/116/releases": {"data": []},
            "/chart/116/albums": {"data": [
                {"id": 7, "title": "Recent", "artist": {"id": 2, "name": "A"}},
                {"id": 8, "title": "Ancient", "artist": {"id": 2, "name": "A"}},
            ]},
            "/album/7": {"release_date": "2026-08-20", "nb_tracks": 12},
            "/album/8": {"release_date": "1993-11-09", "nb_tracks": 12},
        },
    )
    got = asyncio.run(Explorer(charted).new_releases(116, 10))  # type: ignore[arg-type]
    check("a genre with no feed falls back to its chart", got["source"] == "chart", got["source"])
    check("and the fallback is labelled, never passed off as new",
          "chart albums" in got["note"], got["note"])
    check("with the dates fetched rather than assumed",
          "/album/7" in charted.public_calls, str(charted.public_calls))
    check("so a 1993 record does not appear under new releases",
          [a["title"] for a in got["results"]] == ["Recent"], str([a["title"] for a in got["results"]]))

    empty = FakeGW(public={"/editorial/9/releases": {"data": []}, "/chart/9/albums": {"data": []}})
    got = asyncio.run(Explorer(empty).new_releases(9, 10))  # type: ignore[arg-type]
    check("and an empty answer says which sources were tried", got["source"] == "none", got["source"])


# ----------------------------------------------------------------------
# A download that says what it actually is
# ----------------------------------------------------------------------


def quality_checks() -> None:
    from lox.deezer.download import Downloader, DownloadJob, TrackDownload

    job = DownloadJob(id="j", album_id="1", title="T", artist="A")
    job.tracks = [TrackDownload(id="1", title="a", artist="A", number=1, disc=1, fmt="FLAC"),
                  TrackDownload(id="2", title="b", artist="A", number=2, disc=1, fmt="FLAC")]
    check("an all-FLAC download is not lossy", not job.lossy, "")
    check("and reports itself as FLAC", job.quality == "FLAC", job.quality)

    job.tracks[1].fmt = "MP3_320"
    check("one lossy track is enough to ask about", job.lossy, "")
    check("and the release is the worst of what came back", job.quality == "MP3_320", job.quality)
    check("the payload carries the answer to the browser",
          job.as_dict()["lossy"] is True and job.as_dict()["quality"] == "MP3_320", "")
    check("and whether anybody has been asked yet",
          job.as_dict()["decision"] == "", job.as_dict()["decision"])

    # --- the per-download override ------------------------------------
    downloader = Downloader.__new__(Downloader)
    downloader.preferred_format = "FLAC"
    downloader.allow_fallback = False
    check("with the fallback off, only the preferred quality is asked for",
          downloader.formats() == ("FLAC",), str(downloader.formats()))
    check("but one download can still say take whatever there is",
          downloader.formats(allow_lossy=True) == ("FLAC", "MP3_320", "MP3_128"),
          str(downloader.formats(allow_lossy=True)))
    downloader.allow_fallback = True
    check("and with it on, nothing changes", downloader.formats() == downloader.formats(allow_lossy=True), "")

    # --- the folder is named for what is in it -------------------------
    folder = os.path.join(BASE, "downloads", "A - B (2026) [WEB FLAC]")
    os.makedirs(folder, exist_ok=True)
    track = os.path.join(folder, "01. x.mp3")
    with open(track, "w", encoding="utf-8") as handle:
        handle.write("x")

    job = DownloadJob(id="k", album_id="2", title="B", artist="A", folder=folder)
    job.tracks = [TrackDownload(id="1", title="x", artist="A", number=1, disc=1, fmt="MP3_320", path=track)]
    downloader._settle_folder(job)  # noqa: SLF001
    check("a lossy download is not left in a folder claiming to be FLAC",
          job.folder.endswith("[WEB MP3]"), job.folder or "")
    check("and the folder on disk moved with it", os.path.isdir(job.folder or ""), job.folder or "")
    check("with the track paths corrected", os.path.isfile(job.tracks[0].path or ""), job.tracks[0].path or "")

    flac_folder = os.path.join(BASE, "downloads", "C - D (2026) [WEB FLAC]")
    os.makedirs(flac_folder, exist_ok=True)
    job = DownloadJob(id="m", album_id="3", title="D", artist="C", folder=flac_folder)
    job.tracks = [TrackDownload(id="1", title="x", artist="C", number=1, disc=1, fmt="FLAC")]
    downloader._settle_folder(job)  # noqa: SLF001
    check("and a FLAC download is left alone", job.folder == flac_folder, job.folder or "")


# ----------------------------------------------------------------------
# A setting this version no longer has
# ----------------------------------------------------------------------


def retired_settings_checks() -> None:
    from lox.config.store import SettingsStore

    directory = os.path.join(BASE, "retired")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "settings.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('[image]\nptpimg_key = "dead"\nimgbb_key = "alive"\n')

    store = SettingsStore(directory)
    check("a retired key is read in like any other",
          "image.ptpimg_key" in store.values, str(sorted(store.values)))

    from lox import cfg

    failed = store.apply_to(cfg)
    check("but applying it is not reported as a failure to act on",
          "image.ptpimg_key" not in failed, str(failed))
    check("it is swept out of the file instead",
          "image.ptpimg_key" not in store.values, str(sorted(store.values)))
    check("and the settings around it are untouched",
          store.values.get("image.imgbb_key") == "alive", str(store.values.get("image.imgbb_key")))
    with open(path, encoding="utf-8") as handle:
        written = handle.read()
    check("so it does not come back on the next start", "ptpimg" not in written, written.strip())

    # A key the page still knows about, that the config has nowhere to put, is
    # a real failure and still says so -- the sweep is only for keys this
    # version has stopped offering at all.
    class Bare:
        """A config with no image section for the key to land in."""

    store._values["image.imgbb_key"] = "alive"  # noqa: SLF001
    check("a setting the page still offers is reported when it cannot be applied",
          "image.imgbb_key" in store.apply_to(Bare()), str(store.apply_to(Bare())))
    check("and is left in the file, because it is not the one that is retired",
          "image.imgbb_key" in store.values, str(sorted(store.values)))


def main() -> int:
    browse_checks()
    quality_checks()
    retired_settings_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
