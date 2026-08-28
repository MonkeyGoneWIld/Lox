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
import contextlib
import copy
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import asyncclick as click

from lox import debug
from lox.flow import Flow, Step

if TYPE_CHECKING:
    from collections.abc import Iterator

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Upstream writes choices as "[y]es, [N]o, [a]bort". The capitalised initial is
# the default.
_BRACKET = re.compile(r"([A-Za-z]*)\[([A-Za-z])\]([A-Za-z]*)")
# The dupe checker prints candidates as " 01 >> 1605624 | Artist - Title ... | https://..."
# with the id and the url on the same logical line. Capturing them turns
# "paste a URL" into a row of buttons.
_GROUP_LINE = re.compile(r"^\s*\d+\s*>>\s*(\d+)\s*\|\s*(.+?)(?:\s*\|\s*(https?://\S+))?\s*$")
# Open requests print with the URL where a group prints its id:
# "01 >> https://site/requests.php?action=view&id=8811 | Artist - Title (2023) [Album]"
# so _GROUP_LINE never matched one and every request the pipeline found was
# written to the log and offered as nothing. The question says "choose from
# results" -- there have to be results to choose from.
_REQUEST_LINE = re.compile(
    r"^\s*\d+\s*>>\s*(https?://\S*requests\.php\S*)\s*\|\s*(.+?)\s*$", re.IGNORECASE
)
# The line under a request, listing what it will accept.
_REQUIREMENTS = re.compile(r"^Requirements:\s*(.+?)\s*/?\s*$", re.IGNORECASE)
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
_SPECTRAL_IDS = re.compile(r"spectral IDs would you like to upload", re.IGNORECASE)
_OPTION_TAIL = re.compile(r"((?: [a-z][a-z'-]*)+)")
"""The rest of a multi-word option label, e.g. the " type" of "[r]elease type"."""

_BARE_LETTERS = {"a": "Abort", "n": "No", "y": "Yes", "d": "Delete music folder", "m": "Manual"}
"""What the pipeline means by a bracket with no word after it. Without these the
button is labelled with the bare letter, which tells you nothing."""
_DOWNCONVERT = re.compile(r"select formats to convert", re.IGNORECASE)
# A prompt that names pasting a URL as one of its answers.
_PASTE_URL = re.compile(r"paste a url|paste urls", re.IGNORECASE)
# The one question that already loops back to the metadata when answered no.
_RERUN_METADATA = re.compile(r"re-run metadata section", re.IGNORECASE)
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
    "torrents in this group": ("torrents", "Torrents already in this group"),
    # Without this the "old >>> new" lines look like numbered result rows and
    # became option buttons on whatever question came next, which is how a
    # rename ended up as a row of filenames beside "upload to an existing
    # group?".
    "proposed filename changes": ("renames", "Proposed filename changes"),
    "proposed folder name": ("renames", "Proposed folder name"),
    # The folder rename prints the old and new names, then asks whether to go
    # ahead. Without this they were loose log lines, so the question arrived
    # with nothing on screen saying what it would rename the folder to.
    "renaming folder": ("folder", "Folder rename"),
    # The dry run prints the whole payload it would have posted. That is the
    # answer to "what would this have done", so it is kept rather than scrolled
    # past in the log.
    "would have posted": ("dryrun", "What would have been posted"),
    # Which request is about to be filled. "Are you sure you would like to fill
    # this request" was asked with the request itself scattered into the log,
    # which is collapsed -- so the question had no subject on screen.
    "selected request": ("request", "The request this would fill"),
}

#: "Bounty: 10.00 GiB" -- the shape _print_request_details writes.
_LABELLED = re.compile(r"^([A-Z][A-Za-z ]{1,24}):\s+(.+?)\s*$")
# "title                        Sammaouny". Matched after the line has been
# stripped -- it is indented in the pipeline's output, but _echo strips every
# line before this sees it, so requiring the indent here matched nothing and
# the dry-run table came out empty every time.
_DRY_FIELD = re.compile(r"^(\S[\S ]*?)\s{2,}(.+?)\s*$")
# "Old folder name        : Artist - Album (2026) [WEB FLAC]"
_FOLDER_NAME = re.compile(r"^(Old|New pending)\s+folder name\s*:\s*(.+?)\s*$", re.IGNORECASE)
# "[DRY RUN] album description that would have been posted:"
_DESCRIPTION_HEAD = re.compile(r"^\[DRY RUN\]\s+(.+?)\s+that would have been posted", re.IGNORECASE)
_RENAME_LINE = re.compile(r"^(?P<old>.+?)\s+>>>\s+(?P<new>.+?)\s*$")
# "> 2025 / Deluxe / 602488195980 / WEB / AAC / 256" -- year, then any number of
# edition and catalogue-number parts, then media, format and encoding. The three
# that matter for deciding whether your upload duplicates one of these are the
# last three, so they are pulled out as columns rather than left in a sentence.
_TORRENT_LINE = re.compile(r"^>\s*(\d{4})\s*/\s*(.+)$")
# The pipeline announces the group it is about to use before listing its
# contents: "Selected ID: 2617840 | Taylor Swift - The Life of a Showgirl (2025)".
_SELECTED_GROUP = re.compile(r"^Selected ID:\s*(\d+)\s*\|\s*(.+?)\s*$", re.IGNORECASE)
_SPECTRAL_EXT = (".png", ".jpg", ".jpeg")

_STAGES: tuple[tuple[str, str], ...] = (
    # Longest-lived phase last, and each needle has to be specific: "checking
    # for mqa" also matched the dupe check's own output, so the whole
    # existing-group search reported itself as an MQA check.
    ("checking for mqa release", "Checking for MQA (a lossy format some releases are flagged as)"),
    ("no mqa release detected", "Looking for an existing group"),
    ("results matching this release", "Looking for an existing group"),
    ("searching for", "Searching for metadata"),
    ("checking metadata", "Checking metadata"),
    ("checking lossy master", "Generating spectrals"),
    ("finished generating spectrals", "Compressing spectrals"),
    ("finished compressing spectrals", "Waiting on the spectral check"),
    ("uploading to a new torrent group", "Preparing a new group"),
    ("selected id", "Using an existing group"),
    ("proposed tag changes", "Reviewing tags"),
    ("proposed filename changes", "Reviewing filenames"),
    ("checking folder structure", "Checking the folder"),
    ("generating torrent file", "Making the torrent"),
    ("uploading torrent", "Uploading the torrent"),
    ("processing:", "Transcoding"),
    ("transcode completed", "Transcoded"),
    ("generated description", "Writing the description"),
    ("no requests were found", "Checking requests"),
    ("adding to", "Handing to the download client"),
    ("done uploading this release", "Done"),
)
"""What the pipeline says as it enters each phase, and what to call it. Matched
against the line it just printed, longest-lived phase last."""


def _spectrals_module() -> Any:
    """The pipeline's spectral module, imported on demand.

    It drags in the audio stack, so importing it at module scope would make the
    web app unimportable anywhere those wheels are missing.
    """
    import lox.uploader.spectrals as module

    return module


def _review_module() -> Any:
    """The pipeline's metadata review module, imported on demand."""
    import lox.tagger.review as module

    return module


def _metadata_module() -> Any:
    """The pipeline's metadata-gathering module, imported on demand."""
    import lox.tagger.metadata as module

    return module


def _modules_using(name: str) -> list[Any]:
    """Every already-imported lox module holding a reference called ``name``.

    ``from x import y`` copies the reference into the importer, so replacing it
    on the module that defines it leaves every caller pointing at the original.
    """
    import sys

    return [
        module
        for module_name, module in list(sys.modules.items())
        if module_name.startswith("lox.") and module is not None and hasattr(module, name)
    ]


def strip_ansi(text: str) -> str:
    """Remove colour codes so prompt text can be shown as plain text."""
    return _ANSI.sub("", str(text or ""))


ARTIST_ROLES = ("main", "guest", "composer", "conductor", "dj", "remixer", "producer", "arranger")
"""Roles a credit can carry, in the order the trackers list them."""

