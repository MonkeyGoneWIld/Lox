"""Whether a tracker already has a release, answered correctly.

Two failures, opposite in direction, both reported off real uploads.

**A release the tracker had was reported missing.** ``strip_prefix`` exists for
"Beethoven - Symphony No. 5", where the first half is the composer and the
second is the work. It was applied to every title with that shape, so
"BLINGY - The 7th Album" was reduced to "The 7th Album" -- and every search
went out for the subtitle while the group on RED is called "BLINGY". Nothing
matched, the release was queued as missing from a tracker that had it, and a
duplicate upload was one press away.

**A release the tracker did not have was reported present.** ``similarity`` is
a Jaccard measure over character SETS, so "Instrumental Remixes Vol. 4" and
"Instrumental Remixes, Vol. 2" differ by one character out of twenty-three and
score 0.87 -- comfortably over the 0.85 threshold. A volume number is exactly
what distinguishes one release in a series from another, and the measure cannot
see it at all.

Both halves of a split title are searched and compared now, and two titles that
carry different numbers are not the same release.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_matching")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5125",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.checker.matching import (  # noqa: E402
    build_search_queries,
    evaluate_group,
    numbers_in,
    title_head,
    title_matches,
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def group(name: str, artist: str, *, web_flac: bool = True) -> dict:
    """A torrentgroup response with one WEB FLAC in it."""
    torrents = [{"media": "WEB", "format": "FLAC", "encoding": "Lossless"}] if web_flac else []
    return {"group": {"name": name, "artist": artist}, "torrents": torrents}


def split_title_checks() -> None:
    check("a composer prefix still has a head", title_head("Beethoven: Symphony No. 5") == "Beethoven",
          title_head("Beethoven: Symphony No. 5"))
    check("and so does an album with a subtitle",
          title_head("BLINGY - The 7th Album") == "BLINGY", title_head("BLINGY - The 7th Album"))
    check("a title with no split has none", title_head("Kid A") == "", repr(title_head("Kid A")))
    check("and a hyphenated word is not a split",
          title_head("Jay-Z Unplugged") == "", repr(title_head("Jay-Z Unplugged")))

    # The reported case: the group is called BLINGY, the release is called
    # "BLINGY - The 7th Album".
    matched, score = title_matches("BLINGY - The 7th Album", "BLINGY")
    check("a release matches the group named after its first half", matched, f"{score:.2f}")
    check("and the other way round",
          title_matches("BLINGY", "BLINGY - The 7th Album")[0], "")
    # And the classical case it was written for still works.
    check("a work still matches its own title",
          title_matches("Beethoven: Symphony No. 5", "Symphony No. 5")[0], "")

    info = {"title": "BLINGY - The 7th Album", "artist": {"name": "NCT 127"}, "record_type": "album"}
    queries = build_search_queries(info)
    check("the search asks for the first half as well",
          "NCT 127 BLINGY" in queries and "BLINGY" in queries, str(queries))
    check("and still asks for the second", "The 7th Album" in queries, str(queries))
    check("with the artist-and-title guess first, which is the usual hit",
          queries[0] == "NCT 127 the 7th album", queries[0])

    ok, reason = evaluate_group(info, group("BLINGY", "NCT 127"))
    check("so the group the tracker actually has is a match", ok, reason)


def number_checks() -> None:
    check("a volume number is part of what a release is",
          numbers_in("Instrumental Remixes Vol. 4") == ["4"], str(numbers_in("Instrumental Remixes Vol. 4")))
    check("a year is not", numbers_in("Kid A (2009 Remaster)") == [], str(numbers_in("Kid A (2009 Remaster)")))
    check("and a title with no numbers has none", numbers_in("Kid A") == [], "")

    matched, score = title_matches("Instrumental Remixes Vol. 4", "Instrumental Remixes, Vol. 2")
    check("volume 4 is not volume 2", not matched, f"scored {score:.2f}")
    check("volume 4 is volume 4",
          title_matches("Instrumental Remixes Vol. 4", "Instrumental Remixes, Vol. 4")[0], "")

    # The reported case, end to end: a Ronan release matched a Giorgio Moroder
    # one because the two titles differ by a single digit.
    ronan = {
        "title": "Instrumental Remixes Vol. 4",
        "artist": {"name": "Ronan"},
        "record_type": "album",
        "contributors": [{"name": "Ronan Instrumental"}, {"name": "Nikki Ocean"}],
    }
    ok, reason = evaluate_group(ronan, group("Instrumental Remixes, Vol. 2", "Giorgio Moroder"))
    check("a different volume by a different artist is not a match", not ok, reason)
    check("and it is refused on the title, before anyone looks at the artist",
          "title mismatch" in reason, reason)

    # Neighbouring numbers in a series, which is the case this is really for.
    for left, right in (("Greatest Hits Vol 1", "Greatest Hits Vol 2"),
                        ("Now That's What I Call Music 12", "Now That's What I Call Music 13"),
                        ("Disc 1", "Disc 2")):
        check(f"{left!r} is not {right!r}", not title_matches(left, right)[0], "")

    # And a number on one side only says nothing either way, so it must not
    # start refusing editions.
    check("an edition marker on one side alone is not a mismatch",
          title_matches("Kid A", "Kid A (2009 Remaster)")[0], "")
    check("nor is a number on one side and none on the other",
          title_matches("Blonde", "Blonde")[0], "")


def no_regression_checks() -> None:
    """The matches that already worked go on working."""
    cases = [
        ("Scarlet", "Scarlet", "Doja Cat", "Doja Cat"),
        ("LAX", "LAX", "The Game", "The Game"),
        ("Opposites (Deluxe)", "Opposites", "Biffy Clyro", "Biffy Clyro"),
        ("The Life of a Showgirl", "The Life of a Showgirl", "Taylor Swift", "Taylor Swift"),
    ]
    for deezer_title, tracker_title, deezer_artist, tracker_artist in cases:
        info = {"title": deezer_title, "artist": {"name": deezer_artist}, "record_type": "album"}
        ok, reason = evaluate_group(info, group(tracker_title, tracker_artist))
        check(f"{deezer_artist} — {deezer_title} still matches", ok, reason)

    # A group with no WEB FLAC is still not somewhere to stop looking.
    info = {"title": "Scarlet", "artist": {"name": "Doja Cat"}, "record_type": "album"}
    ok, reason = evaluate_group(info, group("Scarlet", "Doja Cat", web_flac=False))
    check("and a group with no WEB FLAC is still not a match", not ok, reason)


def main() -> int:
    split_title_checks()
    number_checks()
    no_regression_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
