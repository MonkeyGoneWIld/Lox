"""Given open tracker requests, find Deezer releases that could fill them.

The mirror image of :mod:`lox.uploader.request_fill`, which asks the
opposite question: given a release being uploaded, which open requests does
it satisfy?

Fetching the request itself is the only step that costs tracker budget. Deezer
search, match scoring, FLAC/streamability checks and external track-count
verification are all free, so they run afterwards and filter hard — a wrong fill
is worse than no fill.
"""

from collections.abc import Callable
from typing import Any

import msgspec

from lox import cfg
from lox.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerUnavailable, plain
from lox.checker.matching import MIN_TOTAL_SCORE, find_best_deezer_match
from lox.checker.request_filters import build_params
from lox.checker.store import CheckerStore
from lox.checker.trackcount import TrackCountVerifier, track_count_from_description
from lox.deezer.gw import DeezerGW, DeezerGWError

ProgressFn = Callable[[str, dict[str, Any]], None]

ACCEPTED_FORMATS = frozenset({"MP3", "FLAC", "Any"})
ACCEPTED_MEDIA = frozenset({"WEB", "Any"})


class RequestMatch(msgspec.Struct):
    """The outcome of checking one request."""

    request_id: str
    tracker: str
    status: str
    artist: str = ""
    album: str = ""
    year: str = ""
    bounty: str = ""
    request_url: str = ""
    reason: str | None = None
    deezer_id: str | None = None
    deezer_title: str | None = None
    deezer_artist: str | None = None
    deezer_url: str | None = None
    deezer_cover: str | None = None
    deezer_tracks: int | None = None
    confidence: float = 0.0
    scores: dict[str, Any] = msgspec.field(default_factory=dict)
    all_flac: bool = False
    all_readable: bool = False
    verification: dict[str, Any] = msgspec.field(default_factory=dict)
    formats: list[str] = msgspec.field(default_factory=list)
    media: list[str] = msgspec.field(default_factory=list)
    bitrates: list[str] = msgspec.field(default_factory=list)

    @property
    def fillable(self) -> bool:
        """True when this request has a usable Deezer source."""
        return self.status == "fillable"

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        data = msgspec.to_builtins(self)
        data["fillable"] = self.fillable
        return data


def format_bounty(value: Any) -> str:
    """Render a byte count as a human-readable bounty."""
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit, divisor in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= divisor:
            return f"{size / divisor:.2f} {unit}"
    return f"{size} B"