_ARTIST_LINE = re.compile(r"^(?P<name>.+?)\s*\((?P<role>[^()]+)\)\s*$")
_ALIAS_MARKER = "Refer to README for syntax"

_EDIT_TITLES = {
    "artists": "Artists",
    "aliases": "Artist aliases",
    "title": "Title",
    "list": "Values",
    "json": "Tracks",
    "text": "Edit",
}
_EDIT_HELP = {
    "artists": "Add, remove or re-credit. Roles are the ones the trackers accept.",
    "aliases": "Rename an artist for this upload, or leave the new name blank to drop the credit.",
    "list": "One per line. Blank rows are dropped.",
    "json": "Edited as JSON because the track map has no simpler shape.",
}


_RESULT_YEAR = re.compile(r"\((\d{4})\)")
_RESULT_TRACKS = re.compile(r"\{Tracks:\s*(\d+)\}", re.IGNORECASE)


def _result_detail(description: str, url: str | None) -> str:
    """The facts worth seeing under a metadata candidate.

    The pipeline writes them into the line -- "(2026)", "{Tracks: 18}" -- where
    they are easy to miss. Pulled out, they are what tells one edition of a
    release from another.
    """
    parts = []
    year = _RESULT_YEAR.search(description)
    if year:
        parts.append(year[1])
    tracks = _RESULT_TRACKS.search(description)
    if tracks:
        parts.append(f"{tracks[1]} tracks")
    if url:
        parts.append(url.split("/")[-1] if "/" in url else url)
    return " · ".join(parts)


def _editor_shape(text: str, extension: str) -> tuple[str, list[dict[str, Any]]]:
    """Work out what kind of thing is being edited, and its current contents.

    Args:
        text: What the pipeline handed to the editor.
        extension: The temp-file extension it asked for.

    Returns:
        A shape name and the rows the UI should render.
    """
    if extension == ".json":
        return "json", [{"value": text}]

    if _ALIAS_MARKER in text:
        names = [n.strip() for n in text.split(_ALIAS_MARKER)[0].split("\n") if n.strip()]
        return "aliases", [{"name": n, "alias": ""} for n in names]

    lines = [line for line in text.split("\n") if line.strip()]
    if lines and all(_ARTIST_LINE.match(line) for line in lines):
        rows = []
        for line in lines:
            match = _ARTIST_LINE.match(line)
            assert match is not None  # every line matched above
            rows.append({"name": match["name"].strip(), "role": match["role"].strip().lower()})
        return "artists", rows

    if len(lines) <= 1 and "\n" not in text.strip():
        return "title", [{"value": text.strip()}]

    return "list", [{"value": line.strip()} for line in lines]


def _editor_text(shape: str, answer: Any, original: str) -> str:
    """Turn the form's answer back into the text the pipeline parses."""
    if shape in ("json", "title", "text"):
        return str(answer if answer is not None else original)

    rows = answer if isinstance(answer, list) else []
    if shape == "artists":
        return "\n".join(
            f"{r.get('name', '').strip()} ({(r.get('role') or 'main').strip().lower()})"
            for r in rows
            if r.get("name", "").strip()
        )
    if shape == "aliases":
        # The parser splits on the marker and reads "existing --> new" lines.
        pairs = "\n".join(
            f"{r.get('name', '').strip()} --> {r.get('alias', '').strip()}"
            for r in rows
            if r.get("name", "").strip() and (r.get("alias", "").strip() or r.get("drop"))
        )
        return f"{_ALIAS_MARKER}\n\n{pairs}"
    return "\n".join(str(r.get("value", "")).strip() for r in rows if str(r.get("value", "")).strip())


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
        head, letter, rest = match[1], match[2], match[3]
        # The bracket often sits inside a word rather than in front of it --
        # "artist a[l]iases", "trac[k]s" -- so the letters before it are part of
        # the label too. Without them these read "Liases" and "Ks".
        label = (head + letter + rest).strip()

        # The word after the bracket is often only the first of several --
        # "[r]elease type", "[d]elete music folder" -- and the rest of it sits
        # after the match, up to the comma or slash that ends the option.
        tail = _OPTION_TAIL.match(text, match.end())
        if tail and label:
            label = f"{label}{tail[1].rstrip()}"

        # A bare "[a]" carries no word at all. Left as-is it becomes a button
        # labelled "A", which says nothing about what pressing it does.
        if not rest:
            label = _BARE_LETTERS.get(letter.lower(), label)

        # "Reopen spectrals" reran the terminal viewer. The images are on the
        # page already, so the button did nothing you could see.
        if "reopen" in label.lower():
            continue

        options.append(
            {
                "value": letter.lower(),
                "label": label[:1].upper() + label[1:] if label else letter,
                "danger": any(word in label.lower() for word in ("abort", "delete")),
            }
        )
    return options


def default_letter(text: str) -> str | None:
    """Return the capitalised bracket letter, which upstream means as default."""
    for match in _BRACKET.finditer(text):
        if match[2].isupper():
            return match[2].lower()
    return None


class _Answer:
    """A confirm result that survives not being awaited.

    See :meth:`FlowPrompts._confirm`. ``bool()`` on an un-awaited coroutine is
    always True, which is why this exists rather than an ``async def``.
    """

    __slots__ = ("_ask", "_default", "_prompt")

    def __init__(self, ask: Any, default: bool, prompt: str) -> None:
        """Initialize with the question and the answer to assume without one."""
        self._ask = ask
        self._default = default
        self._prompt = prompt

    def __await__(self) -> Any:
        """Ask the browser and yield its answer."""
        return self._ask().__await__()

    def __bool__(self) -> bool:
        """The default, for a caller that never awaited."""
        debug.log("confirm not awaited, assuming %s: %s", self._default, self._prompt[:70], level=30)
        return self._default


