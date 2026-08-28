"""Re-checking a queue row must not delete it from the queue.

Pressing "Re-check on trackers" emptied the queue. The row came back missing
from a tracker, which is exactly what the queue is for, and it was gone anyway.

The re-check knows four things about a release -- id, title, artist, source --
because that is all the queue row carries. The stored record was then written
from those four, and ``CheckerStore.put`` replaces rather than merges, so every
other fact on it took its default. ``all_flac`` became False, which
``lossless_gate`` reads as "not all FLAC on Deezer", which ``is_settled`` calls
final -- so the row was not held with a reason, it was dropped, and re-checking
again could not bring it back because re-checking is what did it.

Two changes: the record is merged onto what was already known, and a re-check
that has no availability data goes and gets it. The queue's own advice --
"Deezer formats not checked yet, re-check it to see if it is all FLAC" -- is
true now.
"""

import asyncio
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_queuerecheck")
shutil.rmtree(BASE, ignore_errors=True)
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5133",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.checker import queue_rules  # noqa: E402
from lox.checker.missing import Candidate, MissingScanner  # noqa: E402
from lox.checker.store import CheckerStore  # noqa: E402
from lox.deezer.gw import DeezerGWError, TrackAvailability  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


class FakeGateway:
    """Two configured trackers, both answering."""

    @staticmethod
    def configured_trackers() -> list[str]:
        return ["RED", "OPS"]

    def can_check(self, _code: str, _needed: int = 1) -> bool:
        return True


class FakeGW:
    """Deezer, answering the one question a re-check asks it."""

    def __init__(self, availability_value=None, fail: bool = False) -> None:
        self.availability_value = availability_value
        self.fail = fail
        self.asked = 0

    async def availability(self, _album_id: str):
        self.asked += 1
        if self.fail:
            raise DeezerGWError("Deezer is not answering")
        return self.availability_value


def whole_flac(total: int = 10) -> TrackAvailability:
    """An album Deezer serves as FLAC throughout."""
    return TrackAvailability(
        total=total,
        flac_count=total,
        readable_count=total,
        all_flac=True,
        all_readable=True,
        all_have_id=True,
        all_have_filesize=True,
        unreadable=[],
        release_date="2025-06-01",
    )


def scanner_for(gw: FakeGW) -> MissingScanner:
    """A scanner whose tracker calls are decided rather than made."""
    made = MissingScanner(gw, FakeGateway(), CheckerStore())  # pyright: ignore[reportArgumentType]

    async def _check_one(_candidate, code):
        # Missing from RED, present on OPS -- a queue row either way.
        return (code == "OPS", 4242 if code == "OPS" else None, 1, 1)

    made._check_one = _check_one  # pyright: ignore[reportAttributeAccessIssue]
    return made


def queue_row(entry: dict) -> dict:
    """The row the queue builds from a stored record, as api_found does."""
    return {
        "kind": "scan",
        "sources": ["scan"],
        "missing_from": entry.get("missing_from") or [],
        "found_on": entry.get("found_on") or [],
        "all_flac": entry.get("all_flac"),
        "flac_count": entry.get("flac_count"),
        "deezer_tracks": entry.get("deezer_tracks"),
        "blocked": entry.get("blocked") or "",
    }


