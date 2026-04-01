"""
Asynchronous state machine governing distributed scraping lifecycles.
Employs cooperative multitasking via `asyncio` tasks and buffers telemetry via in-memory queues,
ensuring that high-frequency I/O completely unblocks WebSocket consumers.
"""

import asyncio
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

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
    event_type: str  # started, result_found, progress, completed, failed, rate_limited
    message: str
    count_found: int = 0
    count_total: Optional[int] = None
    result: Optional[dict] = None
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
    count_total: Optional[int] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
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
        self._queues: dict[str, asyncio.Queue] = {}
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
        config: Optional[dict] = None,
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

        progress_queue: asyncio.Queue = asyncio.Queue()

        async with self._lock:
            self._jobs[job_id] = job
            self._queues[job_id] = progress_queue

        # wrap the actual work in a managed task
        task = asyncio.create_task(
            self._run_job(job_id, run_fn, progress_queue, config)
        )
        self._tasks[job_id] = task

        logger.info(f"Job created: {job_id} ({platform}/{mode} for '{client}')")
        return job_id

    async def _run_job(
        self,
        job_id: str,
        run_fn: Callable[..., Coroutine],
        progress_queue: asyncio.Queue,
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

            await progress_queue.put(
                ProgressEvent(
                    job_id=job_id,
                    platform=job.platform,
                    event_type="started",
                    message=f"Job started for {job.platform}/{job.mode}",
                )
            )

            try:
                # the run_fn must accept a progress_callback and keyword args
                async def progress_callback(
                    event_type: str,
                    message: str,
                    count_found: int = 0,
                    count_total: Optional[int] = None,
                    result: Optional[dict] = None,
                ):
                    job.message = message
                    job.count_found = count_found
                    if count_total is not None:
                        job.count_total = count_total
                        job.progress = (
                            count_found / count_total if count_total > 0 else 0.0
                        )

                    event = ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type=event_type,
                        message=message,
                        count_found=count_found,
                        count_total=count_total,
                        result=result,
                    )
                    await progress_queue.put(event)

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

                await progress_queue.put(
                    ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type="completed",
                        message=job.message,
                        count_found=job.count_found,
                    )
                )

                logger.info(f"Job completed: {job_id} ({job.count_found} results)")

            except asyncio.CancelledError:
                job.status = JobState.CANCELLED
                job.message = "Cancelled by user"
                job.finished_at = datetime.now(timezone.utc)

                await progress_queue.put(
                    ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type="failed",
                        message="Job cancelled",
                    )
                )
                logger.info(f"Job cancelled: {job_id}")

            except Exception as exc:
                job.status = JobState.FAILED
                job.error = str(exc)
                job.message = f"Failed: {exc}"
                job.finished_at = datetime.now(timezone.utc)

                await progress_queue.put(
                    ProgressEvent(
                        job_id=job_id,
                        platform=job.platform,
                        event_type="failed",
                        message=f"Error: {exc}",
                    )
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

    def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[dict]:
        return [job.to_dict() for job in self._jobs.values()]

    def get_progress_queue(self, job_id: str) -> Optional[asyncio.Queue]:
        return self._queues.get(job_id)

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
                self._queues.pop(job_id, None)

        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} finished jobs")

    async def run_cleanup_loop(self, interval_seconds: int = 600, max_age_seconds: int = 3600):
        """Background task: Periodically sweeps memory for finished jobs."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self.cleanup_finished(max_age_seconds)

    async def cancel_all(self):
        """
        Critical SIGTERM hook. 
        Ensures zombie browser processes aren't left orphaned on disk during ungraceful server restarts.
        """
        for job_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                logger.info(f"Shutdown: cancelled job {job_id}")
