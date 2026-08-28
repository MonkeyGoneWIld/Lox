"""One release, prepared once, posted to every tracker that was picked.

Uploading to two trackers ran the whole pipeline twice. The second pass
regenerated the spectrals and asked about them again, looked the metadata up
and asked which result to use again, retagged the files, asked whether to
rename the folder, asked whether to rename the files, and asked which formats
to convert to -- all about a release that had not changed in the intervening
minute. From the operator's side it was every question a second time, for
nothing.

The pipeline has always had its own tracker loop that does the shared work once
and only repeats what is genuinely per-tracker. It was being switched off,
because the loop offers whichever trackers are configured and would therefore
name ones the operator had deliberately not picked. It is driven rather than
switched off now: given exactly the trackers picked, in the order picked.

What must remain per-tracker, and is checked here: whether that tracker already
has the group, whether it has an open request, the post itself, and the linked
folder it seeds from.

Also pinned here, because both were reported from a real upload:

  * a request check that confirms the tracker does NOT have the release has to
    say so in the words the queue reads, or a request matched at 100% is held
    out of the queue as "not checked against any tracker yet"
  * the download the release was made from is removed once the trackers are
    seeding from their own copies, or it stays on the Uploading list for ever
"""

import asyncio
import inspect
import os
import pathlib
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_multitracker")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5124",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def source_of(func) -> str:
    """The body of a function, for asserting about control flow."""
    return inspect.getsource(func)


def pipeline_checks() -> None:
    """The shared work happens once; the per-tracker work happens per tracker."""
    from lox.uploader import upload

    src = source_of(upload)
    params = inspect.signature(upload).parameters

    check("the pipeline can be told which trackers to work through",
          "also_upload_to" in params, str(list(params))[-90:])
    check("and given a folder per tracker to seed from", "link_for" in params, "")
    check("and told when each one takes it", "on_uploaded" in params, "")
    check("and asked to announce which one it is on", "on_tracker" in params, "")

    # Everything expensive is before the loop. The loop starts at the "while
    # True" that walks the trackers.
    loop_at = src.index("while True:")
    before, inside = src[:loop_at], src[loop_at:]

    for what, needle in (
        ("the spectrals", "check_spectrals("),
        ("the metadata lookup", "get_metadata("),
        ("the retagging, renaming and integrity check", "edit_metadata("),
        ("the spectral upload", "handle_spectrals_upload_and_deletion("),
    ):
        check(f"{what} happens once, before any tracker",
              needle in before and needle not in inside, "")

    for what, needle in (
        ("whether this tracker has the group", "check_existing_group("),
        ("whether this tracker has an open request", "check_requests("),
        ("the post itself", "upload_and_report("),
    ):
        check(f"{what} happens per tracker", needle in inside, "")

    check("the driven loop takes the next tracker rather than asking",
          "remaining_gazelle_sites[0] if remaining_gazelle_sites else None" in inside, "")
    check("and never offers one that was not picked",
          "list(also_upload_to) if driven else" in src, "")
    check("the downconversion is chosen once for the release",
          "if downconversion_choice is None:" in inside, "")
    check("and reused for the trackers after the first",
          "as chosen for the first tracker" in inside, "")
    check("each tracker seeds from its own folder",
          "upload_path = await link_for(tracker, path)" in inside
          and "upload_and_report(\n                gazelle_site,\n                upload_path," in inside, "")

    # A transcode is a fact about the release, not about the tracker: the same
    # thirteen tracks at V0 whoever is being uploaded to. Encoding it again per
    # tracker wrote a second copy of identical audio and took as long again.
    check("a transcode is produced from the prepared release, not per tracker",
          "# Produced from the prepared release, not from this" in inside, "")
    check("made once and handed to each tracker to place",
          "produced=derived," in inside and "place=(lambda p, tracker=tracker:" in inside, "")

    from lox.uploader import execute_downconversion_tasks

    tasks = source_of(execute_downconversion_tasks)
    check("the downconversion is built once per task name",
          "if name not in produced:" in tasks, "")
    check("both the transcode and the downconvert go through it",
          tasks.count("await make(") == 2, str(tasks.count("await make(")))
    check("and each tracker is given somewhere of its own to seed it from",
          tasks.count("await place(") == 2, str(tasks.count("await place(")))

    # The lossy report is per torrent, so it has to be filed per tracker. It
    # was, and it stays that way now the pipeline runs once: the report sits
    # inside upload_and_report, which the loop calls for each of them.
    from lox.uploader import upload_and_report

    check("the lossy-master report is filed by the per-tracker post",
          "if lossy_master:" in source_of(upload_and_report)
          and "await report_lossy_master(" in source_of(upload_and_report), "")
    check("so every tracker that takes a lossy release gets one",
          "upload_and_report(" in inside, "")

    # A release with no genre is refused by the tracker, and being refused
    # after the spectrals, the tagging and the torrent build is an expensive
    # way to find out.
    from lox.uploader.upload import prepare_and_upload

    check("a release with no genre is stopped before the post",
          'if not metadata.get("genres"):' in source_of(prepare_and_upload), "")
    check("and told why", "needs at least one genre" in source_of(prepare_and_upload), "")


