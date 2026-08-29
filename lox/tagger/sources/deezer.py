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
    "conductor": "conductor",
    "dj": "djcompiler",
    "djcompiler": "djcompiler",
    "compiler": "djcompiler",
}
"""What Deezer calls a credit, and what the trackers call it.

Deliberately not here: composers, writers, lyricists and producers. Deezer
lists every one of them per track, and reading them turned an eighteen-track
album into a release credited to fourteen people -- thirteen of whom wrote a
song on it rather than performed on it. The trackers file releases by performer,
and a songwriting credit is not a reason to be on that list."""

# What a credit becomes when the album itself is not credited to that artist.
# Someone who plays on one track of eighteen is a guest on the release, not one
# of the artists it is filed under.
_DEMOTED = "guest"

#: What Deezer credits a compilation to. Not an artist -- a word meaning "many
#: of them", which is why both trackers file these under it rather than under
#: anybody. Nothing here may be demoted against, promoted onto a track, or
#: written into a credit list.
_VARIOUS = frozenset({"various", "various artists", "va", "verschiedene interpreten", "multi-interprètes"})


def _is_various(name: str) -> bool:
    """Whether a credit is the compilation placeholder rather than a person."""
    return str(name or "").strip().lower() in _VARIOUS


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

    # No parse_comment: the producer line was being used for it, and it is a
    # copyright notice -- "2026 The Basement Records" -- not a comment about
    # the release. It ended up in the middle of the group description, which is
    # not something anyone would have typed there.

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

        Demotion is never allowed to empty a track, though. On a release like
        "Ronan - Instrumental Remixes Vol. 4" the album is credited to Ronan
        and each track is credited to the singer it features, so every name on
        every track was a stranger to the album credits and every track came
        out of here with no main artist at all. The trackers require one, so
        the upload stopped at the metadata form with an error the form had no
        field to fix, and the release could not be posted at all. A track with
        nobody left is a track by the artist whose album it is.

        None of which applies to a compilation. Deezer credits one to "Various
        Artists", which is not somebody the release is by -- it is a word
        meaning "many of them". Demoting against it made every real performer
        on every track a stranger, and then filling the hole that left wrote
        "Various Artists" in as the main artist of all twenty-one tracks. A
        compilation has no album artist, so there is nothing to demote against
        and each track keeps the people who are actually on it.
        """
        def strip(pairs):
            """Without the compilation placeholder, unless that is all there is."""
            kept = [(n, r) for n, r in pairs if not _is_various(n)]
            return kept or list(pairs)

        album = {
            name: role for name, role in self._album_artists(soup).items() if not _is_various(name)
        }
        if not album:
            # A compilation. Nobody to demote against, so every track keeps the
            # people actually on it -- but the placeholder is still not a name
            # and must not reach a tracker as one.
            for disc in tracks.values():
                for track in disc.values():
                    track["artists"] = strip(track["artists"])
            return strip(artists), tracks
        album_mains = [name for name, role in album.items() if role == "main"]

        def fix(pairs):
            out = []
            for name, role in pairs:
                # The placeholder is never a credit, wherever it turns up: on a
                # release with a real album artist it is noise, and it must not
                # reach a tracker as somebody's name.
                if _is_various(name):
                    continue
                if name.lower() in album:
                    out.append((name, album[name.lower()]))
                else:
                    out.append((name, _DEMOTED if role == "main" else role))
            return out

        def fix_track(pairs):
            out = fix(pairs)
            if any(role == "main" for _name, role in out):
                return out
            # Prefer the release's own main artists, restoring their real
            # capitalisation from the credits where the track carries it.
            spelling = {name.lower(): name for name, _role in pairs}
            restored = [(spelling.get(name, name.title()), "main") for name in album_mains]
            if restored:
                return restored + [(n, r) for n, r in out if n.lower() not in album_mains]
            # An album with no main artist of its own -- nothing to promote, so
            # the track keeps the credits it arrived with rather than losing
            # them to a rule that has no better answer.
            return strip(pairs)

        for disc in tracks.values():
            for track in disc.values():
                track["artists"] = fix_track(track["artists"])
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
