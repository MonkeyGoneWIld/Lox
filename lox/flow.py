"""Interactive flows: long operations that ask the browser questions.

This is the piece the previous design got wrong. Uploading needs to ask things
mid-run — is this lossy mastered, which spectrals, new group or existing — and
the old build satisfied that by shelling out to the CLI and piping a terminal
into a ``<pre>``. That is not a user interface; it is a terminal with extra
steps, and it made every prompt a free-text guess.

Here a flow is a coroutine that awaits typed questions. It publishes a Step
describing what it needs — a confirmation, a choice between named options, a
number, a set of checkboxes — and suspends until the UI answers. The browser
renders real controls from the Step's shape and posts back a value. Nothing
parses terminal output, and adding a question to a flow cannot break the UI,
because the UI is generated from the question rather than written to match it.

The same machinery serves progress reporting and non-interactive work, so a
scan and an upload are the same kind of object to the API.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

StepKind = Literal["notice", "confirm", "choice", "multi", "text", "number", "review", "edit"]
FlowState = Literal["running", "waiting", "done", "failed", "cancelled"]

MAX_EVENTS = 500


class FlowCancelled(Exception):
    """Raised inside a flow when the user abandons it."""


class Step:
    """One question, rendered by the UI as real controls.

    Attributes:
        kind: What sort of answer is wanted, which selects the control.
        prompt: The question itself.
        detail: Optional supporting text shown under the prompt.
        options: For ``choice`` and ``multi``, the selectable values.
        default: Pre-selected value.
        danger: Marks an answer as destructive so the UI can style it.
    """

    def __init__(
        self,
        kind: StepKind,
        prompt: str,
        *,
        detail: str = "",
        options: list[dict[str, Any]] | None = None,
        default: Any = None,
        danger: bool = False,
        images: list[dict[str, Any]] | None = None,
        tables: list[dict[str, Any]] | None = None,
        edit_shape: str = "",
        text_label: str = "",
    ) -> None:
        """Initialize a step."""
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.prompt = prompt
        self.detail = detail
        self.options = options or []
        self.default = default
        self.danger = danger
        # Pictures the answer depends on -- spectrals, above all. Deciding
        # whether a release is lossy mastered by reading a filename is not a
        # decision; you have to see them. Each entry pairs a track's full
        # view with its zoom, because they are read together.
        self.images = images or []
        # Structured evidence the answer depends on: the tag diff, the metadata
        # comparison. Prose is fine to read and terrible to check.
        self.tables = tables or []
        # For an ``edit`` step: which form to render. The pipeline edits several
        # different things through one editor call, and they are not the same
        # shape -- a list of credits with roles is not a list of genres.
        self.edit_shape = edit_shape
        # A choice that also accepts something typed. Some prompts name pasting
        # a URL as one of their answers, and offering only buttons made the
        # answer the question names impossible to give -- while replacing the
        # buttons with a text box threw away the candidates it had just found.
        # Both, or neither is right.
        self.text_label = text_label
        self.asked = time.time()

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the UI."""
        return {
            "id": self.id,
            "kind": self.kind,
            "prompt": self.prompt,
            "detail": self.detail,
            "options": self.options,
            "default": self.default,
            "danger": self.danger,
            "images": self.images,
            "tables": self.tables,
            "edit_shape": self.edit_shape,
            "text_label": self.text_label,
        }