def flow_checks() -> None:
    """The web flow drives one pass, not one pass per tracker."""
    import lox.upload_flow as uf

    run_uploads = source_of(uf.run_uploads)
    check("the flow makes one pipeline call for every tracker",
          run_uploads.count("await run_upload(") == 1, str(run_uploads.count("await run_upload(")))
    check("handing the whole list over", "trackers, source=source" in run_uploads, "")
    check("and no longer switches the pipeline's own loop off",
          "multi_tracker_upload = False" not in run_uploads
          and "multi_tracker_upload" not in run_uploads, "")
    check("the per-tracker link is made from the prepared release, not before it",
          "async def link_for(tracker: str, path: str)" in run_uploads
          and "link_release, path, tracker" in run_uploads, "")
    check("what each tracker actually took is recorded, not inferred",
          "posted_to[tracker]" in run_uploads, "")
    check("and the old per-tracker loop is gone",
          not hasattr(uf, "_upload_each"), "")

    # A real run's downconversions are seeding. Offering to delete them on the
    # page that says the upload worked invites you to break four torrents.
    check("only a rehearsal reports files left behind",
          'if _dry_run():' in run_uploads and 'result["transcodes"] = []' in run_uploads, "")

    cleanup = source_of(uf._clean_up_source)  # noqa: SLF001
    check("the download is removed once the trackers seed from their own copies",
          "shutil.rmtree(folder)" in cleanup, "")
    check("never on a dry run", "_dry_run()" in cleanup, "")
    check("never when nothing took it", 'result.get("succeeded")' in cleanup, "")
    check("and never with linking off, where that folder is what is seeding",
          "cfg.linking.enabled" in cleanup, "")


def upload_priority_checks() -> None:
    """An upload does not queue behind whatever the checker is doing."""
    import inspect as _inspect

    from lox.trackers import base

    src = _inspect.getsource(base.BaseGazelleApi.request)
    check("an upload's calls take their own allowance",
          "self._upload_limiter if uploading.get() else self._rate_limiter" in src, "")
    check("which exists", hasattr(base.BaseGazelleApi, "_upload_limiter"), "")
    check("and the flag is per task, not per client",
          isinstance(base.uploading, __import__("contextvars").ContextVar), "")
    check("the flow sets it for the length of the upload",
          "uploading.set(True)" in pathlib.Path("lox/upload_flow.py").read_text(encoding="utf-8"), "")
    check("and puts it back afterwards",
          "uploading.reset(token)" in pathlib.Path("lox/upload_flow.py").read_text(encoding="utf-8"), "")
    # Five seconds total was not enough for a tracker that regularly takes
    # longer than that, and a timeout here is retried five times over.
    check("and a tracker is given long enough to answer",
          "ClientTimeout(total=60)" in src, "")


