"""Run an upload in-process, turning every CLI prompt into a UI control.

The upload pipeline is upstream's and it is interactive: it calls
``asyncclick.confirm`` and ``asyncclick.prompt`` at a dozen points. The previous
build satisfied that by spawning the CLI and piping a terminal into the page.

Instead, this runs the pipeline inside the web process with click's prompt
functions redirected into a :class:`~lox.flow.Flow`. Each prompt becomes a typed
question the browser renders as buttons or a field, and the answer is fed back
as the prompt's return value. The pipeline is unmodified and does not know the
difference.

Prompt text is parsed to recover the shape of the question -- the bracket
notation upstream uses, ``[y]es, [N]o, [r]eopen`` -- so a wall of options becomes
labelled buttons rather than a box you have to type a letter into.
"""

import asyncio
import os
import re
from typing import Any
from urllib.parse import quote

import asyncclick as click

from lox import debug
from lox.flow import Flow

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Upstream writes choices as "[y]es, [N]o, [a]bort". The capitalised initial is
# the default.
_BRACKET = re.compile(r"\[([A-Za-z])\]([A-Za-z]*)")
# The dupe checker prints candidates as " 01 >> 1605624 | Artist - Title ... | https://..."
# with the id and the url on the same logical line. Capturing them turns
# "paste a URL" into a row of buttons.
_GROUP_LINE = re.compile(r"^\s*\d+\s*>>\s*(\d+)\s*\|\s*(.+?)(?:\s*\|\s*(https?://\S+))?\s*$")
_SPECTRAL_EXT = (".png", ".jpg", ".jpeg")


def strip_ansi(text: str) -> str:
    """Remove colour codes so prompt text can be shown as plain text."""
    return _ANSI.sub("", str(text or ""))


def parse_options(text: str) -> list[dict[str, Any]]:
    """Recover named options from a bracket-notation prompt.

    ``"[y]es, [N]o, [r]eopen spectrals"`` becomes three options whose values are
    the letters the pipeline expects and whose labels are the whole words.

    Args:
        text: The prompt text, already stripped of colour.

    Returns:
        Option dicts, empty when the prompt is not of that shape.
    """
    options: list[dict[str, Any]] = []
    for match in _BRACKET.finditer(text):
        letter, rest = match[1], match[2]
        label = (letter + rest).strip()
        options.append(
            {
                "value": letter.lower(),
                "label": label[:1].upper() + label[1:] if label else letter,
                "danger": label.lower() in ("abort", "delete", "delete music folder"),
            }
        )
    return options


def default_letter(text: str) -> str | None:
    """Return the capitalised bracket letter, which upstream means as default."""
    for match in _BRACKET.finditer(text):
        if match[1].isupper():
            return match[1].lower()
    return None


class FlowPrompts:
    """Redirects click's prompt functions into a flow for as long as it is active."""

    def __init__(self, flow: Flow, folder: str = "") -> None:
        """Initialize with the flow that questions should go to.

        Args:
            flow: Where questions and notes are published.
            folder: The release being uploaded, used to locate its spectrals.
        """
        self.flow = flow
        self.folder = folder
        self._saved: dict[str, Any] = {}
        # Group candidates printed since the last question, so the next prompt
        # can offer them instead of asking for a pasted URL.
        self._groups: list[dict[str, Any]] = []
        self._line = ""

    async def _confirm(self, text: str, default: bool = True, abort: bool = False, **_: Any) -> bool:
        """Stand-in for click.confirm."""
        prompt = strip_ansi(text).strip()
        debug.event("upload.prompt", kind="confirm", prompt=prompt[:80])
        answer = await self.flow.confirm(prompt, default=bool(default))
        if abort and not answer:
            raise click.Abort
        return answer

    async def _prompt(self, text: str, default: Any = None, **_: Any) -> Any:
        """Stand-in for click.prompt.

        A bracket-notation prompt becomes buttons; anything else becomes a text
        field, because sometimes free text really is the answer.
        """
        prompt = strip_ansi(text).strip()
        options = parse_options(prompt)
        debug.event("upload.prompt", kind="choice" if options else "text", prompt=prompt[:80])

        # Anything the pipeline listed just before asking becomes a real
        # option, so a found duplicate group is a button rather than a URL you
        # have to copy out of the log.
        found, self._groups = self._groups, []

        if options or found:
            question = prompt.split("[")[0].strip().rstrip("?:,") or "Choose an option"
            chosen = await self.flow.choose(
                question,
                found + options,
                detail=prompt if prompt != question else "",
                default=default_letter(prompt) or (str(default).lower() if default else None),
                images=self._spectral_images() if "spectral" in prompt.lower() else [],
            )
            return chosen

        answer = await self.flow.text(
            prompt,
            default="" if default is None else str(default),
            images=self._spectral_images() if "spectral" in prompt.lower() else [],
        )
        return answer if answer != "" else default

    def _echo(self, message: Any = "", nl: bool = True, **_: Any) -> None:
        """Stand-in for click.echo and click.secho.

        The pipeline builds some lines in pieces with ``nl=False``, so partial
        writes are buffered until the line ends. Completed lines become flow
        notes, and any that describe a duplicate group are also remembered as
        an option for the next question.
        """
        self._line += strip_ansi(message)
        if not nl:
            return
        text, self._line = self._line.strip(), ""
        if not text:
            return

        match = _GROUP_LINE.match(text)
        if match:
            group_id, description, url = match[1], match[2].strip(), match[3]
            self._groups.append(
                {
                    "value": url or group_id,
                    "label": description[:90],
                    "detail": url or f"group {group_id}",
                }
            )
        self.flow.note(text)

    def _spectral_images(self) -> list[str]:
        """URLs for the spectrals generated for this release, if any."""
        if not self.folder:
            return []
        from lox.uploader.spectrals import get_spectrals_path

        directory = get_spectrals_path(self.folder)
        if not os.path.isdir(directory):
            return []
        names = sorted(f for f in os.listdir(directory) if f.lower().endswith(_SPECTRAL_EXT))
        return [f"/spectral-image/{quote(os.path.basename(directory))}/{quote(n)}" for n in names]

    def __enter__(self) -> "FlowPrompts":
        """Patch click's prompt functions."""
        for name, replacement in (
            ("confirm", self._confirm),
            ("prompt", self._prompt),
            ("echo", self._echo),
            ("secho", self._echo),
        ):
            self._saved[name] = getattr(click, name)
            setattr(click, name, replacement)
        return self

    def __exit__(self, *_exc: object) -> None:
        """Restore click's prompt functions."""
        for name, original in self._saved.items():
            setattr(click, name, original)
        self._saved.clear()


