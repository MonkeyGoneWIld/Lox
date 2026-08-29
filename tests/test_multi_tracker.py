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
    css = pathlib.Path("lox/web/static/css/app.css").read_text(encoding="utf-8")
    up_src = pathlib.Path("lox/uploader/__init__.py").read_text(encoding="utf-8")
    up_src = pathlib.Path("lox/uploader/__init__.py").read_text(encoding="utf-8")
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

    # A choice column says several things about a row -- "RED missing", "OPS
    # has it", "already on tracker" -- and the filter offered whole joined
    # values, so asking for one fact meant finding a row whose entire verdict
    # read exactly that. "already on tracker" was drawn as a tag and never put
    # in the value, so it could not be asked for at all.
    check("a choice filter is a set of tick boxes",
          "function choiceParts(column, row)" in js and "th-choicebox" in js, "")
    check("over the individual facts, not the joined value",
          "function trackerParts(row)" in js and js.count("parts:") >= 5, str(js.count("parts:")))
    check("including the one that was only ever a tag",
          "parts.push('already on tracker')" in js, "")
    check("and ticking every box reads as all, not as seven of seven",
          "next.size === options.length ? [] : [...next]" in js, "")

    # The queue was the one list that would not say how big a release is, and
    # deciding about one row meant travelling to the toolbar to do it.
    check("the queue says how many tracks a release has",
          "label: 'Tracks'," in js and "Number(f.deezer_tracks) || 0" in js, "")
    check("and a row can be removed or blocklisted where it sits",
          "dismissRows([f], false)" in js and "dismissRows([f], true)" in js, "")

    # Switching tab in place left the address behind, so the tab buttons
    # pointed at where you already were and pressing one did nothing.
    check("the address keeps up with the tab on screen",
          "function keepAddress(path)" in js
          and js.count("keepAddress(") >= 3, str(js.count("keepAddress(")))

    # A run that finds three fillable requests out of two hundred said
    # "200 checked" and left the three to be found by scrolling. Re-ordering
    # the results was not enough: they are still in among the two hundred, and
    # the point is to look at them before anything is downloaded.
    check("what a check queued gets its own table",
          "function renderQueued()" in js and 'id="requests-queued-panel"' in shell, "")
    check("with the release, the request it fills and how confident the match was",
          "label: 'Fills'," in js and "label: 'Confidence'," in js, "")
    check("and a way to disagree on every row",
          "dismissRows([queueRowFor(r)], false)" in js
          and "dismissRows([queueRowFor(r)], true)" in js, "")
    # Drawn from every result that lands, not from one caller finishing. A
    # search checks each page as it arrives through runCheckJob, which is a
    # different path from the one a pasted list of ids takes, and only the
    # second was drawing this -- so the run that most needs the table was the
    # only run that never got it. Both land in applyRequestResult.
    applied = js[js.index("function applyRequestResult(match) {"):]
    applied = applied[:applied.index(chr(10) + "  }")]
    check("filled in as each match lands, whichever way the check was started",
          "renderQueued();" in applied, "")
    # And NOT reset per run: a page-by-page search is a run per page, so
    # clearing between them would empty the table on every page.
    check("and not emptied between the pages of one search",
          "$('#requests-queued')?.replaceChildren()" not in js, "")
    check("while a refused match leaves it",
          "state.requestResults.delete(resultKey(" in js, "")
    # Behaviour, not shape: tests/test_queued_panel.js runs the real functions.
    check("with the wiring itself exercised rather than read",
          "test_queued_panel.js" in pathlib.Path(".github/workflows/lint.yml").read_text(
              encoding="utf-8"), "")

    # The blacklist said a name and a date, which is most of the way to being
    # the column of Deezer ids it used to be. Deciding whether refusing a
    # release was right takes the same facts the queue shows about it.
    check("the blacklist carries what is known about a release",
          "def _release_facts(store: CheckerStore)" in web_api, "")
    check("read live where the record survives, and from the entry where it does not",
          'live = _release_facts(store)' in web_api
          and '**live.get(str(key), {})' in web_api, "")
    blacklist = js[js.index("name: 'blacklist',"):]
    blacklist = blacklist[:blacklist.index("}));")]
    for column in ("Year", "Tracks", "Trackers", "Source", "Refused"):
        check(f"and shows {column.lower()}", f"label: '{column}'," in blacklist, "")

    # The filter menu was written against two variables this stylesheet does
    # not have, so it had no ground and no edge and the table showed through.
    check("no rule reaches for a variable that does not exist",
          not any(f"var(--{name})" in css
                  for name in ("panel", "line", "hover", "input-bg")), "")
    check("the menu has a ground of its own",
          ".th-choices {" in css and "background: var(--bg-raised);" in css, "")
    check("and says it opens", ".th-choicebox > summary::after" in css, "")
    check("and says when it is narrowing something",
          "th-choicebox-on" in css and "th-choicebox-on" in js, "")
    check("without the header's uppercase reaching the values inside it",
          css.count("text-transform: none;") >= 3, str(css.count("text-transform: none;")))

    # A saved-search list grows without limit and sits between the scan box and
    # its own results.
    check("the saved searches can be folded away",
          'class="panel panel-fold"' in shell and ".panel-fold > summary" in css, "")
    check("and the fold is remembered",
          "function rememberFold(id)" in js and "rememberFold('watchlist-panel')" in js, "")

    # "not checked" was said of an album the sweep passed over and of one still
    # waiting behind the tracker, which are different facts.
    check("a skipped album says why it was skipped",
          "skipped_reason" in js and "'not looked up'" in js, "")
    check("and one still to be checked says that instead",
          "'waiting')" in js, "")
    check("without the sweep then spending a call on it",
          "state.candidates.filter((c) => !c.skipped_reason)" in js, "")

    # Refusing a match, from the list and from the side-by-side view where the
    # two are actually being compared.
    check("a match can be refused", "async function rejectMatch(row)" in js, "")
    check("from the list", "'Not this'" in js, "")
    check("and from the view that puts the two side by side",
          "'Not this release'" in js, "")
    check("with an endpoint that keeps the request open",
          '@routes.post("/api/requests/reject")' in web_api
          and '"deezer_id": None,' in web_api, "")

    # A download that fails for a reason that does not last.
    check("a failed download can be tried again",
          '@routes.post("/api/downloads/{job_id}/retry")' in web_api
          and "'Retry')" in js, "")
    check("starting from scratch rather than into the hole it left",
          "resolve_release_path(request.app, job.folder)" in web_api, "")

    # The upload card knows what it is for and what is behind it.
    check("an upload that fills a request links to it",
          "def _upload_context" in web_api and "context.request_url" in js, "")
    check("and says how many uploads are still waiting",
          "more waiting" in js, "")
    check("without breaking the rail's count of what needs you",
          "head.dataset.state = flow.state;" in js, "")

    # The metadata form pointed at nothing when the validator refused.
    flow_src = pathlib.Path("lox/upload_flow.py").read_text(encoding="utf-8")
    check("a refused field is named rather than left to be found",
          "_PROBLEM_FIELDS" in flow_src and "meta-form-bad" in js, "")
    check("and scrolled to, because the form is taller than the screen",
          "scrollIntoView({ block: 'center'" in js, "")

    # The placeholder Deezer credits a compilation to is not a person.
    deezer_src = pathlib.Path("lox/tagger/sources/deezer.py").read_text(encoding="utf-8")
    check("Various Artists is never written in as a credit",
          "_VARIOUS" in deezer_src and "def _is_various" in deezer_src, "")

    # A list printed for a question that never gets asked must not become the
    # buttons on the next one.
    check("an offered answer belongs to the question it was printed for",
          "def _for_this_question" in flow_src, "")
    check("so a request cannot be offered as a group to post into",
          "_GROUP_QUESTION" in flow_src and "_REQUEST_QUESTION" in flow_src, "")
    fill_src = pathlib.Path("lox/uploader/request_fill.py").read_text(encoding="utf-8")
    # Auto-answering fills the request this release was already matched to, and
    # nothing else. Filling any single search hit decided on a default the one
    # thing worth refusing; filling nothing made the setting a way to turn
    # request filling off.
    check("and auto-answering fills only what was already matched",
          "if cfg.upload.yes_all and linked_request_id:" in fill_src, "")
    check("asked for before anything is searched",
          fill_src.index("linked_request_id:") < fill_src.index("get_request_results("), "")
    check("and confirmed against the tracker, in case it has gone since",
          "no longer there; asking instead" in fill_src, "")

    # The pairing is per tracker, because a request lives on one.
    check("the pipeline is told which request belongs to which tracker",
          "linked_request_id=(linked_requests or {}).get(tracker)" in up_src, "")
    check("and says which one it filled",
          "on_request(tracker, int(request_id))" in up_src, "")
    check("so the card can name it, filled or merely linked",
          "function flowRequestTags(context)" in js and "filled_requests" in js, "")
    check("and the result links it per tracker",
          "function requestHref(code, id)" in js and "`filled #${o.request_id}`" in js, "")
    check("with a manual upload finding the pairing by folder name",
          "def _linked_requests(store: CheckerStore, album_id: str, folder: str)" in web_api
          and "title not in basename or artist not in basename" in web_api, "")
    check("and the context in place before the run starts",
          "context=context," in web_api and "flow.context = dict(context or {})" in
          pathlib.Path("lox/flow.py").read_text(encoding="utf-8"), "")

    # A genre removed came straight back, and the record validated on it.
    check("an emptied genre list is empty",
          'metadata[key] = [str(v).strip() for v in answer[key] if str(v).strip()]' in flow_src
          and 'if values or key == "urls":' not in flow_src, "")

    # The lossy report asked for a comment with an empty box, for a question
    # whose answer lox already knows.
    spectral_src = pathlib.Path("lox/uploader/spectrals.py").read_text(encoding="utf-8")
    check("the lossy report starts from where the release came from",
          "def lossy_comment_default" in spectral_src
          and "default=lossy_comment_default(source_url, download_url)" in spectral_src, "")
    check("which is what lox downloaded, not what the metadata matched",
          'download_url=context.get("deezer_url")' in web_api
          and '"deezer_url"' in web_api, "")

    # Five albums at once meant five times five streams open to Deezer.
    dl_src = pathlib.Path("lox/deezer/download.py").read_text(encoding="utf-8")
    check("the cap on track downloads is shared by every album",
          "def _stream_slots(self)" in dl_src
          and "semaphore = self._stream_slots()" in dl_src, "")
    check("and a slow track fails on its own rather than taking the album",
          "OSError, TimeoutError) as e:" in dl_src, "")
    check("saying what went wrong, not only how many",
          'job.error += f" -- {reasons[0]}"' in dl_src, "")

    # A path comparison is the fragile half: the downloader strips a trailing
    # dot from a title, and the pipeline renames the folder mid-upload.
    check("the download is forgotten by album, not only by path",
          'if album_id and str(job.album_id) == str(album_id):' in web_api, "")

    # The history's Trackers column shows what an upload did, and filling a
    # request is one of the things it does.
    check("the history's tracker tags name the request that was filled",
          "`filled #${o.request_id}`" in js and "o.request_url || requestHref(" in js, "")
    check("and it can be filtered for like any other fact",
          "function uploadOutcomeFacts(row)" in js
          and "${o.tracker} filled a request" in js, "")

    # "Not checked" was said of a request the run deliberately passed over and
    # of one it never reached. Those are different facts and only one of them
    # is an answer -- the same thing the scan's results said, fixed there and
    # not here, because the two lists keep their skips in different places.
    check("a request the run passed over says why",
          "requestSkipped" in js and "const requestSkipReason = (row) =>" in js, "")
    check("and what was already known about it",
          "el('span', { class: 'tag dim' }, 'not looked up')," in js, "")
    check("while one still to be checked says that instead",
          "el('span', { class: 'tag dim' }, 'waiting')" in js, "")
    check("with the reasons belonging to the run that produced them",
          "state.requestSkipped.clear();" in js, "")

    # A details element does not close when you click past it.
    check("the filter menu closes when you click away",
          "$$('.th-choicebox[open]').forEach" in js and "box.contains(e.target)" in js, "")

    # The queue page's batch never touched the Uploading tab's own queue.
    check("the card counts both ways of queueing an upload",
          "state.uploadQueue.length + (state.uploadsPending || 0)" in js
          and "uploadsPending(usable.length - index - 1)" in js, "")

    # The folder is renamed mid-run, so the path the run started with is not
    # the one on disk when it ends.
    check("the tidy-up removes the folder that is really there",
          '"source_folder": source_folder,' in flow_src
          and 'result.get("source_folder") or folder' in flow_src, "")
    check("and so does everything else that runs after an upload",
          'final = result.get("source_folder") or folder' in web_api, "")

    # A sweep that re-checked every queued release paid a tracker call apiece
    # for the newest answers it had.
    recheck_src = pathlib.Path("lox/checker/recheck.py").read_text(encoding="utf-8")
    check("a sweep trusts the answer it already has",
          "confirming: bool = False" in recheck_src
          and "if missing and confirming:" in recheck_src, "")

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


