"""Deezer Explore — channels, charts, editorial selections and channel modules.

Deezer does not expose channels through the public API, so channel pages are
read the same way the browser gets them: fetch the page with the authenticated
session and pull ``window.__DZR_APP_STATE__`` out of the HTML. Charts and
editorial lists come from the public API, which is cheaper and does not need an
ARL.
"""

import json
import re
from typing import Any

import aiohttp

from lox import debug
from lox.deezer.gw import DeezerGW, DeezerGWError

_APP_STATE_RE = re.compile(r"window\.__DZR_APP_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>", re.DOTALL)

# Channel slugs and module IDs arrive from the browser and are interpolated into
# a deezer.com path, so they are constrained rather than trusted.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_MODULE_RE = re.compile(r"^[0-9a-f-]{8,64}$", re.IGNORECASE)

# The gateway wants a JSON blob describing the page it should render.
_PAGE_INPUT = '{"page":"%s","lang":"en","supports":{"ads":false,"long_span":false}}'

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

    async def channels(self) -> list[dict[str, Any]]:
        """List the top-level channels shown on the Explore page.

        Deezer removed the server-rendered /channels page, so scraping it
        returns 404. This asks the gateway for the same page instead, and falls
        back to editorial genres so the tab always shows something usable.

        Returns:
            Channel dicts with ``id``, ``title``, ``slug``, ``image`` and
            ``colour``.
        """
        try:
            results = await self.gw.call("page.get", {"gateway_input": _PAGE_INPUT % "channels/explore"})
            channels = self._parse_channel_page(results)
            if channels:
                debug.log("explore: %d channels from gateway", len(channels), level=20)
                return channels
            debug.log("explore: gateway returned no channels, falling back to genres", level=30)
        except DeezerGWError as e:
            debug.log("explore: gateway channels failed (%s), falling back to genres", e, level=30)

        genres = await self.genres()
        return [
            {"id": g["id"], "title": g["title"], "slug": f"genre:{g['id']}", "image": g["image"], "colour": None}
            for g in genres
        ]

    @staticmethod
    def _parse_channel_page(results: dict) -> list[dict[str, Any]]:
        """Pull channel cards out of a gateway page payload."""
        channels: list[dict[str, Any]] = []
        for section in results.get("sections", []) or []:
            for item in section.get("items", []) or []:
                data = item.get("data") or item
                slug = data.get("SLUG") or data.get("slug")
                title = data.get("TITLE") or data.get("title")
                if not slug or not title:
                    continue
                channels.append(
                    {
                        "id": str(data.get("CHANNEL_ID") or slug),
                        "title": title,
                        "slug": slug,
                        "image": cover_url(data.get("PICTURE") or data.get("PIC_MD5"), "misc", 264),
                        "colour": data.get("COLOR") or data.get("BACKGROUND_COLOR"),
                    }
                )
        return channels

    async def _legacy_channels(self) -> list[dict[str, Any]]:
        """The old scrape, kept only so the parser has a caller in tests."""
        state = await self._page_state("/en/channels")
        channels: list[dict[str, Any]] = []
        for section in state.get("sections", []) or []:
            for item in section.get("items", []) or []:
                data = item.get("data") or {}
                if item.get("type") not in ("channel", "grid") and not data.get("SLUG"):
                    continue
                slug = data.get("SLUG") or item.get("target", "").rsplit("/", 1)[-1]
                if not slug:
                    continue
                channels.append(
                    {
                        "id": data.get("CHANNEL_ID") or slug,
                        "title": data.get("TITLE") or item.get("title") or slug.title(),
                        "slug": slug,
                        "image": cover_url(data.get("PICTURE") or data.get("PIC_MD5"), "misc", 264),
                        "colour": data.get("COLOR") or data.get("BACKGROUND_COLOR"),
                    }
                )
        return channels

    async def channel(self, slug: str) -> dict[str, Any]:
        """Fetch one channel and the modules it contains.

        Args:
            slug: Channel slug, e.g. ``rap`` or ``new-releases``.

        Returns:
            Dict with ``title`` and a list of ``sections``, each carrying the
            module ID needed by :meth:`module` plus a preview of its items.
        """
        if not _SLUG_RE.match(slug):
            raise DeezerGWError(f"Invalid channel slug: {slug!r}")
        state = await self._page_state(f"/en/channels/{slug}")
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
        return {"slug": slug, "title": state.get("title") or slug.title(), "sections": sections}

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
        state = await self._page_state(f"/en/channels/module/{module_id}")
        items: list[dict[str, Any]] = []
        for section in state.get("sections", []) or []:
            items.extend(self._parse_items(section.get("items", []) or []))
        return {"id": module_id, "title": state.get("title") or f"Module {module_id}", "items": items}

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
                        "image": cover_url(data.get("ALB_PICTURE")),
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
                        "image": cover_url(data.get("PLAYLIST_PICTURE"), "playlist"),
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
                        "image": cover_url(data.get("ART_PICTURE"), "artist"),
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

    async def new_releases(self, genre_id: str | int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch new releases for a genre.

        The editorial endpoint returns an empty list for many genres and
        regions. Rather than showing "No new releases" when releases plainly
        exist, fall back to the genre's album chart.

        Args:
            genre_id: Deezer genre ID, 0 for editorial's default selection.
            limit: Maximum albums.

        Returns:
            Album cards, newest-first where the source provides an order.
        """
        data = await self.gw.public(f"/editorial/{genre_id}/releases", {"limit": limit})
        albums = [self.public_album(a) for a in data.get("data", [])]
        if albums:
            debug.log("explore: %d editorial releases for genre %s", len(albums), genre_id, level=20)
            return albums

        debug.log("explore: editorial empty for genre %s, using the chart", genre_id, level=30)
        chart = await self.gw.public(f"/chart/{genre_id}/albums", {"limit": limit})
        return [self.public_album(a) for a in chart.get("data", [])]

    async def artist_albums(self, artist_id: str | int, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch an artist's discography from the public API."""
        data = await self.gw.public(f"/artist/{artist_id}/albums", {"limit": limit})
        return [self.public_album(a) for a in data.get("data", [])]

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
        """Normalize a public-API album into an Explore card."""
        return {
            "type": "album",
            "id": str(album.get("id")),
            "title": album.get("title", ""),
            "artist": (album.get("artist") or {}).get("name", ""),
            "image": album.get("cover_medium") or album.get("cover"),
            "url": album.get("link") or f"https://www.deezer.com/album/{album.get('id')}",
            "tracks": album.get("nb_tracks"),
            "date": album.get("release_date"),
            "explicit": album.get("explicit_lyrics"),
        }