def blacklist_checks() -> None:
    """Refused means refused everywhere, and it can be undone one at a time."""
    import inspect as _inspect

    from lox.checker.deezer_requests import DeezerRequestChecker
    from lox.checker.missing import MissingScanner

    collect = _inspect.getsource(MissingScanner.collect)
    check("a scan does not collect a release that was refused",
          'self.store.get("dismissed", album_id)' in collect, "")

    checker = _inspect.getsource(DeezerRequestChecker)
    check("nor does a request check offer one as a fill",
          'self.store.get("dismissed", str(match.deezer_id or ""))' in checker, "")

    # The trackers are asked only once Deezer has produced a confident match:
    # every earlier failure returns before reaching them, so a request that
    # goes nowhere costs no tracker call beyond the one that read it.
    where = checker.index("async def _locate")
    decide = checker.index('match.status = "fillable"')
    check("and the trackers are only asked after Deezer has answered",
          decide < checker.index("await self._locate(match)"), "")
    check("with every earlier failure returning first",
          checker[:decide].count("return match") >= 6, str(checker[:decide].count("return match")))
    check("and _locate itself asking each tracker once", where > 0, "")

    api = pathlib.Path("lox/web/api.py").read_text(encoding="utf-8")
    check("the blacklist is a list you can read back",
          '@routes.get("/api/blacklist")' in api, "")
    check("with names on it, kept when the entry is made",
          '**named.get(key, {})' in api, "")

    js = pathlib.Path("lox/web/static/scripts/app.js").read_text(encoding="utf-8")
    check("and a page to read it on", "function renderBlacklist" in js, "")
    check("where one can be taken off without clearing the lot",
          "function restoreBlacklisted" in js, "")


def request_verdict_checks() -> None:
    """A request check says which tracker it asked, in the queue's own words."""
    from lox.checker.queue_rules import QueueRules, admits
    from lox.web.api import _request_verdict  # noqa: PLC2701

    # This is the record the checker used to write: matched at 100%, confirmed
    # absent from OPS, and nothing the queue could read.
    old = {
        "tracker": "OPS", "status": "fillable", "already_on_tracker": False,
        "deezer_id": "1", "all_flac": True, "filled": False,
    }
    found, missing = _request_verdict(old)
    check("a request the tracker does not have is missing from it",
          (found, missing) == ([], ["OPS"]), str((found, missing)))

    on_it = {**old, "already_on_tracker": True}
    check("and one it does have is found on it",
          _request_verdict(on_it) == (["OPS"], []), str(_request_verdict(on_it)))

    unknown = {**old, "already_on_tracker": None}
    check("an answer nobody got says nothing either way",
          _request_verdict(unknown) == ([], []), str(_request_verdict(unknown)))

    written = {**old, "found_on": [], "missing_from": ["OPS"]}
    check("and a record that already says so is left alone",
          _request_verdict(written) == ([], ["OPS"]), str(_request_verdict(written)))

    # The consequence, which is the actual bug: without it the queue holds a
    # fillable request out as unchecked.
    lenient = QueueRules(when="any", requests_too=True)
    row = {"sources": ["request"], "all_flac": True, "missing_from": [], "found_on": []}
    ok, why = admits(row, lenient)
    check("a row with no verdict is held out as unchecked",
          not ok and "not checked against any tracker" in why, why)

    row["missing_from"] = missing
    ok, why = admits(row, lenient)
    check("and the same row, once it says which tracker, reaches the queue", ok, why)

    # And the checker writes it from now on, so this is not only a migration.
    from lox.checker.deezer_requests import DeezerRequestChecker, RequestMatch

    fields = RequestMatch.__struct_fields__
    check("the checker's own record carries both lists",
          "found_on" in fields and "missing_from" in fields, "")
    src = inspect.getsource(DeezerRequestChecker)
    check("filled in from the searches it runs",
          "match.found_on.append(code)" in src and "match.missing_from.append(code)" in src, "")
    # Every configured tracker, not only the one the request is on. The release
    # is the same release whoever is asked, and asking only the requesting
    # tracker left the queue row knowing about OPS and nothing about RED -- so
    # the upload that followed either skipped a tracker that wanted it or
    # offered one that already had it.
    check("and asked of every configured tracker, not just the requesting one",
          "for code in self.gateway.configured_trackers():" in src, "")
    check("skipping any that has nothing left to spend",
          "if not self.gateway.can_check(code):" in src, "")
    check("and stored with the rest of the answer",
          '"missing_from": match.missing_from' in src, "")


