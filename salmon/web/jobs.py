"""In-memory registry for long-running UI operations.

Scans and uploads outlive a single HTTP request, so they run as background tasks
and the browser polls for progress. Jobs are kept after finishing so results stay
readable until explicitly cleared.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

MAX_EVENTS = 400
MAX_LOG_LINES = 2000
MAX_RESULTS = 5000
"""Ceiling on retained results. A runaway scan must not exhaust memory."""


class Job:
    """One background operation and everything the UI needs to render it."""

    def __init__(self, kind: str, label: str) -> None:
        """Initialize a job.

        Args:
            kind: Job family, e.g. ``missing_collect`` or ``upload``.
            label: Human-readable description.
        """
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.status = "running"
        self.created = time.time()
        self.finished: float | None = None
        self.error: str | None = None
        self.progress: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.log: list[str] = []
        self.task: asyncio.Task | None = None
        self.stdin: asyncio.StreamWriter | None = None

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """Record an event from the running operation.

        ``progress`` events update the summary in place; ``result`` events are
        appended to the result list. Everything else lands in the event log.

        Args:
            event: Event name.
            payload: Event body.
        """
        if event == "progress":
            self.progress = payload
            return
        if event == "result":
            if len(self.results) < MAX_RESULTS:
                self.results.append(payload)
            return
        self.events.append({"event": event, "at": time.time(), **payload})
        del self.events[:-MAX_EVENTS]

    def write_log(self, line: str) -> None:
        """Append a line of subprocess output."""
        self.log.append(line)
        del self.log[:-MAX_LOG_LINES]

    def as_dict(self, since: int = 0) -> dict[str, Any]:
        """Serialize for the web API.

        Args:
            since: Index of the first result the client has not seen, so polling
                does not re-send the whole list every time.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "error": self.error,
            "progress": self.progress,
            "created": self.created,
            "finished": self.finished,
            "events": self.events[-40:],
            "results": self.results[since:],
            "result_count": len(self.results),
            "log": self.log[-200:],
            "accepts_input": self.stdin is not None and self.status == "running",
        }


class JobRegistry:
    """Holds every job for the lifetime of the server process."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self.jobs: dict[str, Job] = {}

    def create(self, kind: str, label: str) -> Job:
        """Create and register a job."""
        job = Job(kind, label)
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job by ID."""
        return self.jobs.get(job_id)

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Summarize jobs, newest first, optionally filtered by kind."""
        jobs = [j for j in self.jobs.values() if kind is None or j.kind == kind]
        jobs.sort(key=lambda j: j.created, reverse=True)
        return [
            {
                "id": j.id,
                "kind": j.kind,
                "label": j.label,
                "status": j.status,
                "created": j.created,
                "finished": j.finished,
                "result_count": len(j.results),
                "error": j.error,
            }
            for j in jobs
        ]

    def spawn(self, kind: str, label: str, coro_factory: Callable[[Job], Awaitable[Any]]) -> Job:
        """Start a coroutine as a background job.

        Args:
            kind: Job family.
            label: Human-readable description.
            coro_factory: Callable taking the job and returning the coroutine.

        Returns:
            The registered job, already running.
        """
        job = self.create(kind, label)

        async def runner() -> None:
            try:
                await coro_factory(job)
                if job.status == "running":
                    job.status = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                raise
            except Exception as e:  # noqa: BLE001 - surfaced to the UI via job.error
                job.status = "failed"
                job.error = str(e)
            finally:
                job.finished = time.time()

        job.task = asyncio.create_task(runner())
        return job

    def cancel(self, job_id: str) -> bool:
        """Cancel a running job. Returns True if a task was cancelled."""
        job = self.get(job_id)
        if not job or not job.task or job.task.done():
            return False
        job.task.cancel()
        return True

    def clear_finished(self) -> int:
        """Drop finished jobs. Returns how many were removed."""
        finished = [k for k, v in self.jobs.items() if v.status in ("done", "failed", "cancelled")]
        for key in finished:
            del self.jobs[key]
        return len(finished)
