"""Deezer Explore — channels, charts, editorial selections and channel modules.

Three surfaces, and they do not all answer to the same thing.

*Channels* live behind Deezer's private gateway. ``page.get`` takes its
argument in the query string under ``gateway_input``, with an upper-case
``PAGE`` inside it — passing it in the body is what made every channel request
come back ``MISSING_PARAMETER_PAGE``, drop through to the genre fallback, and
then fail a second time because a genre is not a channel slug. Both halves are
fixed here: the call is made the way the browser makes it, and a genre is a
page in its own right rather than a slug that cannot resolve.

*Charts* and *artists* come from the public API, which needs no ARL.

*New releases* is the awkward one. ``/editorial/<genre>/releases`` is
region-dependent and comes back empty for most of the world, which is what made
the tab useless. It is now the first of three sources rather than the only one,
and whichever one answers is named in the payload, so a chart is never quietly
passed off as this week's records.
"""

import asyncio
import json
import re
from datetime import date, timedelta
from typing import Any

import aiohttp

from lox import debug
from lox.deezer.gw import DeezerGW, DeezerGWError

_APP_STATE_RE = re.compile(r"window\.__DZR_APP_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>", re.DOTALL)

# Channel slugs and module IDs arrive from the browser and are interpolated into
# a deezer.com path, so they are constrained rather than trusted.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_MODULE_RE = re.compile(r"^[0-9a-f-]{8,64}$", re.IGNORECASE)

#: A genre browsed as though it were a channel. Deezer has no channel for
#: "Metal" as such, so when the channel list falls back to genres the page
#: behind each card is built here out of public-API calls instead.
GENRE_PREFIX = "genre:"
_GENRE_RE = re.compile(r"^genre:(\d{1,12})$", re.IGNORECASE)

#: How recent a release has to be to count as new. Deezer's own editorial feed
#: runs to roughly a season, and anything older reads as a chart.
RECENT_DAYS = 120

#: Words that say nothing about which genre a name is. "Japanese music" and
#: "Asian Music" share only this, and they are not the same thing.
_VAGUE_WORDS = frozenset({"music", "musique", "and", "the", "of", "hop", "s"})


def _words(value: str) -> list[str]:
    """The meaningful words of a name, lower-cased."""
    from re import split as _split

    return [w for w in _split(r"[^a-z0-9]+", value.lower()) if w and w not in _VAGUE_WORDS]


def _genre_picture(title: str, genres: list[dict[str, Any]]) -> str | None:
    """The editorial genre picture that belongs to a channel, if one does.

    Deezer's channel list and its genre list overlap without agreeing on
    names: "Folk & singer-songwriter" is the "Folk" genre, "Dance & EDM" is
    "Dance", "Rap" is "Rap/Hip Hop", "Electronic" is "Electro". An exact match
    found fourteen of sixty-three, which left most of the grid as plain
    rectangles.

    Deliberately conservative: a wrong photograph on a genre card is worse than
    no photograph, so it takes a shared distinctive word or one name being a
    prefix of the other. A mood or a decade -- Chill, 1990s, Workout -- has no
    genre and gets none.

    Args:
        title: The channel's name.
        genres: Editorial genres, each with a picture.

    Returns:
        A picture URL, or None.
    """
    wanted = _words(title)
    if not wanted:
        return None
    for genre in genres:
        theirs = _words(str(genre.get("title", "")))
        if not theirs:
            continue
        if set(theirs) <= set(wanted) or set(wanted) <= set(theirs):
            return genre["image"]
        # "Electro" for "Electronic": one name begins the other, and both are
        # long enough for that to mean something.
        for mine in wanted:
            if len(mine) >= 5 and any(len(t) >= 5 and (mine.startswith(t) or t.startswith(mine))
                                      for t in theirs):
                return genre["image"]
    return None


#: Channels there is no reason to browse here. lox uploads music; a third of
#: what Deezer serves under "channels" is podcast categories, which cannot be
#: downloaded, cannot be uploaded, and pushed the genres off the first screen.
_PODCAST_RE = re.compile(r"podcast|audiobook", re.IGNORECASE)