class DeezerRequestChecker:
    """Resolves tracker requests to Deezer releases."""

    def __init__(
        self,
        gw: DeezerGW,
        gateway: TrackerGateway,
        store: CheckerStore | None = None,
    ) -> None:
        """Initialize the checker.

        Args:
            gw: Authenticated Deezer private API client.
            gateway: The rate-limited tracker gateway.
            store: Persistent result store. Created from config if omitted.
        """
        self.gw = gw
        self.gateway = gateway
        self.store = store or CheckerStore()

    async def search_requests(
        self,
        tracker: str,
        search: str = "",
        page: int = 1,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        """List one page of requests on a tracker.

        Args:
            tracker: Tracker code.
            search: Search string, matched against artist and title.
            page: Result page, 1-based.
            **filters: Selections by label, passed to
                :func:`lox.checker.request_filters.build_params`, which knows
                what each tracker calls them and which IDs it uses.

        Returns:
            The page's request summaries, and how many pages the tracker says
            there are (0 when it does not say).

        Raises:
            TrackerBudgetExceeded: If the tracker's budget is spent.
        """
        params = build_params(tracker, page=page, search=search, **filters)
        data = await self.gateway.call_action(tracker, "requests", params)
        rows = (data or {}).get("results") or []
        try:
            pages = int((data or {}).get("pages") or 0)
        except (TypeError, ValueError):
            pages = 0

        summaries = []
        for row in rows:
            request_id = self._id_of(row)
            if request_id is None:
                continue
            summaries.append(
                {
                    "id": request_id,
                    "title": plain(row.get("title")),
                    "artist": plain(self._artist_of(row)),
                    "year": str(row.get("year") or ""),
                    "bounty": format_bounty(row.get("totalBounty") or row.get("bounty")),
                    "url": self.gateway.request_url(tracker, int(request_id)),
                }
            )
        return summaries, pages

    @staticmethod
    def _id_of(row: dict) -> str | None:
        """The request's ID, or None if the row has no usable one.

        Tested for presence rather than truth: an ID of 0 is falsy, and a truth
        test silently drops that row instead of listing it.
        """
        for key in ("requestId", "id"):
            value = row.get(key)
            if value is None or value == "":
                continue
            try:
                return str(int(value))
            except (TypeError, ValueError):
                return None
        return None

    async def collect_requests(
        self,
        tracker: str,
        search: str = "",
        *,
        limit: int = 25,
        **filters: Any,
    ) -> dict[str, Any]:
        """Page through requests until ``limit`` of them are gathered.

        The page size belongs to the tracker, not to us -- RED and OPS both
        default to 25 -- so asking for more than that means more than one call.
        Each page is one call against the budget, which is why the count is a
        deliberate choice in the UI rather than something that creeps upward.

        Args:
            tracker: Tracker code.
            search: Search string.
            limit: How many requests to gather.
            **filters: Selections by label, translated per tracker.

        Returns:
            ``requests``, the number of ``calls`` spent, and ``complete``, which
            is False when the tracker ran out of results before the limit.

        Raises:
            TrackerBudgetExceeded: If the budget runs out mid-collection.
        """
        gathered: list[dict[str, Any]] = []
        seen: set[str] = set()
        calls = 0
        page = 1
        total_pages = 0

        while len(gathered) < limit:
            rows, pages = await self.search_requests(tracker, search, page, **filters)
            calls += 1
            total_pages = pages or total_pages
            # A page that repeats what we already have means the tracker is
            # ignoring the page parameter; stop rather than loop forever.
            fresh = [row for row in rows if row["id"] not in seen]
            seen.update(row["id"] for row in fresh)
            gathered.extend(fresh)

            if not rows or not fresh:
                break
            if total_pages and page >= total_pages:
                break
            page += 1

        return {
            "requests": gathered[:limit],
            "calls": calls,
            "complete": len(gathered) >= limit,
            "pages": total_pages,
        }

    @staticmethod
    def _artist_of(row: dict) -> str:
        """Pull a displayable artist name out of a request payload."""
        music_info = row.get("musicInfo")
        if isinstance(music_info, dict):
            artists = music_info.get("artists") or []
            if artists and isinstance(artists[0], dict):
                return artists[0].get("name", "")
        artists = row.get("artists") or []
        if artists and isinstance(artists[0], list) and artists[0]:
            return artists[0][0].get("name", "")
        return ""

    async def check_many(
        self,
        tracker: str,
        request_ids: list[str],
        progress: ProgressFn | None = None,
        skip_known: bool = True,
    ) -> list[RequestMatch]:
        """Check a batch of requests.

        Args:
            tracker: Tracker code the requests belong to.
            request_ids: Request IDs to check.
            progress: Optional callback receiving (event, payload) updates.
            skip_known: Skip requests with a stored final status.

        Returns:
            One RequestMatch per request that was actually checked.
        """
        emit = progress or (lambda *_: None)
        verifier = TrackCountVerifier()
        results: list[RequestMatch] = []

        try:
            for index, request_id in enumerate(request_ids, 1):
                key = f"{tracker}:{request_id}"
                if skip_known and self.store.should_skip("requests", key):
                    continue

                emit("progress", {"current": index, "total": len(request_ids), "request_id": request_id})

                if not self.gateway.can_check(tracker):
                    emit(
                        "budget_exhausted",
                        {"checked": len(results), "remaining": len(request_ids) - index + 1},
                    )
                    break

                try:
                    match = await self._check_one(tracker, request_id, verifier)
                except (TrackerBudgetExceeded, TrackerUnavailable) as e:
                    emit("budget_exhausted", {"error": str(e), "checked": len(results)})
                    break
                except Exception as e:  # noqa: BLE001 - one bad request must not abort the batch
                    match = RequestMatch(
                        request_id=str(request_id),
                        tracker=tracker,
                        status="error",
                        reason=str(e),
                    )

                self.store.put("requests", key, {"status": match.status, "reason": match.reason})
                results.append(match)
                emit("result", match.as_dict())
        finally:
            await verifier.close()
            self.store.flush("requests")

        return results

    async def _check_one(self, tracker: str, request_id: str, verifier: TrackCountVerifier) -> RequestMatch:
        """Run the full pipeline for a single request."""
        raw = await self.gateway.get_request(tracker, int(request_id))
        match = RequestMatch(
            request_id=str(request_id),
            tracker=tracker,
            status="skipped",
            request_url=self.gateway.request_url(tracker, int(request_id)),
        )

        artist = self._artist_of(raw)
        album = raw.get("title")
        match.artist = artist
        match.album = album or ""
        match.year = str(raw.get("year") or "")
        match.bounty = format_bounty(raw.get("totalBounty") or raw.get("bounty"))
        match.formats = raw.get("formatList") or ["Any"]
        match.media = raw.get("mediaList") or []
        match.bitrates = raw.get("bitrateList") or []

        if not artist or not album:
            match.reason = "request has no artist or album (likely a non-music category)"
            return match

        if not (ACCEPTED_FORMATS & set(match.formats)):
            match.reason = f"no acceptable format: {', '.join(match.formats)}"
            return match
        if match.media and not (ACCEPTED_MEDIA & set(match.media)):
            match.reason = f"media excludes WEB: {', '.join(match.media)}"
            return match
        if match.bitrates and all(b == "24bit Lossless" for b in match.bitrates):
            match.reason = "only 24bit Lossless accepted; Deezer cannot supply it"
            return match

        description = raw.get("description") or ""
        expected = await verifier.expected_count(description)

        try:
            albums = await self.gw.search_albums(f"{artist} {album}", limit=25)
        except DeezerGWError as e:
            match.status = "error"
            match.reason = f"Deezer search failed: {e}"
            return match

        if not albums:
            match.reason = "no Deezer results"
            return match

        best, confidence, details = find_best_deezer_match(albums, artist, album, expected)
        match.confidence = round(confidence, 3)
        match.scores = details

        if not best or confidence < max(MIN_TOTAL_SCORE, cfg.checker.min_confidence):
            match.reason = f"no confident Deezer match (best {confidence:.2f})"
            return match

        deezer_id = str(best["id"])
        match.deezer_id = deezer_id
        match.deezer_title = details.get("dz_title")
        match.deezer_artist = details.get("dz_artist")
        match.deezer_url = f"https://www.deezer.com/album/{deezer_id}"
        match.deezer_cover = best.get("cover_medium") or best.get("cover")

        try:
            availability = await self.gw.availability(deezer_id)
        except DeezerGWError as e:
            match.status = "error"
            match.reason = f"availability check failed: {e}"
            return match

        match.all_flac = availability.all_flac
        match.all_readable = availability.all_readable
        match.deezer_tracks = availability.total

        if not availability.all_readable:
            match.reason = f"{len(availability.unreadable)} track(s) not streamable in your region"
            return match

        flac_only = match.formats == ["FLAC"]
        if flac_only and not availability.all_flac:
            match.reason = f"request is FLAC-only but Deezer has {availability.flac_count}/{availability.total} FLAC"
            return match

        described = track_count_from_description(description)
        if described and described != availability.total:
            match.reason = f"description says {described} tracks, Deezer has {availability.total}"
            match.verification = {"agree": [], "disagree": [f"Description ({described})"], "errors": [], "links": {}}
            return match

        match.verification = await verifier.verify(availability.total, description)
        if match.verification["disagree"]:
            match.reason = f"track count disagrees with {', '.join(match.verification['disagree'])}"
            return match

        match.status = "fillable"
        match.reason = None
        return match
