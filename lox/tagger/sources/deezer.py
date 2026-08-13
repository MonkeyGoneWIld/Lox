import re
from collections import defaultdict
from html import unescape

from lox.common import RE_FEAT, parse_copyright, re_split
from lox.sources import DeezerBase
from lox.tagger.sources.base import MetadataMixin

RECORD_TYPES = {
    "album": "Album",
    "ep": "EP",
    "single": "Single",
}

# Deezer's contributor keys, mapped onto the roles the trackers accept. The
# keys vary by release -- the same credit appears as "mainartist" on one and
# "main_artist" on another -- so both spellings of each are listed.
CONTRIBUTOR_ROLES = {
    "mainartist": "main",
    "main_artist": "main",
    "featuredartist": "guest",
    "featuring": "guest",
    "featured": "guest",
    "remixer": "remixer",
    "remixing": "remixer",
    "producer": "producer",
    "producers": "producer",
    "composer": "composer",
    "composers": "composer",
    "writer": "composer",
    "author": "composer",
    "lyricist": "composer",
    "conductor": "conductor",
    "dj": "djcompiler",
    "djcompiler": "djcompiler",
    "compiler": "djcompiler",
}

# What a credit becomes when the album itself is not credited to that artist.
# Someone who plays on one track of eighteen is a guest on the release, not one
# of the artists it is filed under.
_DEMOTED = "guest"


class Scraper(DeezerBase, MetadataMixin):
    def parse_release_title(self, soup):
        return RE_FEAT.sub("", soup["title"])

    def parse_cover_url(self, soup):
        return soup["cover_xl"]

    def parse_release_year(self, soup):
        try:
            match = re.search(r"(\d{4})", soup["release_date"])
            return int(match[1]) if match else None
        except TypeError:
            return None
            # raise ScrapeError('Could not parse release year.') from e

    def parse_release_group_year(self, soup):
        """The year the album first came out, not the year of this edition.

        The public API only has this edition's release date, so a remaster
        arrived with its original year set to the remaster's -- which is the
        one field the trackers use to file it under the right group. The
        private page carries the original date, and it is only reachable with
        an ARL.
        """
        page = soup.get("_album_page") or {}
        for key in ("ORIGINAL_RELEASE_DATE", "PHYSICAL_RELEASE_DATE", "DIGITAL_RELEASE_DATE"):
            match = re.search(r"(\d{4})", str(page.get(key) or ""))
            if match and match[1] != "0000":
                return int(match[1])
        return self.parse_release_year(soup)

    def parse_release_date(self, soup):
        return soup["release_date"]

    def parse_release_label(self, soup):
        """The label, preferring what the private page records over the public one.

        The public API's ``label`` is often the copyright line with the year
        and the (P) marker still in it; the private page has the label on its
        own. Both go through the same cleanup, so whichever is used comes out
        the same shape.
        """
        page = soup.get("_album_page") or {}
        return parse_copyright(page.get("LABEL_NAME") or soup.get("label") or "")

    def parse_comment(self, soup):
        """The producer line, which is the credit Deezer records for a release.

        Nothing else in the scrape carries it, and it is the one piece of
        provenance a WEB upload can state without guessing.
        """
        page = soup.get("_album_page") or {}
        line = str(page.get("PRODUCER_LINE") or "").strip()
        return line or None

    def parse_upc(self, soup):
        """The barcode, from whichever payload has one."""
        page = soup.get("_album_page") or {}
        return soup.get("upc") or page.get("UPC") or None

    def parse_genres(self, soup):
        return {g["name"] for g in soup["genres"]["data"]}

    def parse_release_type(self, soup):
        try:
            return RECORD_TYPES[soup["record_type"]]
        except KeyError:
            return None

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        for track in soup["tracklist"]:
            tracks[str(track["DISK_NUMBER"])][str(track["TRACK_NUMBER"])] = self.generate_track(
                trackno=track["TRACK_NUMBER"],
                discno=track["DISK_NUMBER"],
                artists=self.parse_artists(track["SNG_CONTRIBUTORS"], track["ARTISTS"], track["SNG_TITLE"]),
                title=self.parse_title(track["SNG_TITLE"], track.get("VERSION", None)),
                isrc=track["ISRC"],
                explicit=track["EXPLICIT_LYRICS"],
                stream_id=track["SNG_ID"],
                md5_origin=track.get("MD5_ORIGIN"),
                media_version=track.get("MEDIA_VERSION"),
                lossless=True,
                mp3_320=True,
            )
        return dict(tracks)

    def process_label(self, data):
        if isinstance(data["label"], str) and any(
            data["label"].lower().startswith(a.lower()) and i == "main" for a, i in data["artists"]
        ):
            return "Self-Released"
        return data["label"]

    def parse_artists(self, artists, default_artists, title):
        """
        Iterate over all artists and roles, returning a compliant list of
        artist tuples.
        """
        result = []

        feat = RE_FEAT.search(title)
        if feat:
            for artist in re_split(feat[1]):
                result.append((unescape(artist), "guest"))

        if artists:
            # Every credit Deezer records, not just the two the trackers were
            # historically given: a remixer filed as "main" is why an album
            # came out with six main artists on it.
            named = {name.lower() for name, _role in result}
            for key, role in CONTRIBUTOR_ROLES.items():
                for entry in artists.get(key) or []:
                    for name in re_split(entry):
                        if name.lower() in named:
                            continue
                        named.add(name.lower())
                        result.append((name, role))
        else:
            for artist in default_artists:
                for b in re_split(artist["ART_NAME"]):
                    if (b, "main") not in result:
                        result.append((b, "main"))

        return result

    def refine_artists(self, soup, artists, tracks):
        """Keep "main" for the artists the album itself is credited to.

        Deezer credits every performer on a track as that track's main artist,
        and the album-level list is the union of the tracks -- so a singer who
        appears once on an eighteen-track album arrived as a main artist of the
        whole release, which is not what the release is.

        The album's own credits are the answer to who the release is by. Anyone
        else keeps whatever role their tracks gave them, and a bare "main"
        becomes a guest.
        """
        album = self._album_artists(soup)
        if not album:
            return artists, tracks

        def fix(pairs):
            out = []
            for name, role in pairs:
                if name.lower() in album:
                    out.append((name, album[name.lower()]))
                else:
                    out.append((name, _DEMOTED if role == "main" else role))
            return out

        for disc in tracks.values():
            for track in disc.values():
                track["artists"] = fix(track["artists"])
        return fix(artists), tracks

    @staticmethod
    def _album_artists(soup) -> dict[str, str]:
        """Who the album is credited to, as a lowercased name to role map.

        Read from the public payload's ``contributors`` first, since that is
        where Deezer states the role, and from the private page's ``ARTISTS``
        as a fallback for releases the public API answers thinly.
        """
        album: dict[str, str] = {}
        for entry in soup.get("contributors") or []:
            name = (entry or {}).get("name")
            if not name:
                continue
            role = CONTRIBUTOR_ROLES.get(str(entry.get("role", "")).lower().replace(" ", ""), "main")
            for part in re_split(name):
                album.setdefault(part.lower(), role)

        page = soup.get("_album_page") or {}
        for entry in page.get("ARTISTS") or []:
            name = (entry or {}).get("ART_NAME")
            if not name:
                continue
            for part in re_split(name):
                album.setdefault(part.lower(), "main")

        main_artist = ((soup.get("artist") or {}).get("name")) or page.get("ART_NAME")
        if main_artist:
            for part in re_split(main_artist):
                album.setdefault(part.lower(), "main")
        return album
