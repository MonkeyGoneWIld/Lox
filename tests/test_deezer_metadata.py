"""Credits and the fields only an ARL can reach.

Deezer credits every performer on a track as that track's main artist, and the
album-level list is the union of the tracks -- so a singer who appears once on
an eighteen-track album arrived as a main artist of the whole release. The
album's own credits are the answer to who a release is by, and the private
album page carries several fields the public API does not.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_deezermeta")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5102",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.tagger.sources.deezer import Scraper  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


# An album by one artist, with a guest on one track and a remixer on another --
# the shape that produced three "main" artists on an eighteen-track release.
SOUP = {
    "title": "Sammaouny",
    "artist": {"name": "Mohamed Hamaki"},
    "contributors": [{"name": "Mohamed Hamaki", "role": "Main"}],
    "label": "(P) 2026 The Basement Records",
    "upc": "729771312036",
    "release_date": "2026-07-31",
    "record_type": "album",
    "genres": {"data": [{"name": "Pop"}]},
    "_album_page": {
        "ART_NAME": "Mohamed Hamaki",
        "ARTISTS": [{"ART_NAME": "Mohamed Hamaki"}],
        "ORIGINAL_RELEASE_DATE": "1994-03-01",
        "PHYSICAL_RELEASE_DATE": "2026-07-31",
        "LABEL_NAME": "Rotana",
        "PRODUCER_LINE": "(P) 2026 Rotana Audio Visual",
        "UPC": "729771312036",
    },
}


async def main() -> int:
    scraper = Scraper()

    # --- one track's credits, with every role Deezer records ---------
    parsed = scraper.parse_artists(
        {
            "mainartist": ["Mohamed Hamaki"],
            "featuring": ["Sherine"],
            "remixer": ["DJ Someone"],
            "composer": ["A Composer"],
        },
        [],
        "Sammaouny",
    )
    roles = dict(parsed)
    check("the main artist is main", roles.get("Mohamed Hamaki") == "main", str(parsed))
    check("a featured artist is a guest", roles.get("Sherine") == "guest", str(parsed))
    check("a remixer is a remixer, not a main artist",
          roles.get("DJ Someone") == "remixer", str(parsed))
    # Composers, writers and producers are deliberately not read. Deezer lists
    # them per track, and reading them credited an eighteen-track album to
    # fourteen people, thirteen of whom wrote a song rather than performed on
    # it. The trackers file releases by performer.
    check("a composer is not credited on the release", "A Composer" not in roles, str(parsed))

    # --- and the album demotes anyone it is not credited to ----------
    tracks = {
        "1": {
            "1": {"title": "Beyoulolek Eih", "artists": [("Mohamed Hamaki", "main")]},
            "2": {"title": "Bahareya", "artists": [("Mohamed Hamaki", "main"), ("Sherine", "main")]},
            "3": {"title": "Ayam (Remix)", "artists": [("Mohamed Hamaki", "main"), ("DJ Someone", "remixer")]},
        }
    }
    album = [("Mohamed Hamaki", "main"), ("Sherine", "main"), ("DJ Someone", "remixer")]
    fixed, fixed_tracks = scraper.refine_artists(SOUP, album, tracks)
    roles = dict(fixed)

    check("the album's own artist stays main", roles.get("Mohamed Hamaki") == "main", str(fixed))
    check("someone on one track of three is a guest, not a main artist",
          roles.get("Sherine") == "guest", str(fixed))
    check("a specific role is not overwritten by the demotion",
          roles.get("DJ Someone") == "remixer", str(fixed))
    check("nobody is dropped", len(fixed) == 3, str(fixed))
    check("the tracks are corrected too",
          fixed_tracks["1"]["2"]["artists"] == [("Mohamed Hamaki", "main"), ("Sherine", "guest")],
          str(fixed_tracks["1"]["2"]["artists"]))
    check("and a track the demoted artist is not on is untouched",
          fixed_tracks["1"]["1"]["artists"] == [("Mohamed Hamaki", "main")], "")

    # A release with no album credits at all is left exactly as it was.
    untouched, _ = scraper.refine_artists({}, album, tracks)
    check("with nothing to go on, nothing is changed", untouched == album, str(untouched))

    # --- demotion is not allowed to empty a track --------------------
    # "Ronan - Instrumental Remixes Vol. 4": the album is credited to Ronan
    # and each track is credited to the singer it features, so every name on
    # every track was a stranger to the album credits and each track came out
    # of here with no main artist at all. The trackers require one, so the
    # upload stopped on the metadata form with an error the form had no field
    # to fix, and the release could not be posted to anywhere.
    remixes = {
        "1": {
            "1": {"title": "Nikki", "artists": [("Nikki Ocean", "main"), ("Celso Mendes", "main"),
                                                ("Ronan Instrumental", "remixer")]},
            "2": {"title": "Ronan's Own", "artists": [("Ronan", "main"), ("Nikki Ocean", "main")]},
        }
    }
    ronan_soup = {"artist": {"name": "Ronan"}, "contributors": [{"name": "Ronan", "role": "Main"}]}
    ronan_album = [("Ronan", "main"), ("Nikki Ocean", "main"), ("Celso Mendes", "main")]
    _fixed_album, ronan_tracks = scraper.refine_artists(ronan_soup, ronan_album, remixes)
    first = ronan_tracks["1"]["1"]["artists"]
    check("a track of strangers still has a main artist",
          any(role == "main" for _n, role in first), str(first))
    check("and it is the artist whose album it is",
          first[0] == ("Ronan", "main"), str(first))
    check("with the performers kept, as guests",
          [n for n, _r in first] == ["Ronan", "Nikki Ocean", "Celso Mendes", "Ronan Instrumental"], str(first))
    check("a specific role survives the promotion",
          dict(first).get("Ronan Instrumental") == "remixer", str(first))
    second = ronan_tracks["1"]["2"]["artists"]
    check("and a track that already had one is not rewritten",
          second == [("Ronan", "main"), ("Nikki Ocean", "guest")], str(second))

    # An album with no main artist of its own has nothing to promote, so the
    # track keeps the credits it arrived with rather than losing them to a
    # rule that has no better answer.
    only_remixer = {"contributors": [{"name": "DJ Someone", "role": "Remixer"}]}
    _a, kept = scraper.refine_artists(
        only_remixer, [("Nikki Ocean", "main")], {"1": {"1": {"artists": [("Nikki Ocean", "main")]}}}
    )
    check("a release with no main artist of its own keeps the track's",
          kept["1"]["1"]["artists"] == [("Nikki Ocean", "main")], str(kept["1"]["1"]["artists"]))

    # --- the fields only the private page has ------------------------
    check("the original year comes from the private page, not this pressing",
          scraper.parse_release_group_year(SOUP) == 1994, str(scraper.parse_release_group_year(SOUP)))
    check("while the edition year is still this pressing's",
          scraper.parse_release_year(SOUP) == 2026, str(scraper.parse_release_year(SOUP)))
    check("the label is the private page's, not the copyright line",
          scraper.parse_release_label(SOUP) == "Rotana", str(scraper.parse_release_label(SOUP)))
    # The producer line is a copyright notice, not a comment. Using it as one
    # put "2026 The Basement Records" in the middle of the group description.
    check("the producer line is not used as a comment",
          not hasattr(scraper, "parse_comment") or scraper.parse_comment(SOUP) is None,
          str(getattr(scraper, "parse_comment", lambda _s: None)(SOUP)))
    check("the barcode is picked up", scraper.parse_upc(SOUP) == "729771312036", str(scraper.parse_upc(SOUP)))

    # Without an ARL there is no private page, and nothing breaks.
    public_only = {k: v for k, v in SOUP.items() if k != "_album_page"}
    check("with no private page the edition year is used for both",
          scraper.parse_release_group_year(public_only) == 2026, "")
    check("and the public label still parses",
          scraper.parse_release_label(public_only) == "The Basement Records",
          str(scraper.parse_release_label(public_only)))
    check("and there is still no comment", scraper.parse_comment(public_only) is None, "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
