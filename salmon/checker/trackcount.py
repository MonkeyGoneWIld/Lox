"""Cross-check a release's track count against the sources cited in a request.

A tracker request usually links to Discogs, MusicBrainz, Bandcamp and friends.
If those disagree with the Deezer release's track count, the Deezer release is
almost certainly a different edition and filling the request with it would be
wrong. These lookups are free (no tracker budget), so they run before any
tracker call.
"""

import html
import json
import re
from typing import Any

import aiohttp
import msgspec

from salmon import cfg

TIMEOUT = aiohttp.ClientTimeout(total=20)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

LINK_PATTERNS = {
    "discogs": r"https?://(?:www\.)?discogs\.com/[^/\s]+/release/\d+",
    "beatport": r"https?://(?:www\.)?beatport\.com/release/[^/\s]+/\d+",
    "bandcamp": r"https?://[a-z0-9-]+\.bandcamp\.com/(?:album|track)/[a-z0-9-]+",
    "musicbrainz": (
        r"https?://(?:www\.)?musicbrainz\.org/release/"
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    ),
    "apple_music": r"https?://music\.apple\.com/[a-z]{2}/album/[^/]+/\d+",
    "qobuz": r"https?://(?:(?:www\.)?qobuz\.com/[a-z]{2}/album/[^/]+/\d+|open\.qobuz\.com/album/\d+)",
    "tidal": r"https?://(?:(?:listen\.)?tidal\.com/album/\d+|tidal\.com/(?:browse/)?album/\d+)",
    "metal_archives": r"https?://(?:www\.)?metal-archives\.com/albums/[^/]+/[^/]+/\d+",
    "deezer": r"https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?album/\d+",
}

SERVICE_NAMES = {
    "discogs": "Discogs",
    "beatport": "Beatport",
    "bandcamp": "Bandcamp",
    "musicbrainz": "MusicBrainz",
    "apple_music": "Apple Music",
    "qobuz": "Qobuz",
    "tidal": "Tidal",
    "metal_archives": "Metal-Archives",
}


def extract_links(description: str | None) -> dict[str, str]:
    """Pull the first external release link per service out of a description."""
    if not description:
        return {}
    links: dict[str, str] = {}
    for service, pattern in LINK_PATTERNS.items():
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            links[service] = match[0]
    return links


def track_count_from_description(description: str | None) -> int | None:
    """Infer a track count from the prose and tracklist in a request body.

    Tries an explicit "N tracks" statement first, then a series of tracklist
    shapes (vinyl positions, quoted titles, numbered lines, bare durations).

    Args:
        description: The request's BBCode/HTML description.

    Returns:
        A track count, or None if nothing convincing was found.
    """
    if not description:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(description))

    for pattern in (r"(\d+)\s*track\s*(?:version)?", r"(\d+)\s+tracks?\s*(?:,|$)"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match[1])

    cleaned = text
    for junk in (
        r"^(Tracklist|No\.\s*Title\s*Length|Bonusdisc|Total length:.*)\s*$",
        r"^(Label/Cat#|Year|More information|Country|Genre|Style|Format):.*$",
    ):
        cleaned = re.sub(junk, "", cleaned, flags=re.MULTILINE | re.IGNORECASE)

    shapes = (
        r"^Part\s+(?:One|Two|Three|Four|1|2|3|4|I|II|III|IV)\s+\d+:\d+",
        r"^\s*[A-Z]\d+\s+.+?\d+:\d+",
        r'"([^"]+)"\s+\d+:\d+',
        r"^[^\n]+\(\d{1,2}:\d{2}\)\s*$",
        r"^.+\s+-\s+.+\(\d+:\d+\)\s*$",
        r"^\s*\d{1,2}[.\s]+[^\n]+\d+:\d+",
        r"^.+\s+\d{1,2}:\d{2}\s*$",
    )
    for shape in shapes:
        matches = re.findall(shape, cleaned, re.MULTILINE)
        if not matches:
            continue
        valid = [m for m in matches if not re.search(r"^tracklist|^total|^more info|^label|^year", str(m), re.I)]
        if valid:
            return len(valid)
    return None