def detail_cache_checks() -> None:
    """A request's terms are settled when it is posted, so they are kept."""
    from lox.checker.request_detail import DETAILS, request_detail

    class FakeApi:
        base_url = "https://tracker.invalid"

    class FakeGateway:
        def __init__(self):
            self.calls = 0

        def api(self, _code):
            return FakeApi()

        def request_url(self, _code, request_id):
            return f"https://tracker.invalid/requests.php?action=view&id={request_id}"

        async def get_request(self, _code, request_id, *, interactive=False):
            self.calls += 1
            return {"requestId": request_id, "title": "A Record", "year": 2020,
                    "formatList": ["FLAC"], "totalBounty": 1024}

    class FakeStore:
        def __init__(self):
            self.kept = {}

        def get(self, name, key):
            return self.kept.get((name, key))

        def put(self, name, key, value, flush=False):
            self.kept[(name, key)] = {**value, "checked_at": 123.0}

    gateway, store = FakeGateway(), FakeStore()
    first = asyncio.run(request_detail(gateway, "OPS", 42, store))
    check("the first read asks the tracker", gateway.calls == 1, str(gateway.calls))
    check("and says it did", first["cached"] is False, str(first.get("cached")))
    check("and keeps what it got", (DETAILS, "OPS:42") in store.kept, str(list(store.kept)))

    second = asyncio.run(request_detail(gateway, "OPS", 42, store))
    check("the second does not ask again", gateway.calls == 1, str(gateway.calls))
    check("and says the copy is stored", second["cached"] is True, str(second.get("cached")))
    check("with the same answer", second["title"] == first["title"], second.get("title", ""))
    check("and when it was taken", bool(second.get("cached_at")), str(second.get("cached_at")))

    again = asyncio.run(request_detail(gateway, "OPS", 42, store, refresh=True))
    check("asking for a fresh one asks the tracker", gateway.calls == 2, str(gateway.calls))
    check("and it is not reported as stored", again["cached"] is False, str(again.get("cached")))

    # The checker has already paid for the payload, so it caches on the way past.
    from lox.checker.deezer_requests import DeezerRequestChecker

    check("a check caches the detail it has already fetched",
          "cache_detail(self.store, self.gateway" in inspect.getsource(DeezerRequestChecker), "")


def naming_checks() -> None:
    """A compilation is billed the way the trackers bill it."""
    from lox import cfg

    check("a release by many artists is Various Artists, not Various",
          cfg.upload.formatting.various_artist_word == "Various Artists",
          cfg.upload.formatting.various_artist_word)

    with open(os.path.join(os.path.dirname(ROOT), "data", "config.default.toml"), encoding="utf-8") as f:
        default = f.read()
    check("and the shipped config agrees",
          'various_artist_word = "Various Artists"' in default, "")