async def run_upload(
    flow: Flow,
    folder: str,
    tracker: str,
    *,
    source: str = "WEB",
    auto_rename: bool = True,
) -> dict[str, Any]:
    """Upload one folder to one tracker, asking the browser as it goes.

    Args:
        flow: The flow questions and progress are published to.
        folder: Release folder to upload.
        tracker: Tracker code.
        source: Media source.
        auto_rename: Rename files and folders without asking.

    Returns:
        A summary of what happened.

    Raises:
        Exception: Whatever the pipeline raises, for the registry to record.
    """
    import lox.trackers
    from lox.uploader import upload as run_pipeline

    flow.progress(f"Preparing {tracker}")
    flow.note(f"Uploading {folder} to {tracker}")
    debug.log("upload start tracker=%s folder=%s", tracker, folder, level=20)

    gazelle = lox.trackers.get_class(tracker)()
    with FlowPrompts(flow, folder):
        await run_pipeline(
            gazelle,
            folder,
            None,
            source,
            None,
            (),
            None,
            auto_rename=auto_rename,
        )

    flow.progress(f"Finished {tracker}", 100.0)
    debug.log("upload finished tracker=%s folder=%s", tracker, folder, level=20)
    return {"tracker": tracker, "folder": folder}


async def run_uploads(
    flow: Flow,
    folder: str,
    trackers: list[str],
    *,
    source: str = "WEB",
    auto_rename: bool = True,
) -> dict[str, Any]:
    """Upload one folder to several trackers, hardlinking per tracker first.

    Args:
        flow: The flow to drive.
        folder: Release folder.
        trackers: Tracker codes.
        source: Media source.
        auto_rename: Rename without asking.

    Returns:
        Per-tracker outcomes.
    """
    from lox import cfg
    from lox.seeding.links import LinkError, link_release

    outcomes: list[dict[str, Any]] = []
    for tracker in trackers:
        target = folder
        if cfg.linking.enabled:
            flow.progress(f"Linking for {tracker}")
            try:
                link = await _to_thread(link_release, folder, tracker)
                target = link.destination
                flow.note(f"{tracker}: {'reused' if link.reused else link.method} {link.files} file(s)")
            except LinkError as e:
                flow.note(f"{tracker}: linking failed — {e}", "error")
                outcomes.append({"tracker": tracker, "ok": False, "error": str(e)})
                continue

        try:
            await run_upload(flow, target, tracker, source=source, auto_rename=auto_rename)
            outcomes.append({"tracker": tracker, "ok": True})
        except click.Abort:
            flow.note(f"{tracker}: aborted", "warning")
            outcomes.append({"tracker": tracker, "ok": False, "error": "aborted"})
        except Exception as e:  # noqa: BLE001 - one tracker failing must not stop the rest
            flow.note(f"{tracker}: {type(e).__name__}: {e}", "error")
            outcomes.append({"tracker": tracker, "ok": False, "error": str(e)})

    succeeded = [o["tracker"] for o in outcomes if o["ok"]]
    return {
        "folder": folder,
        "outcomes": outcomes,
        "succeeded": succeeded,
        "dry_run": _dry_run(),
    }


def _dry_run() -> bool:
    """Whether this run was a dry run, read at report time."""
    from lox import cfg

    return bool(cfg.upload.dry_run)


async def _to_thread(func, *args):
    """Run a blocking call off the event loop."""
    return await asyncio.to_thread(func, *args)
