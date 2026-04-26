"""
Asynchronous state machine governing distributed scraping lifecycles.
Employs cooperative multitasking via `asyncio` tasks and buffers telemetry via in-memory queues,
ensuring that high-frequency I/O completely unblocks WebSocket consumers.
"""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from backend.core.config import settings
from backend.core.logger import get_logger

logger = get_logger("jobs")


@dataclass
class ProgressEvent:
    """
    Event-driven payload contract. 
    Represents a single idempotent state transition intended for real-time frontend telemetry.
    """

    job_id: str
    platform: str
    event_type: str  # started, result_found, progress, completed, failed, cancelled, rate_limited
    message: str
    seq: int = 0
    count_found: int = 0
    count_total: int | None = None
    result: dict | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = d["timestamp"].isoformat()

        # Helper to recursively serialize datetimes in nested dicts/lists
        def _serialize(obj: Any) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            elif isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_serialize(i) for i in obj]
            return obj

        if d.get("result"):
            d["result"] = _serialize(d["result"])

        return d


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class JobStatus:
    """
    The source of truth for a job's finite state machine (FSM).
    Tracks aggregate scraping metadata across the task lifecycle boundaries.
    """

    job_id: str
    platform: str
    mode: str  # discovery or analysis
    client: str
    keywords: list[str]
    status: JobState
    progress: float = 0.0
    message: str = "Queued"
    count_found: int = 0
    count_total: int | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("created_at", "started_at", "finished_at"):
            if d[key] is not None:
                d[key] = d[key].isoformat()
        return d


