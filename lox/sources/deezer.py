"""Deezer scraper adapter.

The search and tagger layers expect the ``BaseScraper`` shape — ``create_soup``,
``parse_release_id`` and friends. Everything underneath that is now
:class:`lox.deezer.gw.DeezerGW`, which owns the ARL cookie, the login
handshake and the gw-light calls.

This file used to carry its own copy of all of that. Two clients meant two
sessions, two logins per run, and two places to fix when Deezer changed
something.
"""

import re

from lox.deezer.gw import DeezerGW, DeezerGWError, parse_album_id
from lox.errors import ScrapeError
from lox.sources.base import BaseScraper, SoupType

_shared_client: DeezerGW | None = None


def shared_client() -> DeezerGW:
    """Return the process-wide Deezer client, creating it on first use.

    The web UI builds its own instance for the download and explore paths; this
    one serves the scraper path, which the CLI also uses. Both read the same
    config, so at most two sessions exist rather than one per scraper instance.
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = DeezerGW()
    return _shared_client


async def close_shared_client() -> None:
    """Close the shared client, if one was created."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.close()
        _shared_client = None


class DeezerBase(BaseScraper):
    """Scraper front end over the shared private-API client."""

    url = "https://api.deezer.com"
    site_url = "https://www.deezer.com"
    regex = re.compile(r"^https*:\/\/.*?deezer\.com.*?\/(?:[a-z]+\/)?(album|playlist|track)\/([0-9]+)")
    release_format = "/album/{rls_id}"

    def __init__(self) -> None:
        """Initialize the scraper."""
        self.country_code = None
        super().__init__()

    @property
    def client(self) -> DeezerGW:
        """The shared Deezer client."""
        return shared_client()

    @classmethod
    def parse_release_id(cls, url: str) -> str:
        """Parse a release ID out of a Deezer URL.

        Args:
            url: The Deezer URL.

        Returns:
            The release ID.

        Raises:
            ValueError: If the URL does not look like a Deezer release.
        """
        match = cls.regex.search(url)
        if match:
            return match[2]
        album_id = parse_album_id(url)
        if album_id:
            return album_id
        raise ValueError(f"Invalid Deezer URL: {url}")

    async def create_soup(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
    ) -> SoupType:
        """Fetch album metadata, merging public fields with private track data.

        Args:
            url: The Deezer album URL.
            params: Unused; kept for the BaseScraper signature.
            headers: Unused; kept for the BaseScraper signature.
            follow_redirects: Unused; kept for the BaseScraper signature.

        Returns:
            The public album payload with ``tracklist`` and ``cover_xl`` added.

        Raises:
            ScrapeError: If the album cannot be read.
        """
        album_id = self.parse_release_id(url)
        try:
            data = await self.client.album(album_id)
            results = await self.client.album_page(album_id)
            data["tracklist"] = self.get_tracks(results)
            data["cover_xl"] = self.get_cover(results)
            # The private page carries fields the public API does not: the
            # original release date as distinct from this edition's, the
            # producer/copyright line, the label as Deezer records it, and the
            # album's own credited artists. Keeping it means the scraper can
            # fill in the metadata form instead of leaving it to be typed.
            data["_album_page"] = results.get("DATA") or {}
            return data
        except (DeezerGWError, KeyError) as e:
            raise ScrapeError(f"Failed to grab metadata for {url}.") from e

    async def get_internal_api_data(self, url: str, params: dict | None = None) -> dict:
        """Fetch the private album page for a ``/album/<id>`` path.

        Args:
            url: Path of the form ``/album/<id>``.
            params: Unused; kept for the previous signature.

        Returns:
            The private page results.

        Raises:
            ScrapeError: If the page cannot be read.
        """
        album_id = url.rstrip("/").rsplit("/", 1)[-1]
        try:
            return await self.client.album_page(album_id)
        except DeezerGWError as e:
            raise ScrapeError(f"Failed to fetch Deezer internal data: {e}") from e

    def get_tracks(self, internal_data: dict) -> list:
        """Extract the track list from a private album page.

        Raises:
            ScrapeError: If no tracks are present.
        """
        songs = (internal_data.get("SONGS") or {}).get("data")
        if not songs and internal_data.get("DATA"):
            songs = [internal_data["DATA"]]
        if not songs:
            raise ScrapeError("Failed to scrape track data.")
        return songs

    def get_cover(self, internal_data: dict) -> str:
        """Build the cover URL from a private album page.

        Raises:
            ScrapeError: If the album has no artwork code.
        """
        artwork_code = (internal_data.get("DATA") or {}).get("ALB_PICTURE")
        if not artwork_code:
            raise ScrapeError("Album has no cover artwork.")
        return f"https://e-cdns-images.dzcdn.net/images/cover/{artwork_code}/1000x1000-000000-100-0-0.jpg"
