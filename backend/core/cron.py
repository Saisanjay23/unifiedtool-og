"""
Cron-based keyword monitoring scheduler for the Unified Social Media Tool.
Uses APScheduler with AsyncIOScheduler to run discovery jobs on a schedule.
Reads job definitions from a JSON file and supports hot-reload.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.core.config import settings
from backend.core.logger import get_logger
from backend.core.health import HealthManager
from backend.core.db import save_result

logger = get_logger("core.cron")


class CronScheduler:
    """
    Manages scheduled scraping jobs defined in a JSON config file.
    Each job runs discovery for the specified client/platform/keywords
    at the specified cron interval.
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.health = HealthManager()
        self._config_path = settings.CRON_JOBS_FILE
        self._last_modified: float = 0.0
        self._loaded_jobs: dict[str, dict] = {}

    async def start(self):
        """Load jobs from config and start the scheduler."""
        self._load_config()
        self.scheduler.start()
        logger.info(f"Cron scheduler started with {len(self._loaded_jobs)} jobs")

    def _load_config(self):
        """Read the cron config file and register/update all jobs."""
        if not os.path.exists(self._config_path):
            logger.warning(f"Cron config not found: {self._config_path}")
            return

        try:
            mtime = os.path.getmtime(self._config_path)
            if mtime == self._last_modified:
                return

            with open(self._config_path, "r", encoding="utf-8") as f:
                jobs_config = json.load(f)

            self._last_modified = mtime

            # remove existing jobs that are no longer in config
            current_ids = {j["job_id"] for j in jobs_config if "job_id" in j}
            for existing_id in list(self._loaded_jobs.keys()):
                if existing_id not in current_ids:
                    try:
                        self.scheduler.remove_job(existing_id)
                    except Exception:
                        pass
                    del self._loaded_jobs[existing_id]
                    logger.info(f"Removed cron job: {existing_id}")

            # add or update jobs
            for job_config in jobs_config:
                job_id = job_config.get("job_id")
                if not job_id:
                    continue

                schedule = job_config.get("schedule", "0 */6 * * *")

                # parse cron fields
                cron_parts = schedule.split()
                if len(cron_parts) != 5:
                    logger.warning(
                        f"Invalid cron expression for job {job_id}: {schedule}"
                    )
                    continue

                trigger = CronTrigger(
                    minute=cron_parts[0],
                    hour=cron_parts[1],
                    day=cron_parts[2],
                    month=cron_parts[3],
                    day_of_week=cron_parts[4],
                )

                # remove old version if exists
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass

                self.scheduler.add_job(
                    self._execute_job,
                    trigger=trigger,
                    id=job_id,
                    args=[job_config],
                    replace_existing=True,
                    misfire_grace_time=300,
                )

                self._loaded_jobs[job_id] = job_config
                logger.info(f"Registered cron job: {job_id} ({schedule})")

        except Exception as exc:
            logger.error(f"Failed to load cron config: {exc}")

    async def _execute_job(self, job_config: dict):
        """Execute a single scheduled job."""
        job_id = job_config["job_id"]
        client_name = job_config.get("client", "cron")
        platforms = job_config.get("platforms", [])
        keywords = job_config.get("keywords", [])
        max_results = job_config.get("max_results", 50)

        logger.info(f"Cron job starting: {job_id} — {platforms} — {keywords}")

        for platform in platforms:
            try:
                discoverer = self._get_discoverer(platform)
                if not discoverer:
                    logger.warning(f"Unknown platform in cron job: {platform}")
                    continue

                async def noop_callback(**kwargs):
                    pass

                results = await discoverer.search(
                    progress_callback=noop_callback,
                    client=client_name,
                    keywords=keywords,
                    max_results=max_results,
                    headless=True,
                )

                # save results to DB
                for result in results:
                    await save_result(result)

                logger.info(
                    f"Cron job {job_id}: {platform} found {len(results)} results"
                )

            except Exception as exc:
                logger.error(f"Cron job {job_id}: {platform} failed — {exc}")

        logger.info(f"Cron job completed: {job_id}")

    def _get_discoverer(self, platform: str):
        """Instantiate the appropriate discoverer for a platform."""
        if platform == "facebook":
            from backend.platforms.facebook.discovery import FacebookDiscoverer

            return FacebookDiscoverer(config=settings, health=self.health)
        elif platform == "instagram":
            from backend.platforms.instagram.discovery import InstagramDiscoverer

            return InstagramDiscoverer(config=settings, health=self.health)
        elif platform == "twitter":
            from backend.platforms.twitter.discovery import TwitterDiscoverer

            return TwitterDiscoverer(config=settings, health=self.health)
        elif platform == "youtube":
            from backend.platforms.youtube.discovery import YouTubeDiscoverer

            return YouTubeDiscoverer(config=settings, health=self.health)
        elif platform == "telegram":
            from backend.platforms.telegram.discovery import TelegramDiscoverer

            return TelegramDiscoverer(config=settings, health=self.health)
        elif platform == "tiktok":
            from backend.platforms.tiktok.discovery import TikTokDiscoverer

            return TikTokDiscoverer(config=settings, health=self.health)
        return None

    async def _watch_config(self):
        """Periodically check if the config file has been modified and reload."""
        while True:
            await asyncio.sleep(30)
            self._load_config()

    async def run_forever(self):
        """
        Main blocking loop for --cron mode.
        Starts the scheduler and the config watcher, then blocks.
        """
        await self.start()
        watcher_task = asyncio.create_task(self._watch_config())

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            watcher_task.cancel()
            self.scheduler.shutdown(wait=False)
            logger.info("Cron scheduler stopped")

    def get_scheduled_jobs(self) -> list[dict]:
        """Return information about all registered cron jobs."""
        jobs = []
        for job_id, config in self._loaded_jobs.items():
            scheduled_job = self.scheduler.get_job(job_id)
            next_run = None
            if scheduled_job and scheduled_job.next_run_time:
                next_run = scheduled_job.next_run_time.isoformat()

            jobs.append(
                {
                    "job_id": job_id,
                    "client": config.get("client", ""),
                    "platforms": config.get("platforms", []),
                    "keywords": config.get("keywords", []),
                    "schedule": config.get("schedule", ""),
                    "max_results": config.get("max_results", 50),
                    "next_run": next_run,
                }
            )
        return jobs
