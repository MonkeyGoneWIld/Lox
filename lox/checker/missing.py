"""Find Deezer releases that are not on the trackers yet.

Deliberately split into two phases. :meth:`MissingScanner.collect` reads
playlists and channel modules and applies every filter that can be answered from
Deezer alone — track count, release date, FLAC availability, streamability. It
costs no tracker budget at all.

Only :meth:`MissingScanner.check` talks to RED/OPS, and only for the albums it is
handed. That is what the "Check trackers" button in the UI drives.
"""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import msgspec

from lox import cfg, debug
from lox.checker import recheck
from lox.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerUnavailable, plain
from lox.checker.matching import build_search_queries, evaluate_group
from lox.checker.request_filters import for_tracker
from lox.checker.store import CheckerStore
from lox.deezer.gw import DeezerGW, DeezerGWError, parse_artist_id, parse_module_id, parse_playlist_id

ProgressFn = Callable[[str, dict[str, Any]], None]


# ----------------------------------------------------------------------
# What a scan looks at by default
# ----------------------------------------------------------------------
# Both date filters are relative to today, so neither can be a number written
# into a config file once. Left as fixed dates they are right on the day they
# are set and wrong every day after: a "released after" of a fixed Tuesday
# starts admitting pre-releases the moment that Tuesday passes, and a "released
# before" of 2025 keeps scanning 2025 for ever.
#
# So they are computed, and an empty setting means "use the computed one". A
# date typed in by hand wins and stays put; clearing it goes back to the roll.


def default_min_date(today: date | None = None) -> str:
    """The oldest release a scan looks at: 1 January of last year.

    Wide enough that a January scan is not blind to everything from December,
    narrow enough that a channel module of reissues does not turn into three
    thousand tracker calls.
    """
    return f"{(today or date.today()).year - 1}-01-01"


def default_max_date(today: date | None = None) -> str:
    """The newest: two days out.

    Not today. Deezer lists a release before it is streamable, and a scan that
    stops at today would still be spending tracker budget on tomorrow's
    announcements. Two days is the announced-but-not-yet-available window.
    """
    return ((today or date.today()) + timedelta(days=2)).isoformat()


def effective_filters(checker: Any = None) -> dict[str, Any]:
    """The filter values a scan will actually apply, defaults resolved.

    Args:
        checker: ``cfg.checker``, or None to read it.

    Returns:
        ``min_tracks``, ``min_date`` and ``max_date`` as the scan sees them.
    """
    checker = checker if checker is not None else cfg.checker
    return {
        "min_tracks": checker.min_tracks,
        "min_date": (checker.min_date or "").strip() or default_min_date(),
        "max_date": (checker.max_date or "").strip() or default_max_date(),
    }


class Candidate(msgspec.Struct):
    """An album that passed every Deezer-side filter."""

    album_id: str
    title: str
    artist: str
    # Everything below is optional, because a re-check does not have it and does
    # not need it: checking an album reads its id, title, artist and source, and
    # fetches the rest from Deezer itself. Requiring them meant the Found page's
    # "Re-check on trackers" -- which only ever knew those four -- was rejected
    # with "Object missing required field `year`" and could not be used at all.
    year: str = ""
    tracks: int = 0
    record_type: str = ""
    cover: str | None = None
    source: str = ""
    deezer_url: str = ""
    availability: dict[str, Any] = msgspec.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return msgspec.to_builtins(self)


def _group_artist(info: dict) -> str:
    """The billed artist for a torrent group.

    ``browse`` returns a flat ``artist`` string, but ``torrentgroup`` -- which
    is what an album check reads -- returns ``musicInfo`` with the cast split by
    role and no flat field at all. Reading only the flat one left the artist
    empty, so the group rendered as "— Bedtime Stories (1994)" with the
    separator leading and nobody named.
    """
    flat = plain(info.get("artist"))
    if flat:
        return flat

    music = info.get("musicInfo") or {}
    for role in ("artists", "with", "composers", "conductor", "dj"):
        people = music.get(role) or []
        names = [plain(p.get("name")) for p in people if isinstance(p, dict) and p.get("name")]
        if names:
            return " & ".join(names[:3]) + (" et al." if len(names) > 3 else "")
    return ""