class TrackCountVerifier:
    """Resolves track counts from the external services a request links to."""

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        """Initialize the verifier.

        Args:
            session: Optional shared session. One is created on demand if not
                given, and closed by :meth:`close`.
        """
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the HTTP session, creating it on first use."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": UA})
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the session if this instance created it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _json(self, url: str, **kwargs: Any) -> tuple[dict | None, str | None]:
        """GET a URL and decode JSON, returning (data, error)."""
        session = await self._get_session()
        try:
            async with session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                return msgspec.json.decode(await resp.read()), None
        except (aiohttp.ClientError, msgspec.DecodeError, TimeoutError) as e:
            return None, str(e)

    async def _text(self, url: str, **kwargs: Any) -> tuple[str | None, str | None]:
        """GET a URL and return (body, error)."""
        session = await self._get_session()
        try:
            async with session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                return await resp.text(), None
        except (aiohttp.ClientError, TimeoutError) as e:
            return None, str(e)

    async def discogs(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a Discogs release URL."""
        token = cfg.metadata.discogs_token
        if not token:
            return None, "Discogs token not configured"
        match = re.search(r"/release/(\d+)", url)
        if not match:
            return None, "Invalid Discogs URL"
        data, error = await self._json(
            f"https://api.discogs.com/releases/{match[1]}",
            headers={"Authorization": f"Discogs token={token}"},
        )
        if error:
            return None, error
        tracklist = (data or {}).get("tracklist") or []
        real = [t for t in tracklist if t.get("type_", "track") == "track"]
        return (len(real) or len(tracklist)) or None, None

    async def musicbrainz(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a MusicBrainz release URL."""
        match = re.search(r"release/([a-f0-9-]{36})", url, re.IGNORECASE)
        if not match:
            return None, "Invalid MusicBrainz URL"
        data, error = await self._json(
            f"https://musicbrainz.org/ws/2/release/{match[1]}",
            params={"fmt": "json", "inc": "recordings"},
            headers={"User-Agent": "lox/1.0 (request checker)"},
        )
        if error:
            return None, error
        total = 0
        for medium in (data or {}).get("media") or []:
            count = medium.get("track-count")
            total += int(count) if count else len(medium.get("tracks") or [])
        return total or None, None if total else "No tracks found"

    async def beatport(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a Beatport release URL."""
        match = re.search(r"beatport\.com/release/[^/]+/(\d+)", url, re.IGNORECASE)
        if not match:
            return None, "Invalid Beatport URL"
        data, _ = await self._json(f"https://api.beatport.com/v4/catalog/releases/{match[1]}/")
        tracks = (data or {}).get("tracks") or []
        if tracks:
            return len(tracks), None

        body, error = await self._text(url)
        if error:
            return None, error
        next_data = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.+?)</script>', body or "", re.DOTALL)
        if next_data:
            try:
                payload = json.loads(next_data[1])
                tracks = payload["props"]["pageProps"]["release"]["tracks"]
                if tracks:
                    return len(tracks), None
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        items = re.findall(r'class="[^"]*track-item[^"]*"', body or "", re.IGNORECASE)
        return (len(items) or None), None if items else "Could not extract"

    async def bandcamp(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a Bandcamp album URL."""
        body, error = await self._text(url)
        if error:
            return None, error
        num = re.search(r'"numTracks"\s*:\s*(\d+)', body or "")
        if num:
            return int(num[1]), None
        rows = re.findall(r'<li[^>]*class="[^"]*\btrack_row\b[^"]*"', body or "", re.IGNORECASE)
        return (len(rows) or None), None if rows else "Could not extract"

    async def metal_archives(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a Metal-Archives album URL."""
        body, error = await self._text(url)
        if error:
            return None, error
        rows = re.findall(r'<td[^>]*class="[^"]*track[^"]*"', body or "", re.IGNORECASE)
        if rows:
            return len(rows), None
        songs = re.findall(r'<a[^>]*href="[^"]*/songs/view/\d+"', body or "")
        return (len(songs) or None), None if songs else "Could not extract"

    async def qobuz(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a Qobuz album URL."""
        qobuz_cfg = cfg.metadata.qobuz
        if not qobuz_cfg.app_id:
            return None, "Qobuz credentials not configured"
        match = re.search(r"(?:qobuz\.com/[a-z]{2}/album/[^/]+/|open\.qobuz\.com/album/)(\d+)", url, re.IGNORECASE)
        if not match:
            return None, "Invalid Qobuz URL"
        data, error = await self._json(
            "https://www.qobuz.com/api.json/0.2/album/get",
            params={
                "album_id": match[1],
                "app_id": qobuz_cfg.app_id,
                "user_auth_token": qobuz_cfg.user_auth_token or "",
            },
        )
        if error:
            return None, error
        count = (data or {}).get("tracks_count") or len(((data or {}).get("tracks") or {}).get("items", []))
        return count or None, None

    async def tidal(self, url: str) -> tuple[int | None, str | None]:
        """Track count from a Tidal album URL."""
        token = cfg.metadata.tidal.token
        if not token:
            return None, "Tidal token not configured"
        match = re.search(r"(?:tidal\.com/(?:browse/)?album|listen\.tidal\.com/album)/(\d+)", url, re.IGNORECASE)
        if not match:
            return None, "Invalid Tidal URL"
        data, error = await self._json(
            f"https://api.tidal.com/v1/albums/{match[1]}/items",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"countryCode": "US", "limit": 100},
        )
        if error:
            return None, error
        items = (data or {}).get("items") or []
        return (len(items) or (data or {}).get("totalNumberOfItems")), None

    async def apple_music(self, url: str) -> tuple[int | None, str | None]:
        """Track count from an Apple Music album URL."""
        token = cfg.metadata.apple_music_token
        if not token:
            return None, "Apple Music token not configured"
        match = re.search(r"music\.apple\.com/([a-z]{2})/album/[^/]+/(\d+)", url, re.IGNORECASE)
        if not match:
            return None, "Invalid Apple Music URL"
        data, error = await self._json(
            f"https://api.music.apple.com/v1/catalog/{match[1]}/albums/{match[2]}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if error:
            return None, error
        entries = (data or {}).get("data") or [{}]
        tracks = ((entries[0].get("relationships") or {}).get("tracks") or {}).get("data") or []
        return len(tracks) or (entries[0].get("attributes") or {}).get("trackCount"), None

    def _handler(self, service: str):
        """Return the coroutine handler for a service name, if supported."""
        return {
            "discogs": self.discogs,
            "musicbrainz": self.musicbrainz,
            "beatport": self.beatport,
            "bandcamp": self.bandcamp,
            "metal_archives": self.metal_archives,
            "qobuz": self.qobuz,
            "tidal": self.tidal,
            "apple_music": self.apple_music,
        }.get(service)

    async def expected_count(self, description: str | None) -> int | None:
        """Best guess at a release's track count from its request description.

        Prefers a structured source over prose: the first external link that
        answers wins, and the description text is only used as a fallback.

        Args:
            description: The request description.

        Returns:
            A track count, or None.
        """
        for service, url in extract_links(description).items():
            handler = self._handler(service)
            if not handler:
                continue
            count, _ = await handler(url)
            if count:
                return count
        return track_count_from_description(description)

    async def verify(self, track_count: int, description: str | None) -> dict[str, Any]:
        """Compare a known track count against every linked source.

        Args:
            track_count: The candidate release's track count.
            description: The request description holding the links.

        Returns:
            Dict with ``agree``, ``disagree``, ``errors`` and ``links``.
        """
        links = extract_links(description)
        agree: list[str] = []
        disagree: list[str] = []
        errors: list[str] = []

        for service, url in links.items():
            handler = self._handler(service)
            if not handler:
                continue
            name = SERVICE_NAMES.get(service, service)
            count, error = await handler(url)
            if error:
                errors.append(f"{name}: {error}")
            elif count and count != track_count:
                disagree.append(f"{name} ({count})")
            elif count:
                agree.append(f"{name} ({count})")

        return {"agree": agree, "disagree": disagree, "errors": errors, "links": links}
