"""Everything a tracker knows about one request, in a shape the page can draw.

The request page cannot be embedded -- RED and OPS both send
``X-Frame-Options``, which is a browser-enforced refusal with no client-side way
around it -- so the split view renders the request itself. The ajax API returns
the same record the page is built from, only unrendered: BBCode descriptions,
epoch-ish timestamps, tag maps, and per-tracker names for the same fields.

This normalises all of that into one dict. Nothing is dropped on the way: any
key the tracker sent that is not one of the known ones is carried through under
``extra``, so a field appearing on one site and not the other still reaches the
page instead of vanishing here.
"""

from datetime import datetime, timezone
from typing import Any

from lox.checker.bbcode import render
from lox.checker.deezer_requests import format_bounty
from lox.checker.gateway import TrackerGateway, plain

__all__ = ["request_detail"]

# Fields read explicitly below. Anything else the tracker sends is passed on
# under "extra" rather than being silently discarded.
_KNOWN = {
    "requestId", "id", "requestorId", "requestorName", "requestTaxId", "timeAdded", "lastVote",
    "canEdit", "canVote", "minimumVote", "voteCount", "topContributors", "totalBounty", "bounty",
    "categoryId", "categoryName", "title", "year", "image", "description", "musicInfo",
    "catalogueNumber", "recordLabel", "releaseType", "releaseName", "bitrateList", "formatList",
    "mediaList", "logCue", "isFilled", "fillerId", "fillerName", "torrentId", "timeFilled",
    "tags", "comments", "commentPage", "commentPages", "artists", "oclc", "isBookmarked",
}

_ROLE_LABEL = {
    "artists": "Artists",
    "with": "With",
    "composers": "Composers",
    "conductor": "Conductor",
    "dj": "DJ / compiler",
    "remixedBy": "Remixed by",
    "producer": "Producer",
}


def _when(value: Any) -> str:
    """A timestamp as the tracker gives it, normalised to ISO or left alone.

    Gazelle sends "2016-10-13 18:26:39" for some fields and an epoch for
    others, and the two sites disagree about which. Both are accepted; anything
    else is passed through untouched rather than guessed at.
    """
    if value in (None, "", 0, "0"):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _people(music_info: Any) -> list[dict[str, Any]]:
    """The cast, grouped by the role the tracker filed them under."""
    if not isinstance(music_info, dict):
        return []
    groups = []
    for key, label in _ROLE_LABEL.items():
        entries = music_info.get(key) or []
        names = [
            plain(entry.get("name", "")) if isinstance(entry, dict) else plain(entry)
            for entry in entries
            if entry
        ]
        names = [name for name in names if name]
        if names:
            groups.append({"role": label, "names": names})
    return groups


def _tags(raw: Any) -> list[str]:
    """Tags, which arrive as a list on one tracker and an id-to-name map on the other."""
    if isinstance(raw, dict):
        return [plain(name) for name in raw.values() if name]
    if isinstance(raw, list):
        return [plain(tag) for tag in raw if tag]
    return []


def _contributors(raw: Any) -> list[dict[str, str]]:
    """Who put bounty on it, and how much."""
    rows = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "name": plain(entry.get("userName") or entry.get("username") or ""),
                "id": str(entry.get("userId") or entry.get("id") or ""),
                "bounty": format_bounty(entry.get("bounty")),
            }
        )
    return [row for row in rows if row["name"]]


def _comments(raw: Any, base_url: str) -> list[dict[str, Any]]:
    """The comment thread, rendered."""
    rows = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        body = entry.get("comment") or entry.get("body") or ""
        rows.append(
            {
                "id": str(entry.get("postId") or entry.get("id") or ""),
                "author": plain(entry.get("name") or entry.get("username") or entry.get("author") or ""),
                "author_id": str(entry.get("authorId") or entry.get("userId") or ""),
                "added": _when(entry.get("addedTime") or entry.get("time")),
                "edited_by": plain(entry.get("editedUsername") or ""),
                "edited": _when(entry.get("editedTime")),
                "html": render(body, base_url),
            }
        )
    return rows