#: How many chart albums to look up release dates for when the editorial feed
#: has nothing. Public-API calls are free but not instant, so this is bounded.
_DATE_LOOKUP_LIMIT = 60
_DATE_LOOKUP_CONCURRENCY = 8

#: What the gateway is told this client can render. A module whose type is not
#: listed is left out of the response entirely, so the list is generous.
_SUPPORTED = [
    "album", "artist", "channel", "flow", "livestream", "playlist",
    "radio", "show", "smarttracklist", "user", "video-link",
]
_PAGE_SUPPORT: dict[str, list[str]] = {
    "ads": [],
    "deeplink": ["deeplink"],
    "event": ["event"],
    "grid": list(_SUPPORTED),
    "grid-preview-one": list(_SUPPORTED),
    "grid-preview-two": list(_SUPPORTED),
    "horizontal-grid": list(_SUPPORTED),
    "large-card": ["album", "playlist", "show", "video-link"],
    "list": list(_SUPPORTED),
    "long-card-horizontal-grid": list(_SUPPORTED),
    "message": ["call_onboarding"],
    "slideshow": list(_SUPPORTED),
    "small-horizontal-grid": ["flow"],
}

COVER_TEMPLATE = "https://e-cdns-images.dzcdn.net/images/{kind}/{code}/{size}x{size}-000000-80-0-0.jpg"


def cover_url(code: str | None, kind: str = "cover", size: int = 320) -> str | None:
    """Build a Deezer CDN image URL from a picture code.

    Args:
        code: The MD5 picture code from the private API.
        kind: Image family — ``cover``, ``artist``, ``playlist`` or ``talk``.
        size: Square edge length in pixels.

    Returns:
        A CDN URL, or None when there is no code.
    """
    return COVER_TEMPLATE.format(kind=kind, code=code, size=size) if code else None


def picture_of(node: dict, kind: str = "misc", size: int = 264) -> str | None:
    """The image for a gateway item, whichever way this page spells it.

    The gateway is not consistent: an album carries ``ALB_PICTURE``, a playlist
    ``PLAYLIST_PICTURE``, and a channel carries a ``pictures`` list of
    ``{md5, type}`` with no upper-case key at all -- which is why every channel
    card came back with no artwork on it.

    Args:
        node: An item or its ``data``.
        kind: Image family to fall back on when the entry does not say.
        size: Square edge length.

    Returns:
        A CDN URL, or None.
    """
    for key in ("PICTURE", "PIC_MD5", "MD5", "md5"):
        if node.get(key):
            return cover_url(node[key], kind, size)
    pictures = node.get("pictures") or node.get("PICTURES") or []
    if isinstance(pictures, list) and pictures:
        first = pictures[0]
        if isinstance(first, dict) and first.get("md5"):
            return cover_url(first["md5"], first.get("type") or kind, size)
    return None


def _album_date(data: dict) -> str:
    """The release date of a gateway album item, in the order Deezer means."""
    for key in ("ORIGINAL_RELEASE_DATE", "DIGITAL_RELEASE_DATE", "PHYSICAL_RELEASE_DATE"):
        value = str(data.get(key) or "").strip()
        # Deezer writes an unknown date as 0000-00-00 rather than leaving it out.
        if value and not value.startswith("0000"):
            return value
    return ""


