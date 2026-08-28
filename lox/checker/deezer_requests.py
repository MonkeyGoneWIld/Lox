"""Given open tracker requests, find Deezer releases that could fill them.

The mirror image of :mod:`lox.uploader.request_fill`, which asks the
opposite question: given a release being uploaded, which open requests does
it satisfy?

Fetching the request itself is the only step that costs tracker budget. Deezer
search, match scoring, FLAC/streamability checks and external track-count
verification are all free, so they run afterwards and filter hard — a wrong fill
is worse than no fill.
"""

import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

import msgspec

from lox import cfg, debug
from lox.checker import recheck
from lox.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerUnavailable, plain
from lox.checker.matching import MIN_TOTAL_SCORE, find_best_deezer_match
from lox.checker.request_filters import PAGE_SIZE, build_params
from lox.checker.store import CheckerStore
from lox.checker.trackcount import TrackCountVerifier, track_count_from_description
from lox.deezer.gw import DeezerGW, DeezerGWError

ProgressFn = Callable[[str, dict[str, Any]], None]

ACCEPTED_FORMATS = frozenset({"MP3", "FLAC", "Any"})
ACCEPTED_MEDIA = frozenset({"WEB", "Any"})


def _unset(value: object) -> bool:
    """Whether a Gazelle field is one of its several spellings of "nothing".

    The tracker fills unset columns with a zero rather than leaving them out:
    an unfilled request carries ``torrentId`` 0, ``fillerId`` 0 and
    ``timeFilled`` "0000-00-00 00:00:00". Every one of those is a non-empty
    string in JSON, so a plain truth test reads them as values -- which is how
    open requests came back reported as "already filled on 0000-00-00", were
    dropped from the results, and never reached the queue.

    Anything whose digits are all zero means never. A real id or a real date
    has a digit that is not a zero somewhere in it.

    Args:
        value: A field from a request row.

    Returns:
        True when the field carries no value.
    """
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return not any(char.isdigit() and char != "0" for char in text)


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
    #: Track titles Deezer will not hand over, so the page can name them
    #: rather than only counting them.
    deezer_unavailable: list[str] = msgspec.field(default_factory=list)
    release_date: str = ""
    verification: dict[str, Any] = msgspec.field(default_factory=dict)
    # Whether the tracker already has this release, and where. A request left
    # open after somebody uploaded it is not worth filling twice.
    already_on_tracker: bool | None = None
    tracker_group_url: str | None = None
    #: The same answer in the vocabulary the queue reads.
    #:
    #: "already_on_tracker: false" and "missing_from: [OPS]" are the same
    #: sentence, and only the second one is a sentence the queue understands.
    #: Writing only the first is why a request that was checked, matched at
    #: 100% and confirmed absent from the tracker was held out of the queue as
    #: "not checked against any tracker yet".
    found_on: list[str] = msgspec.field(default_factory=list)
    missing_from: list[str] = msgspec.field(default_factory=list)
    group_ids: dict[str, int] = msgspec.field(default_factory=dict)
    # Whether the request was already filled before we looked at it, and by
    # whom. A filled request cannot be filled again, so this ends the check.
    filled: bool = False
    filled_by: str = ""
    filled_at: str = ""
    # When the request was created, so the page can say how long it has sat.
    created: str = ""
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


