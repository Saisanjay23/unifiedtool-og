"""
Health monitoring for scraping sessions across all platforms.
Tracks request success/failure rates, selector reliability,
and manages adaptive rate limiting with predictive pausing.
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.core.config import settings
from backend.core.logger import get_logger

logger = get_logger("health")


@dataclass
class PlatformHealth:
    """Tracks the health state for a single platform."""

    platform: str
    health_score: float = 1.0
    total_requests: int = 0
    total_errors: int = 0
    requests_this_hour: int = 0
    hour_window_start: float = 0.0
    consecutive_errors: int = 0
    is_suspended: bool = False
    suspension_reason: str = ""
    last_request_at: float = 0.0
    last_error_at: float = 0.0
    last_rate_limit_at: float = 0.0
    cooldown_until: float = 0.0
    selector_hits: int = 0
    selector_misses: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class HealthManager:
    """
    Singleton that monitors request health across all platforms.
    Provides adaptive rate limiting and correlated failure detection.
    """

    _instance: Optional["HealthManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._platforms: dict[str, PlatformHealth] = {}
        for platform in ["facebook", "instagram", "twitter", "youtube", "telegram", "tiktok"]:
            self._platforms[platform] = PlatformHealth(platform=platform)

        self._lock = asyncio.Lock()
        self._degradation_timestamps: list[float] = []

        logger.info("HealthManager initialized for all platforms")

    def _get_platform(self, platform: str) -> PlatformHealth:
        key = platform.lower()
        if key not in self._platforms:
            self._platforms[key] = PlatformHealth(platform=key)
        return self._platforms[key]

    def _reset_hour_window(self, health: PlatformHealth):
        """Roll over the hourly request counter if the window has expired."""
        now = time.time()
        if now - health.hour_window_start >= 3600:
            health.requests_this_hour = 0
            health.hour_window_start = now

    async def record_request(
        self, platform: str, success: bool, status_code: int | None = None
    ):
        """Record a single request outcome."""
        async with self._lock:
            health = self._get_platform(platform)
            now = time.time()

            health.total_requests += 1
            health.requests_this_hour += 1
            health.last_request_at = now
            self._reset_hour_window(health)

            if success:
                health.consecutive_errors = 0
            else:
                health.total_errors += 1
                health.consecutive_errors += 1
                health.last_error_at = now

                # handle specific status codes
                if status_code in (429, 403):
                    health.last_rate_limit_at = now
                    # exponential cooldown: 30s, 60s, 120s, 240s based on consecutive errors
                    backoff = min(
                        240, 30 * (2 ** min(health.consecutive_errors - 1, 3))
                    )
                    health.cooldown_until = now + backoff
                    logger.warning(
                        f"{platform}: Rate limited (HTTP {status_code}), "
                        f"cooldown for {backoff}s"
                    )

                if status_code == 401:
                    health.is_suspended = True
                    health.suspension_reason = "Session expired (401)"

            self._recalculate_score(health)
            self._check_correlated_failure(now)

    async def record_selector_hit(self, platform: str, selector: str, success: bool):
        """Track whether a CSS selector found content on the page."""
        async with self._lock:
            health = self._get_platform(platform)
            if success:
                health.selector_hits += 1
            else:
                health.selector_misses += 1
            self._recalculate_score(health)

    def _recalculate_score(self, health: PlatformHealth):
        """
        Health score is a weighted composite:
        - 50%: error rate (errors / total requests)
        - 30%: consecutive error penalty
        - 20%: rate limit proximity (how close to hourly limit)
        """
        if health.is_suspended:
            health.health_score = 0.0
            return

        score = 1.0

        # error rate component
        if health.total_requests > 0:
            error_rate = health.total_errors / health.total_requests
            score -= error_rate * 0.5

        # consecutive error penalty
        if health.consecutive_errors >= 5:
            score -= 0.3
        elif health.consecutive_errors >= 3:
            score -= 0.15

        # rate limit proximity (predictive)
        hourly_limit = settings.get_rate_limit(health.platform)
        if hourly_limit > 0:
            utilization = health.requests_this_hour / hourly_limit
            if utilization > 0.8:
                score -= (utilization - 0.8) * 1.0  # up to -0.2 at 100%

        # selector reliability
        total_selectors = health.selector_hits + health.selector_misses
        if total_selectors > 10:
            selector_rate = health.selector_misses / total_selectors
            if selector_rate > 0.3:
                score -= 0.1

        health.health_score = max(0.0, min(1.0, score))

    def _check_correlated_failure(self, now: float):
        """
        If 3+ platforms degrade within 5 minutes, suspect an IP block.
        """
        degraded_count = sum(
            1
            for h in self._platforms.values()
            if h.health_score < settings.HEALTH_DEGRADED_THRESHOLD
        )

        if degraded_count >= 3:
            # check if this is a new correlation event
            recent_events = [t for t in self._degradation_timestamps if now - t < 300]
            if len(recent_events) < 3:
                self._degradation_timestamps.append(now)
                logger.warning(
                    f"IP_BLOCK_SUSPECTED: {degraded_count} platforms degraded simultaneously"
                )

    def get_health_score(self, platform: str) -> float:
        health = self._get_platform(platform)
        return health.health_score

    def get_health_status(self, platform: str) -> str:
        score = self.get_health_score(platform)
        health = self._get_platform(platform)

        if health.is_suspended:
            return "suspended"
        if score >= settings.HEALTH_DEGRADED_THRESHOLD:
            return "healthy"
        if score >= settings.HEALTH_CRITICAL_THRESHOLD:
            return "degraded"
        return "critical"

    def get_all_health(self) -> dict:
        """Return health data for all platforms."""
        result = {}
        for name, health in self._platforms.items():
            self._reset_hour_window(health)
            result[name] = {
                "score": round(health.health_score, 3),
                "status": self.get_health_status(name),
                "total_requests": health.total_requests,
                "requests_this_hour": health.requests_this_hour,
                "consecutive_errors": health.consecutive_errors,
                "is_suspended": health.is_suspended,
                "last_request_at": health.last_request_at,
            }
        return result

    def should_pause(self, platform: str) -> bool:
        """Check if we should hold off on requests."""
        health = self._get_platform(platform)
        now = time.time()

        # respect cooldown periods
        if now < health.cooldown_until:
            return True

        # pause if suspended
        if health.is_suspended:
            return True

        # pause if health is critical
        if health.health_score < settings.HEALTH_CRITICAL_THRESHOLD:
            return True

        # pause if approaching rate limit
        hourly_limit = settings.get_rate_limit(platform)
        if health.requests_this_hour >= hourly_limit * 0.9:
            return True

        return False

    def get_recommended_delay(self, platform: str) -> float:
        """
        Returns an adaptive delay in seconds.
        Longer delays when health is lower.
        """
        health = self._get_platform(platform)
        now = time.time()

        # if in cooldown, return remaining cooldown time
        if now < health.cooldown_until:
            return health.cooldown_until - now

        score = health.health_score

        if score > 0.8:
            return 2.0
        elif score > 0.6:
            return 5.0
        elif score > 0.4:
            return 10.0
        else:
            return 30.0

    async def write_health_snapshot(self):
        """Write current health to the log file (called periodically)."""
        snapshot = self.get_all_health()
        snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()

        log_file = os.path.join(settings.LOG_PATH, "health.jsonl")
        os.makedirs(settings.LOG_PATH, exist_ok=True)

        try:
            def _write():
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(snapshot, default=str) + "\n")
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.error(f"Failed to write health snapshot: {exc}")

    async def run_health_writer(self):
        """Background task: write health snapshot every 60 seconds."""
        while True:
            await self.write_health_snapshot()
            await asyncio.sleep(60)

    def clear_suspension(self, platform: str):
        """Manually clear a platform suspension (e.g., after re-login)."""
        health = self._get_platform(platform)
        health.is_suspended = False
        health.suspension_reason = ""
        health.consecutive_errors = 0
        health.cooldown_until = 0.0
        self._recalculate_score(health)
        logger.info(f"{platform}: Suspension cleared manually")