def _sort_by_release(albums: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort albums newest first, tolerating missing or malformed dates."""
    return sorted(albums, key=lambda a: str(a.get("date") or ""), reverse=True)


def _cutoff(days: int = RECENT_DAYS) -> str:
    """The oldest release date that still counts as new, as ``YYYY-MM-DD``."""
    return (date.today() - timedelta(days=days)).isoformat()


def _is_recent(album: dict[str, Any], since: str) -> bool:
    """Whether an album's release date is on or after ``since``.

    An album with no date at all is kept: plenty of real releases carry a blank
    one, and dropping them would hide records rather than stale ones.
    """
    stamp = str(album.get("date") or "")[:10]
    return not stamp or stamp >= since


def genre_of(slug: str) -> str | None:
    """The genre id behind a ``genre:132`` pseudo-channel slug, or None."""
    match = _GENRE_RE.match(slug or "")
    return match[1] if match else None


class Explorer:
    """Reads Deezer's browse surfaces: channels, modules, charts and genres."""

    def __init__(self, gw: DeezerGW) -> None:
        """Initialize the explorer.

        Args:
            gw: The private API client whose session and ARL are reused.
        """
        self.gw = gw

    async def _page_state(self, path: str) -> dict:
        """Fetch a deezer.com page and return its embedded app state.

        Args:
            path: Path on www.deezer.com, e.g. ``/en/channels/rap``.

        Returns:
            The decoded ``__DZR_APP_STATE__`` object.

        Raises:
            DeezerGWError: If the page cannot be fetched or has no app state.
        """
        session = await self.gw.session()
        url = f"https://www.deezer.com{path}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise DeezerGWError(f"{url} returned HTTP {resp.status}")
                html = await resp.text()
        except aiohttp.ClientError as e:
            raise DeezerGWError(f"Failed to fetch {url}: {e}") from e

        match = _APP_STATE_RE.search(html)
        if not match:
            raise DeezerGWError(f"No __DZR_APP_STATE__ found on {url}")
        try:
            return json.loads(match[1])
        except json.JSONDecodeError as e:
            raise DeezerGWError(f"Could not decode app state from {url}: {e}") from e

    async def gw_page(self, page: str) -> dict:
        """Fetch one of Deezer's own pages through the private gateway.

        ``page.get`` is not shaped like the rest of gw-light: its argument goes
        in the query string as ``gateway_input``, and the key inside is
        upper-case ``PAGE``. Sending ``{"page": ...}`` as a body is answered
        with ``MISSING_PARAMETER_PAGE``, which is what made every channel in the
        Browse tab fail.

        Args:
            page: A page path, e.g. ``channels`` or ``channels/rap``.

        Returns:
            The gateway's ``results`` object, normally carrying ``sections``.

        Raises:
            DeezerGWError: If the gateway refuses the page.
        """
        payload = json.dumps(
            {"PAGE": page, "VERSION": "2.5.0", "SUPPORT": _PAGE_SUPPORT, "LANG": "en", "OPTIONS": []}
        )
        return await self.gw.call("page.get", {}, query={"gateway_input": payload})

    async def channels(self) -> list[dict[str, Any]]:
        """List the top-level channels shown on the Explore page.

        Deezer removed the server-rendered /channels page, so scraping it
        returns 404. The gateway is asked for the same page instead, and the
        editorial genres stand in when it will not answer -- with an ARL that
        cannot see channels, a grid of genres is still somewhere to browse.

        Returns:
            Channel dicts with ``id``, ``title``, ``slug``, ``image``,
            ``colour`` and ``kind`` -- ``channel`` for a real one, ``genre``
            for the fallback, so the page can say which it is showing.
        """
        # channels/explore is the page Deezer actually serves; a bare
        # "channels" is answered with "Channel identifier format is incorrect",
        # so it is the fallback rather than the first thing tried.
        for page in ("channels/explore", "channels"):
            try:
                results = await self.gw_page(page)
            except DeezerGWError as e:
                debug.log("explore: gateway page %s failed (%s)", page, e, level=30)
                continue
            channels = self._parse_channel_page(results)
            if channels:
                debug.log("explore: %d channels from %s", len(channels), page, level=20)
                return await self._with_artwork(channels)
            debug.log("explore: gateway page %s returned no channels", page, level=30)

        genres = await self.genres()
        debug.log("explore: falling back to %d genres", len(genres), level=30)
        return [
            {
                "id": g["id"],
                "title": g["title"],
                # Browsable in its own right: the page behind this card is
                # built from the public API rather than fetched as a channel,
                # which is what "Invalid channel slug: 'genre:132'" was.
                "slug": f"{GENRE_PREFIX}{g['id']}",
                "image": g["image"],
                "colour": None,
                "group": "Genres",
                "kind": "genre",
            }
            for g in genres
        ]

    async def _with_artwork(self, channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give the channels Deezer left blank a picture where one exists.

        Most channels come back with a colour and no artwork, which leaves two
        thirds of the grid as plain rectangles you have to read the caption
        under to tell apart. A good number of them are genres by another name --
        "Rock", "Jazz", "Classical" -- and the editorial genre list has a
        picture for each of those, so they are matched up by name. What is
        still blank is drawn as its initial on its own colour, which the page
        does.

        One free public call, and a failure changes nothing.

        Args:
            channels: Channel cards, some without an image.

        Returns:
            The same cards, with images filled in where one was found.
        """
        if all(c.get("image") for c in channels):
            return channels
        try:
            genres = [g for g in await self.genres() if g.get("image")]
        except DeezerGWError:
            return channels
        for channel in channels:
            if channel.get("image"):
                continue
            picture = _genre_picture(str(channel.get("title", "")), genres)
            if picture:
                channel["image"] = picture
        return channels

    @staticmethod
    def _parse_channel_page(results: dict) -> list[dict[str, Any]]:
        """Pull channel cards out of a gateway page payload.

        Only real channels: the page also carries albums and playlists, and an
        album whose payload happens to have a slug is not somewhere to browse.
        Duplicates are dropped, because Deezer lists the popular channels twice
        -- once in a highlight strip and again in the grid below it.
        """
        channels: list[dict[str, Any]] = []
        seen: set[str] = set()
        for section in results.get("sections", []) or []:
            group = section.get("title") or ""
            for item in section.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                data = item.get("data") or item
                if not isinstance(data, dict):
                    continue
                kind = str(item.get("type") or data.get("__TYPE__") or "").lower()
                if kind and kind != "channel":
                    continue
                # lox uploads music. A podcast cannot be downloaded from
                # Deezer, cannot be uploaded to a music tracker, and Deezer
                # serves thirty-five categories of them -- a third of the grid,
                # and the first thing on it.
                if _PODCAST_RE.search(f"{group} {data.get('slug', '')} "
                                      f"{data.get('title') or data.get('name') or ''}"):
                    continue
                slug = data.get("SLUG") or data.get("slug")
                title = data.get("TITLE") or data.get("title") or item.get("title")
                if not slug or not title or not _SLUG_RE.match(str(slug)):
                    continue
                if slug in seen:
                    continue
                seen.add(slug)
                channels.append(
                    {
                        "id": str(data.get("CHANNEL_ID") or data.get("id") or slug),
                        "title": title,
                        "slug": str(slug),
                        "image": picture_of(data) or picture_of(item),
                        # Every channel has a colour and only some have artwork,
                        # so the colour is what stops a card being a grey box.
                        "colour": (data.get("COLOR") or data.get("BACKGROUND_COLOR")
                                   or data.get("background_color") or item.get("background_color")),
                        # Which strip of the page it came from. Deezer serves
                        # close to a hundred of these -- genres, moods and
                        # thirty-five podcast categories -- and one flat grid
                        # of ninety-eight is not somewhere anybody browses.
                        "group": group,
                        "kind": "channel",
                    }
                )
        return channels

    async def channel(self, slug: str) -> dict[str, Any]:
        """Fetch one channel and the modules it contains.

        A ``genre:<id>`` slug is not a Deezer channel and never was -- it is
        what the channel list falls back to when the gateway will not answer.
        Rejecting it, which is what used to happen, meant every card in that
        fallback led to an error message. It is built from the public API
        instead, so the fallback is a page you can actually use.

        Args:
            slug: Channel slug, e.g. ``rap``, or ``genre:132``.

        Returns:
            Dict with ``title`` and a list of ``sections``, each carrying the
            module ID needed by :meth:`module` plus a preview of its items.

        Raises:
            DeezerGWError: If the slug is not one of those shapes, or Deezer
                will not describe the channel.
        """
        genre_id = genre_of(slug)
        if genre_id is not None:
            return await self.genre_page(genre_id)
        if not _SLUG_RE.match(slug):
            raise DeezerGWError(f"Invalid channel slug: {slug!r}")

        # The gateway first: the server-rendered page it replaced is gone for
        # most channels, and scraping one that is gone returns a 404 rather
        # than an empty channel.
        state: dict[str, Any] | None = None
        try:
            state = await self.gw_page(f"channels/{slug}")
        except DeezerGWError as e:
            debug.log("explore: gateway channel %s failed (%s), scraping", slug, e, level=30)
        if not (state or {}).get("sections"):
            state = await self._page_state(f"/en/channels/{slug}")

        sections = self._parse_sections(state or {})
        return {
            "slug": slug,
            "title": (state or {}).get("title") or slug.replace("-", " ").title(),
            "kind": "channel",
            "sections": sections,
        }

    def _parse_sections(self, state: dict) -> list[dict[str, Any]]:
        """Normalize a page's modules into sections of cards."""
        sections = []
        for section in state.get("sections", []) or []:
            items = self._parse_items(section.get("items", []) or [])
            if not items:
                continue
            sections.append(
                {
                    "id": section.get("module_id") or section.get("id"),
                    "title": section.get("title") or "",
                    "layout": section.get("layout") or section.get("type") or "grid",
                    "items": items,
                }
            )
        return sections

    async def genre_page(self, genre_id: str) -> dict[str, Any]:
        """Build a browsable page for one genre out of the public API.

        Deezer has no channel for "Blues" as such, so this is assembled: what
        is new in it, what is charting in it, and who is charting in it. All
        three are public-API calls, so this works with no ARL at all -- which
        matters, because a genre page is exactly what you get when the ARL
        cannot see channels.

        Args:
            genre_id: Deezer genre id, or ``0`` for everything.

        Returns:
            The same shape :meth:`channel` returns.
        """
        name = await self.genre_name(genre_id)
        releases, chart = await asyncio.gather(
            self.new_releases(genre_id, 60),
            self.chart(genre_id, 40),
            return_exceptions=True,
        )
        sections: list[dict[str, Any]] = []
        note = ""

        if isinstance(releases, dict) and releases.get("results"):
            heading = "New releases" if releases.get("source") != "chart" else "Recent, from the chart"
            sections.append({"id": "", "title": heading, "layout": "grid", "items": releases["results"]})
            note = releases.get("note") or ""
        elif isinstance(releases, dict):
            note = releases.get("note") or ""

        if isinstance(chart, dict):
            if chart.get("albums"):
                sections.append({"id": "", "title": "Top albums", "layout": "grid", "items": chart["albums"]})
            if chart.get("artists"):
                sections.append({"id": "", "title": "Top artists", "layout": "grid", "items": chart["artists"]})

        return {"slug": f"{GENRE_PREFIX}{genre_id}", "title": name, "kind": "genre",
                "note": note, "sections": sections}

    async def genre_name(self, genre_id: str | int) -> str:
        """What a genre is called, falling back to a readable placeholder."""
        if str(genre_id) in ("0", ""):
            return "All genres"
        try:
            info = await self.gw.public(f"/genre/{genre_id}")
        except DeezerGWError:
            return f"Genre {genre_id}"
        return info.get("name") or f"Genre {genre_id}"

    async def module(self, module_id: str) -> dict[str, Any]:
        """Fetch a single channel module by ID.

        This is the surface the playlist scanner also uses — a module URL pasted
        into the Missing tab resolves through here.

        Args:
            module_id: The module UUID from a ``/channels/module/<id>`` URL.

        Returns:
            Dict with ``title`` and the module's ``items``.
        """
        if not _MODULE_RE.match(module_id):
            raise DeezerGWError(f"Invalid module ID: {module_id!r}")
        state: dict[str, Any] | None = None
        try:
            state = await self.gw_page(f"channels/module/{module_id}")
        except DeezerGWError as e:
            debug.log("explore: gateway module %s failed (%s), scraping", module_id, e, level=30)
        if not (state or {}).get("sections"):
            state = await self._page_state(f"/en/channels/module/{module_id}")
        items: list[dict[str, Any]] = []
        for section in (state or {}).get("sections", []) or []:
            items.extend(self._parse_items(section.get("items", []) or []))
        return {"id": module_id, "title": (state or {}).get("title") or f"Module {module_id}", "items": items}

    @staticmethod
    def _parse_items(raw_items: list) -> list[dict[str, Any]]:
        """Normalize app-state items into album/playlist/artist cards."""
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            data = item.get("data") or {}
            if not isinstance(data, dict):
                continue
            kind = item.get("type")

            if kind == "album" or data.get("ALB_ID"):
                album_id = data.get("ALB_ID") or item.get("id")
                if not album_id:
                    continue
                items.append(
                    {
                        "type": "album",
                        "id": str(album_id),
                        "title": data.get("ALB_TITLE") or item.get("title") or "",
                        "artist": data.get("ART_NAME") or "",
                        # Carried so a card can link through to the artist, and
                        # so a list of these can be sorted by recency rather
                        # than by whatever order the module came in.
                        "artist_id": str(data.get("ART_ID") or ""),
                        "date": _album_date(data),
                        "image": cover_url(data.get("ALB_PICTURE")) or picture_of(data, "cover", 320),
                        "url": f"https://www.deezer.com/album/{album_id}",
                    }
                )
            elif kind == "playlist" or data.get("PLAYLIST_ID"):
                playlist_id = data.get("PLAYLIST_ID") or item.get("id")
                if not playlist_id:
                    continue
                items.append(
                    {
                        "type": "playlist",
                        "id": str(playlist_id),
                        "title": data.get("TITLE") or item.get("title") or "",
                        "artist": (data.get("PARENT_USER") or {}).get("USER_NAME") or "Deezer",
                        "image": (cover_url(data.get("PLAYLIST_PICTURE"), "playlist")
                                  or picture_of(data, "playlist", 320)),
                        "url": f"https://www.deezer.com/playlist/{playlist_id}",
                    }
                )
            elif kind == "artist" or data.get("ART_ID"):
                artist_id = data.get("ART_ID") or item.get("id")
                if not artist_id:
                    continue
                items.append(
                    {
                        "type": "artist",
                        "id": str(artist_id),
                        "title": data.get("ART_NAME") or item.get("title") or "",
                        "artist": "",
                        "image": cover_url(data.get("ART_PICTURE"), "artist") or picture_of(data, "artist", 320),
                        "url": f"https://www.deezer.com/artist/{artist_id}",
                    }
                )
        return items

    # ------------------------------------------------------------------
    # Public API surfaces — no ARL required
    # ------------------------------------------------------------------

    async def genres(self) -> list[dict[str, Any]]:
        """List Deezer's editorial genres."""
        data = await self.gw.public("/genre")
        return [
            {"id": str(g["id"]), "title": g.get("name", ""), "image": g.get("picture_medium")}
            for g in data.get("data", [])
            if g.get("id")
        ]

    async def chart(self, genre_id: str | int = 0, limit: int = 50) -> dict[str, Any]:
        """Fetch the album/track/artist chart for a genre.

        Args:
            genre_id: Deezer genre ID, 0 for the global chart.
            limit: Maximum entries per section.

        Returns:
            Dict with ``albums``, ``tracks`` and ``artists`` lists.
        """
        data = await self.gw.public(f"/chart/{genre_id}", {"limit": limit})
        return {
            "albums": [self.public_album(a) for a in (data.get("albums") or {}).get("data", [])],
            "tracks": [
                {
                    "type": "track",
                    "id": str(t.get("id")),
                    "title": t.get("title", ""),
                    "artist": (t.get("artist") or {}).get("name", ""),
                    "image": (t.get("album") or {}).get("cover_medium"),
                    "album_id": str((t.get("album") or {}).get("id") or ""),
                }
                for t in (data.get("tracks") or {}).get("data", [])
            ],
            "artists": [
                {
                    "type": "artist",
                    "id": str(a.get("id")),
                    "title": a.get("name", ""),
                    "artist": "",
                    "image": a.get("picture_medium"),
                }
                for a in (data.get("artists") or {}).get("data", [])
            ],
        }

    async def new_releases(self, genre_id: str | int = 0, limit: int = 50) -> dict[str, Any]:
        """Fetch genuinely new releases for a genre.

        Three sources, tried in order, because the first is empty for most of
        the world and being empty is what made this tab useless:

        1. ``/editorial/<genre>/releases`` -- Deezer's own new-releases feed.
           Right when it answers, region-dependent, and often silent.
        2. The gateway's new-releases channel, which needs an ARL but is not
           tied to the editorial feed.
        3. The genre chart, with each album's real release date looked up and
           anything older than :data:`RECENT_DAYS` dropped.

        The third is how a 1993 Wu-Tang record once appeared here. It is safe
        now because the dates are fetched rather than assumed, and because the
        source is named in the payload either way: a chart is never passed off
        as this week's records.

        Args:
            genre_id: Deezer genre ID, 0 for the editorial default.
            limit: Maximum albums.

        Returns:
            Dict with ``results``, the ``source`` that answered, and a ``note``
            when the result is empty or came from somewhere other than the
            editorial feed.
        """
        since = _cutoff()

        albums = await self._editorial_releases(genre_id, limit)
        if albums:
            return {"results": _sort_by_release(albums)[:limit], "source": "editorial", "note": ""}

        albums = await self._gateway_releases(genre_id, limit)
        if albums:
            return {
                "results": _sort_by_release(albums)[:limit],
                "source": "channel",
                "note": "No editorial feed for this genre in your region, so this is Deezer's new-releases channel.",
            }

        albums = await self._recent_from_chart(genre_id, since, limit)
        if albums:
            return {
                "results": albums,
                "source": "chart",
                "note": (
                    "Deezer publishes no new-releases feed for this genre in your region. "
                    f"These are its chart albums released in the last {RECENT_DAYS} days, newest first."
                ),
            }

        return {
            "results": [],
            "source": "none",
            "note": (
                "Deezer returned nothing new for this genre - no editorial feed, no channel, and nothing "
                f"on its chart from the last {RECENT_DAYS} days. Try another genre, or the Charts tab."
            ),
        }

    async def _editorial_releases(self, genre_id: str | int, limit: int) -> list[dict[str, Any]]:
        """Deezer's editorial new-releases feed for a genre, or []."""
        try:
            data = await self.gw.public(f"/editorial/{genre_id}/releases", {"limit": limit})
        except DeezerGWError as e:
            debug.log("explore: editorial genre=%s failed (%s)", genre_id, e, level=30)
            return []
        albums = [self.public_album(a) for a in data.get("data", []) or []]
        debug.log("explore: editorial genre=%s returned %d", genre_id, len(albums), level=20)
        return albums

    async def _gateway_releases(self, genre_id: str | int, limit: int) -> list[dict[str, Any]]:
        """The gateway's new-releases channel, or [] when it will not answer.

        Deezer's own new-releases page is ``channels/new`` -- not
        ``channels/releases``, and not ``channels/new-releases``, both of which
        answer "Channel identifier format is incorrect". It is not per-genre,
        so this only stands in for the global feed; a genre falls through to
        its chart.
        """
        if str(genre_id) not in ("0", ""):
            return []
        for page in ("channels/new", "channels/charts"):
            try:
                state = await self.gw_page(page)
            except DeezerGWError as e:
                debug.log("explore: gateway releases %s failed (%s)", page, e, level=30)
                continue
            items = [
                item
                for section in self._parse_sections(state)
                for item in section["items"]
                if item.get("type") == "album"
            ]
            if items:
                debug.log("explore: %d releases from %s", len(items), page, level=20)
                return items[:limit]
        return []

    async def _recent_from_chart(self, genre_id: str | int, since: str, limit: int) -> list[dict[str, Any]]:
        """Chart albums released since ``since``, newest first.

        The chart payload carries no release date, so the dates are fetched --
        bounded and in parallel, because this is the fallback and it should not
        take longer than the page is worth.
        """
        try:
            data = await self.gw.public(f"/chart/{genre_id}/albums", {"limit": _DATE_LOOKUP_LIMIT})
        except DeezerGWError as e:
            debug.log("explore: chart genre=%s failed (%s)", genre_id, e, level=30)
            return []
        albums = [self.public_album(a) for a in (data.get("data") or [])][:_DATE_LOOKUP_LIMIT]
        if not albums:
            return []

        gate = asyncio.Semaphore(_DATE_LOOKUP_CONCURRENCY)

        async def stamp(album: dict[str, Any]) -> None:
            if album.get("date"):
                return
            async with gate:
                try:
                    full = await self.gw.public(f"/album/{album['id']}")
                except DeezerGWError:
                    return
            album["date"] = full.get("release_date") or ""
            album["tracks"] = album.get("tracks") or full.get("nb_tracks")

        await asyncio.gather(*(stamp(a) for a in albums))
        # An undated album is kept everywhere else, but here it would be the
        # whole answer: a chart with no dates would come back as "all of this
        # is new". A date is required to call something new.
        recent = [a for a in albums if str(a.get("date") or "")[:10] >= since]
        debug.log("explore: chart genre=%s gave %d recent of %d", genre_id, len(recent), len(albums), level=20)
        return _sort_by_release(recent)[:limit]

    async def artist(self, artist_id: str | int) -> dict[str, Any]:
        """Fetch an artist and their discography, grouped by release type.

        The public API tags every release with ``record_type``, which is what
        makes an artist page readable: albums, EPs and singles are different
        things and a flat grid of 200 covers is not a discography.

        Args:
            artist_id: Deezer artist ID.

        Returns:
            Artist detail plus ``groups``, ordered album, EP, single, then the
            rest, each sorted newest first.
        """
        info = await self.gw.public(f"/artist/{artist_id}")
        raw = await self.gw.public(f"/artist/{artist_id}/albums", {"limit": 300})
        albums = [self.public_album(a) for a in raw.get("data", [])]

        buckets: dict[str, list[dict[str, Any]]] = {}
        for album in albums:
            buckets.setdefault(album.get("record_type") or "album", []).append(album)

        order = ["album", "ep", "single", "compilation", "live", "soundtrack"]
        labels = {
            "album": "Albums",
            "ep": "EPs",
            "single": "Singles",
            "compilation": "Compilations",
            "live": "Live albums",
            "soundtrack": "Soundtracks",
        }
        groups = [
            {"type": kind, "label": labels.get(kind, kind.title()), "albums": _sort_by_release(buckets[kind])}
            for kind in order + sorted(set(buckets) - set(order))
            if buckets.get(kind)
        ]

        top = await self.gw.public(f"/artist/{artist_id}/top", {"limit": 10})
        debug.log("artist %s: %d releases in %d groups", artist_id, len(albums), len(groups), level=20)

        return {
            "id": str(info.get("id")),
            "name": info.get("name"),
            "picture": info.get("picture_xl") or info.get("picture_big") or info.get("picture_medium"),
            "fans": info.get("nb_fan"),
            "albums": info.get("nb_album"),
            "url": info.get("link"),
            "groups": groups,
            "top_tracks": [
                {
                    "id": str(tr.get("id")),
                    "title": tr.get("title"),
                    "duration": tr.get("duration"),
                    "album_id": str((tr.get("album") or {}).get("id") or ""),
                    "image": (tr.get("album") or {}).get("cover_small"),
                }
                for tr in top.get("data", [])
            ],
        }

    async def playlist_albums(self, playlist_id: str | int) -> list[dict[str, Any]]:
        """Collapse a playlist's tracks down to the distinct albums they sit on.

        Args:
            playlist_id: Deezer playlist ID.

        Returns:
            One card per album, in first-appearance order.
        """
        tracks = await self.gw.playlist_tracks(playlist_id)
        seen: dict[str, dict[str, Any]] = {}
        for track in tracks:
            album = track.get("album") or {}
            album_id = album.get("id")
            if not album_id or str(album_id) in seen:
                continue
            seen[str(album_id)] = {
                "type": "album",
                "id": str(album_id),
                "title": album.get("title", ""),
                "artist": (track.get("artist") or {}).get("name", ""),
                "image": album.get("cover_medium"),
                "url": f"https://www.deezer.com/album/{album_id}",
            }
        return list(seen.values())

    @staticmethod
    def public_album(album: dict) -> dict[str, Any]:
        """Normalize a public-API album into a card.

        Carries the artist ID as well as the name, so a card can link through
        to the artist instead of being a dead end.
        """
        artist = album.get("artist") or {}
        return {
            "type": "album",
            "id": str(album.get("id")),
            "title": album.get("title", ""),
            "artist": artist.get("name", ""),
            "artist_id": str(artist.get("id") or ""),
            "image": album.get("cover_medium") or album.get("cover"),
            "url": album.get("link") or f"https://www.deezer.com/album/{album.get('id')}",
            "tracks": album.get("nb_tracks"),
            "date": album.get("release_date"),
            "record_type": (album.get("record_type") or "album").lower(),
            "explicit": album.get("explicit_lyrics"),
        }