class FlowPrompts:
    """Redirects click's prompt functions into a flow for as long as it is active."""

    #: Which tracker the pipeline is currently working on, so a question that
    #: is only about one of them can say so. Uploading to two trackers asks
    #: "upload the torrent?" twice, and the two look identical.
    tracker: str = ""

    def __init__(self, flow: Flow, folder: str = "") -> None:
        """Initialize with the flow that questions should go to.

        Args:
            flow: Where questions and notes are published.
            folder: The release being uploaded, used to locate its spectrals.
        """
        self.flow = flow
        self.folder = folder
        self._saved: dict[str, Any] = {}
        self._saved_viewer: Any = None
        self._saved_editors: dict[str, Any] = {}
        self._saved_manual: Any = None
        self._saved_reviews: list[tuple[Any, Any]] = []
        self._saved_tqdm: Any = None
        # Group candidates printed since the last question, so the next prompt
        # can offer them instead of asking for a pasted URL.
        self._candidates: list[dict[str, Any]] = []
        # The last question's candidates, kept so a rejected answer does not
        # take the buttons with it when the pipeline asks again.
        self._offered: tuple[str, list[dict[str, Any]]] | None = None
        self._line = ""
        self._spectrals_ready = False
        # Track number to filename, as the pipeline's own viewer was given it.
        self._spectral_names: dict[int, str] = {}
        # Structured blocks captured since the last question. The pipeline
        # prints a tag diff and a metadata comparison as prose; both are tables
        # and are far easier to check as tables.
        self._tables: list[dict[str, Any]] = []
        # The phase those tables came from, so they are only shown with a
        # question asked in the same phase.
        self._table_stage: str | None = None
        # What a dry run said it would post, kept for the result panel.
        self.dry_run_payload: dict[str, str] = {}
        # The descriptions a dry run would have posted, in full. Summarising
        # them as a character count is not something anyone can check.
        self.dry_run_descriptions: dict[str, list[str]] = {}
        # One entry per torrent the run would have posted.
        self.dry_run_posts: list[dict[str, Any]] = []
        self._description = ""
        self._block: dict[str, Any] | None = None
        self._file = ""

    def _start_post(self) -> None:
        """Begin collecting a new torrent's payload, keeping the previous one."""
        if self.dry_run_payload or self.dry_run_descriptions:
            self.dry_run_posts.append(
                {
                    "fields": dict(self.dry_run_payload),
                    "descriptions": {
                        name: "\n".join(lines) for name, lines in self.dry_run_descriptions.items()
                    },
                }
            )
        self.dry_run_payload = {}
        self.dry_run_descriptions = {}
        self._description = ""

    def finish_posts(self) -> list[dict[str, Any]]:
        """Every torrent this run would have posted, in order."""
        self._start_post()
        return self.dry_run_posts

    def _confirm(self, text: str, default: bool = True, abort: bool = False, **_: Any) -> "_Answer":
        """Stand-in for click.confirm, awaited or not.

        The pipeline is inconsistent about this: it awaits ``click.confirm`` in
        some places and calls it bare in at least five others -- the retagger,
        the folder renamer, the integrity check. A plain coroutine satisfies the
        first and silently breaks the second, because an un-awaited coroutine is
        an object, and every object is truthy. So every one of those questions
        answered itself "yes" no matter what it asked, and Python logged a
        RuntimeWarning for each.

        The answer returned here is awaitable *and* has a truth value. Awaited,
        it asks. Used as a bare condition, it is the default the caller passed,
        which is the answer that call site documents for itself.
        """
        prompt = strip_ansi(text).strip()
        debug.event("upload.prompt", kind="confirm", prompt=prompt[:80])
        tables, self._tables, self._block = self._tables, [], None
        # Candidates belong to the question they were printed for. Leaving them
        # behind here meant anything listed before a confirm turned up as an
        # option on whatever was asked next.
        self._candidates = []

        async def ask() -> bool:
            # The pipeline's own way back: answering no here re-runs the
            # metadata step. As "Yes/No" that is invisible, so it is spelled
            # out -- everything up to the post is still changeable, and a typo
            # in the metadata should not mean aborting the whole upload.
            if _RERUN_METADATA.search(prompt):
                where = f" to {self.tracker}" if self.tracker else ""
                choice = await self.flow.choose(
                    f"Upload the torrent{where}?",
                    [
                        {"value": "yes", "label": f"Upload to {self.tracker}" if self.tracker
                                                  else "Upload the torrent"},
                        {"value": "no", "label": "← Back to the metadata"},
                    ],
                    detail=(f"Nothing has been posted{where} yet, so going back changes what will be."
                            if self.tracker
                            else "Nothing has been posted yet, so going back changes what will be."),
                    default="yes",
                    tables=tables,
                )
                return str(choice) != "no"
            answer = await self.flow.confirm(prompt, default=bool(default), tables=tables)
            if abort and not answer:
                raise click.Abort
            return answer

        return _Answer(ask, bool(default), prompt)

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
        # A prompt that did not like your answer asks again without reprinting
        # its list, so the second time round the buttons were gone and the only
        # way left to answer was the one that had just been rejected. The
        # candidates belong to the question, not to the attempt.
        if found:
            self._offered = (prompt, found)
        elif self._offered and self._offered[0] == prompt:
            found = self._offered[1]
        # Only the tables from this phase. One captured while the files were
        # being retagged is not evidence for a question asked while the
        # requests are being checked, and showing it there makes the question
        # look like it is about something it is not.
        tables = self._tables if self._table_stage == self.flow.stage else []
        self._tables, self._block, self._table_stage = [], None, None
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

        # Downconversion is one decision, so it is one click. Rendering the menu
        # as checkboxes -- and then adding "none" and "all" from the prompt text
        # as two more checkboxes, every one of them pre-ticked -- offered
        # combinations that contradict each other and matched nothing anyone
        # would type at the real prompt.
        if _DOWNCONVERT.search(prompt):
            # Only the numbered formats. "0" is the menu's own "Skip
            # downconversion", which is the same answer as the "Do not convert"
            # button added below and appeared beside it as a second, differently
            # worded copy; and anything carrying the rename arrow is a filename
            # that has no business being an answer to this question.
            formats = [
                option
                for option in found
                if option["value"].isdigit() and option["value"] != "0" and ">>>" not in option["label"]
            ]
            return await self.flow.choose(
                prompt.split("(")[0].strip().rstrip("?:,") or "Convert to another format?",
                [
                    *formats,
                    {"value": "*", "label": "Every format"},
                    {"value": "0", "label": "Do not convert"},
                ],
                detail=prompt,
                # Converting is the point of being asked, so that is what the
                # button under your finger does.
                default="*",
            )

        # "Which spectrals shall I upload" has three answers worth offering and
        # a fourth nobody uses. None, all, or let it pick -- as buttons, beside
        # the spectrals themselves.
        if _SPECTRAL_IDS.search(prompt):
            question = prompt.split("(")[0].strip().rstrip("?:,")
            return await self.flow.choose(
                question,
                [
                    {"value": "*", "label": "All of them"},
                    {"value": "+", "label": "Pick a few for me", "detail": "A randomised selection"},
                    {"value": "0", "label": "None"},
                ],
                detail=prompt,
                default="*",
                images=self._spectral_images(),
            )

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
                # Some of these name pasting a URL as one of their answers --
                # the existing-group question and the metadata results both do.
                # A field beside the buttons, never instead of them: replacing
                # them threw away the candidates the pipeline had just found,
                # which are the answer nearly every time.
                text_label="Or paste a URL" if _PASTE_URL.search(prompt) else "",
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

        # Which description the "| ..." lines that follow belong to. Detected
        # here rather than inside the payload block: the first heading closes
        # that block, so a second heading was never recognised and the release
        # description was appended to the album description under one key.
        heading = _DESCRIPTION_HEAD.match(text)
        if heading:
            self._description = heading[1].strip().lower()
            self.flow.note(text)
            return
        if text.startswith("[DRY RUN]"):
            self._description = ""

        if self._description and text.startswith("|"):
            body = self.dry_run_descriptions.setdefault(self._description, [])
            body.append(text[1:].lstrip())
            return

        request = _REQUEST_LINE.match(text)
        requirements = _REQUIREMENTS.match(text)
        group = _GROUP_LINE.match(text)
        if request:
            url, description = request[1], request[2].strip()
            # The URL is the value, not the row number. The pipeline reads a
            # bare number as an index into its own list, so sending "2" back
            # depends on that list being in the order the buttons were built
            # in -- and getting it wrong fills someone else's request, which no
            # part of a tracker lets you undo. A URL names exactly one request.
            self._candidates.append(
                {
                    "value": url,
                    "label": description,
                    "detail": "",
                    "url": url,
                    "kind": "group",
                    "link_label": "Open request ↗",
                }
            )
        elif requirements and self._last_request() is not None:
            # Printed on the line after its request, and it is the whole reason
            # to pick one request over another: what the requester will accept.
            wanted = requirements[1].strip().rstrip("/").strip()
            self._last_request()["detail"] = f"Accepts {wanted}"  # pyright: ignore[reportOptionalSubscript]
        elif group:
            group_id, description, url = group[1], group[2].strip(), group[3]
            # Not truncated: this is the line you decide on, and the edition and
            # tags that distinguish one group from another live at the end of
            # it. `url` lets the page link straight to the group so the check
            # can be made against the tracker rather than against a summary.
            self._candidates.append(
                {
                    "value": url or group_id,
                    "label": description,
                    "detail": f"group {group_id}",
                    "url": url or "",
                    "kind": "group",
                }
            )
        else:
            # Metadata results and numbered menus are offered the same way: the
            # pipeline wants the index back, so that is the option's value.
            result = _RESULT_LINE.match(text) if text.startswith(">") else None
            menu = _MENU_LINE.match(text)
            if result:
                index, description, url = result[1], result[2].strip(), result[3]
                # Same card as a tracker group: this is a release you are
                # choosing between, so it gets room for its track count and
                # year, and a link to check it on Deezer before committing.
                self._candidates.append(
                    {
                        "value": str(int(index)),
                        "label": description,
                        "detail": _result_detail(description, url),
                        "url": url or "",
                        "kind": "group",
                        "link_label": "Open on Deezer ↗",
                    }
                )
            elif menu and len(text) < 60:
                self._candidates.append({"value": menu[1], "label": menu[2][:60], "detail": ""})

        if "spectrals are available" in text.lower():
            self._spectrals_ready = True

        self._report_stage(text)

        if self._capture_block(text):
            return
        self.flow.note(text)

    def _last_request(self) -> dict[str, Any] | None:
        """The request candidate just added, if the last line was one."""
        if self._candidates and self._candidates[-1].get("link_label", "").startswith("Open request"):
            return self._candidates[-1]
        return None

    def _report_stage(self, text: str) -> None:
        """Say what the pipeline is doing, from what it just said it did.

        A running upload showed a blank card with the last stage set at the
        start, so the only way to know whether it was hashing, transcoding or
        stuck was to open the log. The pipeline announces each phase as it
        enters it; these are those announcements, shortened.
        """
        lowered = text.lower()
        for needle, stage in _STAGES:
            if needle in lowered:
                self.flow.progress(stage)
                return

    def _capture_block(self, text: str) -> bool:
        """Fold tag diffs and metadata listings into structured tables.

        Returns:
            True when the line belonged to a block and should not also be shown
            as a loose note.
        """
        # Trailing dots as well as colons: the pipeline writes some of these
        # headers as "Renaming folder..." and some as "Proposed tag changes:".
        head = _BLOCK_HEADS.get(text.strip().rstrip(":. ").lower())
        if head:
            kind, title = head
            if kind == "dryrun":
                # A release with a downconversion posts more than one torrent,
                # each with its own payload and its own descriptions. They were
                # all appended into one, which is why the album description
                # came back repeated four times over.
                self._start_post()
            self._block = {"kind": kind, "title": title, "rows": []}
            # Which phase this table belongs to. A table is evidence for the
            # question asked in the phase that produced it, and for no other:
            # with prompts auto-answered nothing consumed the tag diff or the
            # folder rename, so they queued up and arrived attached to whatever
            # was asked next -- a request question wearing a "Folder rename"
            # table, which reads as a rename prompt with the wrong buttons.
            self._table_stage = self.flow.stage
            self._tables.append(self._block)
            self._file = ""
            return True

        if self._block is None:
            return False

        if not text:
            self._block = None
            return False

        if self._block["kind"] == "torrents":
            torrent = _TORRENT_LINE.match(text)
            if not torrent:
                return False
            parts = [p.strip() for p in torrent[2].split("/") if p.strip()]
            # The last three are media, format and encoding; whatever precedes
            # them is the edition and its catalogue numbers, which vary in
            # count and are only worth reading as one piece of text.
            media, fmt, encoding = (parts[-3:] + ["", "", ""])[:3] if len(parts) >= 3 else ("", "", "")
            self._block["rows"].append(
                {
                    "year": torrent[1],
                    "edition": " / ".join(parts[:-3]) if len(parts) > 3 else "",
                    "media": media,
                    "format": fmt,
                    "encoding": encoding,
                }
            )
            return True

        if self._block["kind"] == "folder":
            name = _FOLDER_NAME.match(text)
            if not name:
                # Anything else means the rename has moved on; let it be a note.
                self._block = None
                return False
            rows = self._block["rows"]
            if not rows:
                rows.append({"group": "", "label": "", "before": "", "after": "", "changed": True})
            rows[0]["before" if name[1].lower() == "old" else "after"] = name[2]
            return True

        if self._block["kind"] == "dryrun":
            # The payload is followed by the descriptions in full, which are
            # prose and must not be mined for "key   value" pairs.
            if text.startswith("[DRY RUN]"):
                self._block = None
                return False
            field = _DRY_FIELD.match(text)
            if not field:
                return False
            self._block["rows"].append(
                {"group": "", "label": field[1].strip(), "before": field[2].strip(),
                 "after": "", "changed": False}
            )
            # Kept for the result summary as well as the question it sits under.
            self.dry_run_payload[field[1].strip()] = field[2].strip()
            return True

        if self._block["kind"] == "request":
            labelled = _LABELLED.match(text)
            if not labelled:
                return False
            self._block["rows"].append(
                {"group": "", "label": labelled[1].strip(), "before": labelled[2].strip(),
                 "after": "", "changed": False}
            )
            return True

        if self._block["kind"] == "renames":
            rename = _RENAME_LINE.match(text)
            if not rename:
                return False
            self._block["rows"].append(
                {"group": "", "label": "", "before": rename["old"].strip(),
                 "after": rename["new"].strip(), "changed": True}
            )
            return True

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

        # Metadata is a field-and-value list, not a diff. A field either carries
        # its value inline ("> TITLE : ...") or introduces a list of them on the
        # following ">>>" lines; in the second case the field row is written
        # when the first item arrives, so an empty row is never emitted for it.
        field = _META_FIELD.match(text)
        if field:
            self._file = field[1].strip()
            value = field[2].strip()
            if value:
                self._block["rows"].append({"group": "", "label": self._file, "before": value,
                                            "after": "", "changed": False})
                self._file = ""
            return True
        item = _META_ITEM.match(text)
        if item:
            rows = self._block["rows"]
            # Extra values join the field they belong to rather than each
            # becoming a headerless row of their own.
            if self._file and rows and rows[-1]["label"] == self._file:
                rows[-1]["before"] = f"{rows[-1]['before']}, {item[1].strip()}"
            else:
                rows.append({"group": "", "label": self._file, "before": item[1].strip(),
                             "after": "", "changed": False})
            return True
        return False

    def _wants_images(self, prompt: str) -> bool:
        """Whether this question is one the spectrals inform.

        Only questions actually about the spectrals. This used to also return
        True for anything asked after they were generated, which meant every
        later question -- metadata, downconversion, the description -- carried
        a wall of spectrograms it had nothing to do with, and they never went
        away for the rest of the upload.
        """
        lowered = prompt.lower()
        return "spectral" in lowered or "lossy master" in lowered

    def _spectral_images(self) -> list[dict[str, str]]:
        """The spectrals for this release, paired per track.

        The pipeline writes two images per track, ``NN Full.png`` and
        ``NN Zoom.png``. They belong side by side -- the full view to see the
        shelf, the zoom to see whether it is real -- so they are returned
        grouped rather than as one long list of unlabelled thumbnails.
        """
        if not self.folder:
            return []
        from lox.uploader.spectrals import get_spectrals_path

        directory = get_spectrals_path(self.folder)
        if not os.path.isdir(directory):
            return []

        base = quote(os.path.basename(directory))
        grouped: dict[str, dict[str, str]] = {}
        for name in sorted(f for f in os.listdir(directory) if f.lower().endswith(_SPECTRAL_EXT)):
            stem = os.path.splitext(name)[0]
            track, _, kind = stem.partition(" ")
            entry = grouped.setdefault(track, {"track": track, "label": self._track_label(track),
                                               "full": "", "zoom": ""})
            entry["zoom" if kind.strip().lower() == "zoom" else "full"] = (
                f"/spectral-image/{base}/{quote(name)}"
            )
        return list(grouped.values())

    def _track_label(self, track: str) -> str:
        """What to caption a pair of spectrals with.

        The audio filename, which is what the pipeline's own viewer captions
        them with and already carries the track number. The bare number is the
        fallback, for spectrals that were on disk before this upload started
        and so were never announced to the viewer.
        """
        try:
            filename = self._spectral_names.get(int(track), "")
        except ValueError:
            filename = ""
        return os.path.basename(filename) or track

    async def _edit(self, text: Any = "", extension: str = ".txt", **_: Any) -> str | None:
        """Stand-in for click.edit.

        The pipeline edits metadata by shelling out to ``$EDITOR``. In a
        container that is vim with no terminal attached: it prints "Output is
        not to a terminal" and blocks forever, which is what made an upload
        freeze the moment you chose a field to edit.

        Each blob it edits has a known shape, so instead of a text box this
        publishes a typed form -- artist rows with a role each, a list of
        genres, a title -- and serialises the answer back into exactly the text
        the pipeline's parser expects. It never learns the difference.
        """
        original = "" if text is None else str(text)
        shape, fields = _editor_shape(original, extension)
        debug.event("upload.edit", shape=shape, chars=len(original))

        answer = await self.flow.ask(
            Step(
                "edit",
                _EDIT_TITLES.get(shape, "Edit"),
                detail=_EDIT_HELP.get(shape, ""),
                options=fields,
                default=original,
                edit_shape=shape,
            )
        )
        if answer is None:
            return None
        return _editor_text(shape, answer, original)

    def _metadata_sections(self, metadata: dict) -> list[dict[str, Any]]:
        """Every editable field of a release, as one form.

        The pipeline's metadata screen is a menu: it prints the record, asks
        which single field you want to change, opens an editor for that one
        field, prints the record again and asks again. Changing four things
        means going round four times, and at no point can you see the release
        as a whole while editing it.

        This is the whole record at once, each field with the control that
        suits it, and one Save.
        """
        from lox.constants import RELEASE_TYPES

        tracks = [
            {
                "key": f"{disc}/{num}",
                "label": f"Disc {disc} · track {num}" if len(metadata.get("tracks") or {}) > 1 else f"Track {num}",
                "value": track.get("title") or "",
                # Editable, because the trackers refuse a track with no main
                # artist and this is the only place that can be said. Roles
                # ride along so a name keeps the one it already has.
                "artists": ", ".join(name for name, _role in (track.get("artists") or [])),
                "roles": [{"name": name, "role": role} for name, role in (track.get("artists") or [])],
            }
            for disc, disc_tracks in (metadata.get("tracks") or {}).items()
            for num, track in disc_tracks.items()
        ]

        def text(key: str, label: str, hint: str = "") -> dict[str, Any]:
            return {"kind": "text", "key": key, "label": label, "value": metadata.get(key) or "", "hint": hint}

        # Grouped, because thirteen fields in one run is a wall. Each group is
        # a question you can answer on its own: what is this release, who is on
        # it, which pressing is it, how is it filed.
        return [
            # The two years sit together. They are the same question asked
            # twice -- when the album came out, and when this pressing did --
            # and putting them in different groups meant comparing them was a
            # scroll.
            {"group": "The release", "fields": [
                text("title", "Album title"),
                {"kind": "select", "key": "rls_type", "label": "Release type",
                 "value": metadata.get("rls_type") or "Album", "choices": list(RELEASE_TYPES)},
                {"kind": "number", "key": "group_year", "label": "Original release year",
                 "value": metadata.get("group_year") or "", "hint": "When the album first came out."},
                {"kind": "number", "key": "year", "label": "Edition year",
                 "value": metadata.get("year") or "", "hint": "When this pressing came out."},
            ]},
            {"group": "Credits", "fields": [
                {"kind": "artists", "key": "artists", "label": "Artists",
                 "hint": "Main is for the artists the release is by. Everyone else is a guest or a specific role.",
                 "rows": [{"name": name, "role": role} for name, role in (metadata.get("artists") or [])]},
            ]},
            {"group": "This edition", "fields": [
                text("edition_title", "Edition title", "Deluxe, Remastered. Blank for the original."),
                text("label", "Record label"),
                text("catno", "Catalogue number"),
                text("upc", "UPC"),
            ]},
            {"group": "Filing", "fields": [
                {"kind": "list", "key": "genres", "label": "Genres", "hint": "At least one is required.",
                 "rows": [{"value": genre} for genre in (metadata.get("genres") or [])]},
                {"kind": "list", "key": "urls", "label": "URLs",
                 "rows": [{"value": url} for url in (metadata.get("urls") or [])]},
                {"kind": "textarea", "key": "comment", "label": "Comment",
                 "value": metadata.get("comment") or ""},
            ]},
            {"group": "Tracks", "fields": [
                {"kind": "tracks", "key": "tracks", "label": "Tracks", "rows": tracks,
                 "hint": "Each track needs at least one main artist. The first name listed is the main "
                         "one unless the credits above give it another role."},
            ]},
        ]

    def _apply_metadata(self, metadata: dict, answer: Any) -> None:
        """Write a metadata form's answer back over the release.

        Only the keys the form sent are touched, so a field the form does not
        offer keeps whatever the scraper found for it. ``answer`` is whatever
        the browser sent, so it is checked rather than assumed.
        """
        if not isinstance(answer, dict):
            return

        if isinstance(answer.get("artists"), list):
            people = [
                (str(row.get("name", "")).strip(), str(row.get("role") or "main").strip().lower())
                for row in answer["artists"]
                if str(row.get("name", "")).strip()
            ]
            if people:
                metadata["artists"] = people
                roles = dict(people)
                for disc in (metadata.get("tracks") or {}).values():
                    for track in disc.values():
                        track["artists"] = [(n, roles.get(n, r)) for n, r in (track.get("artists") or [])]

        for key in ("title", "rls_type"):
            value = str(answer.get(key, "")).strip()
            if value:
                metadata[key] = value

        for key in ("year", "group_year"):
            value = str(answer.get(key, "")).strip()
            if re.fullmatch(r"\d{4}", value):
                metadata[key] = value

        # These four are legitimately blank on most releases, so an empty box
        # means None rather than "leave it alone".
        for key in ("edition_title", "label", "catno", "upc", "comment"):
            if key in answer:
                metadata[key] = str(answer.get(key) or "").strip() or None

        for key in ("genres", "urls"):
            if isinstance(answer.get(key), list):
                values = [str(v).strip() for v in answer[key] if str(v).strip()]
                if values or key == "urls":
                    metadata[key] = values

        edits = answer.get("tracks")
        if isinstance(edits, dict):
            album_roles = {name.lower(): role for name, role in (metadata.get("artists") or [])}
            for key, edit in edits.items():
                disc, _, num = str(key).partition("/")
                if disc not in metadata.get("tracks", {}) or num not in metadata["tracks"][disc]:
                    continue
                track = metadata["tracks"][disc][num]
                # The form used to send a bare title. Both shapes are read so
                # an answer in flight across a restart still lands.
                if not isinstance(edit, dict):
                    edit = {"title": edit}

                text_title = str(edit.get("title", "")).strip()
                if text_title:
                    track["title"] = text_title

                if "artists" in edit:
                    track["artists"] = self._track_artists(
                        str(edit.get("artists") or ""), track.get("artists") or [], album_roles
                    )

    @staticmethod
    def _track_artists(
        written: str, current: list[tuple[str, str]], album_roles: dict[str, str]
    ) -> list[tuple[str, str]]:
        """The artists of one track, as edited, each with a role.

        The form asks for names, because that is what a track credit reads
        like. The role comes from what the name already had here, then from
        what the release credits it as, and a track with nobody left as a main
        artist takes its first name as one -- the trackers require one, and a
        release that cannot satisfy that cannot be posted at all.

        Args:
            written: The names as typed, separated by commas.
            current: What the track was credited with before the edit.
            album_roles: Lowercased name to role, from the release credits.

        Returns:
            Name and role pairs, in the order they were written.
        """
        was = {name.lower(): role for name, role in current}
        people: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in (n.strip() for n in written.split(",")):
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            people.append((name, was.get(name.lower()) or album_roles.get(name.lower()) or "guest"))

        if not people:
            return list(current)
        if not any(role == "main" for _name, role in people):
            people[0] = (people[0][0], "main")
        return people

    def _review_metadata(self) -> Any:
        """A replacement for the pipeline's metadata menu: one form, one Save."""

        async def review_metadata(metadata: dict, validator: Any) -> dict:
            from lox.errors import InvalidMetadataError

            problem = ""
            while True:
                answer = await self.flow.ask(
                    Step(
                        "edit",
                        "Release metadata",
                        detail=problem or "Everything the upload will be posted with. Change what you need, then save.",
                        options=self._metadata_sections(metadata),
                        edit_shape="metadata",
                    )
                )
                if answer is not None:
                    self._apply_metadata(metadata, answer)

                try:
                    validator(metadata)
                except InvalidMetadataError as e:
                    # Back into the form with the reason on it, rather than a
                    # yes/no about whether to "revisit the metadata step".
                    problem = f"{e} Fix it below."
                    continue
                return metadata

        return review_metadata

    async def _form(self, title: str, shape: str, rows: list[dict[str, Any]], detail: str = "") -> Any:
        """Ask one metadata form and return its answer, or None if cancelled."""
        return await self.flow.ask(
            Step("edit", title, detail=detail, options=rows, edit_shape=shape)
        )

    def _metadata_editors(self) -> dict[str, Any]:
        """Replacements for the pipeline's editor-based metadata screens.

        Each one is an ``async def _edit_x(metadata)`` that mutates the dict in
        place, so replacing them is enough -- the pipeline calls them and reads
        the dict afterwards either way. Doing it here rather than through
        ``click.edit`` matters for two reasons: ``click.edit`` is synchronous,
        so an async replacement returns a coroutine the pipeline never awaits,
        and the metadata is already structured, so round-tripping it through
        text to parse it back was work with nothing to show for it.
        """

        async def edit_artists(metadata: dict) -> None:
            rows = [{"name": name, "role": role} for name, role in metadata["artists"]]
            answer = await self._form("Artists", "artists", rows,
                                      "Add, remove or re-credit. Roles are the ones the trackers accept.")
            if answer is None:
                return
            people = [
                (str(r.get("name", "")).strip(), str(r.get("role") or "main").strip().lower())
                for r in answer
                if str(r.get("name", "")).strip()
            ]
            if not people:
                return
            metadata["artists"] = people
            roles = dict(people)
            for disc in metadata["tracks"].values():
                for track in disc.values():
                    track["artists"] = [(n, roles.get(n, r)) for n, r in track["artists"]]

        async def alias_artists(metadata: dict) -> None:
            rows = [{"name": name, "alias": "", "drop": False} for name in {a for a, _ in metadata["artists"]}]
            answer = await self._form("Artist aliases", "aliases", rows,
                                      "Rename an artist for this upload, or drop the credit entirely.")
            if answer is None:
                return
            renames = {
                str(r["name"]).lower(): str(r.get("alias", "")).strip()
                for r in answer
                if str(r.get("alias", "")).strip()
            }
            dropped = {str(r["name"]).lower() for r in answer if r.get("drop")}
            if not renames and not dropped:
                return

            def apply(pairs: list) -> list:
                out = []
                for name, role in pairs:
                    key = name.lower()
                    if key in dropped:
                        continue
                    out.append((renames.get(key, name), role))
                return out

            metadata["artists"] = apply(metadata["artists"])
            for disc in metadata["tracks"].values():
                for track in disc.values():
                    track["artists"] = apply(track["artists"])

        async def edit_title(metadata: dict) -> None:
            answer = await self._form("Album title", "form", [
                {"key": "title", "label": "Title", "kind": "text", "value": metadata["title"] or ""},
            ])
            if answer:
                metadata["title"] = str(answer.get("title", "")).strip() or metadata["title"]

        async def edit_years(metadata: dict) -> None:
            answer = await self._form("Years", "form", [
                {"key": "year", "label": "Edition year", "kind": "number", "value": metadata["year"]},
                {"key": "group_year", "label": "Original release year", "kind": "number",
                 "value": metadata["group_year"]},
            ], "The original year is the group's; the edition year is this pressing's.")
            if not answer:
                return
            for key in ("year", "group_year"):
                value = str(answer.get(key, "")).strip()
                if re.fullmatch(r"\d{4}", value):
                    metadata[key] = value

        async def edit_genres(metadata: dict) -> None:
            answer = await self._form("Genres", "list",
                                      [{"value": g} for g in metadata["genres"]], "One per line.")
            if answer is not None:
                picked = [str(r.get("value", "")).strip() for r in answer if str(r.get("value", "")).strip()]
                if picked:
                    metadata["genres"] = picked

        async def edit_urls(metadata: dict) -> None:
            answer = await self._form("URLs", "list", [{"value": u} for u in metadata["urls"]])
            if answer is not None:
                metadata["urls"] = [
                    str(r.get("value", "")).strip() for r in answer if str(r.get("value", "")).strip()
                ]

        async def edit_edition_info(metadata: dict) -> None:
            answer = await self._form("Edition", "form", [
                {"key": "edition_title", "label": "Edition title", "kind": "text",
                 "value": metadata["edition_title"] or ""},
                {"key": "label", "label": "Record label", "kind": "text", "value": metadata["label"] or ""},
                {"key": "catno", "label": "Catalogue number", "kind": "text", "value": metadata["catno"] or ""},
                {"key": "upc", "label": "UPC", "kind": "text", "value": metadata["upc"] or ""},
            ])
            if not answer:
                return
            for key in ("edition_title", "label", "catno", "upc"):
                metadata[key] = str(answer.get(key, "")).strip() or None

        async def edit_comment(metadata: dict) -> None:
            answer = await self._form("Comment", "form", [
                {"key": "comment", "label": "Comment", "kind": "textarea", "value": metadata["comment"] or ""},
            ])
            if answer is not None:
                metadata["comment"] = str(answer.get("comment", "")).strip() or None

        async def edit_release_type(metadata: dict) -> None:
            from lox.constants import RELEASE_TYPES

            answer = await self.flow.choose(
                "Release type",
                [{"value": r, "label": r} for r in RELEASE_TYPES],
                default=metadata.get("rls_type") or "Album",
            )
            if answer:
                metadata["rls_type"] = answer

        async def edit_tracks(metadata: dict) -> None:
            # One row per track, which is what the pipeline's text blob was
            # describing the long way round.
            rows = [
                {"key": f"{disc}/{num}", "label": f"Disc {disc} track {num}", "kind": "text",
                 "value": track.get("title") or ""}
                for disc, tracks in metadata["tracks"].items()
                for num, track in tracks.items()
            ]
            answer = await self._form("Track titles", "form", rows)
            if not answer:
                return
            for key, value in answer.items():
                disc, _, num = key.partition("/")
                title = str(value).strip()
                if title and disc in metadata["tracks"] and num in metadata["tracks"][disc]:
                    metadata["tracks"][disc][num]["title"] = title

        return {
            "_edit_release_type": edit_release_type,
            "_edit_tracks": edit_tracks,
            "_edit_artists": edit_artists,
            "_alias_artists": alias_artists,
            "_edit_title": edit_title,
            "_edit_years": edit_years,
            "_edit_genres": edit_genres,
            "_edit_urls": edit_urls,
            "_edit_edition_info": edit_edition_info,
            "_edit_comment": edit_comment,
        }

    def _manual_metadata(self, rls_data: dict) -> dict:
        """Stand in for the pipeline's raw-JSON metadata editor.

        "Manual" mode dumps the metadata to JSON, opens it in ``$EDITOR`` and
        parses whatever comes back. That is a synchronous function, so it
        cannot ask a question here; and if the parse fails it re-opens the
        editor forever, which is what it would do with a coroutine in place of
        the text.

        There is nothing to ask. Manual means "start from what the tags say",
        and the very next step is the metadata review, where every one of those
        fields is an editable form. So hand the tag-derived data straight back
        and let the forms do the editing.
        """
        # A deep copy, because the version this replaces round-tripped through
        # JSON and so handed back something the caller could edit freely.
        metadata = copy.deepcopy(rls_data)
        genres = metadata.get("genres")
        if isinstance(genres, str):
            metadata["genres"] = [genres]
        self.flow.note("Using the metadata from the file tags. Edit it in the next step.")
        return metadata

    def _progress_bar(self) -> Any:
        """A stand-in for tqdm that reports into the flow.

        Every per-file phase of an upload -- generating spectrals, compressing
        them, checking integrity, transcoding -- runs through
        ``lox.common.files.process_files``, which counts them off on a tqdm bar
        written to stderr. In a browser that is invisible, so the longest parts
        of an upload showed a stage name and nothing else for minutes at a
        time. Same counter, reported where it can be seen.
        """
        flow = self.flow

        class Bar:
            def __init__(self, total: int = 0, desc: str = "", **_: Any) -> None:
                self.total = total or 0
                self.desc = desc or "Working"
                self.n = 0
                self._report()

            def _report(self) -> None:
                percent = (self.n / self.total * 100) if self.total else None
                flow.progress(f"{self.desc} ({self.n}/{self.total})" if self.total else self.desc, percent)

            def update(self, count: int = 1) -> None:
                self.n += count
                self._report()

            def close(self) -> None:
                self.n = self.total
                self._report()

            def __enter__(self) -> "Bar":
                return self

            def __exit__(self, *_exc: object) -> None:
                self.close()

        return Bar

    async def _view_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        """Stand in for the pipeline's spectral viewer.

        The pipeline's own viewer symlinks the spectral directory into the
        installed package's static folder and starts a second web server to
        serve it. In a container that first step is a
        ``PermissionError: [Errno 1] Operation not permitted`` -- the package
        directory belongs to the image, and the process runs as PUID -- which
        killed every upload immediately after spectrals were compressed.

        None of it is needed here. The images are attached to the questions
        they inform, so they appear on the page you are already looking at
        rather than behind a link to a second server. Marking them ready and
        keeping the track names it was going to caption them with is the whole
        job.
        """
        self._spectrals_ready = True
        self._spectral_names = dict(all_spectral_ids or {})
        self.flow.note(f"Spectrals ready: {os.path.basename(spectrals_path)}")

    def __enter__(self) -> "FlowPrompts":
        """Patch click's prompt functions, the metadata editors and the spectral viewer."""
        for name, replacement in (
            ("confirm", self._confirm),
            ("prompt", self._prompt),
            ("echo", self._echo),
            ("secho", self._echo),
            # Without this the pipeline shells out to vim, which in a container
            # has no terminal and blocks the upload forever.
            ("edit", self._edit),
        ):
            self._saved[name] = getattr(click, name)
            setattr(click, name, replacement)

        # review_metadata builds its dispatch table from module globals every
        # time it runs, so replacing the attributes is enough to redirect it.
        review = _review_module()
        for name, replacement in self._metadata_editors().items():
            self._saved_editors[name] = getattr(review, name)
            setattr(review, name, replacement)

        # The whole screen, not just the editors under it: the menu that asks
        # which single field you would like to change is the thing being
        # replaced. The individual editors stay patched above because the
        # pipeline also calls them directly when a release arrives with no
        # release type or no genres.
        #
        # Patched on every module that imported the name, not just the one that
        # defines it -- "from x import y" binds y in the importer, so replacing
        # it on x alone would change nothing for the two callers that matter.
        form = self._review_metadata()
        for module in _modules_using("review_metadata"):
            self._saved_reviews.append((module, module.review_metadata))
            module.review_metadata = form

        metadata = _metadata_module()
        self._saved_manual = metadata._get_manual_metadata
        metadata._get_manual_metadata = self._manual_metadata

        # The per-file counter for the slow phases, moved from a terminal
        # progress bar nobody can see to the flow's own progress.
        import lox.common.files as files

        self._saved_tqdm = files.tqdm
        files.tqdm = self._progress_bar()

        # Imported here rather than at module scope: the uploader pulls in the
        # whole audio stack, and the web app has to be importable without it.
        spectrals = _spectrals_module()
        self._saved_viewer = spectrals.view_spectrals
        spectrals.view_spectrals = self._view_spectrals
        return self

    def __exit__(self, *_exc: object) -> None:
        """Restore click's prompt functions, the metadata editors and the spectral viewer."""
        for name, original in self._saved.items():
            setattr(click, name, original)
        self._saved.clear()
        if self._saved_editors:
            review = _review_module()
            for name, original in self._saved_editors.items():
                setattr(review, name, original)
            self._saved_editors.clear()
        for module, original in self._saved_reviews:
            module.review_metadata = original
        self._saved_reviews.clear()
        if self._saved_tqdm is not None:
            import lox.common.files as files

            files.tqdm = self._saved_tqdm
            self._saved_tqdm = None
        if self._saved_manual is not None:
            _metadata_module()._get_manual_metadata = self._saved_manual
            self._saved_manual = None
        if self._saved_viewer is not None:
            _spectrals_module().view_spectrals = self._saved_viewer
            self._saved_viewer = None


