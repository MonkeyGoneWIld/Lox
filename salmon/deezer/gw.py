"""Async client for Deezer's internal gw-light API.

Authentication is a single ARL cookie. Everything else (the CSRF ``api_token``
and the ``license_token`` needed to resolve stream URLs) is derived from
``deezer.getUserData``.
"""

import asyncio
import random
import re
from typing import Any

import aiohttp
import msgspec

from salmon import cfg

GW_URL = "https://www.deezer.com/ajax/gw-light.php"
MEDIA_URL = "https://media.deezer.com/v1/get_url"
PUBLIC_API = "https://api.deezer.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Origin": "https://www.deezer.com",
    "Referer": "https://www.deezer.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

# Methods that must be called with an empty api_token.
_TOKENLESS_METHODS = frozenset({"deezer.getUserData", "user.getArl"})

FORMATS = ("FLAC", "MP3_320", "MP3_128")
"""Stream qualities in descending preference order."""


class DeezerGWError(Exception):
    """Raised when the Deezer private API cannot satisfy a request."""


class TrackAvailability(msgspec.Struct, frozen=True):
    """What the private API says about an album's tracks.

    The upload pipeline only wants releases where every track is lossless and
    actually streamable in the account's region, so each condition is tracked
    separately rather than collapsed into one boolean.
    """

    total: int
    flac_count: int
    readable_count: int
    all_flac: bool
    all_readable: bool
    all_have_id: bool
    all_have_filesize: bool
    unreadable: list[str]

    @property
    def uploadable(self) -> bool:
        """True when the release passes every availability check."""
        return self.all_flac and self.all_readable and self.all_have_id and self.all_have_filesize

    def reason(self) -> str | None:
        """Return why the release is not uploadable, or None if it is."""
        if not self.total:
            return "no tracks returned"
        if not self.all_have_id:
            return "some tracks have no song ID"
        if not self.all_have_filesize:
            return "some tracks have no filesize"
        if not self.all_flac:
            return f"only {self.flac_count}/{self.total} tracks are FLAC"
        if not self.all_readable:
            return f"{self.total - self.readable_count} track(s) not streamable"
        return None