def _release_type_name(code: str, value: Any) -> str:
    """Turn a numeric release type into its name, using that tracker's table.

    The numbers differ per tracker -- 17 is Demo on RED and DJ Mix on OPS -- so
    the vocabulary transcribed from their own search pages is what resolves it.
    An unknown number yields nothing rather than another tracker's word for it.
    """
    if value is None:
        return ""
    spec = for_tracker(code)
    if spec is None:
        return ""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return ""
    return next((name for name, number in spec.release_types.items() if number == wanted), "")


class GroupHit(msgspec.Struct):
    """One tracker torrent group that was inspected, and why it did or did not match.

    Near misses are kept deliberately. When the checker says a release is
    missing, the useful question is "what did it look at?", and a link to the
    rejected group is the fastest way to answer it by eye.
    """

    group_id: int
    name: str
    artist: str
    year: int | None
    url: str
    matched: bool
    reason: str
    formats: list[str] = msgspec.field(default_factory=list)
    release_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return msgspec.to_builtins(self)


class TrackerVerdict(msgspec.Struct):
    """What one tracker said about one album."""

    tracker: str
    status: str
    match: GroupHit | None = None
    inspected: list[GroupHit] = msgspec.field(default_factory=list)
    queries: list[str] = msgspec.field(default_factory=list)
    calls_used: int = 0
    error: str | None = None
    artist_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return msgspec.to_builtins(self)


class AlbumCheck(msgspec.Struct):
    """The per-album check the UI runs before offering an upload."""

    album_id: str
    title: str
    artist: str
    verdicts: list[TrackerVerdict] = msgspec.field(default_factory=list)
    # What Deezer will actually hand over. "No tracker has it" is only half of
    # "worth uploading"; this is the other half, and it used to go unasked on
    # this path -- so an album checked from Search or Browse joined the queue
    # without anyone knowing whether there was a lossless release behind it.
    all_flac: bool | None = None
    flac_count: int | None = None
    deezer_tracks: int | None = None
    #: Titles Deezer will not hand over, and why the release is unusable.
    unavailable: list[str] = msgspec.field(default_factory=list)
    release_date: str = ""
    blocked: str = ""

    @property
    def missing_from(self) -> list[str]:
        """Trackers that definitely do not have this release."""
        return [v.tracker for v in self.verdicts if v.status == "missing"]

    @property
    def found_on(self) -> list[str]:
        """Trackers that already have this release."""
        return [v.tracker for v in self.verdicts if v.status == "found"]

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return {
            "album_id": self.album_id,
            "title": self.title,
            "artist": self.artist,
            "verdicts": [v.as_dict() for v in self.verdicts],
            "missing_from": self.missing_from,
            "found_on": self.found_on,
            "uploadable_to": self.missing_from,
            "all_flac": self.all_flac,
            "flac_count": self.flac_count,
            "deezer_tracks": self.deezer_tracks,
            "unavailable": self.unavailable,
            "release_date": self.release_date,
            "blocked": self.blocked,
        }


class ScanResult(msgspec.Struct):
    """Outcome of checking one album against the trackers."""

    album_id: str
    title: str
    artist: str
    status: str
    found_on: list[str] = msgspec.field(default_factory=list)
    missing_from: list[str] = msgspec.field(default_factory=list)
    errors: dict[str, str] = msgspec.field(default_factory=dict)
    group_ids: dict[str, int] = msgspec.field(default_factory=dict)
    queries_used: int = 0
    calls_used: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return msgspec.to_builtins(self)


