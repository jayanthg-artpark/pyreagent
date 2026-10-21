"""Background jobs for long operations.

A plate read or a temperature ramp can take minutes, so those capabilities
return a job immediately and the caller polls ``GET /jobs/{id}``.
"""

from __future__ import annotations

import asyncio
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, PrivateAttr

from .errors import Cancelled, NotFound, PyreagentError, translate_exception


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINISHED = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class Job(BaseModel):
    id: str
    device: str
    capability: str
    args: dict[str, Any]
    caller: str
    status: JobStatus = JobStatus.PENDING
    progress: float | None = Field(default=None, description="0.0 to 1.0 when the device reports it.")
    message: str | None = Field(default=None, description="Latest progress message from the device.")
    result: Any = None
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _exception: PyreagentError | None = PrivateAttr(default=None)

    @property
    def finished(self) -> bool:
        return self.status in FINISHED


_current_job: ContextVar[Job | None] = ContextVar("pyreagent_current_job", default=None)


def current_job() -> Job | None:
    """The job whose task is running the current coroutine, if any."""
    return _current_job.get()


class JobManager:
    def __init__(self, *, retention: int = 500) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._retention = retention

    def submit(
        self,
        *,
        device: str,
        capability: str,
        args: dict[str, Any],
        caller: str,
        run: Callable[[], Awaitable[Any]],
        on_finished: Callable[[Job], None] | None = None,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], device=device, capability=capability, args=args, caller=caller)
        self._jobs[job.id] = job
        # Eager start runs the job synchronously up to its first await, so the
        # device mutex is taken before submit() returns: no window in which a
        # second caller could slip in between "accepted" and "running".
        task = asyncio.Task(
            self._run(job, run, on_finished),
            loop=asyncio.get_running_loop(),
            name=f"pyreagent-job-{job.id}",
            eager_start=True,
        )
        if not task.done():
            self._tasks[job.id] = task
        self._prune()
        return job

    async def _run(self, job: Job, run: Callable[[], Awaitable[Any]], on_finished: Callable[[Job], None] | None) -> None:
        _current_job.set(job)
        job.status = JobStatus.RUNNING
        job.started_at = utcnow()
        try:
            job.result = await run()
            job.status = JobStatus.SUCCEEDED
            job.progress = 1.0
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.error = Cancelled(f"Job {job.id} was cancelled.").to_dict()
        except Exception as exc:
            job.status = JobStatus.FAILED
            job._exception = translate_exception(exc)
            job.error = job._exception.to_dict()
        finally:
            job.finished_at = utcnow()
            self._tasks.pop(job.id, None)
            if on_finished is not None:
                on_finished(job)

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise NotFound(f"No job with id {job_id!r}.", suggested_action="Call GET /jobs to list recent jobs.") from None

    def list(self, *, device: str | None = None, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        jobs = [j for j in self._jobs.values() if (device is None or j.device == device) and (status is None or j.status == status)]
        return list(reversed(jobs))[:limit]

    def running_for(self, device: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.device == device and not j.finished]

    async def wait(self, job_id: str, timeout_s: float) -> Job:
        """Long-poll: return as soon as the job finishes, or after ``timeout_s``."""
        job = self.get(job_id)
        task = self._tasks.get(job_id)
        if task is not None and timeout_s > 0:
            await asyncio.wait({task}, timeout=timeout_s)
        return job

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=10)
        return job

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=10)

    def _prune(self) -> None:
        if len(self._jobs) <= self._retention:
            return
        for job_id in [j.id for j in self._jobs.values() if j.finished][: len(self._jobs) - self._retention]:
            del self._jobs[job_id]
