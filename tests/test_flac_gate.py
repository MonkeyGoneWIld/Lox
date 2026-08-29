"""Nothing reaches the queue that Deezer cannot actually supply.

"No tracker has this" is half of "worth uploading". The other half is whether
there is a release to give them, and only the request path ever asked: an album
checked from Search or Browse went into the queue on the tracker answer alone,
so releases Deezer serves as MP3 sat there looking like work. That is what this
covers.

The rule, in one sentence: a release that is not all FLAC on Deezer does not
belong in the queue unless an open request says in as many words that lossy
will do -- MP3, V0, 320, Any. Silence is not consent; a request that names no
format and no encoding is read as wanting lossless, because a release held back
for that is one re-check from the queue while a lossy upload against a lossless
request is somebody else's trumped torrent.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

os.environ.setdefault("LOX_HOST", "127.0.0.1")
os.environ.setdefault("LOX_PORT", "5018")
os.environ.setdefault("LOX_AUTH_TOKEN", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("LOX_DOWNLOAD_DIR", os.path.join(ROOT, "_flacgate", "downloads"))
os.environ.setdefault("LOX_TORRENTS_DIR", os.path.join(ROOT, "_flacgate", "torrents"))
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def main() -> int:
    from lox.checker.queue_rules import QueueRules, admits, lossless_gate, partition, request_allows_lossy

    lenient = QueueRules(when="any", requests_too=True)

    def row(**kw):
        base = {
            "id": "1", "album_id": "1", "kind": "scan", "sources": ["scan"],
            "missing_from": ["RED"], "found_on": [],
        }
        base.update(kw)
        return base

    # --- what a request has to say for lossy to be allowed ----------------
    cases = [
        ("says nothing at all", [], [], False),
        ("FLAC and nothing else", ["FLAC"], [], False),
        ("FLAC, any bitrate", ["FLAC"], ["Any"], False),
        ("FLAC at Lossless", ["FLAC"], ["Lossless"], False),
        ("FLAC or MP3", ["FLAC", "MP3"], [], True),
        ("MP3", ["MP3"], [], True),
        ("Any format", ["Any"], [], True),
        ("MP3 at V0", ["MP3"], ["V0 (VBR)"], True),
        ("FLAC or MP3, but only Lossless", ["FLAC", "MP3"], ["Lossless"], False),
        ("FLAC or MP3, 24bit only", ["FLAC", "MP3"], ["24bit Lossless"], False),
        ("no format named, V0 wanted", [], ["V0 (VBR)"], True),
        ("no format named, 320 wanted", [], ["320"], True),
        ("no format named, Lossless wanted", [], ["Lossless"], False),
        ("no format named, any bitrate", [], ["Any"], True),
        ("Ogg Vorbis", ["Ogg Vorbis"], [], True),
    ]
    for name, formats, encodings, want in cases:
        got = request_allows_lossy(formats, encodings)
        check(f"a request that {name} {'accepts' if want else 'refuses'} lossy", got == want,
              "" if got == want else f"got {got}")

    # --- the gate ---------------------------------------------------------
    check("all FLAC goes through", lossless_gate(row(all_flac=True))[0], "")

    blocked, why = lossless_gate(row(all_flac=False, flac_count=3, deezer_tracks=12))
    check("not all FLAC does not", not blocked, "")
    check("and the reason counts the tracks", "3 of 12" in why, why)

    unknown, why = lossless_gate(row(all_flac=None))
    check("never checked is held rather than assumed fine", not unknown, "")
    check("and says a re-check would answer it", "re-check" in why, why)

    # A request row is the one exception, and only on its own terms.
    ok, _ = lossless_gate(row(all_flac=False, sources=["request"],
                              request_formats=["FLAC", "MP3"], request_encodings=["V0 (VBR)"]))
    check("a request that asked for MP3 takes a lossy source", ok, "")

    # A total with no tally must not render as "only None of 9".
    _, why = lossless_gate(row(all_flac=False, deezer_tracks=9))
    check("an unknown tally is left out rather than printed as None",
          "None" not in why, why)

    blocked, why = lossless_gate(row(all_flac=False, sources=["request"],
                                     request_formats=["FLAC"], request_encodings=[]))
    check("a FLAC-only request does not", not blocked, "")
    check("and says why", "no open request accepts lossy" in why, why)

    # A scan row cannot borrow a request's permission it does not have.
    blocked, _ = lossless_gate(row(all_flac=False, request_formats=["MP3"]))
    check("a scan row is not excused by formats with no request behind them", not blocked, "")

    # --- the gate runs before the request shortcut ------------------------
    # This is the ordering that matters: requests_too waves every request row
    # through, so a gate placed after it would never see the rows it is for.
    ok, why = admits(row(all_flac=False, kind="request", sources=["request"],
                         request_formats=["FLAC"]), lenient)
    check("a FLAC-only request is held even with 'queue anything that fills a request' on",
          not ok, why)
    ok, _ = admits(row(all_flac=True, kind="request", sources=["request"]), lenient)
    check("and an all-FLAC one still goes through", ok, "")

    # --- the reason survives into the held list ---------------------------
    shown, held = partition(
        [row(id="a", all_flac=True), row(id="b", all_flac=False), row(id="c", all_flac=None)],
        lenient,
    )
    check("only the provable one is queued", [r["id"] for r in shown] == ["a"], str([r["id"] for r in shown]))
    check("the rest are held, not dropped", len(held) == 2, str(len(held)))
    check("each with its own reason",
          all(r.get("held_reason") for r in held), str([r.get("held_reason") for r in held]))

    # --- nothing to upload still beats everything -------------------------
    ok, why = admits(row(all_flac=True, missing_from=[], found_on=["RED"]), lenient)
    check("a release every tracker has is still not queued", not ok, why)

    # --- the paths that record it -----------------------------------------
    import inspect

    from lox.checker import missing as missing_mod

    source = inspect.getsource(missing_mod.MissingScanner.check_album)
    check("the single-album check asks Deezer what it can supply",
          "availability(album_id)" in source, "")
    check("and stores the answer", '"all_flac": check.all_flac' in inspect.getsource(missing_mod), "")

    from lox.checker import deezer_requests as dr

    check("a request check stores what the request accepts",
          '"request_formats": match.formats' in inspect.getsource(dr), "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
