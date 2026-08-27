"""Saved Deezer searches.

A watchlist is a re-runnable Deezer query kept for later — "that channel module
I keep checking", "everything this artist puts out". Running one is free: it
only reads Deezer. What it produces feeds straight into the same collect/check
flow as a pasted link, so a saved search never costs tracker budget on its own.

One is made from a link and nothing else. It used to take three answers -- a
name you invented, a kind from a dropdown, and an id you had to go and find --
which is three questions about one thing you were already holding. Deezer knows
what a link points at, what it is called and how big it is, so it is asked.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

import msgspec

from lox.checker.store import CheckerStore
from lox.deezer.explore import Explorer
from lox.deezer.gw import (
    DeezerGW,
    DeezerGWError,
    parse_album_id,
    parse_artist_id,
    parse_module_id,
    parse_playlist_id,
)

WatchKind = Literal["new_releases", "chart", "search", "artist", "playlist", "module", "album"]

KIND_LABELS: dict[str, str] = {
    "new_releases": "New releases",
    "chart": "Chart",
    "search": "Search",
    "artist": "Artist",
    "playlist": "Playlist",
    "module": "Channel module",
    "album": "Album",
}

#: The kinds that are a link, and how to write it back. A scan takes these
#: straight, so the albums it finds are labelled with the playlist or the
#: artist they came from rather than all reading "Direct link".
SOURCE_URLS: dict[str, str] = {
    "playlist": "https://www.deezer.com/playlist/{target}",
    "module": "https://www.deezer.com/en/channels/module/{target}",
    "artist": "https://www.deezer.com/artist/{target}",
    "album": "https://www.deezer.com/album/{target}",
}

#: What the stored count counts, per kind. A playlist's size is its tracks; an
#: artist's is releases. Saying "142" on its own says nothing.
COUNT_NOUNS: dict[str, str] = {
    "playlist": "tracks",
    "module": "albums",
    "artist": "releases",
    "album": "album",
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
    #: How big Deezer said it was when it was saved, so the list can say what a
    #: name alone cannot: whether this is the playlist with forty tracks on it
    #: or the one with four hundred.
    count: int = 0

    def url(self) -> str:
        """The Deezer link this was made from, when it is one."""
        template = SOURCE_URLS.get(self.kind)
        return template.format(target=self.target) if template else ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        data = msgspec.to_builtins(self)
        data["kind_label"] = KIND_LABELS.get(self.kind, self.kind)
        data["url"] = self.url()
        noun = COUNT_NOUNS.get(self.kind, "albums")
        data["holds"] = f"{self.count:,} {noun}" if self.count else ""
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

    def saved(self) -> list[Watchlist]:
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

    def create(self, name: str, kind: WatchKind, target: str = "0", limit: int = 50,
               count: int = 0) -> Watchlist:
        """Save a new watchlist.

        Args:
            name: Display name.
            kind: What sort of query this is.
            target: Genre ID, search string, or entity ID depending on kind.
            limit: Maximum albums to return per run.
            count: How big Deezer says it is, for the list to show.

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
            count=max(0, int(count)),
        )
        self.store.put(self.COLLECTION, watch.id, msgspec.to_builtins(watch))
        return watch

    async def resolve(self, link: str) -> tuple[WatchKind, str, str, int]:
        """Ask Deezer what a link points at.

        Args:
            link: A Deezer playlist, channel module, artist or album URL.

        Returns:
            ``(kind, target, name, count)``.

        Raises:
            DeezerGWError: If the link is not one of those, or Deezer will not
                say what it is.
        """
        playlist_id = parse_playlist_id(link)
        if playlist_id:
            info = await self.gw.playlist(playlist_id)
            return ("playlist", playlist_id,
                    info.get("title") or f"Playlist {playlist_id}", int(info.get("nb_tracks") or 0))

        module_id = parse_module_id(link)
        if module_id:
            module = await self.explorer.module(module_id)
            albums = [i for i in module.get("items", []) if i.get("type") == "album"]
            return ("module", module_id, module.get("title") or f"Module {module_id}", len(albums))

        artist_id = parse_artist_id(link)
        if artist_id:
            info = await self.gw.public(f"/artist/{artist_id}")
            return ("artist", artist_id,
                    info.get("name") or f"Artist {artist_id}", int(info.get("nb_album") or 0))

        album_id = parse_album_id(link)
        if album_id:
            info = await self.gw.album(album_id)
            billed = (info.get("artist") or {}).get("name") or ""
            title = info.get("title") or f"Album {album_id}"
            return ("album", album_id, f"{billed} \u2014 {title}" if billed else title, 1)

        raise DeezerGWError("Not a Deezer playlist, channel module, artist or album link")

    async def save_link(self, link: str) -> tuple[Watchlist, bool]:
        """Save a Deezer link as a search, naming it from Deezer.

        Args:
            link: The URL to save.

        Returns:
            ``(watchlist, already_saved)``. Saving the same link twice is one
            saved search, not two: pasting a list and pressing Save again is a
            thing people do, and it should be harmless.

        Raises:
            DeezerGWError: If the link is not one Deezer can identify.
        """
        kind, target, name, count = await self.resolve(link)
        existing = next((w for w in self.saved() if w.kind == kind and w.target == target), None)
        if existing:
            return existing, True
        return self.create(name, kind, target, count=count), False

    def rename(self, watch_id: str, name: str) -> Watchlist | None:
        """Give a saved search a name of your own. Returns None if it is gone.

        Deezer's name for a channel module is whatever the editor called that
        week, and a playlist's is whoever made it. Being stuck with it is the
        reason to be able to change it.
        """
        watch = self.get(watch_id)
        if not watch:
            return None
        watch.name = name.strip() or watch.name
        self.store.put(self.COLLECTION, watch.id, msgspec.to_builtins(watch))
        return watch

    def delete(self, watch_id: str) -> bool:
        """Delete a watchlist. Returns True if one was removed."""
        if not self.store.get(self.COLLECTION, watch_id):
            return False
        self.store.delete(self.COLLECTION, watch_id)
        return True

    async def _execute(self, watch: Watchlist) -> list[dict[str, Any]]:
        """Dispatch a watchlist to the right Deezer surface."""
        if watch.kind == "new_releases":
            # Carries the source it answered from alongside the albums, so that
            # a fallback is visible rather than silently passed off as fresh.
            return (await self.explorer.new_releases(watch.target, watch.limit)).get("results", [])
        if watch.kind == "chart":
            chart = await self.explorer.chart(watch.target, watch.limit)
            return chart["albums"]
        if watch.kind == "search":
            rows = await self.gw.search_albums(watch.target, watch.limit)
            return [self.explorer.public_album(a) for a in rows]
        if watch.kind == "artist":
            # Grouped by release type for the artist page; a watchlist wants the
            # flat list, newest first, which is the order the groups are in.
            artist = await self.explorer.artist(watch.target)
            albums = [album for group in artist.get("groups", []) for album in group.get("albums", [])]
            return albums[: watch.limit] if watch.limit else albums
        if watch.kind == "playlist":
            return await self.explorer.playlist_albums(watch.target)
        if watch.kind == "module":
            module = await self.explorer.module(watch.target)
            return [i for i in module.get("items", []) if i.get("type") == "album"]
        raise DeezerGWError(f"Unknown watchlist kind: {watch.kind}")

    async def scan_sources(self, watch_ids: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
        """The scan sources for a set of saved searches.

        Several at once, because "check everything I follow" is one job and was
        six clicks: run a search, watch it fill the box, run the next, watch it
        overwrite what the last one put there.

        A link-backed search contributes its own link, so the scan expands it
        and labels each album with the playlist or artist it came from. A genre
        chart or a text search is not a link, so it is run here and the albums
        it returns become the sources instead.

        A failing search is reported rather than aborting the rest: one dead
        module id should not hide the other five.

        Args:
            watch_ids: Which saved searches to scan.

        Returns:
            ``(sources, problems)``, sources deduplicated in the order asked
            for, problems naming the search that could not be read and why.
        """
        sources: list[str] = []
        problems: list[dict[str, Any]] = []

        for watch_id in watch_ids:
            watch = self.get(watch_id)
            if not watch:
                problems.append({"id": watch_id, "name": "", "error": "no longer saved"})
                continue

            link = watch.url()
            if link:
                sources.append(link)
            else:
                try:
                    albums = await self._execute(watch)
                except (DeezerGWError, KeyError) as e:
                    problems.append({"id": watch.id, "name": watch.name, "error": str(e)})
                    continue
                if not albums:
                    problems.append({"id": watch.id, "name": watch.name, "error": "returned nothing"})
                    continue
                watch.last_count = len(albums)
                sources.extend(f"https://www.deezer.com/album/{a['id']}" for a in albums if a.get("id"))

            watch.last_run = time.time()
            self.store.put(self.COLLECTION, watch.id, msgspec.to_builtins(watch))

        return list(dict.fromkeys(sources)), problems