class Flow:
    """A running operation the UI can watch and answer."""

    def __init__(self, kind: str, label: str) -> None:
        """Initialize a flow.

        Args:
            kind: Family, e.g. ``upload`` or ``check``.
            label: Human-readable description.
        """
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.state: FlowState = "running"
        self.created = time.time()
        self.finished: float | None = None
        self.error: str | None = None

        self.step: Step | None = None
        self._answer: asyncio.Future | None = None

        self.stage = ""
        self.percent: float | None = None
        self.events: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.task: asyncio.Task | None = None
        #: What this run is about, beyond its label. An upload that fills a
        #: request carries the request here, so the card can link to the thing
        #: being filled rather than leaving the operator to find it -- the one
        #: page where getting the wrong release is not undoable.
        self.context: dict[str, Any] = {}

    # -- driver side ---------------------------------------------------

    async def ask(self, step: Step) -> Any:
        """Publish a question and suspend until the UI answers it.

        Args:
            step: The question.

        Returns:
            The value the user chose.

        Raises:
            FlowCancelled: If the flow is cancelled while waiting.
        """
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self.step = step
        self.state = "waiting"
        try:
            return await self._answer
        finally:
            # answer() already cleared these. Only tidy up if we left by another
            # route, such as cancellation.
            if self.step is step:
                self.step = None
            self._answer = None
            if self.state == "waiting":
                self.state = "running"

    async def confirm(
        self,
        prompt: str,
        *,
        detail: str = "",
        default: bool = True,
        danger: bool = False,
        tables: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Ask a yes/no question."""
        step = Step("confirm", prompt, detail=detail, default=default, danger=danger, tables=tables)
        return bool(await self.ask(step))

    async def choose(
        self,
        prompt: str,
        options: list[dict[str, Any]],
        *,
        detail: str = "",
        default: Any = None,
        images: list[dict[str, Any]] | None = None,
        tables: list[dict[str, Any]] | None = None,
        text_label: str = "",
    ) -> Any:
        """Ask the user to pick one of a named set.

        Args:
            prompt: The question.
            options: Dicts with ``value`` and ``label``, optionally ``detail``.
            detail: Supporting text.
            default: Pre-selected value.
            images: Pictures shown with the question.

        Returns:
            The chosen ``value``.
        """
        return await self.ask(
            Step("choice", prompt, detail=detail, options=options, default=default,
                 images=images, tables=tables, text_label=text_label)
        )

    async def choose_many(
        self,
        prompt: str,
        options: list[dict[str, Any]],
        *,
        detail: str = "",
        default: list[Any] | None = None,
    ) -> list[Any]:
        """Ask the user to pick any number of a named set."""
        answer = await self.ask(Step("multi", prompt, detail=detail, options=options, default=default or []))
        return list(answer or [])

    async def text(
        self,
        prompt: str,
        *,
        detail: str = "",
        default: str = "",
        images: list[dict[str, Any]] | None = None,
        tables: list[dict[str, Any]] | None = None,
    ) -> str:
        """Ask for free text, where free text is genuinely what is wanted."""
        step = Step("text", prompt, detail=detail, default=default, images=images, tables=tables)
        return str(await self.ask(step) or "")

    async def review(self, prompt: str, rows: list[dict[str, Any]], *, detail: str = "") -> bool:
        """Show a table of facts and ask whether to go ahead.

        Args:
            prompt: The question.
            rows: Dicts with ``label`` and ``value``.
            detail: Supporting text.

        Returns:
            True to proceed.
        """
        return bool(await self.ask(Step("review", prompt, detail=detail, options=rows, default=True)))

    def progress(self, stage: str, percent: float | None = None) -> None:
        """Report what the flow is doing now.

        Args:
            stage: Short description of the current phase.
            percent: Completion of this phase, if known.
        """
        self.stage = stage
        self.percent = percent

    def note(self, message: str, level: str = "info") -> None:
        """Record something worth showing but not worth asking about."""
        self.events.append({"at": time.time(), "level": level, "message": message})
        del self.events[:-MAX_EVENTS]

    # -- UI side -------------------------------------------------------

    def answer(self, step_id: str, value: Any) -> bool:
        """Deliver an answer from the browser.

        Args:
            step_id: The step being answered, so a stale tab cannot answer a
                question that has already moved on.
            value: The user's answer.

        Returns:
            True if the answer was accepted.
        """
        if not self.step or self.step.id != step_id or not self._answer or self._answer.done():
            return False
        # Retire the step here rather than when the driver resumes. The driver
        # only wakes on the next loop tick, and a UI that polls in between would
        # otherwise still see a question it has already answered -- and could
        # answer it twice.
        self.step = None
        self.state = "running"
        self._answer.set_result(value)
        return True

    def cancel(self) -> bool:
        """Abandon the flow, waking it if it is waiting on an answer."""
        if self.state in ("done", "failed", "cancelled"):
            return False
        if self._answer and not self._answer.done():
            self._answer.set_exception(FlowCancelled(self.label))
        elif self.task and not self.task.done():
            self.task.cancel()
        self.state = "cancelled"
        return True

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the UI."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "stage": self.stage,
            "percent": self.percent,
            "step": self.step.as_dict() if self.step else None,
            "events": self.events[-60:],
            "result": self.result,
            "error": self.error,
            "created": self.created,
            "finished": self.finished,
            "context": self.context,
        }


class FlowRegistry:
    """Every flow for the lifetime of the process."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self.flows: dict[str, Flow] = {}

    def start(
        self,
        kind: str,
        label: str,
        driver: Callable[[Flow], Awaitable[Any]],
        context: dict[str, Any] | None = None,
    ) -> Flow:
        """Run a driver coroutine as a flow.

        Args:
            kind: Family.
            label: Description.
            driver: Coroutine taking the flow.
            context: What the run is about, set before the driver starts. It
                has to be in place first: the driver adds to it as it goes --
                which request it filled, and on which tracker -- and a caller
                assigning it afterwards would wipe that.

        Returns:
            The registered flow, already running.
        """
        flow = Flow(kind, label)
        flow.context = dict(context or {})
        self.flows[flow.id] = flow

        async def run() -> None:
            try:
                result = await driver(flow)
                if isinstance(result, dict):
                    flow.result = result
                if flow.state not in ("cancelled", "failed"):
                    flow.state = "done"
            except FlowCancelled:
                flow.state = "cancelled"
                flow.note("Cancelled.", "warning")
            except asyncio.CancelledError:
                flow.state = "cancelled"
                raise
            except Exception as e:  # noqa: BLE001 - surfaced to the UI
                flow.state = "failed"
                flow.error = f"{type(e).__name__}: {e}"
                flow.note(flow.error, "error")
            finally:
                flow.finished = time.time()
                flow.step = None

        flow.task = asyncio.create_task(run())
        return flow

    def get(self, flow_id: str) -> Flow | None:
        """Look up a flow."""
        return self.flows.get(flow_id)

    def active(self, kind: str | None = None) -> list[Flow]:
        """Flows still running or waiting, newest first."""
        found = [
            f for f in self.flows.values()
            if f.state in ("running", "waiting") and (kind is None or f.kind == kind)
        ]
        return sorted(found, key=lambda f: f.created, reverse=True)

    def summaries(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Serialize every flow, newest first."""
        flows = [f for f in self.flows.values() if kind is None or f.kind == kind]
        flows.sort(key=lambda f: f.created, reverse=True)
        return [f.as_dict() for f in flows]

    def dismiss(self, flow_id: str) -> bool:
        """Drop one finished flow.

        A finished upload card stayed on the page until the browser was
        reloaded, so the tab that says what is uploading kept showing what
        already had. Only finished ones go: a run still working is dismissed by
        cancelling it, which is a different decision and says so.

        Args:
            flow_id: The flow to forget.

        Returns:
            True when it was there and finished.
        """
        flow = self.flows.get(flow_id)
        if flow is None or flow.state not in ("done", "failed", "cancelled"):
            return False
        del self.flows[flow_id]
        return True

    def clear_finished(self) -> int:
        """Drop finished flows. Returns how many went."""
        done = [k for k, v in self.flows.items() if v.state in ("done", "failed", "cancelled")]
        for key in done:
            del self.flows[key]
        return len(done)