async def run_upload(
    flow: Flow,
    folder: str,
    trackers: list[str],
    *,
    source: str = "WEB",
    auto_rename: bool = False,
    link_for: Any = None,
    on_uploaded: Any = None,
    link_derived: Any = None,
) -> dict[str, Any]:
    """Upload one folder to every chosen tracker, asking the browser as it goes.

    One pass of the pipeline, not one per tracker. Everything that is a fact
    about the release -- the spectrals, the metadata lookup, the retagging, the
    folder and file names, which formats to convert to -- happens once; only
    what is genuinely per-tracker happens per tracker.

    Args:
        flow: The flow questions and progress are published to.
        folder: Release folder to upload.
        trackers: Tracker codes, in the order they should run.
        source: Media source.
        auto_rename: Rename files and folders without asking.
        link_for: Called with ``(tracker, path)`` before each tracker's post,
            returning the path that tracker seeds from.
        on_uploaded: Called with ``(tracker, url)`` after each tracker takes it.
        link_derived: Called with ``(tracker, path)`` for a transcode, so it is
            made once and linked rather than encoded again per tracker.

    Returns:
        A summary of what happened.

    Raises:
        Exception: Whatever the pipeline raises, for the registry to record.
    """
    import lox.trackers
    from lox.uploader import upload as run_pipeline

    first = trackers[0]
    flow.progress(f"Preparing {', '.join(trackers)}")
    flow.note(f"Uploading {folder} to {', '.join(trackers)}")
    debug.log("upload start trackers=%s folder=%s", ",".join(trackers), folder, level=20)

    gazelle = lox.trackers.get_class(first)()
    # This task is an upload, so its tracker calls take the upload allowance
    # rather than queueing behind whatever the checker is doing. A scan makes
    # hundreds of calls; an upload is one release and a person watching it.
    from lox.trackers.base import uploading  # noqa: PLC0415

    token = uploading.set(True)
    with FlowPrompts(flow, folder) as prompts:
        prompts.tracker = first

        def on_tracker(tracker: str) -> None:
            """Which tracker the questions from here on are about."""
            prompts.tracker = tracker
            flow.progress(f"Uploading to {tracker}")

        await run_pipeline(
            gazelle,
            folder,
            None,
            source,
            None,
            (),
            None,
            auto_rename=auto_rename,
            also_upload_to=list(trackers),
            link_for=link_for,
            on_uploaded=on_uploaded,
            on_tracker=on_tracker,
            link_derived=link_derived,
        )

    uploading.reset(token)
    flow.progress(f"Finished {', '.join(trackers)}", 100.0)
    debug.log("upload finished trackers=%s folder=%s", ",".join(trackers), folder, level=20)
    posts = prompts.finish_posts()
    return {
        "posts": posts,
        # The first is the release itself; any others are its downconversions.
        "fields": posts[0]["fields"] if posts else {},
        "descriptions": posts[0]["descriptions"] if posts else {},
    }