def ui_checks() -> None:
    """What the browser shows about all of this."""
    static = os.path.join(os.path.dirname(ROOT), "lox", "web", "static")
    with open(os.path.join(static, "scripts", "app.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(static, "css", "app.css"), encoding="utf-8") as f:
        css = f.read()

    check("the request page draws the stored copy and offers a fresh one",
          "loadRequestSide(true)" in js and "refresh=1" in js, "")
    check("saying how old the one on screen is", "cached_at" in js, "")
    check("a rehearsal's leftovers are the only ones offered for deletion",
          "if (!result.dry_run) return null;" in js, "")

    # An upload cannot be run again to find out what it did.
    shell = pathlib.Path(os.path.join(os.path.dirname(ROOT), "lox", "web", "templates", "app.html")
                         ).read_text(encoding="utf-8")
    check("finished uploads are kept and can be read back",
          'id="upload-tab-history"' in shell and "function loadUploadHistory" in js, "")
    check("with what it was posted as and everything it printed",
          "function uploadDetail" in js and "upload-log" in js, "")
    check("and folders can be removed in a batch, not one at a time",
          'id="folders-delete"' in shell and "function deleteSelectedFolders" in js, "")

    # Deezer gives most of its channels a colour and no picture, which left two
    # thirds of the Browse grid as plain rectangles.
    check("a channel with no artwork is drawn as its initial",
          "card-initial" in js and "card-initial" in css, "")

    # Two bare numbers over a column of dates is not a question anybody can
    # answer. "In the last six hours" and "older than three months" are both
    # asked of the same column.
    check("a date filter says what its two numbers are measured in",
          "const TIME_UNITS = [['hours'" in js and "th-unit" in js, "")
    check("and the predicate scales by it", "unitSize(wanted && wanted.unit)" in js, "")

    # `display: flex` on a <td> takes it out of the row's height calculation,
    # so the cell stopped short of the row and drew its bottom border there --
    # a bright short line under the tags with the real row line below it.
    check("the trackers cell is a cell, not a flex container",
          ".found-trackers { display: flex" not in css, "")
    check("and the tags inside it centre themselves",
          ".found-trackers > span," in css and "justify-content: center;" in css, "")

    # replaceChildren is not el(): handed a null it appends the word "null".
    check("nothing hands a bare conditional to replaceChildren",
          "function fill(node, ...children)" in js
          and "fill(bar," in js and "fill(host," in js and "fill(panel," in js, "")

    # Dropping a chip onto the one to its right used to insert it before that
    # one -- which is exactly where it already was -- so dragging right did
    # nothing while dragging left worked.
    check("a tracker dropped on another takes that one's position",
          "const to = target ? state.uploadTrackers.indexOf(target)" in js
          and "order.splice(to, 0, code);" in js, "")

    # The queue's default action is the one that finishes the job. Downloading
    # alone leaves the release in the download folder waiting for a second
    # decision nobody meant to make.
    check("the queue's default is download and upload",
          '<button class="primary" id="found-upload">' in shell, "")
    check("and the queue says which pressing a row is",
          "label: 'Year'," in js and '"year": str(entry.get("year")' in
          pathlib.Path("lox/web/api.py").read_text(encoding="utf-8"), "")

    # One store, one answer. A re-check from the queue wrote the same album
    # record the scan's history reads, but each screen kept its own copy of
    # what it last read -- so the queue dropped the row and the history went on
    # showing the answer from before.
    check("a check drops every screen's cached copy",
          "function releasesChanged" in js, "")
    check("and is called wherever a check finishes",
          js.count("releasesChanged();") >= 4, str(js.count("releasesChanged();")))

    # Answering is not instant. Nothing said so, so Save looked like it had
    # done nothing and a second press was met with "that question has already
    # been answered" -- an error about having been patient.
    check("an answer on its way says so", "answering-note" in js and "state.answering" in js, "")
    check("and cannot be sent twice", "if (state.answering.has(step.id)) return;" in js, "")
    check("nor reports the second one as a failure",
          "already been answered/i.test(e.message)" in js, "")

    # ...but the state it puts the card into was only ever taken off again
    # when sending FAILED. A card that answered successfully stayed greyed for
    # the rest of the run, so every later question -- the spectrals among them
    # -- was asked through a form that looked disabled and read as broken.
    check("the busy state is lifted in one place", "function unbusy(card)" in js, "")
    redraw = js[js.index("stepBox.dataset.step = stepId;"):js.index("stepBox.replaceChildren")]
    check("and lifted whenever the next question is drawn", "unbusy(card);" in redraw, redraw.strip()[:60])
    check("with the note and the disabled controls going with it",
          "card.classList.remove('answering');" in js
          and "$$('.answering-note', card).forEach" in js, "")
    check("and only the question's own controls are disabled, not Cancel",
          "$('.flow-step', card) || card" in js, "")

    # The trackers refuse a track with no main artist. The form showed that
    # error and offered track titles and nothing else, so Save could only ask
    # the same question again -- a button that did nothing, forever.
    check("a track's artists are a field, not a caption",
          "placeholder: 'Artists, separated by commas'" in js, "")
    check("and are sent back with the title",
          "{ title: r.value ?? '', artists: r.artists ?? '' }" in js, "")
    flow_py = pathlib.Path("lox/upload_flow.py").read_text(encoding="utf-8")
    check("the form offers the role each name already has",
          '"roles": [{"name": name, "role": role}' in flow_py, "")
    check("and a track left with no main artist is given one",
          "def _track_artists" in flow_py
          and 'people[0] = (people[0][0], "main")' in flow_py, "")
    check("while an emptied box keeps the credits it had",
          "if not people:" in flow_py and "return list(current)" in flow_py, "")
    check("and the old title-only answer is still understood",
          'edit = {"title": edit}' in flow_py, "")

    # The demotion that caused it: an album credited to one artist whose tracks
    # are credited to the singers they feature left every track with nobody.
    deezer_py = pathlib.Path("lox/tagger/sources/deezer.py").read_text(encoding="utf-8")
    check("demotion cannot empty a track",
          "def fix_track" in deezer_py and "album_mains" in deezer_py, "")

    # An upload was given its own rate allowance so it would not queue behind a
    # scan -- and then given the same size bucket, so it still stalled, just in
    # a queue of its own.
    base_py = pathlib.Path("lox/trackers/base.py").read_text(encoding="utf-8")
    check("the upload allowance is wider than the scanner's",
          "_rate_limiter = AsyncLimiter(10, 10)" in base_py
          and "_upload_limiter = AsyncLimiter(30, 10)" in base_py, "")
    # And it stopped spending that allowance on the same login. The pipeline
    # builds a client per tracker per torrent, so one release with two
    # downconversions to two trackers opened with eight identical index calls.
    check("a login is asked for once, not once per client",
          "_CREDENTIALS: dict[tuple[str, str], tuple[str, str]]" in base_py
          and "cached = _CREDENTIALS.get((self.site_code, self.cookie))" in base_py, "")
    check("keyed by the cookie, so a new login is a miss",
          "_CREDENTIALS[(self.site_code, self.cookie)] = (self.authkey, self.passkey)" in base_py, "")
    check("and dropped when the tracker says the session is gone",
          "_CREDENTIALS.pop((self.site_code, self.cookie), None)" in base_py, "")

    # The history's tracker names went to a search for the folder name. The
    # pipeline hands over the exact torrent URL as each tracker takes it, and
    # the outcome dropped it on the floor.
    check("an outcome carries where the torrent landed",
          '"url": posted_to.get(tracker, ""),' in flow_py, "")
    check("and the finished card links to it rather than naming it",
          "o.ok && o.url && !result.dry_run" in js, "")

    # A finished card sat on the page until the browser was reloaded.
    check("a finished run can be dismissed", "def dismiss(self, flow_id: str)" in
          pathlib.Path("lox/flow.py").read_text(encoding="utf-8"), "")
    web_api = pathlib.Path("lox/web/api.py").read_text(encoding="utf-8")
    check("with an endpoint that refuses to drop a running one",
          '@routes.post("/api/flows/{flow_id}/dismiss")' in web_api
          and '"that run is not finished"' in web_api, "")
    check("and a button that appears once it has finished",
          "async function dismissFlow(flowId)" in js and "'Dismiss')" in js, "")

    # A re-check started from a lookup history reported into the queue's log on
    # another page: the button looked dead and the work was invisible.
    check("a re-check reports where it was started from",
          "async function recheckReleases(picked, boxSel = '#found-log')" in js
          and "recheckReleases(picked, '#scanhistory-log')" in js, "")
    check("and a request re-check does too",
          "logSel = '#requests-log'" in js and "logSel: '#history-log'" in js, "")
    check("with somewhere on each page to report it",
          'id="scanhistory-log"' in shell and 'id="history-log"' in shell, "")
    scan_rerun = js[js.index("async function scanHistoryRerun"):]
    scan_rerun = scan_rerun[:scan_rerun.index("\n  }")]
    hist_rerun = js[js.index("async function historyRerun"):]
    hist_rerun = hist_rerun[:hist_rerun.index("\n  }")]
    check("and neither jumps to another sub-tab to do it",
          "showScanTab(" not in scan_rerun and "showRequestTab(" not in hist_rerun, "")

    # Days only, beside an Added column that had the units all along.
    check("the lookup history says which check it means",
          "label: 'Latest tracker check'," in js and "label: 'Days since lookup'," not in js, "")
    check("and says it in the same units as Added",
          js.count("value: (r) => daysAgo(r.checked_at),") == 2, "")

    # "Open" is an anchor styled as a button, so the button-to-button gap rule
    # never applied to it and it sat flush against Rename.
    css = pathlib.Path("lox/web/static/css/app.css").read_text(encoding="utf-8")
    check("every control in a row-actions cell is spaced",
          ".table td.row-actions > span {" in css
          and ".table td.row-actions button + button" not in css, "")

    # The user can see which trackers a row is missing from; the paragraph
    # saying that a row says so is not information.
    check("the queue does not narrate its own columns",
          "where it came from, and how" not in shell, "")

    # The number beside Queue was set by the queue page drawing itself, so it
    # only moved when that tab was open: a scan running elsewhere filled the
    # queue and the rail went on showing whatever it last said.
    check("the queue count is counted where every page can see it",
          "def _queue_rows(store: CheckerStore)" in web_api
          and "def queue_size(store: CheckerStore)" in web_api, "")
    check("and carried by the status poll",
          '"queue": {"size": queue_size(request.app["store"])},' in web_api, "")
    check("which is what sets the rail",
          "if (status.queue) railCount('#found-count-rail', status.queue.size);" in js, "")
    check("so it does not depend on the queue being on screen",
          "setInterval(refreshStatus, 15000)" in js, "")

    # Posting to one tracker and then remembering the other is a second pass
    # over the same release.
    check("every configured tracker is an upload target by default",
          "state.uploadTrackers = [...codes].sort(" in js
          and "state.uploadTrackers = [codes[0]];" not in js, "")
    check("with OPS running first", "const UPLOAD_ORDER = ['OPS', 'RED'];" in js, "")

    # A tag diff is a receipt, not a question. Open by default it pushed the
    # question actually being asked off the bottom of the screen.
    check("the tag diff starts collapsed",
          "const receipt = table.kind === 'tags' || table.kind === 'album_tags';" in js
          and "open: !receipt && table.rows.length <= 40" in js, "")

    # A download that fails partway leaves a real folder behind -- the folder
    # is made before the first track is fetched. Delete was shown only for
    # status "done", so that folder had no way off the page: Cancel had gone,
    # Delete never appeared, and Clear finished drops the row and leaves the
    # files.
    check("a failed download can have its folder deleted",
          "job.folder && job.status !== 'queued' && job.status !== 'running'" in js
          and "job.status === 'done' && job.folder" not in js, "")
    check("and the row goes with the folder",
          '_forget_download(request.app["downloader"], path)' in web_api, "")

    # An alias album id must not sink every private lookup after it.
    gw_py = pathlib.Path("lox/deezer/gw.py").read_text(encoding="utf-8")
    check("an album the gateway will not answer for is resolved once",
          "def _canonical_album_id" in gw_py and '"album::getData" not in str(e)' in gw_py, "")
    check("and the answer is remembered", "self._album_aliases" in gw_py, "")

    # A status poll can be in flight while a switch is clicked, carrying the
    # value from before it. Landing after the save it put the box back, so the
    # toast said "on" and the box was off.
    check("an upload switch is read back rather than assumed",
          "const stored = key === 'upload.dry_run'" in js, "")
    check("and a poll cannot undo a click it raced",
          "state.flagWrittenAt" in js and "SETTLE_MS" in js, "")

    # lox uploads music.
    check("podcast channels are not offered for browsing",
          "_PODCAST_RE" in pathlib.Path("lox/deezer/explore.py").read_text(encoding="utf-8"), "")
    check("and a channel borrows a genre picture where the names agree",
          "def _genre_picture" in pathlib.Path("lox/deezer/explore.py").read_text(encoding="utf-8"), "")

    # One frame, not three. Each track was its own dark rectangle inside the
    # panel's rectangle, with a hairline down the middle of every pair.
    spectral = css[css.index("/* ---------- spectrals ---------- */"):]
    check("the spectral panel has no rule of its own", "border: 0;" in spectral, "")
    check("nor any padding beside the images",
          "padding-right: 0;" in spectral and "padding-top: 0;" in spectral, "")
    check("and the full and the zoom meet exactly",
          re.search(r"\.spectral-zoom \{[^}]*margin-left: 0;", spectral) is not None, "")


def main() -> int:
    pipeline_checks()
    flow_checks()
    upload_priority_checks()
    blacklist_checks()
    request_verdict_checks()
    detail_cache_checks()
    naming_checks()
    ui_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
