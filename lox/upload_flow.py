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
# Metadata candidates print differently: "> 01 Artist - Title {Tracks: 14} | https://..."
_RESULT_LINE = re.compile(r"^>?\s*(\d{1,2})\s+(.+?)(?:\s*\|\s*(https?://\S+))?\s*$")
# Numbered menus, as used by downconversion: "  1. MP3 320"
_MENU_LINE = re.compile(r"^\s*(\d{1,2}|\*)\.\s+(\S.*?)\s*$")
# "(Options: ptpimg, catbox, ptscreens)" and "Your choices are OPS or [n]one."
_PAREN_OPTIONS = re.compile(r"\(Options:\s*([^)]+)\)", re.IGNORECASE)
_CHOICES_ARE = re.compile(r"choices are\s+([A-Za-z0-9]+(?:\s*(?:,|or)\s*[A-Za-z0-9]+)*)", re.IGNORECASE)
# A prompt that only wants acknowledgement.
_PRESS_ENTER = re.compile(r"press enter|once you are finished", re.IGNORECASE)
# Space-separated id lists, which are a multi-select wearing a text box.
_ID_LIST = re.compile(r"space-separated list of IDs", re.IGNORECASE)
# Tag diffs print as "  tracknumber          ••• 1/14 >>> 1", album tags as
# "> album         ••• Jiggy Buckaroo" with the arrow only when it changes.
_TAG_CHANGE = re.compile(r"^>?\s*(\S[^•]*?)\s+•{3}\s+(.*?)(?:\s+>>>\s+(.*))?$")
_FILE_HEADER = re.compile(r"^>\s*(\d{1,3}[.\-]?\s.*\.(?:flac|mp3|m4a|ogg))\s*$", re.IGNORECASE)
# Metadata blocks: "> TITLE         : Jiggy Buckaroo" and ">>>  Artist [main]".
_META_FIELD = re.compile(r"^>\s+([A-Z][A-Z /]*[A-Z])\s*:\s*(.*)$")
_META_ITEM = re.compile(r"^>>>\s+(\S.*)$")
_BLOCK_HEADS = {
    "proposed tag changes": ("tags", "Proposed tag changes"),
    "album tags (applied to all)": ("album_tags", "Album tags, applied to every file"),
    "previous metadata": ("previous", "Previous metadata"),
    "pending metadata": ("pending", "Pending metadata"),
}
_SPECTRAL_EXT = (".png", ".jpg", ".jpeg")


def strip_ansi(text: str) -> str:
    """Remove colour codes so prompt text can be shown as plain text."""
    return _ANSI.sub("", str(text or ""))