async def main() -> int:
    rules = queue_rules.QueueRules(when=queue_rules.ANY, requests_too=True)

    # --- the reported failure, end to end ----------------------------
    gw = FakeGW(whole_flac())
    scanner = scanner_for(gw)
    # A scan put it in the queue, knowing it is all FLAC.
    scanner.store.put(
        "albums",
        "111",
        {
            "status": "missing_red",
            "title": "SCRT",
            "artist": "Regina Demina",
            "year": "2025",
            "source": "scan",
            "found_on": [],
            "missing_from": ["RED", "OPS"],
            "all_flac": True,
            "flac_count": 10,
            "deezer_tracks": 10,
            "release_date": "2025-06-01",
            "blocked": "",
        },
        flush=True,
    )
    before = scanner.store.get("albums", "111") or {}
    admitted, why = queue_rules.admits(queue_row(before), rules)
    check("a scanned release is in the queue", admitted, why)

    # The queue's re-check knows only these four fields.
    await scanner.check(
        [Candidate(album_id="111", title="SCRT", artist="Regina Demina", source="found")],
        ["RED", "OPS"],
    )
    after = scanner.store.get("albums", "111") or {}

    check("a re-check asks Deezer what it can supply", gw.asked == 1, str(gw.asked))
    check("so the release is still known to be all FLAC",
          after.get("all_flac") is True, str(after.get("all_flac")))
    admitted, why = queue_rules.admits(queue_row(after), rules)
    check("and it is still in the queue afterwards", admitted, why)
    check("with the verdict the re-check reached",
          after.get("missing_from") == ["RED"] and after.get("found_on") == ["OPS"],
          f"missing={after.get('missing_from')} found={after.get('found_on')}")

    # Everything the re-check did not re-derive is still there.
    check("the year survives a re-check", after.get("year") == "2025", str(after.get("year")))
    check("and so does the release date",
          after.get("release_date") == "2025-06-01", str(after.get("release_date")))
    check("and when it was first seen", after.get("first_seen") == before.get("first_seen"), "")
    check("the group it matched is recorded, so the tag can be a link",
          after.get("group_ids") == {"OPS": 4242}, str(after.get("group_ids")))

    # --- and the reason it was dropped rather than held --------------
    dropped = queue_row({**after, "all_flac": False})
    _ok, reason = queue_rules.admits(dropped, rules)
    check("all_flac False is what removed it", "not all FLAC" in reason, reason)
    check("and that reason is final, so the row was not even held back",
          queue_rules.is_settled(reason), reason)

    # --- a release that really is not all FLAC still says so ---------
    partial = TrackAvailability(
        total=11, flac_count=4, readable_count=11, all_flac=False, all_readable=True,
        all_have_id=True, all_have_filesize=True, unreadable=[], release_date="2025-06-01",
    )
    gw2 = FakeGW(partial)
    scanner2 = scanner_for(gw2)
    await scanner2.check(
        [Candidate(album_id="222", title="Half", artist="Someone", source="found")],
        ["RED", "OPS"],
    )
    entry2 = scanner2.store.get("albums", "222") or {}
    check("a release Deezer serves half in MP3 is recorded as such",
          entry2.get("all_flac") is False and entry2.get("flac_count") == 4,
          str(entry2.get("flac_count")))
    _ok2, why2 = queue_rules.admits(queue_row(entry2), rules)
    check("and it is kept out, with the count in the reason", "4/11" in why2, why2)
    # Deezer's own wording for the shortfall, which arrives as `blocked` and is
    # read before the gate composes a sentence of its own. Every other reason
    # TrackAvailability gives was already final; this one was not, so a release
    # Deezer serves half in MP3 sat in the held-back list waiting on a re-check
    # that could only ever say the same thing.
    check("and dropped rather than parked, because no re-check changes it",
          queue_rules.is_settled(why2), why2)

    # --- Deezer failing must not be read as an answer ----------------
    gw3 = FakeGW(None, fail=True)
    scanner3 = scanner_for(gw3)
    scanner3.store.put(
        "albums",
        "333",
        {
            "status": "missing_red",
            "title": "Known",
            "artist": "Someone",
            "year": "2024",
            "found_on": [],
            "missing_from": ["RED"],
            "all_flac": True,
            "flac_count": 9,
            "deezer_tracks": 9,
        },
        flush=True,
    )
    await scanner3.check(
        [Candidate(album_id="333", title="Known", artist="Someone", source="found")],
        ["RED", "OPS"],
    )
    entry3 = scanner3.store.get("albums", "333") or {}
    check("a failed Deezer lookup claims nothing",
          entry3.get("all_flac") is True, str(entry3.get("all_flac")))
    admitted3, why3 = queue_rules.admits(queue_row(entry3), rules)
    check("so the row stays in the queue", admitted3, why3)

    # And one nobody ever answered for is held with a reason, not deleted.
    unknown = queue_row({"missing_from": ["RED"], "found_on": [], "all_flac": None})
    ok4, why4 = queue_rules.admits(unknown, rules)
    check("an unanswered release is held rather than admitted", not ok4, why4)
    check("and held is not settled, so a re-check can still fix it",
          not queue_rules.is_settled(why4), why4)

    # --- a scan still writes what it already knows -------------------
    gw5 = FakeGW(whole_flac())
    scanner5 = scanner_for(gw5)
    await scanner5.check(
        [
            Candidate(
                album_id="555", title="Scanned", artist="Someone", year="2026", source="scan",
                availability={"all_flac": True, "flac_count": 12, "total": 12},
            )
        ],
        ["RED", "OPS"],
    )
    check("a scan does not re-ask Deezer for what it already carries",
          gw5.asked == 0, str(gw5.asked))
    entry5 = scanner5.store.get("albums", "555") or {}
    check("and the candidate's own answer is what is stored",
          entry5.get("all_flac") is True and entry5.get("deezer_tracks") == 12,
          str(entry5.get("deezer_tracks")))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
