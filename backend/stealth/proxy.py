"""
Proxy rotation for the Unified Social Media Tool.
Cycles through a list of proxy URLs, assigning a different one per browser launch.
Inspired by Scrapling's ProxyRotator but adapted for our Playwright architecture.

Usage:
    rotator = ProxyRotator(["socks5://p1:1080", "http://p2:8080"])
    proxy = rotator.next()  # Returns next proxy in cycle
"""

import itertools
import threading

from backend.core.logger import get_logger

logger = get_logger("stealth.proxy")


class ProxyRotator:
    """
    Thread-safe proxy rotator.
    Cycles through a list of proxy URLs in round-robin fashion.
    Each call to next() returns the next proxy in the sequence.
    """

    def __init__(self, proxies: list[str]):
        if not proxies:
            raise ValueError("ProxyRotator requires at least one proxy URL")

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for p in proxies:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                unique.append(p)

        if not unique:
            raise ValueError("No valid proxy URLs provided")

        self._proxies = unique
        self._cycle = itertools.cycle(unique)
        self._lock = threading.Lock()
        logger.info(f"ProxyRotator initialized with {len(unique)} proxies")

    def next(self) -> str:
        """Return the next proxy URL in the rotation."""
        with self._lock:
            proxy = next(self._cycle)
        return proxy

    @property
    def count(self) -> int:
        """Number of unique proxies in the pool."""
        return len(self._proxies)

    @property
    def proxies(self) -> list[str]:
        """Read-only copy of the proxy list."""
        return list(self._proxies)

    def __repr__(self) -> str:
        return f"ProxyRotator(count={self.count})"