def age_of(stamp: Any) -> str:
    """How long ago a tracker timestamp was, in words.

    Gazelle sends "2026-08-20 09:23:07" in the site's own timezone and no
    offset, so this is read as naive local time -- close enough for "how long
    has this been sitting there", which is the only question it answers.

    Args:
        stamp: The tracker's timestamp string.

    Returns:
        Something like "3 days" or "5 months", or "" if it cannot be read.
    """
    text = str(stamp or "").strip()
    if not text:
        return ""
    try:
        when = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    seconds = (datetime.now() - when).total_seconds()
    if seconds < 0:
        return "just now"
    for size, unit in ((31536000, "year"), (2592000, "month"), (604800, "week"),
                       (86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return "just now"


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
    ) -> tuple[list[dict[str, Any]], int, int]:
        """List one page of requests on a tracker.

        Args:
            tracker: Tracker code.
            search: Search string, matched against artist and title.
            page: Result page, 1-based.
            **filters: Selections by label, passed to
                :func:`lox.checker.request_filters.build_params`, which knows
                what each tracker calls them and which IDs it uses.

        Returns:
            The page's request summaries, how many pages the tracker says there
            are (0 when it does not say), and how many rows were dropped as
            already filled.

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

        # OPS answers show_filled=false with filled requests anyway. Measured on
        # a real fetch: 73 of 100 rows across four pages came back carrying
        # isFilled, a filler and a torrent id, which is a request nobody can
        # fill again -- so three quarters of a paid-for page was spent looking
        # up Deezer albums for requests that were already closed.
        #
        # The row says so itself, so that is what gets believed. Asking the
        # tracker nicely and then trusting the answer is what produced this;
        # every row is checked here whatever the parameter did.
        want_filled = bool(filters.get("show_filled"))
        dropped = 0

        summaries = []
        for row in rows:
            if not want_filled and self._is_filled(row):
                dropped += 1
                continue
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
                    # How long it has sat there, and whether anyone has filled
                    # it. Both come free in the row the tracker already sent.
                    "created": plain(row.get("timeAdded") or ""),
                    "age": age_of(row.get("timeAdded")),
                    "filled": self._is_filled(row),
                    "filled_by": plain(row.get("fillerName") or ""),
                }
            )
        if dropped:
            debug.log(
                "requests %s page %s: %s rows, %s already filled and dropped, %s usable",
                tracker, page, len(rows), dropped, len(summaries), level=20,
            )
        else:
            debug.log(
                "requests %s page %s: %s rows, none filled", tracker, page, len(rows), level=10
            )
        return summaries, pages, dropped

    @staticmethod
    def _is_filled(row: dict) -> bool:
        """Whether the tracker says this request has already been filled.

        Read from several fields because one alone is not reliable across the
        two sites: a filled request carries ``isFilled``, but it also names its
        filler and the torrent that filled it, and a row that has those has
        been filled whatever the flag says.
        """
        if row.get("isFilled") in (True, 1, "1", "true"):
            return True
        return any(not _unset(row.get(key)) for key in ("torrentId", "fillerId", "timeFilled"))

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
        start_page: int = 1,
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
            start_page: Which page to begin at, 1-based. Asking for one page at
                a time is how the browser drives a search it can show progress
                for and stop halfway through: a cancelled search then costs
                only the pages already read, where one long call would have
                spent every page's budget before anyone could stop it.
            **filters: Selections by label, translated per tracker.

        Returns:
            ``requests``, the number of ``calls`` spent, ``filtered`` -- how many
            already-filled rows were dropped -- ``complete``, which is False
            when the tracker ran out of results before the limit, and how much
            of the whole search was read: ``pages_read`` of ``pages``, with
            ``total_estimate`` rows matching in total.

        Raises:
            TrackerBudgetExceeded: If the budget runs out mid-collection.
        """
        gathered: list[dict[str, Any]] = []
        seen: set[str] = set()
        calls = 0
        filtered = 0
        page = max(1, start_page)
        total_pages = 0

        # The limit is a page count in disguise: the UI asks for pages, because
        # a page is a tracker call and calls are the thing worth counting. So
        # the ceiling here is calls, not rows.
        #
        # Looping until `limit` rows were gathered was fine while every row
        # counted. Now that the filled ones are dropped -- three quarters of
        # them on OPS -- the same loop would have kept paying for pages until it
        # scraped together a hundred, turning a four-call fetch into fifteen and
        # spending a budget nobody agreed to. Ask for four pages, pay for four
        # pages, and be told what they contained.
        max_calls = max(1, -(-limit // PAGE_SIZE))

        while len(gathered) < limit and calls < max_calls:
            rows, pages, dropped = await self.search_requests(tracker, search, page, **filters)
            calls += 1
            filtered += dropped
            total_pages = pages or total_pages
            # A page that repeats what we already have means the tracker is
            # ignoring the page parameter; stop rather than loop forever.
            fresh = [row for row in rows if row["id"] not in seen]
            seen.update(row["id"] for row in fresh)
            gathered.extend(fresh)

            # `rows` is what survived the filter, so a page can legitimately be
            # empty while the tracker still has plenty left. Stopping on that
            # would end the fetch on the first page that happened to be all
            # filled. Only an empty page from the tracker itself means the end.
            if not rows and not dropped:
                break
            if not fresh and not dropped:
                break
            if total_pages and page >= total_pages:
                break
            page += 1

        debug.log(
            "requests %s: %s call(s), %s usable, %s already filled",
            tracker, calls, len(gathered), filtered, level=20,
        )
        return {
            "requests": gathered[:limit],
            "calls": calls,
            "filtered": filtered,
            "complete": len(gathered) >= limit,
            "pages": total_pages,
            # How much of the search was actually looked at. A four-page fetch
            # against a search the tracker answers with four hundred pages
            # returned a hundred rows and said "100 requests from 4 calls",
            # which reads as the whole result -- so a search that matched ten
            # thousand requests and a search that matched a hundred looked
            # identical. Gazelle reports pages, not a row count, so the total
            # is an estimate and is described as one.
            "pages_read": min(calls, total_pages) if total_pages else calls,
            "page_size": PAGE_SIZE,
            "total_estimate": total_pages * PAGE_SIZE if total_pages else 0,
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
        force: bool = False,
    ) -> list[RequestMatch]:
        """Check a batch of requests, skipping the ones already answered.

        A check is a tracker call and a Deezer search per request. Running the
        same search twice paid for all of it twice: ``should_skip`` was asking
        whether the status was one of the *album* scanner's final statuses, and
        a request is never any of those, so nothing was ever skipped and every
        run started from nothing.

        Args:
            tracker: Tracker code the requests belong to.
            request_ids: Request IDs to check.
            progress: Optional callback receiving (event, payload) updates.
            skip_known: Reuse answers that are still inside the recheck window.
            force: Check everything, however recently it was checked. This is
                what "run them anyway" does.

        Returns:
            One RequestMatch per request that was actually checked. What was
            skipped, and why, is emitted as a ``skipped`` event before any call
            is made -- a run that quietly did a tenth of what was asked looks
            broken, and the caller needs the list to offer running them anyway.
        """
        emit = progress or (lambda *_: None)
        verifier = TrackCountVerifier()
        results: list[RequestMatch] = []

        window = int(getattr(cfg.checker, "request_recheck_after_days", 30) or 0)
        if skip_known and not force:
            request_ids, skipped = recheck.plan(
                self.store, tracker, [str(r) for r in request_ids], recheck_after_days=window
            )
        else:
            skipped = []
        if skipped:
            debug.log(
                "requests %s: %s already answered, %s to check",
                tracker, len(skipped), len(request_ids), level=20,
            )
        emit("skipped", {"tracker": tracker, "requests": skipped, "count": len(skipped),
                         "recheck_after_days": window})

        try:
            for index, request_id in enumerate(request_ids, 1):
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

                # The whole match, not just its verdict. A fillable request is
                # the useful half of a check -- which release fills it, where it
                # is on Deezer, how confident the match was -- and storing only
                # the status meant the Found tab had nothing to show.
                self.store.put(
                    "requests",
                    f"{tracker}:{request_id}",
                    {
                        "status": match.status,
                        "reason": match.reason,
                        "tracker": match.tracker,
                        "artist": match.artist,
                        "album": match.album,
                        "year": match.year,
                        "bounty": match.bounty,
                        "request_url": match.request_url,
                        "deezer_id": match.deezer_id,
                        "deezer_title": match.deezer_title,
                        "deezer_artist": match.deezer_artist,
                        # Stored so the Found page can exclude it without
                        # re-reading the tracker, and so a re-check knows.
                        "filled": match.filled,
                        "filled_by": match.filled_by,
                        "filled_at": match.filled_at,
                        "created": match.created,
                        "deezer_url": match.deezer_url,
                        "confidence": match.confidence,
                        "already_on_tracker": match.already_on_tracker,
                        "tracker_group_url": match.tracker_group_url,
                        "found_on": match.found_on,
                        "missing_from": match.missing_from,
                        "group_ids": match.group_ids,
                        # What Deezer has, and what this request will take.
                        # The queue needs both to answer the only question
                        # that matters for a source that is not all FLAC: did
                        # anyone actually ask for lossy?
                        "all_flac": match.all_flac,
                        "deezer_tracks": match.deezer_tracks,
                        "deezer_unavailable": match.deezer_unavailable,
                        "release_date": match.release_date,
                        "request_formats": match.formats,
                        "request_encodings": match.bitrates,
                    },
                )
                results.append(match)
                emit("result", match.as_dict())
        finally:
            await verifier.close()
            self.store.flush("requests")

        return results

    async def _check_one(self, tracker: str, request_id: str, verifier: TrackCountVerifier) -> RequestMatch:
        """Run the full pipeline for a single request."""
        raw = await self.gateway.get_request(tracker, int(request_id))
        # The payload is already in hand and the tracker takes the better part
        # of a minute to produce it, so the page's copy is rendered and kept
        # here rather than fetched again the first time somebody opens the row.
        # Imported here: request_detail imports this module for format_bounty.
        from lox.checker.request_detail import cache_detail  # noqa: PLC0415

        cache_detail(self.store, self.gateway, tracker, int(request_id), raw)
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
        match.created = plain(raw.get("timeAdded") or "")

        # A filled request is finished. Nothing below this can change that, and
        # everything below it costs something: a Deezer search, an availability
        # lookup, sometimes a second tracker call to ask whether the release is
        # already up. All of it was being spent on requests that had already
        # been closed -- and when the "is it on the tracker" search then missed,
        # the release was filed under Found as worth uploading. Twice over
        # wrong, and both halves stop here.
        if self._is_filled(raw):
            match.filled = True
            match.filled_by = plain(raw.get("fillerName") or "")
            match.filled_at = plain(raw.get("timeFilled") or "")
            match.already_on_tracker = True
            match.status = "filled"
            who = f" by {match.filled_by}" if match.filled_by else ""
            when = f" on {match.filled_at.split(' ')[0]}" if match.filled_at else ""
            match.reason = f"already filled{who}{when}"
            debug.log(
                "request %s:%s already filled%s -- no Deezer check",
                tracker, request_id, who, level=20,
            )
            return match
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
        match.deezer_unavailable = list(availability.unreadable)
        match.release_date = availability.release_date
        match.deezer_tracks = availability.total

        # Whatever Deezer cannot supply, in its own words: not out yet, only
        # four of eleven tracks fetchable, no song ids. One verdict rather
        # than this path's own partial re-statement of it.
        blocked = availability.reason()
        if blocked and not availability.all_flac and availability.all_readable:
            # FLAC alone is the request's business, not ours: a request that
            # takes MP3 is still fillable from a lossy source.
            blocked = None
        if blocked:
            match.reason = blocked
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

        # A release the operator has blacklisted is not an upload, whatever
        # a request says about it. Checked here rather than when the queue is
        # drawn, so the row never gets made and no tracker call is spent
        # confirming something that has already been refused.
        if self.store.get("dismissed", str(match.deezer_id or "")):
            match.status = "skipped"
            match.reason = "the release it matches is blacklisted"
            return match

        match.status = "fillable"
        match.reason = None

        # One more question, and the one that decides whether filling is worth
        # doing: who already has this release? A request can sit open for
        # something somebody uploaded since, and filling it with a duplicate
        # helps nobody.
        #
        # Every configured tracker, not only the one the request is on. The
        # release is the same release whoever is asked, and asking only the
        # requesting tracker produced a queue row that knew about OPS and
        # nothing about RED -- so the upload that followed either skipped a
        # tracker that wanted it or offered one that already had it.
        await self._locate(match)

        # The requesting tracker's answer, kept as its own field because
        # "should I fill this request" is a question about that tracker.
        if tracker in match.found_on:
            match.already_on_tracker = True
        elif tracker in match.missing_from:
            match.already_on_tracker = False

        return match

    async def _locate(self, match: RequestMatch) -> None:
        """Ask every configured tracker whether it already has this release.

        Writes ``found_on``, ``missing_from`` and ``group_ids`` on the match --
        the same three facts a scan produces, in the same words, so the queue
        cannot tell a request-matched release apart from a scanned one.

        A tracker with no budget left, or one that errors, is simply not in
        either list: not asked is not the same as asked and answered.
        """
        for code in self.gateway.configured_trackers():
            if not self.gateway.can_check(code):
                debug.log("request %s: %s not asked, no budget", match.request_id, code, level=20)
                continue
            try:
                on_it = await self._on_tracker(code, match)
            except Exception as e:  # noqa: BLE001 - an extra fact, never a failure
                debug.log("tracker search for request %s on %s failed: %s",
                          match.request_id, code, e, level=30)
                continue
            if on_it is True:
                match.found_on.append(code)
            elif on_it is False:
                match.missing_from.append(code)

    async def _on_tracker(self, tracker: str, match: RequestMatch) -> bool | None:
        """Whether one tracker already has the release this match would fill."""
        query = " ".join(p for p in (match.deezer_artist, match.deezer_title) if p).strip()
        if not query:
            return None
        data = await self.gateway.call_action(tracker, "browse", {"searchstr": query, "group_results": 1})
        groups = (data or {}).get("results") or []
        # Scored the same way a request is scored against Deezer, just in the
        # other direction: the tracker's groups are the candidates here.
        candidates = [
            {
                "id": group.get("groupId"),
                "title": plain(group.get("groupName") or ""),
                "artist": {"name": plain(group.get("artist") or "")},
            }
            for group in groups
            if group.get("groupName")
        ]
        if not candidates:
            return False

        best, score, _ = find_best_deezer_match(
            candidates, match.deezer_artist or "", match.deezer_title or ""
        )
        if best and score >= MIN_TOTAL_SCORE:
            # The link belongs to the requesting tracker, which is the one
            # the "already on tracker" tag on the request row is about.
            if tracker == match.tracker:
                match.tracker_group_url = f"{self.gateway.api(tracker).base_url}/torrents.php?id={best.get('id')}"
            group_id = best.get("id")
            if group_id is not None:
                with contextlib.suppress(TypeError, ValueError):
                    match.group_ids[tracker] = int(group_id)
            return True
        return False
