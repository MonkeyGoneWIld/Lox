"""Filling a request from an upload, driven the way the browser drives it.

This runs the pipeline's own request-fill code through the real FlowPrompts
bridge and reads back the Step the browser would be sent, because every bug
below was invisible from either side on its own -- the pipeline was doing what
it always did, and the bridge was faithfully relaying a question with no answers
in it.

What shipped broken:

  * every request found was written to the collapsed log and offered as
    nothing, so "choose from results" listed no results to choose from
  * a bare number is a row number, not a request id, and nothing said so --
    with the list invisible, typing the id of the request you wanted quietly
    selected a different one, and filling the wrong request cannot be undone
  * an answer that matched no branch re-asked forever, in silence
  * a re-ask dropped the buttons, leaving only the answer just rejected
  * a request id that did not exist aborted the upload -- after the cover had
    been uploaded and the spectrals generated, immediately before posting
  * an empty answer to "are you sure" raised IndexError, in the same place
"""

import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_requestfill")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5110",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
        "LOX_TMP_DIR": os.path.join(BASE, "spectrals"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox import cfg  # noqa: E402
from lox.errors import RequestError  # noqa: E402
from lox.flow import Flow  # noqa: E402
from lox.upload_flow import FlowPrompts  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def load_request_fill():
    """The module under test, with or without the audio wheels installed.

    ``lox.uploader.request_fill`` needs nothing heavier than asyncclick, but
    importing it by name executes ``lox/uploader/__init__.py``, which pulls in
    av and numpy. Where those are present this is a plain import; where they
    are not, the file is loaded directly so the suite still runs.
    """
    try:
        from lox.uploader.request_fill import check_requests  # noqa: PLC0415

        return check_requests
    except ImportError:
        pass

    parent = sys.modules.get("lox.uploader") or types.ModuleType("lox.uploader")
    parent.__path__ = [os.path.join(os.path.dirname(ROOT), "lox", "uploader")]
    sys.modules.setdefault("lox.uploader", parent)
    spectrals = types.ModuleType("lox.uploader.spectrals")
    spectrals.view_spectrals = lambda *_a, **_k: None
    sys.modules.setdefault("lox.uploader.spectrals", spectrals)

    spec = importlib.util.spec_from_file_location(
        "lox.uploader.request_fill",
        os.path.join(os.path.dirname(ROOT), "lox", "uploader", "request_fill.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["lox.uploader.request_fill"] = module
    spec.loader.exec_module(module)
    return module.check_requests


check_requests = load_request_fill()

OPEN_REQUESTS = [
    {
        "requestId": 8811,
        "categoryName": "Music",
        "title": "Scarlet",
        "year": 2023,
        "releaseType": "Album",
        "artists": [[{"id": 1, "name": "Doja Cat"}]],
        "bitrateList": ["Lossless"],
        "formatList": ["FLAC"],
        "mediaList": ["WEB", "CD"],
        "totalBounty": 5368709120,
        "requestorName": "someone",
        "bbDescription": "Any lossless source.",
    },
    {
        "requestId": 9902,
        "categoryName": "Music",
        "title": "Scarlet (Deluxe)",
        "year": 2024,
        "releaseType": "Album",
        "artists": [[{"id": 1, "name": "Doja Cat"}]],
        "bitrateList": ["Lossless", "24bit Lossless"],
        "formatList": ["FLAC"],
        "mediaList": ["WEB"],
        "totalBounty": 1073741824,
        "requestorName": "another",
        "bbDescription": "Deluxe only.",
    },
    # Not music. It must not be offered, and the one below it has no
    # categoryName at all -- which used to raise KeyError and lose the upload.
    {"requestId": 7000, "categoryName": "E-Books", "title": "A book", "artists": [[]]},
    {"requestId": 7001, "title": "No category at all", "artists": [[]]},
]

URL_8811 = "https://redacted.sh/requests.php?action=view&id=8811"
URL_9902 = "https://redacted.sh/requests.php?action=view&id=9902"


class StubTracker:
    """A tracker that answers the two calls request-filling makes."""

    site_code = "RED"
    site_string = "RED"
    base_url = "https://redacted.sh"

    def request_url(self, request_id):
        return f"{self.base_url}/requests.php?action=view&id={request_id}"

    async def request(self, action, data=None, **_):
        if action == "requests":
            return {"results": OPEN_REQUESTS}
        if action == "request":
            wanted = int((data or {}).get("id", 0))
            for r in OPEN_REQUESTS:
                if r["requestId"] == wanted:
                    return {**r, "musicInfo": {"artists": [{"name": "Doja Cat"}]}}
            raise RequestError(f"no request {wanted}")
        return {}


async def drive(answers: list, tracker=None, linked=None):
    """Run check_requests, answering each question in turn.

    Args:
        answers: One answer per question, in order.
        tracker: The stub tracker to use.
        linked: The request this release was already matched to on this
            tracker, as a request check would have recorded it.

    Returns:
        Tuple of (steps seen, returned value, log lines).
    """
    flow = Flow("upload", "request fill")
    seen: list[dict] = []
    out: dict = {}
    last = {"id": None}

    async def run():
        with FlowPrompts(flow, os.environ["LOX_DOWNLOAD_DIR"]):
            out["value"] = await check_requests(
                tracker or StubTracker(), ["Doja Cat Scarlet"], linked_request_id=linked)

    task = asyncio.create_task(run())
    for reply in answers:
        for _ in range(400):
            if task.done() or (flow.step and flow.step.id != last["id"]):
                break
            await asyncio.sleep(0.01)
        if task.done() or not flow.step or flow.step.id == last["id"]:
            break
        step = flow.step
        last["id"] = step.id
        seen.append(
            {
                "prompt": step.prompt,
                "options": list(step.options),
                "values": [o.get("value") for o in step.options],
                "text_label": step.text_label,
            }
        )
        flow.answer(step.id, reply)
        await asyncio.sleep(0.05)

    for _ in range(60):
        if task.done():
            break
        await asyncio.sleep(0.01)
    stuck = not task.done()
    if stuck:
        task.cancel()
    else:
        await task
    return seen, ("<never finished>" if stuck else out.get("value", "<no return>")), [
        e.get("message", "") for e in flow.events
    ]


async def main() -> int:
    class Empty(StubTracker):
        async def request(self, action, data=None, **_):
            if action == "requests":
                return {"results": []}
            return await StubTracker.request(self, action, data)

    cfg.upload.requests.check_requests = True
    cfg.upload.requests.always_ask_for_request_fill = False
    cfg.upload.yes_all = False

    # --- the requests found are the answers, so they are buttons ------
    seen, value, log = await drive([URL_8811, "y"])
    first = seen[0]
    check("the question offers the requests it found",
          first["values"][:2] == [URL_8811, URL_9902], str(first["values"]))
    check("each carries a link to the request itself",
          all(o.get("url") for o in first["options"][:2]), "")
    check("labelled with what it is for",
          "Scarlet" in first["options"][0]["label"] and "2023" in first["options"][0]["label"],
          first["options"][0]["label"])
    check("and what it will accept",
          "FLAC" in (first["options"][0].get("detail") or ""), first["options"][0].get("detail", ""))
    check("with a way to fill nothing", "n" in first["values"], str(first["values"]))
    check("and the paste field still there for one not in the list",
          first["text_label"] != "", first["text_label"])
    check("picking one returns that request", value == 8811, str(value))

    # A button's value is the URL, never the row number: the row number means
    # "whatever is at that index" and filling the wrong request is permanent.
    check("a button sends the request's URL, not its position",
          all(str(v).startswith("http") for v in first["values"][:2]), str(first["values"][:2]))

    # --- what is not music is not offered -----------------------------
    check("a request in another category is not offered",
          not any("7000" in str(v) for v in first["values"]), str(first["values"]))
    check("nor is one that does not say what it is",
          not any("7001" in str(v) for v in first["values"]), str(first["values"]))

    # --- a bare number is a row, and says so --------------------------
    seen, value, log = await drive(["2", "y"])
    check("a row number picks that row", value == 9902, str(value))
    check("and says which request that was",
          any("request 9902" in line for line in log), "")
    seen, value, log = await drive(["9902", "y"])
    check("a number past the end is read as a request id", value == 9902, str(value))

    # --- an answer it cannot read is said so, not looped on -----------
    seen, value, log = await drive(["%%junk%%", URL_9902, "y"])
    check("an unreadable answer does not loop in silence",
          any("did not understand" in line.lower() for line in log), "")
    check("it asks again rather than giving up", len(seen) >= 2, str(len(seen)))
    check("and the buttons are still there the second time",
          len(seen) >= 2 and seen[1]["values"][:2] == [URL_8811, URL_9902],
          str(seen[1]["values"]) if len(seen) >= 2 else "")
    check("then the good answer is taken", value == 9902, str(value))

    # --- a URL is a URL however it was pasted -------------------------
    for label, pasted in (
        ("with different capitalisation", "https://REDACTED.sh/requests.php?action=view&id=9902"),
        ("with surrounding space", f"  {URL_9902}  "),
        ("with extra query parameters", f"{URL_9902}&foo=bar"),
    ):
        _, value, _ = await drive([pasted, "y"])
        check(f"a URL {label} is understood", value == 9902, str(value))

    # --- a request that does not exist is not fatal -------------------
    #
    # This runs after the cover is on an image host and the spectrals are
    # generated, immediately before the release is posted. Aborting there threw
    # all of it away over one wrong digit.
    seen, value, log = await drive(["https://redacted.sh/requests.php?action=view&id=404404", "1", "y"])
    check("a request id that does not exist says so",
          any("no request 404404" in line.lower() for line in log), "")
    check("and the upload is not abandoned for it", value == 8811, str(value))
    check("it asks again with the list intact",
          len(seen) >= 2 and seen[1]["values"][:2] == [URL_8811, URL_9902],
          str(seen[1]["values"]) if len(seen) >= 2 else "")

    # --- declining ----------------------------------------------------
    _, value, _ = await drive(["n"])
    check("choosing to fill nothing fills nothing", value is None, str(value))
    _, value, _ = await drive([URL_8811, "n"])
    check("and so does saying no at the confirmation", value is None, str(value))
    _, value, _ = await drive([URL_8811, ""])
    check("an empty confirmation is a yes, not an IndexError", value == 8811, str(value))

    # --- nothing found -------------------------------------------------
    seen, value, _ = await drive(["n"], tracker=Empty())
    check("with nothing found a real upload does not ask at all", not seen and value is None, str(seen))

    cfg.upload.requests.always_ask_for_request_fill = True
    seen, value, _ = await drive([URL_9902, "y"], tracker=Empty())
    check("unless you asked to be asked anyway", len(seen) == 2, str(len(seen)))
    check("and pasting one still works then", value == 9902, str(value))
    cfg.upload.requests.always_ask_for_request_fill = False

    # --- a dry run rehearses the step whatever the search found --------
    #
    # A rehearsal exists to show every part of a run before any of it happens
    # for real, and a step that silently does not run is indistinguishable from
    # one that is broken. Nothing is filled either way: the upload itself is
    # what posts the fill, and in a dry run that is replaced by a report.
    cfg.upload.dry_run = True
    try:
        seen, value, log = await drive(["n"], tracker=Empty())
        check("a dry run asks even when nothing matched", len(seen) == 1, str(len(seen)))
        check("and says so rather than offering a list that is not there",
              seen and "nothing matched" in seen[0]["prompt"].lower(),
              seen[0]["prompt"] if seen else "")
        check("with the paste field, so an unmatched request can still be filled",
              seen and seen[0]["text_label"] != "", seen[0]["text_label"] if seen else "")
        check("and declining still fills nothing", value is None, str(value))

        seen, value, _ = await drive([URL_9902, "y"], tracker=Empty())
        check("pasting one in a dry run reaches the payload", value == 9902, str(value))

        # With matches, a dry run is the same question a real upload asks.
        seen, value, _ = await drive([URL_8811, "y"])
        check("and with matches it is the same question as a real upload",
              seen and seen[0]["values"][:2] == [URL_8811, URL_9902], str(seen[0]["values"]) if seen else "")
        check("still returning the request so the rehearsal can report it",
              value == 8811, str(value))
    finally:
        cfg.upload.dry_run = False

    # --- auto-answering fills what was already decided, and nothing else ---
    #
    # This rule has been wrong twice in opposite directions. First it filled
    # nothing ever, which made the auto-answer setting a way to turn request
    # filling off: a release queued BECAUSE it fills an open request went up
    # without filling it. Then it filled any single search hit, which decided
    # on a default the one thing worth refusing -- filling a request is a claim
    # against somebody else's specific request and there is no undo.
    #
    # What it fills now is the request this release was already matched to by a
    # request check, on this tracker. That pairing was decided by a search that
    # ran on its own time and can be read back in the lookup history; filling
    # it is carrying out a decision, not making one. Everything else is asked,
    # whatever the setting says.
    cfg.upload.yes_all = True
    try:
        cfg.upload.dry_run = False

        # The linked one: filled, without a question.
        seen, value, log = await drive([], linked=8811)
        check("auto-answering fills the request already matched to this release",
              value == 8811, str(value))
        check("without asking about it", not seen, str([s["prompt"] for s in seen]))
        check("and says which one, so an unattended run can be read back",
              any("8811" in line for line in log), "")

        # No linked request: asked, even though prompts are auto-answered.
        # A search at upload time that turns up candidates is a choice.
        seen, value, _log = await drive(["n"])
        check("with nothing linked it asks, auto-answer or not",
              bool(seen), str([s["prompt"] for s in seen]))
        check("offering what the search found",
              seen and seen[0]["values"][:2] == [URL_8811, URL_9902],
              str(seen[0]["values"]) if seen else "")
        check("and fills nothing when the answer is no", value is None, str(value))

        # One search hit is still a choice when nothing linked it.
        class OneRequest(StubTracker):
            """A tracker with exactly one open request for this release."""

            async def request(self, action, data=None, **_):
                if action == "requests":
                    return {"results": [OPEN_REQUESTS[0]]}
                return await StubTracker.request(self, action, data)

        seen, value, _log = await drive(["n"], tracker=OneRequest())
        check("a single unlinked match is asked about rather than assumed",
              bool(seen), str([s["prompt"] for s in seen]))

        # A linked request the tracker no longer has: not filled on the
        # strength of a stale record, and not abandoned either -- the search
        # runs and the question is asked.
        class GoneRequest(StubTracker):
            """The linked request has been filled or deleted since."""

            async def request(self, action, data=None, **_):
                if action == "requests":
                    return {"results": [OPEN_REQUESTS[0]]}
                raise RequestError("no such request")

        seen, value, log = await drive(["n"], tracker=GoneRequest(), linked=4041)
        check("a linked request that has gone is not filled", value is None, str(value))
        check("and it says so rather than going quiet",
              any("no longer there" in line for line in log), "")
        check("then asks, rather than abandoning the request search",
              bool(seen), str([s["prompt"] for s in seen]))
    finally:
        cfg.upload.yes_all = False
        cfg.upload.dry_run = False

    # --- a question only carries evidence from its own phase -----------
    #
    # With prompts auto-answered nothing consumed the tag diff or the folder
    # rename, so they queued up and arrived attached to the next question --
    # a request question wearing a "Folder rename" table, which reads as a
    # rename prompt that has lost its buttons.
    flow = Flow("upload", "tables")
    with FlowPrompts(flow, os.environ["LOX_DOWNLOAD_DIR"]) as prompts:
        prompts._echo("Checking metadata...")  # noqa: SLF001
        prompts._echo("Renaming folder...")  # noqa: SLF001
        prompts._echo("Old Name >>> New Name")  # noqa: SLF001
        prompts._echo("")  # noqa: SLF001
        carried = [t["title"] for t in prompts._tables]  # noqa: SLF001
        check("a folder rename is captured as a table", carried == ["Folder rename"], str(carried))

        same_stage = asyncio.get_running_loop().create_task(prompts._prompt("Rename it? [Y]es, [n]o"))  # noqa: SLF001
        for _ in range(200):
            if flow.step:
                break
            await asyncio.sleep(0.01)
        titles = [t["title"] for t in (flow.step.tables if flow.step else [])]
        check("and shown with the question asked in that same phase",
              titles == ["Folder rename"], str(titles))
        flow.answer(flow.step.id, "y")
        await same_stage

        prompts._echo("Renaming folder...")  # noqa: SLF001
        prompts._echo("Old Name >>> New Name")  # noqa: SLF001
        prompts._echo("")  # noqa: SLF001
        # The pipeline moves on to another phase without anything having asked.
        prompts._echo("No requests were found on RED")  # noqa: SLF001
        later = asyncio.get_running_loop().create_task(prompts._prompt("Fill a request? do[n]t"))  # noqa: SLF001
        for _ in range(200):
            if flow.step:
                break
            await asyncio.sleep(0.01)
        titles = [t["title"] for t in (flow.step.tables if flow.step else [])]
        check("but not with a question from a later phase", titles == [], str(titles))
        check("which is the phase the run had moved on to",
              flow.stage == "Checking requests", flow.stage)
        flow.answer(flow.step.id, "n")
        await later

    # Nothing here posts a fill. The fill rides on the upload, and a dry run
    # replaces the upload with a report -- asserted against the source, since
    # importing the tracker to prove it would need a configured site.
    with open(os.path.join(os.path.dirname(ROOT), "lox", "trackers", "base.py"), encoding="utf-8") as handle:
        base = handle.read()
    body = base[base.index("    async def upload(self, data: dict"):]
    body = body[: body.index("_log_dry_run_upload(self")]
    check("a dry run never reaches the code that posts an upload",
          body.index("if cfg.upload.dry_run:") < body.index("api_key_upload"), "")
    check("and reports the request it would have filled",
          '[DRY RUN] Would have filled request' in base, "")
    check("with the id in the payload it prints",
          '"requestid"' in base, "")

    # --- and it is off when it is off ----------------------------------
    check("the setting that turns it off is the one the page shows",
          hasattr(cfg.upload.requests, "check_requests"), "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
