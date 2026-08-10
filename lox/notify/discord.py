"""Discord webhook notifications for checker results.

Sending is opt-in and off by default: a scan that finds fifty missing albums
should not fire fifty webhooks unless you asked for it.
"""

import asyncio
from typing import Any

import aiohttp

from lox import cfg

TIMEOUT = aiohttp.ClientTimeout(total=15)

COLOURS = {
    "missing": 10181046,
    "fillable": 65280,
    "partial": 3447003,
    "summary": 3066993,
    "warning": 16776960,
}


class DiscordNotifier:
    """Posts embeds to a Discord webhook, honouring 429 backoff."""

    def __init__(self, webhook_url: str | None = None) -> None:
        """Initialize the notifier.

        Args:
            webhook_url: Override for the configured webhook.
        """
        self.webhook_url = webhook_url or cfg.notifications.discord_webhook

    @property
    def enabled(self) -> bool:
        """True when notifications are switched on and a webhook is set."""
        return bool(cfg.notifications.enabled and self.webhook_url)

    async def _post(self, payload: dict[str, Any], retries: int = 3) -> bool:
        """POST a payload, retrying while Discord rate limits us."""
        if not self.enabled:
            return False
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            for attempt in range(retries):
                try:
                    async with session.post(self.webhook_url, json=payload) as resp:
                        if resp.status == 429:
                            retry_after = float(resp.headers.get("Retry-After", 5))
                            await asyncio.sleep(min(retry_after, 30))
                            continue
                        return 200 <= resp.status < 300
                except aiohttp.ClientError:
                    if attempt == retries - 1:
                        return False
                    await asyncio.sleep(2 * (attempt + 1))
        return False

    async def missing_album(self, result: dict[str, Any], candidate: dict[str, Any]) -> bool:
        """Announce an album that is missing from one or more trackers.

        Args:
            result: A serialized ScanResult.
            candidate: The serialized Candidate it came from.

        Returns:
            True if Discord accepted the message.
        """
        missing = result.get("missing_from") or []
        found = result.get("found_on") or []
        title = f"Missing on {' and '.join(missing)}" if missing else "Tracker check"

        fields = [
            {"name": "Artist", "value": candidate.get("artist") or "?", "inline": True},
            {"name": "Album", "value": candidate.get("title") or "?", "inline": True},
            {"name": "Year", "value": candidate.get("year") or "?", "inline": True},
            {"name": "Tracks", "value": str(candidate.get("tracks") or "?"), "inline": True},
            {"name": "Type", "value": candidate.get("record_type") or "?", "inline": True},
            {"name": "Source", "value": candidate.get("source") or "?", "inline": True},
        ]
        if found:
            fields.append({"name": "Already on", "value": ", ".join(found), "inline": True})
        if result.get("errors"):
            errors = "\n".join(f"{k}: {v}" for k, v in result["errors"].items())
            fields.append({"name": "Errors", "value": errors[:1000], "inline": False})
        fields.append(
            {
                "name": "Links",
                "value": f"[Deezer]({candidate.get('deezer_url')})",
                "inline": False,
            }
        )

        return await self._post(
            {
                "content": title,
                "embeds": [
                    {
                        "title": f"{candidate.get('artist')} - {candidate.get('title')}",
                        "url": candidate.get("deezer_url"),
                        "color": COLOURS["missing"] if missing else COLOURS["partial"],
                        "thumbnail": {"url": candidate["cover"]} if candidate.get("cover") else None,
                        "fields": fields,
                    }
                ],
            }
        )

    async def fillable_request(self, match: dict[str, Any]) -> bool:
        """Announce a tracker request that a Deezer release could fill.

        Args:
            match: A serialized RequestMatch.

        Returns:
            True if Discord accepted the message.
        """
        verification = match.get("verification") or {}
        fields = [
            {"name": "Request", "value": f"{match.get('artist')} - {match.get('album')}", "inline": False},
            {"name": "Bounty", "value": match.get("bounty") or "0 B", "inline": True},
            {"name": "Year", "value": match.get("year") or "?", "inline": True},
            {"name": "Confidence", "value": f"{match.get('confidence', 0):.2f}", "inline": True},
            {"name": "Deezer", "value": f"{match.get('deezer_artist')} - {match.get('deezer_title')}", "inline": False},
            {"name": "Tracks", "value": str(match.get("deezer_tracks") or "?"), "inline": True},
            {"name": "FLAC", "value": "All tracks" if match.get("all_flac") else "Partial", "inline": True},
            {"name": "Formats", "value": ", ".join(match.get("formats") or []) or "Any", "inline": True},
        ]
        if verification.get("agree"):
            fields.append({"name": "Verified against", "value": ", ".join(verification["agree"]), "inline": False})
        fields.append(
            {
                "name": "Links",
                "value": f"[Deezer]({match.get('deezer_url')}) · [Request]({match.get('request_url')})",
                "inline": False,
            }
        )

        return await self._post(
            {
                "content": f"Fillable request on {match.get('tracker')}",
                "embeds": [
                    {
                        "title": f"{match.get('artist')} - {match.get('album')}",
                        "url": match.get("request_url"),
                        "color": COLOURS["fillable"] if match.get("all_flac") else COLOURS["warning"],
                        "thumbnail": {"url": match["deezer_cover"]} if match.get("deezer_cover") else None,
                        "fields": fields,
                    }
                ],
            }
        )

    async def summary(self, title: str, stats: dict[str, Any]) -> bool:
        """Post a run summary.

        Args:
            title: Headline for the embed.
            stats: Label/value pairs to render as inline fields.

        Returns:
            True if Discord accepted the message.
        """
        fields = [{"name": str(k), "value": str(v), "inline": True} for k, v in stats.items()]
        return await self._post(
            {"content": title, "embeds": [{"title": title, "color": COLOURS["summary"], "fields": fields}]}
        )