def history_checks() -> None:
    """What an upload did is kept, including the request it answered.

    The upload history exists because an upload cannot be run again to find out
    what it did -- and it recorded only the torrent. A release posted to fill a
    request answered a second page, and the one permanent record of the run
    could not say whether that had happened or where to look.
    """
    import types

    import lox.trackers
    import lox.web.api as api
    from lox.checker.store import CheckerStore

    check("a request has an address, like a group and an artist do",
          lox.trackers.request_url("OPS", 76397).endswith(
              "/requests.php?action=view&id=76397"),
          lox.trackers.request_url("OPS", 76397))
    check("and nothing is invented for a tracker lox does not know",
          lox.trackers.request_url("ZZZ", 1) == "", "")
    check("nor for an upload that filled no request",
          lox.trackers.request_url("OPS", None) == "", "")

    store = CheckerStore(os.path.join(BASE, "state-history"))
    flow = types.SimpleNamespace(id="abc123", created=1.0, state="done", error=None, events=[])
    api._record_upload(  # noqa: SLF001 - the thing under test
        store, flow, "/downloads/X - Y (2026) [WEB FLAC]", ["OPS", "RED"], "111",
        {
            "succeeded": ["OPS"],
            "outcomes": [
                {"tracker": "OPS", "ok": True, "url": "https://orpheus.network/torrents.php?torrentid=9",
                 "folder": "/seed/OPS/X", "request_id": 76397},
                {"tracker": "RED", "ok": False, "error": "did not reach this tracker"},
            ],
        },
    )
    kept = (store.load(api.UPLOADS) or {})["abc123"]["outcomes"]
    posted = kept[0]
    check("the history keeps which request a post filled",
          posted.get("request_id") == 76397, str(posted.get("request_id")))
    # Stored rather than built when the page is drawn: this is a permanent
    # record, and a tracker later removed from the config would otherwise take
    # its own links with it.
    check("and where that request is, on the record itself",
          str(posted.get("request_url", "")).endswith("id=76397"), str(posted.get("request_url")))
    check("a post that filled nothing claims nothing",
          kept[1].get("request_id") is None, str(kept[1].get("request_id")))
    check("and the torrent it posted is still there",
          posted.get("url", "").endswith("torrentid=9"), str(posted.get("url")))


def main() -> int:
    pipeline_checks()
    flow_checks()
    upload_priority_checks()
    blacklist_checks()
    request_verdict_checks()
    detail_cache_checks()
    naming_checks()
    history_checks()
    ui_checks()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