class JobManager:
    """
    Concurrency Gateway.
    Singleton pattern guaranteeing thread-safe limits strictly bound by an `asyncio.Semaphore`. 
    Prevents CPU saturation and IP ban avalanches by globally gating concurrent browser initialization.
    """

    _instance: Optional["JobManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._jobs: dict[str, JobStatus] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._history: dict[str, list[ProgressEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._next_seq: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_JOBS)

        logger.info(
            f"JobManager initialized (max concurrent: {settings.MAX_CONCURRENT_JOBS})"
        )

    async def create_job(
        self,
        platform: str,
        mode: str,
        client: str,
        keywords: list[str],
        run_fn: Callable[..., Coroutine],
        config: dict | None = None,
    ) -> str:
        """
        Task Orchestration injection point.
        Reserves concurrency capacity and encapsulates the unit of work inside the event loop.
        Initializes isolated memory channels (`asyncio.Queue`) to prevent event cross-talk.
        
        Args:
            platform: Target namespace
            mode: Logical execution mode ("discovery" | "analysis")
            client: Tenant routing key
            keywords: Operational payload
            run_fn: Untrusted asynchronous logic binding
            config: Dependency injection kwargs
        """
        job_id = f"{platform}_{mode}_{uuid.uuid4().hex[:8]}"
        config = config or {}

        job = JobStatus(
            job_id=job_id,
            platform=platform,
            mode=mode,
            client=client,
            keywords=keywords,
            status=JobState.QUEUED,
            config=config,
        )

        async with self._lock:
            self._jobs[job_id] = job
            self._history[job_id] = []
            self._subscribers[job_id] = set()
            self._next_seq[job_id] = 0

        # wrap the actual work in a managed task
        task = asyncio.create_task(
            self._run_job(job_id, run_fn, config)
        )
        self._tasks[job_id] = task

        logger.info(f"Job created: {job_id} ({platform}/{mode} for '{client}')")
        return job_id

    async def _run_job(
        self,
        job_id: str,
        run_fn: Callable[..., Coroutine],
        config: dict,
    ):
        """
        Protected Coroutine Boundary. 
        Wraps untrusted scraping logic inside safety nets, managing strict FSM transitions 
        and guaranteeing the ultimate release of the concurrency semaphore.
        """
        job = self._jobs[job_id]

        # wait for a concurrency slot
        async with self._semaphore:
            job.status = JobState.RUNNING
            job.started_at = datetime.now(timezone.utc)
            job.message = "Starting..."

            await self._publish_event(
                job_id,
                ProgressEvent(
                    job_id=job_id,
                    platform=job.platform,
                    event_type="started",
                    message=f"Job started for {job.platform}/{job.mode}",
                ),
            )

            try:
                # the run_fn must accept a progress_callback and keyword args
                async def progress_callback(
                    event_type: str,
                    message: str,
                    count_found: int = 0,
                    count_total: int | None = None,
                    result: dict | None = None,
                ):
                    job.message = message
                    job.count_found = max(job.count_found, count_found)
                    if count_total is not None:
                        job.count_total = count_total
                        current_progress = (
                            count_found / count_total if count_total > 0 else 0.0
                        )
                        job.progress = max(job.progress, current_progress)

                    event = ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type=event_type,
                        message=message,
                        count_found=count_found,
                        count_total=count_total,
                        result=result,
                    )
                    await self._publish_event(job_id, event)

                await run_fn(
                    progress_callback=progress_callback,
                    client=job.client,
                    keywords=job.keywords,
                    **config,
                )

                job.status = JobState.COMPLETED
                job.progress = 1.0
                job.message = f"Completed — found {job.count_found} results"
                job.finished_at = datetime.now(timezone.utc)

                await self._publish_event(
                    job_id,
                    ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type="completed",
                        message=job.message,
                        count_found=job.count_found,
                    ),
                )

                logger.info(f"Job completed: {job_id} ({job.count_found} results)")

            except asyncio.CancelledError:
                job.status = JobState.CANCELLED
                job.message = "Cancelled by user"
                job.finished_at = datetime.now(timezone.utc)

                await self._publish_event(
                    job_id,
                    ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type="cancelled",
                        message="Job cancelled",
                        count_found=job.count_found,
                        count_total=job.count_total,
                    ),
                )
                logger.info(f"Job cancelled: {job_id}")

            except Exception as exc:
                job.status = JobState.FAILED
                job.error = str(exc)
                job.message = f"Failed: {exc}"
                job.finished_at = datetime.now(timezone.utc)

                await self._publish_event(
                    job_id,
                    ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type="failed",
                        message=f"Error: {exc}",
                        count_found=job.count_found,
                        count_total=job.count_total,
                    ),
                )
                logger.error(f"Job failed: {job_id} — {exc}", exc_info=True)

    async def cancel_job(self, job_id: str) -> bool:
        """
        Preemptive Task Termination. 
        Leverages `asyncio`'s internal `cancel()` propagation to violently interrupt headless page execution.
        """
        if job_id in self._tasks:
            task = self._tasks[job_id]
            if not task.done():
                task.cancel()
                logger.info(f"Cancellation requested for job {job_id}")
                return True
        return False

    def get_job_status(self, job_id: str) -> JobStatus | None:
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[dict]:
        return [job.to_dict() for job in self._jobs.values()]

    async def subscribe(
        self, job_id: str, after_seq: int = 0
    ) -> tuple[list[ProgressEvent], asyncio.Queue] | None:
        """
        Register a live subscriber and return any backlog after the provided sequence.
        """
        async with self._lock:
            if job_id not in self._jobs:
                return None

            queue: asyncio.Queue = asyncio.Queue()
            history = [
                event for event in self._history.get(job_id, []) if event.seq > after_seq
            ]
            self._subscribers.setdefault(job_id, set()).add(queue)
            return history, queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(job_id)
            if subscribers is not None:
                subscribers.discard(queue)

    async def _publish_event(self, job_id: str, event: ProgressEvent) -> None:
        async with self._lock:
            next_seq = self._next_seq.get(job_id, 0) + 1
            self._next_seq[job_id] = next_seq
            event.seq = next_seq

            history = self._history.setdefault(job_id, [])
            history.append(event)
            subscribers = list(self._subscribers.get(job_id, ()))

        for subscriber in subscribers:
            subscriber.put_nowait(event)

    async def cleanup_finished(self, max_age_seconds: int = 3600):
        """
        Memory leak mitigation. 
        Sweeps dead FSM pointers from the manager's heap structures to prevent boundless memory growth on long-running servers.
        """
        now = datetime.now(timezone.utc)
        to_remove = []

        for job_id, job in self._jobs.items():
            if job.status in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                if (
                    job.finished_at
                    and (now - job.finished_at).total_seconds() > max_age_seconds
                ):
                    to_remove.append(job_id)

        async with self._lock:
            for job_id in to_remove:
                self._jobs.pop(job_id, None)
                self._tasks.pop(job_id, None)
                self._history.pop(job_id, None)
                self._subscribers.pop(job_id, None)
                self._next_seq.pop(job_id, None)

        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} finished jobs")

    async def run_cleanup_loop(self, interval_seconds: int = 600, max_age_seconds: int = 3600):
        """Background task: Periodically sweeps memory for finished jobs."""
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await self.cleanup_finished(max_age_seconds)
        except asyncio.CancelledError:
            logger.info("Job cleanup loop stopped")
            raise

    async def cancel_all(self):
        """
        Critical SIGTERM hook. 
        Ensures zombie browser processes aren't left orphaned on disk during ungraceful server restarts.
        """
        tasks_to_wait: list[asyncio.Task] = []

        for job_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                tasks_to_wait.append(task)
                logger.info(f"Shutdown: cancelled job {job_id}")

        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
