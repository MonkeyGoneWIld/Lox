"""Find Deezer releases that are not on the trackers yet.

Deliberately split into two phases. :meth:`MissingScanner.collect` reads
playlists and channel modules and applies every filter that can be answered from
Deezer alone — track count, release date, FLAC availability, streamability. It
costs no tracker budget at all.

Only :meth:`MissingScanner.check` talks to RED/OPS, and only for the albums it is
handed. That is what the "Check trackers" button in the UI drives.
"""

from collections.abc import Callable
from typing import Any

import msgspec

from lox import cfg
from lox.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerUnavailable, plain
from lox.checker.matching import build_search_queries, evaluate_group
from lox.checker.request_filters import for_tracker
from lox.checker.store import CheckerStore
from lox.deezer.gw import DeezerGW, DeezerGWError, parse_module_id, parse_playlist_id

ProgressFn = Callable[[str, dict[str, Any]], None]


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
        skip_known: bool = True,
    ) -> list[Candidate]:
        """Expand playlist and module URLs into filtered album candidates.

        Args:
            sources: Deezer playlist URLs, channel module URLs, or bare album URLs.
            progress: Optional callback receiving (event, payload) updates.
            skip_known: Skip albums whose stored status is final.

        Returns:
            Candidates that passed every Deezer-side filter, newest source first.
        """
        emit = progress or (lambda *_: None)
        album_sources: dict[str, str] = {}

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

            if skip_known and self.store.should_skip("albums", album_id):
                continue

            candidate = await self._evaluate_candidate(album_id, source, emit)
            if candidate:
                candidates.append(candidate)

        self.store.flush("albums")
        emit("collect_done", {"candidates": len(candidates)})
        return candidates

    async def _expand_source(self, source: str, album_sources: dict[str, str], emit: ProgressFn) -> None:
        """Resolve one source URL into album IDs, recording where each came from."""
        from lox.deezer.explore import Explorer
        from lox.deezer.gw import parse_album_id

        playlist_id = parse_playlist_id(source)
        module_id = parse_module_id(source)
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
        elif album_id:
            album_sources.setdefault(album_id, "Direct link")
            emit("source_done", {"source": source, "kind": "album", "albums": 1})
        else:
            emit("source_error", {"source": source, "error": "Not a Deezer playlist, module or album URL"})

    async def _evaluate_candidate(self, album_id: str, source: str, emit: ProgressFn) -> Candidate | None:
        """Apply the Deezer-side filters to one album."""
        checker = cfg.checker
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

        reason = self._filter_reason(nb_tracks, release_date, checker)
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
            if not availability.all_have_id:
                status = "skipped_missing_track_ids"
            elif not availability.all_have_filesize:
                status = "skipped_no_filesize"
            self.store.put(
                "albums",
                album_id,
                {"status": status, "title": title, "artist": artist, "reason": unavailable_reason, "source": source},
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
    def _filter_reason(nb_tracks: int | None, release_date: str, checker) -> str | None:
        """Return why an album fails the configured filters, or None."""
        if checker.min_tracks and nb_tracks is not None and nb_tracks < checker.min_tracks:
            return f"track count {nb_tracks} below minimum {checker.min_tracks}"
        if checker.min_date and release_date and release_date < checker.min_date:
            return f"released {release_date}, before {checker.min_date}"
        if checker.max_date and release_date and release_date > checker.max_date:
            return f"released {release_date}, after {checker.max_date}"
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
                    "source": candidate.source,
                    "found_on": result.found_on,
                    "missing_from": result.missing_from,
                    "errors": result.errors,
                },
            )
            results.append(result)
            emit("result", result.as_dict())

            if budget_hit and stop_on_budget:
                emit("budget_exhausted", {"checked": len(results), "remaining": len(candidates) - len(results)})
                break

        self.store.flush("albums")
        return results

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
