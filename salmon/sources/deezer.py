import re
from random import choice

import aiohttp
import msgspec

from salmon import cfg
from salmon.constants import UAGENTS
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper, SoupType

HEADERS = {
    "User-Agent": choice(UAGENTS),
    "Content-Language": "en-US",
    "Cache-Control": "max-age=0",
    "Accept": "*/*",
    "Accept-Charset": "utf-8,ISO-8859-1;q=0.7,*;q=0.3",
    "Accept-Language": "en",
}


class DeezerBase(BaseScraper):
    """Base scraper for Deezer metadata."""

    url = "https://api.deezer.com"
    site_url = "https://www.deezer.com"
    regex = re.compile(r"^https*:\/\/.*?deezer\.com.*?\/(?:[a-z]+\/)?(album|playlist|track)\/([0-9]+)")
    release_format = "/album/{rls_id}"

    def __init__(self) -> None:
        """Initialize Deezer scraper."""
        self.country_code = None
        super().__init__()
        self._csrf_token: str | None = None
        self._login_csrf_token: str | None = None
        self._arl_invalid_warned: bool = False
        self._arl_checked: bool = False

    @staticmethod
    def _get_arl() -> str | None:
        """Return the configured Deezer ARL, if any."""
        deezer_cfg = getattr(cfg.metadata, "deezer", None)
        if not deezer_cfg:
            return None
        return getattr(deezer_cfg, "arl", None) or None

    def _get_cookies(self) -> dict:
        """Return cookies for www.deezer.com requests."""
        arl = self._get_arl()
        return {"arl": arl} if arl else {}

    def _warn_arl_invalid(self, reason: str) -> None:
        """Print a one-time warning if the ARL is invalid or expired."""
        if self._arl_invalid_warned or not self._get_arl():
            return
        self._arl_invalid_warned = True
        red = "\033[31m"
        reset = "\033[0m"
        print(
            f"{red}[Deezer] ARL is invalid or expired ({reason}). "
            f"Falling back to unauthenticated requests.{reset}"
        )

    async def _check_arl(self) -> bool:
        """Validate the configured ARL by calling deezer.getUserData."""
        if self._arl_checked:
            return not self._arl_invalid_warned
        self._arl_checked = True

        arl = self._get_arl()
        if not arl:
            return True

        params = {
            "api_version": "1.0",
            "api_token": "null",
            "input": "3",
            "method": "deezer.getUserData",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
                session.get(
                    "https://www.deezer.com/ajax/gw-light.php",
                    params=params,
                    headers=HEADERS,
                ) as response,
            ):
                data = await response.json(loads=msgspec.json.decode)
        except (msgspec.DecodeError, aiohttp.ClientError) as e:
            self._warn_arl_invalid(f"request failed: {e}")
            return False

        try:
            user_id = data["results"]["USER"]["USER_ID"]
        except KeyError:
            self._warn_arl_invalid("no USER data in response")
            return False

        if not user_id or user_id == 0:
            self._warn_arl_invalid("USER_ID is 0 (guest session)")
            return False

        # Cache tokens if present; they are not required for metadata scraping.
        self._csrf_token = data["results"].get("checkForm")
        self._login_csrf_token = data["results"].get("checkFormLogin")
        return True

    async def _ensure_api_token(self) -> str | None:
        """Return cached API token, validating ARL first if needed."""
        await self._check_arl()
        return self._csrf_token

    @classmethod
    def parse_release_id(cls, url: str) -> str:
        """Parse release ID from Deezer URL."""
        match = cls.regex.search(url)
        if not match:
            raise ValueError(f"Invalid Deezer URL: {url}")
        return match[2]

    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> SoupType:
        """Fetch album data from Deezer API."""
        params = params or {}
        album_id = self.parse_release_id(url)
        try:
            data = await self.get_json(f"/album/{album_id}", params=params, headers=HEADERS)
            internal_data = await self.get_internal_api_data(f"/album/{album_id}", params)
            data["tracklist"] = self.get_tracks(internal_data)
            data["cover_xl"] = self.get_cover(internal_data)
            return data
        except msgspec.DecodeError as e:
            raise ScrapeError("Deezer page did not return valid JSON.") from e
        except (KeyError, ScrapeError) as e:
            raise ScrapeError(f"Failed to grab metadata for {url}.") from e

    async def get_internal_api_data(self, url: str, params: dict | None = None) -> dict:
        """Fetch internal API data from Deezer."""
        await self._check_arl()

        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
                session.get(self.site_url + url, params=(params or {}), headers=HEADERS) as response,
            ):
                if response.status != 200:
                    raise ScrapeError(
                        f"Deezer internal API returned status {response.status} for {self.site_url + url}"
                    )
                text = await response.text()
        except (TimeoutError, aiohttp.ClientError) as e:
            raise ScrapeError(f"Failed to fetch Deezer internal data: {e}") from e

        r = re.search(
            r"window.__DZR_APP_STATE__ = ({.*?}})</script>",
            text.replace("\n", ""),
        )
        if not r:
            raise ScrapeError("Failed to scrape track data.")
        raw = re.sub(r"{(\s*)type\: +\'([^\']+)\'", r'{\1type: "\2"', r[1])
        raw = re.sub("\t+([^:]+): ", r'"\1":', raw)
        return msgspec.json.decode(raw)

    def get_tracks(self, internal_data: dict) -> list:
        """Extract track list from internal data."""
        return internal_data["SONGS"]["data"]

    def get_cover(self, internal_data: dict) -> str:
        """Extract cover URL from internal data."""
        artwork_code = internal_data["DATA"]["ALB_PICTURE"]
        return f"https://e-cdns-images.dzcdn.net/images/cover/{artwork_code}/1000x1000-000000-100-0-0.jpg"