def _int(value: Any) -> int:
    """Coerce a Deezer field to int, treating anything unparseable as 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class DeezerGW:
    """Authenticated session against Deezer's private API.

    A single instance is reused for the lifetime of the process; the login
    handshake is performed once and refreshed only when Deezer invalidates it.
    """

    def __init__(self, arl: str | None = None, timeout: int = 30) -> None:
        """Initialize the client.

        Args:
            arl: ARL cookie. Falls back to ``cfg.metadata.deezer.arl``.
            timeout: Per-request timeout in seconds.
        """
        self.arl = arl if arl is not None else self._configured_arl()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self.api_token: str | None = None
        self.license_token: str | None = None
        self.user_id: int | None = None
        self.country: str | None = None

    @staticmethod
    def _configured_arl() -> str | None:
        """Read the ARL out of the salmon config, if one is set."""
        deezer_cfg = getattr(cfg.metadata, "deezer", None)
        return (getattr(deezer_cfg, "arl", None) or None) if deezer_cfg else None

    @property
    def authenticated(self) -> bool:
        """True once a login handshake has produced a real user ID."""
        return bool(self.user_id)

    async def __aenter__(self) -> "DeezerGW":
        """Enter the async context, opening the HTTP session."""
        await self._ensure_session()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Exit the async context, closing the HTTP session."""
        await self.close()

    async def session(self) -> aiohttp.ClientSession:
        """Return the shared HTTP session, creating it on first use.

        One session is reused across the client, the downloader and the
        explorer so the ARL cookie and connection pool are shared.
        """
        if self._session is None or self._session.closed:
            cookies = {"arl": self.arl} if self.arl else {}
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=HEADERS, cookies=cookies)
        return self._session

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Deprecated alias for :meth:`session`."""
        return await self.session()

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def login(self, force: bool = False) -> None:
        """Perform the getUserData handshake.

        Args:
            force: Re-run even if already authenticated.

        Raises:
            DeezerGWError: If no ARL is configured or the ARL is rejected.
        """
        async with self._lock:
            if self.authenticated and not force:
                return
            if not self.arl:
                raise DeezerGWError("No Deezer ARL configured. Set metadata.deezer.arl in your config.")

            data = await self._call_raw("deezer.getUserData", {})
            results = data.get("results") or {}
            user = results.get("USER") or {}
            user_id = _int(user.get("USER_ID"))
            if not user_id:
                raise DeezerGWError("ARL is invalid or expired (guest session returned).")

            self.user_id = user_id
            self.api_token = results.get("checkForm") or None
            self.country = results.get("COUNTRY")
            self.license_token = ((user.get("OPTIONS") or {}).get("license_token")) or None

    async def _call_raw(self, method: str, payload: dict) -> dict:
        """POST to gw-light without requiring a prior login."""
        session = await self.session()
        params = {
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": "" if method in _TOKENLESS_METHODS else (self.api_token or ""),
            "cid": str(random.randint(0, 1_000_000_000)),
        }
        try:
            async with session.post(GW_URL, params=params, json=payload) as resp:
                if resp.status != 200:
                    raise DeezerGWError(f"gw-light returned HTTP {resp.status} for {method}")
                body = await resp.read()
        except aiohttp.ClientError as e:
            raise DeezerGWError(f"gw-light request failed for {method}: {e}") from e

        try:
            data = msgspec.json.decode(body)
        except msgspec.DecodeError as e:
            raise DeezerGWError(f"gw-light returned non-JSON for {method}") from e

        error = data.get("error")
        if error and error not in ([], {}):
            raise DeezerGWError(f"gw-light error for {method}: {error}")
        return data

    async def call(self, method: str, payload: dict | None = None, retries: int = 2) -> dict:
        """Call a gw-light method, refreshing the session token if it expires.

        Args:
            method: The gw method name, e.g. ``deezer.pageAlbum``.
            payload: JSON body for the method.
            retries: How many times to re-login and retry on token errors.

        Returns:
            The ``results`` object from the response.

        Raises:
            DeezerGWError: If the call fails after retries.
        """
        await self.login()
        last_error: DeezerGWError | None = None
        for attempt in range(retries + 1):
            try:
                data = await self._call_raw(method, payload or {})
                return data.get("results") or {}
            except DeezerGWError as e:
                last_error = e
                if "token" not in str(e).lower() or attempt == retries:
                    break
                await self.login(force=True)
        raise last_error or DeezerGWError(f"{method} failed")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def album_page(self, album_id: str | int) -> dict:
        """Fetch the private album page for an album ID."""
        return await self.call("deezer.pageAlbum", {"alb_id": int(album_id), "lang": "en"})

    async def playlist_page(self, playlist_id: str | int, nb: int = 2000) -> dict:
        """Fetch the private playlist page for a playlist ID."""
        return await self.call(
            "deezer.pagePlaylist",
            {"playlist_id": str(playlist_id), "lang": "en", "nb": nb, "start": 0, "tab": 0, "tags": True},
        )

    async def artist_page(self, artist_id: str | int) -> dict:
        """Fetch the private artist page for an artist ID."""
        return await self.call("deezer.pageArtist", {"art_id": int(artist_id), "lang": "en"})

    async def track_data(self, track_ids: list[str | int]) -> list[dict]:
        """Fetch full song records, including the track tokens needed to stream.

        Args:
            track_ids: Deezer song IDs.

        Returns:
            The song records, in the order Deezer returns them.
        """
        if not track_ids:
            return []
        results = await self.call("song.getListData", {"sng_ids": [str(t) for t in track_ids]})
        return results.get("data") or []

    async def album_tracks(self, album_id: str | int) -> list[dict]:
        """Return the raw song records for an album."""
        results = await self.album_page(album_id)
        songs = results.get("SONGS") or {}
        tracks = songs.get("data") or []
        if not tracks and results.get("DATA"):
            tracks = [results["DATA"]]
        return tracks

    async def availability(self, album_id: str | int) -> TrackAvailability:
        """Check whether every track on an album is lossless and streamable.

        This is the gate the request checker and the playlist scanner both use
        before spending any tracker rate limit on a release.

        Args:
            album_id: Deezer album ID.

        Returns:
            A populated TrackAvailability.

        Raises:
            DeezerGWError: If the album returns no tracks at all.
        """
        tracks = await self.album_tracks(album_id)
        if not tracks:
            raise DeezerGWError(f"No tracks returned for album {album_id}")

        flac_count = readable_count = 0
        all_have_id = all_have_filesize = True
        unreadable: list[str] = []

        for track in tracks:
            title = track.get("SNG_TITLE") or track.get("title") or "Unknown"
            if _int(track.get("FILESIZE_FLAC")) > 0:
                flac_count += 1
            if bool(track.get("readable", True)):
                readable_count += 1
            else:
                unreadable.append(title)
            if not (track.get("SNG_ID") or track.get("id")):
                all_have_id = False
            if _int(track.get("FILESIZE")) <= 0:
                all_have_filesize = False

        total = len(tracks)
        return TrackAvailability(
            total=total,
            flac_count=flac_count,
            readable_count=readable_count,
            all_flac=flac_count == total and flac_count > 0,
            all_readable=readable_count == total,
            all_have_id=all_have_id,
            all_have_filesize=all_have_filesize,
            unreadable=unreadable,
        )

    # ------------------------------------------------------------------
    # Public API passthrough
    # ------------------------------------------------------------------

    async def public(self, path: str, params: dict | None = None) -> dict:
        """GET a path on the public api.deezer.com, honouring its rate limit.

        Args:
            path: Path beginning with a slash, e.g. ``/album/123``.
            params: Query parameters.

        Returns:
            The decoded JSON body.

        Raises:
            DeezerGWError: On transport failure or a persistent rate limit.
        """
        session = await self.session()
        for attempt in range(4):
            try:
                async with session.get(PUBLIC_API + path, params=params or {}) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(int(resp.headers.get("Retry-After", 5)))
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    body = await resp.read()
            except aiohttp.ClientError as e:
                if attempt == 3:
                    raise DeezerGWError(f"public API request failed for {path}: {e}") from e
                await asyncio.sleep(2 * (attempt + 1))
                continue

            try:
                return msgspec.json.decode(body)
            except msgspec.DecodeError as e:
                raise DeezerGWError(f"public API returned non-JSON for {path}") from e
        raise DeezerGWError(f"public API rate limited for {path}")

    async def search_albums(self, query: str, limit: int = 25) -> list[dict]:
        """Search albums on the public API."""
        data = await self.public("/search/album", {"q": query, "limit": limit})
        return data.get("data") or []

    async def search_tracks(self, query: str, limit: int = 25) -> list[dict]:
        """Search tracks on the public API."""
        data = await self.public("/search/track", {"q": query, "limit": limit})
        return data.get("data") or []

    async def search_artists(self, query: str, limit: int = 25) -> list[dict]:
        """Search artists on the public API."""
        data = await self.public("/search/artist", {"q": query, "limit": limit})
        return data.get("data") or []

    async def album(self, album_id: str | int) -> dict:
        """Fetch an album from the public API."""
        return await self.public(f"/album/{album_id}")

    async def playlist(self, playlist_id: str | int) -> dict:
        """Fetch a playlist from the public API."""
        return await self.public(f"/playlist/{playlist_id}")

    async def playlist_tracks(self, playlist_id: str | int) -> list[dict]:
        """Page through every track on a public playlist."""
        tracks: list[dict] = []
        path: str | None = f"/playlist/{playlist_id}/tracks"
        params: dict | None = {"limit": 1000}
        while path:
            data = await self.public(path, params)
            tracks.extend(data.get("data") or [])
            next_url = data.get("next")
            if not next_url:
                break
            path, params = next_url.replace(PUBLIC_API, "", 1), None
        return tracks

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_url(self, track: dict, formats: tuple[str, ...] = FORMATS) -> tuple[str, str]:
        """Resolve a playable, still-encrypted stream URL for a track.

        Args:
            track: A song record carrying ``TRACK_TOKEN``.
            formats: Qualities to request, in descending preference.

        Returns:
            Tuple of (url, format name that was actually served).

        Raises:
            DeezerGWError: If no requested format is available for the account.
        """
        await self.login()
        token = track.get("TRACK_TOKEN")
        if not token:
            raise DeezerGWError(f"Track {track.get('SNG_ID')} has no TRACK_TOKEN")
        if not self.license_token:
            raise DeezerGWError("Account has no license_token; cannot resolve stream URLs.")

        payload = {
            "license_token": self.license_token,
            "media": [
                {
                    "type": "FULL",
                    "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt} for fmt in formats],
                }
            ],
            "track_tokens": [token],
        }

        session = await self.session()
        try:
            async with session.post(MEDIA_URL, json=payload) as resp:
                body = await resp.read()
        except aiohttp.ClientError as e:
            raise DeezerGWError(f"media API request failed: {e}") from e

        try:
            data = msgspec.json.decode(body)
        except msgspec.DecodeError as e:
            raise DeezerGWError("media API returned non-JSON") from e

        media_list = (data.get("data") or [{}])[0].get("media") or []
        if not media_list:
            errors = (data.get("data") or [{}])[0].get("errors") or data.get("errors")
            raise DeezerGWError(f"No media returned for track {track.get('SNG_ID')}: {errors}")

        media = media_list[0]
        sources = media.get("sources") or []
        if not sources:
            raise DeezerGWError(f"No sources returned for track {track.get('SNG_ID')}")
        return sources[0]["url"], media.get("format", formats[0])


ALBUM_URL_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?album/(\d+)", re.IGNORECASE)
PLAYLIST_URL_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?playlist/(\d+)", re.IGNORECASE)
MODULE_URL_RE = re.compile(r"channels/module/([0-9a-fA-F-]+)", re.IGNORECASE)


def parse_album_id(url: str) -> str | None:
    """Extract an album ID from a Deezer URL, or None."""
    match = ALBUM_URL_RE.search(url)
    return match[1] if match else None


def parse_playlist_id(url: str) -> str | None:
    """Extract a playlist ID from a Deezer URL, or None."""
    match = PLAYLIST_URL_RE.search(url)
    return match[1] if match else None


def parse_module_id(url: str) -> str | None:
    """Extract a channel module ID from a Deezer URL, or None."""
    match = MODULE_URL_RE.search(url)
    return match[1] if match else None