async def run_uploads(
    flow: Flow,
    folder: str,
    trackers: list[str],
    *,
    source: str = "WEB",
    auto_rename: bool = False,
) -> dict[str, Any]:
    """Upload one folder to several trackers, hardlinking per tracker as it goes.

    This used to call the pipeline once per tracker, which meant the second
    tracker asked every question the first had already answered: the spectrals
    were regenerated and re-reviewed, the metadata was looked up and confirmed
    again, the files were retagged and the folder renamed again, and the
    downconversions were chosen again -- all about a release that had not
    changed in the intervening minute. The pipeline has always had its own
    tracker loop that does the shared work once; it was being switched off.

    It is driven rather than switched off now. The loop is given exactly the
    trackers picked in the UI, in the order picked, so it never offers one that
    was deliberately left out -- which was the reason for switching it off in
    the first place.

    Args:
        flow: The flow to drive.
        folder: Release folder.
        trackers: Tracker codes, in the order they should run.
        source: Media source.
        auto_rename: Rename without asking.

    Returns:
        Per-tracker outcomes.
    """
    from lox import cfg
    from lox.seeding.links import LinkError, link_release

    outcomes: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    # Transcodes made beside the download and linked into each tracker's
    # folder. The originals are duplicates once that is done.
    derived_links: list[str] = []
    # Which trackers actually took the torrent, recorded as it happens. A run
    # that stops on the second tracker must not report the second as uploaded,
    # nor the first as failed.
    posted_to: dict[str, str] = {}

    def on_uploaded(tracker: str, url: Any) -> None:
        posted_to[tracker] = str(url or "")
        flow.note(f"{tracker} took the upload.")

    async def link(tracker: str, path: str, label: str) -> str:
        """Place one prepared folder where a tracker can seed it from.

        Args:
            tracker: Tracker code.
            path: The folder as it was prepared, once, for the release.
            label: What it is, for the note.

        Returns:
            Where that tracker seeds it from -- the same bytes, one hardlink.
        """
        if not cfg.linking.enabled:
            return path
        flow.progress(f"Linking {label} for {tracker}")
        try:
            made = await _to_thread(link_release, path, tracker)
        except LinkError as e:
            # Not fatal: the tracker can still have the release, it just seeds
            # from the same folder as the others.
            flow.note(f"{tracker}: linking {label} failed — {e}", "error")
            return path
        flow.note(f"{tracker}: {'reused' if made.reused else made.method} "
                  f"{made.files} file(s) for {label}")
        return made.destination

    async def link_for(tracker: str, path: str) -> str:
        """Where this tracker should seed the release from."""
        if tracker in seen:
            return seen[tracker]
        destination = await link(tracker, path, "the release")
        seen[tracker] = destination
        return destination

    async def link_derived(tracker: str, path: str) -> str:
        """Where this tracker should seed a transcode from.

        A transcode is a fact about the release, not about the tracker: the
        same thirteen tracks at V0 whoever is being uploaded to. It is made
        once and linked, exactly like the lossless folder beside it -- encoding
        it again per tracker wrote a second copy of identical audio and took as
        long again to do it.
        """
        derived_links.append(path)
        return await link(tracker, path, os.path.basename(path))

    result: dict[str, Any] = {}
    with _record_transcodes() as transcoded:
        try:
            posted = await run_upload(
                flow, folder, trackers, source=source, auto_rename=auto_rename,
                link_for=link_for, on_uploaded=on_uploaded, link_derived=link_derived,
            )
        except click.Abort:
            flow.note("Aborted.", "warning")
            outcomes = [{"tracker": t, "ok": False, "error": "aborted"} for t in trackers]
            posted = {}
        except Exception as e:  # noqa: BLE001 - reported rather than raised
            flow.note(f"{type(e).__name__}: {e}", "error")
            outcomes = [{"tracker": t, "ok": False, "error": str(e)} for t in trackers]
            posted = {}
        else:
            reached = set(posted_to)
            outcomes = [
                {
                    "tracker": tracker,
                    "ok": tracker in reached,
                    "folder": seen.get(tracker, folder),
                    "would_post": posted.get("fields") or {},
                    "descriptions": posted.get("descriptions") or {},
                    "posts": posted.get("posts") or [],
                }
                if tracker in reached
                else {"tracker": tracker, "ok": False, "error": "did not reach this tracker"}
                for tracker in trackers
            ]

        succeeded = [o["tracker"] for o in outcomes if o["ok"]]
        result = {
            "folder": folder,
            "outcomes": outcomes,
            "succeeded": succeeded,
            "dry_run": _dry_run(),
        }
        # Kept, never deleted, and only worth mentioning on a rehearsal. A dry
        # run exists to be inspected, and deleting the transcodes it just made
        # removes the part you most wanted to look at. A real run's
        # downconversions were uploaded and are seeding, so they are not
        # leftovers at all.
        if _dry_run():
            for path in transcoded:
                flow.note(f"Transcode kept at {path}")
            result["transcodes"] = list(transcoded)
            for outcome in result.get("outcomes") or []:
                outcome["kept"] = list(transcoded)
        else:
            result["transcodes"] = []

    _discard_spectrals(flow, folder)
    _clean_up_source(flow, folder, result, derived_links)
    return result