def _artist_name(raw: dict) -> str:
    """The headline artist, however this tracker chose to say it."""
    music_info = raw.get("musicInfo")
    if isinstance(music_info, dict):
        for key in ("artists", "with", "composers", "dj", "conductor"):
            entries = music_info.get(key) or []
            if entries and isinstance(entries[0], dict) and entries[0].get("name"):
                return plain(entries[0]["name"])
    artists = raw.get("artists") or []
    if artists and isinstance(artists[0], list) and artists[0]:
        first = artists[0][0]
        if isinstance(first, dict):
            return plain(first.get("name", ""))
    return ""


async def request_detail(gateway: TrackerGateway, tracker: str, request_id: int) -> dict[str, Any]:
    """Fetch one request and lay it out for display.

    Args:
        gateway: The tracker gateway, which spends the call against the budget.
        tracker: Tracker code.
        request_id: The request's ID on that tracker.

    Returns:
        Every field the tracker returned: the header, the terms it will accept,
        the vote and bounty state, who filled it if anyone, the description and
        the full comment thread -- descriptions and comments rendered from
        BBCode -- plus anything unrecognised under ``extra``.
    """
    raw = await gateway.get_request(tracker, int(request_id))
    raw = raw if isinstance(raw, dict) else {}
    base_url = gateway.api(tracker).base_url.rstrip("/")

    filled = bool(raw.get("isFilled"))
    detail: dict[str, Any] = {
        "tracker": tracker,
        "id": str(raw.get("requestId") or raw.get("id") or request_id),
        "url": gateway.request_url(tracker, int(request_id)),
        "title": plain(raw.get("title")),
        "artist": _artist_name(raw),
        "year": str(raw.get("year") or ""),
        "image": raw.get("image") or "",
        "category": plain(raw.get("categoryName")),
        "release_type": plain(raw.get("releaseType")),
        "record_label": plain(raw.get("recordLabel")),
        "catalogue_number": plain(raw.get("catalogueNumber")),
        "release_name": plain(raw.get("releaseName")),
        "oclc": plain(raw.get("oclc")),
        "log_cue": plain(raw.get("logCue")),
        "created": _when(raw.get("timeAdded")),
        "requestor": plain(raw.get("requestorName")),
        "requestor_id": str(raw.get("requestorId") or ""),
        "bitrates": [plain(b) for b in (raw.get("bitrateList") or [])],
        "formats": [plain(f) for f in (raw.get("formatList") or [])],
        "media": [plain(m) for m in (raw.get("mediaList") or [])],
        "votes": raw.get("voteCount") or 0,
        "last_vote": _when(raw.get("lastVote")),
        "minimum_vote": format_bounty(raw.get("minimumVote")),
        "bounty": format_bounty(raw.get("totalBounty") or raw.get("bounty")),
        "contributors": _contributors(raw.get("topContributors")),
        "tags": _tags(raw.get("tags")),
        "people": _people(raw.get("musicInfo")),
        "filled": filled,
        "filled_by": plain(raw.get("fillerName")),
        "filled_at": _when(raw.get("timeFilled")) if filled else "",
        "torrent_id": str(raw.get("torrentId") or "") if filled else "",
        "torrent_url": (
            f"{base_url}/torrents.php?torrentid={raw['torrentId']}"
            if filled and raw.get("torrentId")
            else ""
        ),
        "description_html": render(raw.get("description"), base_url),
        "comments": _comments(raw.get("comments"), base_url),
        "comment_page": raw.get("commentPage") or 1,
        "comment_pages": raw.get("commentPages") or 1,
        "bookmarked": bool(raw.get("isBookmarked")),
    }
    detail["extra"] = {
        key: value for key, value in raw.items() if key not in _KNOWN and not isinstance(value, (dict, list))
    }
    return detail
