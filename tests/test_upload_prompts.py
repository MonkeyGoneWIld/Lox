"""Check that the upload's questions are actually asked.

``asyncclick.confirm`` is synchronous, so the pipeline calls it bare. The web UI
has to replace it with something awaited, and an un-awaited replacement still
returns an object that gets used as the answer -- so every question in the
upload answered itself with its default. The ones defaulting to "no" turned into
aborts with nothing on screen, and the ones defaulting to "yes" renamed files,
re-encoded a failed integrity check and posted the torrent without asking.

This drives the real functions with a confirm that can only be read by awaiting
it, which is what the UI's is.
"""

import ast
import asyncio
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_uploadprompts")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5099",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
        "LOX_TMP_DIR": os.path.join(BASE, "spectrals"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

import asyncclick as click  # noqa: E402

from lox import cfg  # noqa: E402
from lox.common.prompts import confirm as ask_confirm  # noqa: E402
from lox.errors import IntegrityCheckError  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


class OnlyAwaitable:
    """What the UI's confirm returns: readable only by awaiting it.

    Reading it any other way raises, which is how a call site that forgot to
    await gets caught here instead of in production.
    """

    def __init__(self, answer: bool, abort: bool, prompt: str, seen: list) -> None:
        self._answer, self._abort, self._prompt, self._seen = answer, abort, prompt, seen

    def __await__(self):
        async def ask():
            self._seen.append(self._prompt)
            if self._abort and not self._answer:
                raise click.Abort
            return self._answer

        return ask().__await__()

    def __bool__(self):
        raise AssertionError(f"confirm was used without awaiting it: {self._prompt[:70]}")


def patched_confirm(answer: bool, seen: list):
    def confirm(text="", default=True, abort=False, **_):
        return OnlyAwaitable(answer, abort, str(text), seen)

    return confirm


async def main() -> int:
    # --- every confirm in the upload path is awaited ------------------
    # A source scan, because a call site that is never reached by a test would
    # still be a question that answers itself in production.
    lox_root = pathlib.Path(__file__).resolve().parent.parent / "lox"
    unawaited = []

    class Scan(ast.NodeVisitor):
        def __init__(self, path):
            self.path, self.stack = path, []

        def visit_FunctionDef(self, node):
            self.stack.append(False)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node):
            self.stack.append(True)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            f = node.func
            in_async = bool(self.stack and self.stack[-1])
            if (isinstance(f, ast.Attribute) and f.attr in ("confirm", "edit")
                    and isinstance(f.value, ast.Name) and f.value.id == "click" and in_async):
                unawaited.append(f"{self.path.name}:{node.lineno} click.{f.attr}")
            self.generic_visit(node)

        def visit_Await(self, node):
            # The value of an await is fine however it is spelled.
            if isinstance(node.value, ast.Call):
                self.generic_visit_skipping(node.value)
            else:
                self.generic_visit(node)

        def generic_visit_skipping(self, call):
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                self.visit(arg)

    for path in sorted(lox_root.rglob("*.py")):
        # The bridge defines the replacements; review.py and metadata.py keep
        # click.edit for the terminal, and the web run replaces those functions
        # whole rather than the editor under them.
        if path.name in ("upload_flow.py", "prompts.py", "review.py", "metadata.py"):
            continue
        Scan(path).visit(ast.parse(path.read_text(encoding="utf-8")))
    check("no confirm or edit in async code goes un-awaited", not unawaited, "; ".join(unawaited))

    # --- the helper awaits what needs awaiting, and passes through ----
    seen: list[str] = []
    saved = click.confirm
    click.confirm = patched_confirm(True, seen)
    try:
        check("an awaitable answer is awaited", await ask_confirm("ready?") is True, "")
        check("and the question was actually asked", seen == ["ready?"], str(seen))
    finally:
        click.confirm = saved

    # The terminal's confirm returns a plain bool and must pass straight
    # through, so the CLI keeps working exactly as it did.
    click.confirm = lambda text="", **_: True
    try:
        check("a plain bool passes straight through", await ask_confirm("x") is True, "")
    finally:
        click.confirm = saved

    # --- the folder rename asks, and renames where it stands ----------
    from lox.tagger.foldername import rename_folder

    release = os.path.join(BASE, "links", "RED", "Artist - Album (2026) [WEB FLAC]")
    os.makedirs(release, exist_ok=True)
    pathlib.Path(release, "01. Track.flac").write_bytes(b"x")

    metadata = {
        "artists": [("Artist", "main"), ("Guest", "main")],
        "title": "Album", "year": "2026", "group_year": "2026", "scene": False,
        "format": "FLAC", "encoding": "Lossless", "encoding_vbr": False,
        "catno": "", "label": "", "edition_title": None, "source": "WEB",
    }
    seen.clear()
    click.confirm = patched_confirm(True, seen)
    was_yes_all, cfg.upload.yes_all = cfg.upload.yes_all, False
    try:
        new_path = await rename_folder(release, metadata, auto_rename=True)
    finally:
        click.confirm = saved
        cfg.upload.yes_all = was_yes_all

    check("the rename stays in the folder it was in",
          os.path.dirname(new_path) == os.path.dirname(release), new_path)
    check("which is the tracker's link directory, not the download directory",
          os.path.abspath(cfg.directory.download_directory) not in os.path.abspath(new_path),
          f"{new_path} vs {cfg.directory.download_directory}")
    check("the tracker directory still exists", os.path.isdir(os.path.dirname(new_path)),
          os.path.dirname(new_path))
    check("and the files came with it", os.path.isfile(os.path.join(new_path, "01. Track.flac")), new_path)

    # A release that really did arrive in a wrapper folder still gets tidied,
    # but a configured directory never does -- that was the one deleted in
    # production, taking the next upload's destination with it.
    from lox.tagger.foldername import _remove_empty_source_parent

    wrapper = os.path.join(BASE, "downloads", "wrapper")
    os.makedirs(wrapper, exist_ok=True)
    _remove_empty_source_parent(os.path.join(wrapper, "release"), os.path.join(BASE, "downloads", "release"))
    check("an emptied wrapper folder is tidied away", not os.path.exists(wrapper), wrapper)

    tracker_dir = os.path.dirname(new_path)
    _remove_empty_source_parent(os.path.join(tracker_dir, "x"), os.path.join(BASE, "elsewhere", "x"))
    check("a tracker's own directory is never removed", os.path.isdir(tracker_dir), tracker_dir)

    downloads = os.environ["LOX_DOWNLOAD_DIR"]
    _remove_empty_source_parent(os.path.join(downloads, "x"), os.path.join(BASE, "elsewhere", "x"))
    check("nor is the download directory", os.path.isdir(downloads), downloads)

    # --- a failed sanitize puts the file back -------------------------
    # lox.checks.__init__ pulls in the log and MQA checks, and those need av
    # and numpy, which are not built for every Python this has to run on. The
    # integrity module itself needs neither, so import it without running the
    # package __init__ rather than faking the audio stack.
    import types

    if "lox.checks" not in sys.modules:
        package = types.ModuleType("lox.checks")
        package.__path__ = [str(lox_root / "checks")]  # type: ignore[attr-defined]
        sys.modules["lox.checks"] = package
    import lox.checks.integrity as integrity

    track = os.path.join(BASE, "sanitize", "01. Track.flac")
    os.makedirs(os.path.dirname(track), exist_ok=True)
    pathlib.Path(track).write_bytes(b"original bytes")

    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"flac: ERROR"

    async def failing_run(*_a, **_k):
        return Failed()

    saved_run = integrity.anyio.run_process
    integrity.anyio.run_process = failing_run
    try:
        ok = await integrity._sanitize_flac(track)
    finally:
        integrity.anyio.run_process = saved_run

    check("a failed sanitize reports failure", ok is False, str(ok))
    check("the original track is still there", os.path.isfile(track), track)
    check("with its bytes intact", pathlib.Path(track).read_bytes() == b"original bytes", "")
    check("and no .corrupted leftover", not os.path.exists(track + ".corrupted"), "")

    # --- an empty folder says why instead of aborting silently --------
    empty = os.path.join(BASE, "empty")
    os.makedirs(empty, exist_ok=True)
    try:
        await integrity.check_integrity(empty)
        check("an empty folder raises", False, "nothing raised")
    except IntegrityCheckError as e:
        check("an empty folder says what is wrong", empty in str(e), str(e))
    except click.Abort:
        check("an empty folder says what is wrong", False, "still a bare Abort")

    missing = os.path.join(BASE, "gone")
    try:
        await integrity.check_integrity(missing)
        check("a missing folder raises", False, "nothing raised")
    except IntegrityCheckError as e:
        check("a missing folder names the path", missing in str(e), str(e))
    except click.Abort:
        check("a missing folder names the path", False, "still a bare Abort")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