def _clean_up_source(flow: Flow, folder: str, result: dict[str, Any],
                     derived: list[str] | None = None) -> None:
    """Remove the download the release was made from, once it is somewhere else.

    A finished upload leaves the release seeding from the per-tracker link
    directories, and the original download sitting in the download folder --
    where it stays on the Uploading list for ever, offering to upload a release
    that has already been uploaded.

    Only ever when all three are true: the upload was real, at least one
    tracker took it, and linking is on so the bytes genuinely exist somewhere
    else. With linking off the tracker is seeding from this very folder, and
    deleting it would break the torrent.
    """
    import shutil

    from lox import cfg

    if _dry_run() or not result.get("succeeded"):
        return
    if not cfg.linking.enabled or not cfg.linking.remove_source_after_upload:
        return
    linked = [o.get("folder") for o in result.get("outcomes") or [] if o.get("ok")]
    if not any(path and os.path.abspath(path) != os.path.abspath(folder) for path in linked):
        # Nothing was linked anywhere else, so this folder is what is seeding.
        return
    if not os.path.isdir(folder):
        return
    # The transcodes were made beside the download and linked into each
    # tracker's folder, so the ones here are the same duplicates the release
    # itself is, and go the same way.
    for path in dict.fromkeys(derived or ()):
        if not os.path.isdir(path) or os.path.abspath(path) == os.path.abspath(folder):
            continue
        try:
            shutil.rmtree(path)
        except OSError as e:
            flow.note(f"Could not remove {path}: {e}", "warning")

    try:
        shutil.rmtree(folder)
    except OSError as e:
        flow.note(f"Could not remove {folder}: {e}", "warning")
        return
    flow.note(f"Removed the download at {folder}; the trackers seed from their own copies.")