class MissingScanner:
    """Collects Deezer albums from playlists/modules and checks tracker presence."""

    def __init__(
        self,
        gw: DeezerGW,
        gateway: TrackerGateway,
        store: CheckerStore | None = None,
    ) -> None:
        """Initialize the scanner.

        Args:
            gw: Authenticated Deezer private API client.
            gateway: The rate-limited tracker gateway.
            store: Persistent result store. Created from config if omitted.
        """
        self.gw = gw
        self.gateway = gateway
        self.store = store or CheckerStore()

    # ------------------------------------------------------------------
    # Phase 1: collect (no tracker calls)
    # ------------------------------------------------------------------

    async def collect(
        self,
        sources: list[str],
        progress: ProgressFn | None = None,
        manual: bool = False,
    ) -> list[Candidate]:
        """Expand Deezer links into filtered album candidates.

        An album already answered is skipped, and how long an answer is trusted
        is the recheck window -- ``checker.album_recheck_after_days``, which the
        Scan tab offers as a filter. There used to be a tickbox beside it
        saying the same thing in fewer words, so two controls governed one
        decision and the tickbox could contradict the window sitting under it.

        Args:
            sources: Deezer playlist, channel module, artist or album URLs.
            progress: Optional callback receiving (event, payload) updates.
            manual: These albums were picked one at a time, so the scan's own
                narrowing does not apply to them. The filters exist to stop a
                sweep of a channel module spending budget on four hundred
                singles; somebody who ticked a release and pressed Check
                trackers has already made that decision. Neither the track
                count and date filters nor the "already looked up" skip runs.
                What Deezer can actually supply still does: that is a fact
                about the release, not a preference about what to sweep.

        Returns:
            Candidates that passed every Deezer-side filter, newest source first.
        """
        emit = progress or (lambda *_: None)
        album_sources: dict[str, str] = {}
        skipped: list[dict[str, Any]] = []

        for source in sources:
            source = source.strip()
            if not source:
                continue
            try:
                await self._expand_source(source, album_sources, emit)
            except DeezerGWError as e:
                emit("source_error", {"source": source, "error": str(e)})

        emit("collected", {"albums": len(album_sources)})

        candidates: list[Candidate] = []
        for index, (album_id, source) in enumerate(album_sources.items(), 1):
            emit("progress", {"phase": "filter", "current": index, "total": len(album_sources)})

            # One answer is worth keeping: on every tracker there is. Anything
            # else can still move -- a filter that changed, a rule that
            # widened, somebody else uploading it first -- so it is asked
            # again. See lox.checker.recheck.album_verdict for why each case
            # is on the side it is.
            # Blacklisted means blacklisted. It was consulted only when the
            # queue was drawn, so a release you had said "never show me this
            # again" about was still collected, still looked up, still paid a
            # tracker call for, and still turned up in the middle of a
            # collection you scanned for something else -- it was only kept off
            # the final list. The point of saying no is not to be asked again.
            if self.store.get("dismissed", album_id):
                skipped.append({"album_id": album_id, "reason": "blacklisted",
                                "title": "", "artist": ""})
                continue

            if not manual:
                stored = self.store.get("albums", album_id)
                keep, why = recheck.album_verdict(
                    stored, self._recheck_window(), trackers=TrackerGateway.configured_trackers())
                if not keep:
                    skipped.append({"album_id": album_id, "reason": why,
                                    "title": (stored or {}).get("title", ""),
                                    "artist": (stored or {}).get("artist", "")})
                    continue

                # An album a filter stopped is reconsidered every scan, because
                # the filter is a setting and settings change. Reconsidering it
                # does not need Deezer asked again: the filter is a function of
                # a track count and a release date, and both are on the record.
                # Without this, re-scanning a module of four hundred singles
                # went from free to two Deezer reads apiece for the privilege
                # of dropping them again.
                if stored and stored.get("status") == "skipped_filter":
                    still = self._filter_reason(stored.get("nb_tracks"), stored.get("release_date") or "")
                    if still:
                        skipped.append({"album_id": album_id, "reason": still,
                                        "title": stored.get("title", ""),
                                        "artist": stored.get("artist", "")})
                        continue

            candidate = await self._evaluate_candidate(album_id, source, emit, filtered=not manual)
            if candidate:
                candidates.append(candidate)

        self.store.flush("albums")
        # Said before anything is spent, so a scan that quietly did a tenth of
        # what was asked is explained rather than looking broken.
        if skipped:
            emit("skipped", {"albums": skipped[:200], "count": len(skipped),
                             "recheck_after_days": self._recheck_window()})
            debug.log("scan: %s album(s) already looked up, %s to check",
                      len(skipped), len(candidates), level=20)
        emit("collect_done", {"candidates": len(candidates), "skipped": len(skipped)})
        return candidates

    @staticmethod
    def _recheck_window() -> int:
        """How long a scan's answer about an album is trusted, in days."""
        return int(getattr(cfg.checker, "album_recheck_after_days", 30) or 0)

    async def _expand_source(self, source: str, album_sources: dict[str, str], emit: ProgressFn) -> None:
        """Resolve one source URL into album IDs, recording where each came from."""
        from lox.deezer.explore import Explorer
        from lox.deezer.gw import parse_album_id

        playlist_id = parse_playlist_id(source)
        module_id = parse_module_id(source)
        artist_id = parse_artist_id(source)
        album_id = parse_album_id(source)

        if playlist_id:
            info = await self.gw.playlist(playlist_id)
            label = info.get("title") or f"Playlist {playlist_id}"
            tracks = await self.gw.playlist_tracks(playlist_id)
            for track in tracks:
                album = track.get("album") or {}
                if album.get("id"):
                    album_sources.setdefault(str(album["id"]), label)
            emit("source_done", {"source": label, "kind": "playlist", "albums": len(tracks)})
        elif module_id:
            module = await Explorer(self.gw).module(module_id)
            label = module.get("title") or f"Module {module_id}"
            albums = [i for i in module.get("items", []) if i.get("type") == "album"]
            for item in albums:
                album_sources.setdefault(item["id"], label)
            emit("source_done", {"source": label, "kind": "module", "albums": len(albums)})
        elif artist_id:
            # A discography, flattened. The artist page groups by release type
            # because that is how a discography is read; a scan wants the list.
            artist = await Explorer(self.gw).artist(artist_id)
            label = artist.get("name") or f"Artist {artist_id}"
            albums = [a for group in artist.get("groups", []) for a in group.get("albums", [])]
            for album in albums:
                if album.get("id"):
                    album_sources.setdefault(str(album["id"]), label)
            emit("source_done", {"source": label, "kind": "artist", "albums": len(albums)})
        elif album_id:
            album_sources.setdefault(album_id, "Direct link")
            emit("source_done", {"source": source, "kind": "album", "albums": 1})
        else:
            emit("source_error",
                 {"source": source, "error": "Not a Deezer playlist, channel module, artist or album link"})

    async def _evaluate_candidate(self, album_id: str, source: str, emit: ProgressFn,
                                  filtered: bool = True) -> Candidate | None:
        """Apply the Deezer-side filters to one album."""
        try:
            info = await self.gw.album(album_id)
        except DeezerGWError as e:
            self.store.put("albums", album_id, {"status": "deezer_info_failed", "error": str(e), "source": source})
            return None

        title = info.get("title")
        artist = (info.get("artist") or {}).get("name")
        if not title or not artist:
            self.store.put("albums", album_id, {"status": "skipped_missing_info", "source": source})
            return None

        nb_tracks = info.get("nb_tracks")
        release_date = info.get("release_date") or ""

        reason = self._filter_reason(nb_tracks, release_date) if filtered else ""
        if reason:
            self.store.put(
                "albums",
                album_id,
                {
                    "status": "skipped_filter",
                    "title": title,
                    "artist": artist,
                    "reason": reason,
                    "source": source,
                    "nb_tracks": nb_tracks,
                    "release_date": release_date,
                },
            )
            return None

        try:
            availability = await self.gw.availability(album_id)
        except DeezerGWError as e:
            self.store.put("albums", album_id, {"status": "flac_check_failed", "error": str(e), "source": source})
            return None

        unavailable_reason = availability.reason()
        if unavailable_reason:
            status = "skipped_no_flac" if not availability.all_flac else "skipped_unreadable"
            if availability.unreleased:
                status = "skipped_unreleased"
            elif not availability.all_have_id:
                status = "skipped_missing_track_ids"
            elif not availability.all_have_filesize:
                status = "skipped_no_filesize"
            self.store.put(
                "albums",
                album_id,
                {
                    "status": status,
                    "title": title,
                    "artist": artist,
                    "reason": unavailable_reason,
                    "source": source,
                    "deezer_unavailable": list(availability.unreadable),
                    "release_date": availability.release_date,
                },
            )
            emit("filtered", {"album_id": album_id, "reason": unavailable_reason})
            return None

        if nb_tracks is not None and availability.total != nb_tracks:
            self.store.put(
                "albums",
                album_id,
                {
                    "status": "skipped_track_count_mismatch",
                    "title": title,
                    "artist": artist,
                    "expected": nb_tracks,
                    "actual": availability.total,
                    "source": source,
                },
            )
            return None

        return Candidate(
            album_id=str(album_id),
            title=title,
            artist=artist,
            year=release_date[:4] or "",
            tracks=availability.total,
            record_type=(info.get("record_type") or "album").title(),
            cover=info.get("cover_medium") or info.get("cover"),
            source=source,
            deezer_url=f"https://www.deezer.com/album/{album_id}",
            availability=msgspec.to_builtins(availability),
        )

    @staticmethod
    def _filter_reason(nb_tracks: int | None, release_date: str) -> str | None:
        """Return why an album fails the scan's filters, or None.

        Reads the effective values rather than the config directly: both dates
        default to something relative to today, and a blank setting means that
        default rather than "no limit".
        """
        active = effective_filters()
        min_tracks = active["min_tracks"]
        min_date, max_date = active["min_date"], active["max_date"]
        if min_tracks and nb_tracks is not None and nb_tracks < min_tracks:
            return f"track count {nb_tracks} below minimum {min_tracks}"
        if min_date and release_date and release_date < min_date:
            return f"released {release_date}, before {min_date}"
        if max_date and release_date and release_date > max_date:
            return f"released {release_date}, after {max_date}"
        return None

    # ------------------------------------------------------------------
    # Phase 2: check (costs tracker budget)
    # ------------------------------------------------------------------

    def estimate(self, candidates: list[Candidate], trackers: list[str]) -> dict[str, int]:
        """Estimate tracker calls needed to check a candidate set.

        The estimate assumes the first query matches, which is the common case;
        the real cost is bounded above by ``queries x (1 + groups)``.

        Args:
            candidates: Albums to check.
            trackers: Tracker codes to check against.

        Returns:
            Per-tracker estimated call counts.
        """
        per_album = 3
        return {code: len(candidates) * per_album for code in trackers}

    async def check(
        self,
        candidates: list[Candidate],
        trackers: list[str],
        progress: ProgressFn | None = None,
        stop_on_budget: bool = True,
    ) -> list[ScanResult]:
        """Check candidates against the trackers.

        This is the only method here that spends tracker budget. It stops early
        rather than burning through a limit, so a partial result is normal and
        the remaining albums keep their pending state for the next run.

        Args:
            candidates: Albums to check.
            trackers: Tracker codes, e.g. ``["RED", "OPS"]``.
            progress: Optional callback receiving (event, payload) updates.
            stop_on_budget: Stop the whole scan when a tracker runs out of budget.

        Returns:
            One ScanResult per album that was actually checked.
        """
        emit = progress or (lambda *_: None)
        results: list[ScanResult] = []
        usable = [code for code in trackers if code in self.gateway.configured_trackers()]
        if not usable:
            emit("source_error", {"source": "trackers", "error": "none of the requested trackers are configured"})
            return results

        for index, candidate in enumerate(candidates, 1):
            emit(
                "progress",
                {
                    "phase": "tracker",
                    "current": index,
                    "total": len(candidates),
                    "album": f"{candidate.artist} - {candidate.title}",
                },
            )
            result = ScanResult(
                album_id=candidate.album_id,
                title=candidate.title,
                artist=candidate.artist,
                status="checked",
            )

            budget_hit = False
            for code in usable:
                if not self.gateway.can_check(code):
                    result.errors[code] = "no budget or cooling down"
                    budget_hit = True
                    continue
                try:
                    found, group_id, calls, queries = await self._check_one(candidate, code)
                except TrackerBudgetExceeded as e:
                    result.errors[code] = str(e)
                    budget_hit = True
                    continue
                except TrackerUnavailable as e:
                    result.errors[code] = str(e)
                    continue
                except Exception as e:  # noqa: BLE001 - one tracker failing must not abort the scan
                    result.errors[code] = str(e)
                    continue

                result.calls_used += calls
                result.queries_used += queries
                if found:
                    result.found_on.append(code)
                    if group_id:
                        result.group_ids[code] = group_id
                else:
                    result.missing_from.append(code)

            result.status = self._status_for(result, usable)
            self.store.put(
                "albums",
                candidate.album_id,
                {
                    "status": result.status,
                    "title": candidate.title,
                    "artist": candidate.artist,
                    # Which pressing this is. Two editions of one record are
                    # two different uploads, and the queue could not say which
                    # of them it was looking at.
                    "year": candidate.year,
                    "source": candidate.source,
                    "found_on": result.found_on,
                    "missing_from": result.missing_from,
                    # Which group each tracker matched, so "RED has it" can be
                    # the link to the group RED has rather than a dead label
                    # that leaves you searching for it by hand.
                    "group_ids": {code: int(gid) for code, gid in (result.group_ids or {}).items()},
                    "errors": result.errors,
                    # A candidate only gets this far by being all FLAC -- the
                    # filter above drops the rest as skipped_no_flac -- but the
                    # queue should not have to know that to trust the row. Say
                    # it, so a row is readable on its own.
                    "all_flac": bool((candidate.availability or {}).get("all_flac")),
                    "flac_count": (candidate.availability or {}).get("flac_count"),
                    "deezer_tracks": (candidate.availability or {}).get("total"),
                },
            )
            # A request that this same release would fill lives in its own
            # collection, keyed by tracker and request id rather than by album.
            # Nothing joined the two, so checking a release from its own page,
            # finding it on every tracker, and coming back to Found still showed
            # it as worth uploading -- the answer had been written somewhere the
            # Found page does not read for that row.
            self._mirror_to_requests(candidate.album_id, result)
            results.append(result)
            emit("result", result.as_dict())

            if budget_hit and stop_on_budget:
                emit("budget_exhausted", {"checked": len(results), "remaining": len(candidates) - len(results)})
                break

        self.store.flush("albums")
        return results

    def _mirror_to_requests(self, album_id: str, result: Any) -> None:
        """Write an album's tracker verdict onto any request it would fill.

        Args:
            album_id: The Deezer album that was checked.
            result: The check result for it.
        """
        if not album_id:
            return
        for key, entry in list((self.store.load("requests") or {}).items()):
            if str(entry.get("deezer_id") or "") != str(album_id):
                continue
            self.store.put(
                "requests",
                key,
                {
                    **entry,
                    "found_on": list(result.found_on),
                    "missing_from": list(result.missing_from),
                    # getattr, because this is also called with a stand-in
                    # verdict that carries only the two tracker lists.
                    "group_ids": {code: int(gid)
                                  for code, gid in (getattr(result, "group_ids", None) or {}).items()},
                    # On every tracker that was asked, and missing from none:
                    # there is nothing left to upload.
                    "already_on_tracker": bool(result.found_on) and not result.missing_from,
                },
                flush=False,
            )
        self.store.flush("requests")

    async def _check_one(self, candidate: Candidate, code: str) -> tuple[bool, int | None, int, int]:
        """Search one tracker for one album.

        Returns:
            Tuple of (found, group id, tracker calls spent, queries tried).
        """
        info = await self.gw.album(candidate.album_id)
        verdict = await self._inspect(info, code, collect_all=False)
        if verdict.error:
            raise TrackerBudgetExceeded(verdict.error)
        found = verdict.status == "found"
        group_id = verdict.match.group_id if verdict.match else None
        return found, group_id, verdict.calls_used, len(verdict.queries)

    async def _inspect(self, info: dict, code: str, collect_all: bool = True) -> TrackerVerdict:
        """Search one tracker and record every group it looked at.

        Args:
            info: Public-API album payload.
            code: Tracker code.
            collect_all: Keep searching after a match so the UI can show the
                near misses too. The batch scanner sets this false and stops at
                the first hit to save budget.

        Returns:
            A populated TrackerVerdict.
        """
        verdict = TrackerVerdict(tracker=code, status="missing")
        queries = build_search_queries(info)
        seen_groups: set[int] = set()

        for query in queries:
            if not self.gateway.can_check(code):
                verdict.error = f"{code} budget spent after {verdict.calls_used} call(s)"
                verdict.status = "incomplete"
                return verdict

            verdict.queries.append(query)
            rows = await self.gateway.browse(code, query)
            verdict.calls_used += 1

            for row in rows:
                raw_id = row.get("groupId") or row.get("id")
                if not raw_id or int(raw_id) in seen_groups:
                    continue
                group_id = int(raw_id)
                seen_groups.add(group_id)

                if not self.gateway.can_check(code):
                    verdict.error = f"{code} budget spent after {verdict.calls_used} call(s)"
                    verdict.status = "incomplete"
                    return verdict

                group = await self.gateway.torrentgroup(code, group_id)
                verdict.calls_used += 1
                matched, reason = evaluate_group(info, group)
                hit = self._group_hit(code, group_id, group, matched, reason)
                verdict.inspected.append(hit)

                if matched:
                    verdict.status = "found"
                    verdict.match = hit
                    if not collect_all:
                        return verdict

            if verdict.status == "found":
                break

        return verdict

    def _group_hit(self, code: str, group_id: int, group: dict, matched: bool, reason: str) -> GroupHit:
        """Summarize a torrent group for display."""
        info = group.get("group") or {}
        torrents = group.get("torrents") or []
        formats = sorted(
            {
                f"{t.get('media', '?')} {t.get('format', '?')} {t.get('encoding', '?')}".strip()
                for t in torrents
            }
        )
        return GroupHit(
            group_id=group_id,
            name=plain(info.get("name")),
            artist=_group_artist(info),
            year=info.get("year"),
            url=f"{self.gateway.api(code).base_url}/torrents.php?id={group_id}",
            matched=matched,
            reason=reason,
            formats=formats[:8],
            release_type=_release_type_name(code, info.get("releaseType")),
        )

    async def check_album(self, album_id: str, trackers: list[str]) -> AlbumCheck:
        """Check a single album against trackers, keeping every group inspected.

        This backs the per-album flow in the UI: check, eyeball the links, then
        decide whether to upload. It costs more budget than the batch scanner
        because it does not stop at the first hit.

        Args:
            album_id: Deezer album ID.
            trackers: Tracker codes to check.

        Returns:
            An AlbumCheck with one verdict per tracker.

        Raises:
            DeezerGWError: If the album cannot be read from Deezer.
        """
        info = await self.gw.album(album_id)
        check = AlbumCheck(
            album_id=str(album_id),
            title=info.get("title") or "",
            artist=(info.get("artist") or {}).get("name") or "",
        )

        # Before any tracker call: what can Deezer give us? A release that is
        # not all FLAC is not an upload unless a request says lossy will do, so
        # the answer belongs on the record whatever the trackers say. It costs
        # no tracker budget, and a failure here is not fatal -- the check is
        # still worth running, the row just stays unproven.
        try:
            availability = await self.gw.availability(album_id)
        except DeezerGWError as e:
            debug.log("availability check for %s failed: %s", album_id, e, level=30)
        else:
            check.all_flac = availability.all_flac
            check.flac_count = availability.flac_count
            check.deezer_tracks = availability.total
            check.unavailable = list(availability.unreadable)
            check.release_date = availability.release_date
            # Whatever Deezer cannot supply. Recorded here so the queue can
            # keep the release out and the page can say which tracks are
            # missing, rather than offering a download that cannot complete.
            check.blocked = availability.reason() or ""

        for code in trackers:
            if code not in self.gateway.configured_trackers():
                check.verdicts.append(TrackerVerdict(tracker=code, status="unconfigured", error="not configured"))
                continue
            if not self.gateway.can_check(code):
                status = self.gateway.status(code).as_dict()
                message = (
                    f"cooling down for {status['cooldown_seconds']}s"
                    if status["cooldown_seconds"]
                    else f"no budget left ({status['remaining']}/{status['budget']})"
                )
                check.verdicts.append(TrackerVerdict(tracker=code, status="unavailable", error=message))
                continue

            try:
                # Stop at the first confirmed match. Continuing costs tracker
                # budget to learn nothing: one match already answers "is it
                # here?", and the rejected groups seen up to that point are
                # still returned for review.
                verdict = await self._inspect(info, code, collect_all=False)
            except Exception as e:  # noqa: BLE001 - one tracker failing must not hide the other
                check.verdicts.append(TrackerVerdict(tracker=code, status="error", error=str(e)))
                continue

            artist_name = (info.get("artist") or {}).get("name") or ""
            if artist_name:
                verdict.artist_url = self.gateway.artist_url(code, artist_name)
            check.verdicts.append(verdict)

        if check.found_on:
            status = "exists_" + "_".join(sorted(c.lower() for c in check.found_on))
        elif check.missing_from:
            status = "missing_" + "_".join(sorted(c.lower() for c in check.missing_from))
        else:
            # Every tracker was unconfigured, out of budget or errored. Leave it
            # retestable rather than recording a verdict nobody reached.
            status = "tracker_failed"

        self.store.put(
            "albums",
            album_id,
            {
                "status": status,
                "title": check.title,
                "artist": check.artist,
                "found_on": check.found_on,
                "missing_from": check.missing_from,
                "source": "album check",
                # Read by the queue, which will not list a release Deezer
                # cannot supply.
                "all_flac": check.all_flac,
                "flac_count": check.flac_count,
                "deezer_tracks": check.deezer_tracks,
                "deezer_unavailable": check.unavailable,
                "release_date": check.release_date,
                "blocked": check.blocked,
                # The verdicts too, not just the summary. Re-opening the album
                # should show the groups it found and what it rejected without
                # spending the budget again to learn the same thing.
                "verdicts": [v.as_dict() for v in check.verdicts],
            },
            flush=True,
        )
        return check

    def saved_album_check(self, album_id: str) -> dict[str, Any] | None:
        """The last stored check for an album, or None. Costs no tracker calls."""
        return self.store.get("albums", str(album_id))

    @staticmethod
    def _status_for(result: ScanResult, trackers: list[str]) -> str:
        """Collapse per-tracker outcomes into a single stored status."""
        if result.errors and not result.found_on and not result.missing_from:
            return "tracker_failed"
        if len(result.found_on) == len(trackers) and trackers:
            return "exists_both" if len(trackers) > 1 else f"exists_{trackers[0].lower()}"
        if result.found_on:
            return "exists_" + "_".join(sorted(c.lower() for c in result.found_on))
        if result.missing_from:
            return "missing_" + "_".join(sorted(c.lower() for c in result.missing_from))
        return "unknown"