def parse_extra_options(text: str) -> list[dict[str, Any]]:
    """Recover options the pipeline states in prose rather than in brackets.

    Two shapes appear in real runs: "(Options: ptpimg, catbox, ...)" when a
    spectral upload fails, and "Your choices are OPS or [n]one" when offering
    another tracker. Both were falling through to a text box.

    Args:
        text: The prompt text.

    Returns:
        Option dicts, empty when neither shape is present.
    """
    found: list[dict[str, Any]] = []
    match = _PAREN_OPTIONS.search(text) or _CHOICES_ARE.search(text)
    if match:
        for raw in re.split(r"\s*(?:,|or)\s*", match[1]):
            name = raw.strip().strip(".")
            if name and not _BRACKET.match(name):
                found.append({"value": name, "label": name})
    return found


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
        self._candidates: list[dict[str, Any]] = []
        self._line = ""
        self._spectrals_ready = False
        # Structured blocks captured since the last question. The pipeline
        # prints a tag diff and a metadata comparison as prose; both are tables
        # and are far easier to check as tables.
        self._tables: list[dict[str, Any]] = []
        self._block: dict[str, Any] | None = None
        self._file = ""

    async def _confirm(self, text: str, default: bool = True, abort: bool = False, **_: Any) -> bool:
        """Stand-in for click.confirm."""
        prompt = strip_ansi(text).strip()
        debug.event("upload.prompt", kind="confirm", prompt=prompt[:80])
        tables, self._tables, self._block = self._tables, [], None
        answer = await self.flow.confirm(prompt, default=bool(default), tables=tables)
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
        found, self._candidates = self._candidates, []
        tables, self._tables, self._block = self._tables, [], None
        options = options or parse_extra_options(prompt)

        # "Press enter once you are finished viewing" wants acknowledgement,
        # not typing -- and it is the moment the spectrals are meant to be
        # looked at, so they belong on this question.
        if _PRESS_ENTER.search(prompt) and not options and not found:
            await self.flow.choose(
                prompt.split("[")[0].strip() or "Continue",
                [{"value": "", "label": "Continue"}],
                images=self._spectral_images(),
            )
            return default if default is not None else ""

        # A space-separated list of IDs is a multi-select wearing a text box.
        if _ID_LIST.search(prompt) and found:
            picked = await self.flow.choose_many(
                prompt.split("(")[0].strip().rstrip("?:,"),
                found,
                detail=prompt,
                default=[o["value"] for o in found],
            )
            return " ".join(picked) if picked else "0"

        if options or found:
            question = prompt.split("[")[0].strip().rstrip("?:,") or "Choose an option"
            chosen = await self.flow.choose(
                question,
                found + options,
                detail=prompt if prompt != question else "",
                default=default_letter(prompt) or (str(default).lower() if default else None),
                images=self._spectral_images() if self._wants_images(prompt) else [],
                tables=tables,
            )
            return chosen

        answer = await self.flow.text(
            prompt,
            default="" if default is None else str(default),
            images=self._spectral_images() if self._wants_images(prompt) else [],
            tables=tables,
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

        group = _GROUP_LINE.match(text)
        if group:
            group_id, description, url = group[1], group[2].strip(), group[3]
            self._candidates.append(
                {"value": url or group_id, "label": description[:90], "detail": url or f"group {group_id}"}
            )
        else:
            # Metadata results and numbered menus are offered the same way: the
            # pipeline wants the index back, so that is the option's value.
            result = _RESULT_LINE.match(text) if text.startswith(">") else None
            menu = _MENU_LINE.match(text)
            if result:
                index, description, url = result[1], result[2].strip(), result[3]
                self._candidates.append(
                    {"value": str(int(index)), "label": description[:90], "detail": url or ""}
                )
            elif menu and len(text) < 60:
                self._candidates.append({"value": menu[1], "label": menu[2][:60], "detail": ""})

        if "spectrals are available" in text.lower():
            self._spectrals_ready = True

        if self._capture_block(text):
            return
        self.flow.note(text)

    def _capture_block(self, text: str) -> bool:
        """Fold tag diffs and metadata listings into structured tables.

        Returns:
            True when the line belonged to a block and should not also be shown
            as a loose note.
        """
        head = _BLOCK_HEADS.get(text.rstrip(":").strip().lower())
        if head:
            kind, title = head
            self._block = {"kind": kind, "title": title, "rows": []}
            self._tables.append(self._block)
            self._file = ""
            return True

        if self._block is None:
            return False

        if not text:
            self._block = None
            return False

        if self._block["kind"] in ("tags", "album_tags"):
            header = _FILE_HEADER.match(text)
            if header:
                self._file = header[1].strip()
                return True
            change = _TAG_CHANGE.match(text)
            if change:
                field, before, after = change[1].strip(), change[2].strip(), change[3]
                self._block["rows"].append(
                    {"group": self._file, "label": field, "before": before,
                     "after": (after or "").strip(), "changed": after is not None}
                )
                return True
            return False

        field = _META_FIELD.match(text)
        if field:
            self._file = field[1].strip()
            value = field[2].strip()
            self._block["rows"].append({"group": "", "label": self._file, "before": value, "after": "",
                                        "changed": False})
            return True
        item = _META_ITEM.match(text)
        if item:
            self._block["rows"].append({"group": self._file, "label": "", "before": item[1].strip(),
                                        "after": "", "changed": False})
            return True
        return False

    def _wants_images(self, prompt: str) -> bool:
        """Whether this question is one the spectrals inform."""
        lowered = prompt.lower()
        return "spectral" in lowered or "lossy master" in lowered or self._spectrals_ready

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
