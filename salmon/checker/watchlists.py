"""Saved Deezer searches.

A watchlist is a named, re-runnable query — "new house releases", "everything
this label puts out", "that channel module I keep checking". Running one is
free: it only reads Deezer. The albums it returns feed straight into the same
collect/check flow as a pasted URL, so a saved search never costs tracker budget
on its own.
"""

import time
import uuid
from typing import Any, Literal

import msgspec

from salmon.checker.store import CheckerStore
from salmon.deezer.explore import Explorer
from salmon.deezer.gw import DeezerGW, DeezerGWError

WatchKind = Literal["new_releases", "chart", "search", "artist", "playlist", "module"]

KIND_LABELS: dict[str, str] = {
    "new_releases": "New releases",
    "chart": "Chart",
    "search": "Search",
    "artist": "Artist discography",
    "playlist": "Playlist",
    "module": "Channel module",
}


class Watchlist(msgspec.Struct):
    """One saved search."""

    id: str
    name: str
    kind: WatchKind
    # Genre ID, search string, artist/playlist/module ID depending on kind.
    target: str = "0"
    limit: int = 50
    created: float = 0.0
    last_run: float | None = None
    last_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        data = msgspec.to_builtins(self)
        data["kind_label"] = KIND_LABELS.get(self.kind, self.kind)
        return data


class WatchlistManager:
    """Creates, stores and runs saved Deezer searches."""

    COLLECTION = "watchlists"

    def __init__(self, gw: DeezerGW, store: CheckerStore | None = None) -> None:
        """Initialize the manager.

        Args:
            gw: Deezer client used to run searches.
            store: Persistent store. Created from config if omitted.
        """
        self.gw = gw
        self.explorer = Explorer(gw)
        self.store = store or CheckerStore()

    def list(self) -> list[Watchlist]:
        """Return every saved watchlist, newest first."""
        entries = self.store.load(self.COLLECTION)
        lists: list[Watchlist] = []
        for key, raw in entries.items():
            try:
                lists.append(msgspec.convert({**raw, "id": key}, Watchlist, strict=False))
            except msgspec.ValidationError:
                continue
        lists.sort(key=lambda w: w.created, reverse=True)
        return lists

    def get(self, watch_id: str) -> Watchlist | None:
        """Return one watchlist by ID, or None."""
        raw = self.store.get(self.COLLECTION, watch_id)
        if not raw:
            return None
        try:
            return msgspec.convert({**raw, "id": watch_id}, Watchlist, strict=False)
        except msgspec.ValidationError:
            return None

    def create(self, name: str, kind: WatchKind, target: str = "0", limit: int = 50) -> Watchlist:
        """Save a new watchlist.

        Args:
            name: Display name.
            kind: What sort of query this is.
            target: Genre ID, search string, or entity ID depending on kind.
            limit: Maximum albums to return per run.

        Returns:
            The created watchlist.
        """
        watch = Watchlist(
            id=uuid.uuid4().hex[:10],
            name=name.strip() or KIND_LABELS.get(kind, kind),
            kind=kind,
            target=str(target),
            limit=max(1, min(int(limit), 200)),
            created=time.time(),
        )
        self.store.put(self.COLLECTION, watch.id, msgspec.to_builtins(watch))
        return watch

    def delete(self, watch_id: str) -> bool:
        """Delete a watchlist. Returns True if one was removed."""
        if not self.store.get(self.COLLECTION, watch_id):
            return False
        self.store.delete(self.COLLECTION, watch_id)
        return True

    async def run(self, watch_id: str) -> list[dict[str, Any]]:
        """Run a saved search and return the albums it produced.

        Args:
            watch_id: The watchlist to run.

        Returns:
            Album cards, in the same shape Explore and Search use.

        Raises:
            KeyError: If the watchlist does not exist.
            DeezerGWError: If Deezer cannot answer the query.
        """
        watch = self.get(watch_id)
        if not watch:
            raise KeyError(watch_id)

        albums = await self._execute(watch)
        watch.last_run = time.time()
        watch.last_count = len(albums)
        self.store.put(self.COLLECTION, watch.id, msgspec.to_builtins(watch))
        return albums

    async def _execute(self, watch: Watchlist) -> list[dict[str, Any]]:
        """Dispatch a watchlist to the right Deezer surface."""
        if watch.kind == "new_releases":
            return await self.explorer.new_releases(watch.target, watch.limit)
        if watch.kind == "chart":
            chart = await self.explorer.chart(watch.target, watch.limit)
            return chart["albums"]
        if watch.kind == "search":
            rows = await self.gw.search_albums(watch.target, watch.limit)
            return [self.explorer.public_album(a) for a in rows]
        if watch.kind == "artist":
            return await self.explorer.artist_albums(watch.target, watch.limit)
        if watch.kind == "playlist":
            return await self.explorer.playlist_albums(watch.target)
        if watch.kind == "module":
            module = await self.explorer.module(watch.target)
            return [i for i in module.get("items", []) if i.get("type") == "album"]
        raise DeezerGWError(f"Unknown watchlist kind: {watch.kind}")

    async def run_all(self) -> dict[str, list[dict[str, Any]]]:
        """Run every saved watchlist.

        A failing watchlist yields an empty list rather than aborting the rest,
        so one dead module ID does not hide the others.

        Returns:
            Mapping of watchlist ID to its albums.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for watch in self.list():
            try:
                out[watch.id] = await self.run(watch.id)
            except (DeezerGWError, KeyError):
                out[watch.id] = []
        return out
