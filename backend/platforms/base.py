"""
Defines the Polymorphic Strategy Pattern for platform scraping.
Establishes strict interface contracts allowing the `JobManager` to orchestrate 
disparate platforms interchangeably without tight coupling to their underlying DOM/API mechanics.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Coroutine, Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.stealth.human import HumanBehavior


class AbstractDiscoverer(ABC):
    """
    Broad-phase OSINT collection.
    Subclasses must implement the transformation of raw, unstructured platform DOM trees/API responses 
    into the strictly typed and unified `ProfileResult` schema.
    """

    def __init__(
        self,
        config: Settings,
        health: HealthManager,
    ):
        self.config = config
        self.health = health

    @abstractmethod
    async def search(
        self,
        progress_callback: Callable[..., Coroutine],
        client: str,
        keywords: list[str],
        max_results: int = 50,
        headless: bool = True,
        **kwargs,
    ) -> list[ProfileResult]:
        """
        Executes a horizontal keyword sweep.

        Contract Requirements:
            - Must continuously yield via `progress_callback` to ensure UI telemetry isn't blocked.
            - Must strictly honor `max_results` to prevent aggressive rate-limiting or IP burning.
            - Must gracefully handle transient 429s or DOM shifts without crashing the event loop.

        Args:
            progress_callback: Asynchronous injection for real-time WebSocket telemetry
            client: Tenant/workspace boundary identifier
            keywords: Operational payload
            max_results: Strict ceiling for collection
            headless: Toggle for debugging bypass mechanisms

        Returns:
            List of strictly validated `ProfileResult` identities
        """
        ...


class AbstractAnalyzer(ABC):
    """
    Deep-phase PII enrichment and contextual analysis.
    Takes a single entry point (URL) and extracts a highly hydrated identity footprint.
    """

    def __init__(
        self,
        config: Settings,
        health: HealthManager,
    ):
        self.config = config
        self.health = health

    @abstractmethod
    async def analyze(
        self,
        url: str,
        client: str,
        headless: bool = True,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> ProfileResult:
        """
        Executes deep profile hydration.

        Contract Requirements:
            - Must honor the injected `semaphore` to tightly bound concurrent page requests,
              protecting the stealth instance from connection saturation or CAPTCHA triggers.

        Args:
            url: Absolute platform endpoint
            client: Tenant/workspace boundary identifier
            headless: Toggle for debugging bypass mechanisms
            semaphore: Context manager for concurrency throttling

        Returns:
            A heavily enriched, strictly validated `ProfileResult`
        """
        ...