@contextlib.contextmanager
def _record_transcodes() -> "Iterator[list[str]]":
    """Collect the transcode folders an upload creates.

    A downconversion writes a new release folder beside the source -- ``[WEB
    MP3 V0]`` next to ``[WEB FLAC]`` -- which in a real run is uploaded and
    seeded in its own right. The uploader binds ``transcode_folder`` by name at
    import, so the recording wrapper has to replace it there rather than on the
    module it came from.

    A folder that already existed is never recorded: it belongs to an earlier
    run, and that run may well have been a real one.
    """
    import lox.uploader as uploader
    from lox.converter import transcoding

    created: list[str] = []
    original = uploader.transcode_folder

    async def recording(path: str, bitrate: Any) -> str:
        try:
            existed = os.path.isdir(transcoding._build_output_path(path, bitrate))  # noqa: SLF001
        except Exception:  # noqa: BLE001 - if we cannot tell, assume it was already there
            existed = True
        result = await original(path, bitrate)
        if not existed and result:
            created.append(result)
        return result

    uploader.transcode_folder = recording
    try:
        yield created
    finally:
        uploader.transcode_folder = original


def _discard_spectrals(flow: Flow, folder: str) -> None:
    """Delete the spectral scratch folder once the upload is over.

    They are regenerated per run and only exist to be looked at during it, so
    leaving them behind fills the scratch directory with images nothing will
    read again. Cleanup failing is never worth failing an upload over.
    """
    import shutil

    from lox.uploader.spectrals import get_spectrals_path

    path = get_spectrals_path(folder)
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path)
        flow.note(f"Removed spectral scratch: {os.path.basename(path)}")
    except OSError as e:
        flow.note(f"Could not remove {path}: {e}", "warning")


def _dry_run() -> bool:
    """Whether this run was a dry run, read at report time."""
    from lox import cfg

    return bool(cfg.upload.dry_run)


async def _to_thread(func, *args):
    """Run a blocking call off the event loop."""
    return await asyncio.to_thread(func, *args)
