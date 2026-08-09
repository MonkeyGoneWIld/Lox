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

from salmon import cfg
from salmon.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerUnavailable
from salmon.checker.matching import build_search_queries, evaluate_group
from salmon.checker.store import CheckerStore
from salmon.deezer.gw import DeezerGW, DeezerGWError, parse_module_id, parse_playlist_id

ProgressFn = Callable[[str, dict[str, Any]], None]


class Candidate(msgspec.Struct):
    """An album that passed every Deezer-side filter."""

    album_id: str
    title: str
    artist: str
    year: str
    tracks: int
    record_type: str
    cover: str | None
    source: str
    deezer_url: str
    availability: dict[str, Any] = msgspec.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return msgspec.to_builtins(self)


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

        emit("collect_done", {"candidates": len(candidates)})
        return candidates

    async def _expand_source(self, source: str, album_sources: dict[str, str], emit: ProgressFn) -> None:
        """Resolve one source URL into album IDs, recording where each came from."""
        from salmon.deezer.explore import Explorer
        from salmon.deezer.gw import parse_album_id

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

        return results

    async def _check_one(self, candidate: Candidate, code: str) -> tuple[bool, int | None, int, int]:
        """Search one tracker for one album.

        Returns:
            Tuple of (found, group id, tracker calls spent, queries tried).
        """
        info = await self.gw.album(candidate.album_id)
        queries = build_search_queries(info)
        calls = 0
        tried = 0
        seen_groups: set[int] = set()

        for query in queries:
            if not self.gateway.can_check(code):
                raise TrackerBudgetExceeded(f"{code} budget spent mid-album")
            tried += 1
            rows = await self.gateway.browse(code, query)
            calls += 1

            for row in rows:
                group_id = row.get("groupId") or row.get("id")
                if not group_id or group_id in seen_groups:
                    continue
                seen_groups.add(group_id)
                if not self.gateway.can_check(code):
                    raise TrackerBudgetExceeded(f"{code} budget spent mid-album")
                group = await self.gateway.torrentgroup(code, int(group_id))
                calls += 1
                matched, _reason = evaluate_group(info, group)
                if matched:
                    return True, int(group_id), calls, tried

        return False, None, calls, tried

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
