from pathlib import Path

import aiohttp
import anyio
import msgspec

from lox import cfg
from lox.errors import ImageUploadFailed
from lox.images.base import BaseImageUploader

API_URL = "https://ptscreens.com/api/1/upload"


def headers() -> dict[str, str]:
    """Auth header, read when the request is made.

    Built at import time this was a snapshot: a key entered on the settings
    page updates cfg in place, but never the dict, so the new key did not
    reach an upload until the process restarted.
    """
    return {"X-API-Key": cfg.image.ptscreens_key or ""}


class ImageUploader(BaseImageUploader):
    """Image uploader for ptscreens.com."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to ptscreens.com.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url).

        Raises:
            ImageUploadFailed: If upload fails.
        """
        async with await anyio.open_file(filename, "rb") as f:
            file_data = await f.read()

        data = aiohttp.FormData()
        data.add_field("source", file_data, filename=Path(filename).name)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(API_URL, headers=headers(), data=data) as resp,
            ):
                resp.raise_for_status()
                r = await resp.json(loads=msgspec.json.decode)
                return r["image"]["url"], None
        except (ValueError, KeyError) as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
