"""
Fast HTTP client with browser TLS fingerprint impersonation.
Uses curl_cffi to make requests that look like they come from a real browser
without the overhead of launching Chromium (~5-8s startup vs instant).

Inspired by Scrapling's Fetcher class. Use for operations that don't need
JS rendering: API calls, image downloads, session validation, URL checks.

Usage:
    # Sync
    with StealthHTTP() as client:
        resp = client.get("https://api.example.com/data")

    # Async
    async with AsyncStealthHTTP() as client:
        resp = await client.get("https://api.example.com/data")

    # With proxy
    async with AsyncStealthHTTP(proxy="socks5://proxy:1080") as client:
        resp = await client.get("https://api.example.com/data")

Do NOT use for pages requiring JavaScript rendering — use create_stealth_browser() instead.
"""

from backend.core.logger import get_logger

logger = get_logger("stealth.http_client")

# Browser TLS fingerprints available in curl_cffi:
# "chrome" (latest), "safari", "firefox" — or specific versions like "chrome124"
DEFAULT_IMPERSONATE = "chrome"


def _check_curl_cffi():
    """Check if curl_cffi is installed."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        logger.warning(
            "curl_cffi not installed. Install with: pip install curl_cffi\n"
            "Falling back to standard requests where possible."
        )
        return False


class StealthHTTP:
    """
    Synchronous HTTP client with browser TLS fingerprint impersonation.
    Wraps curl_cffi to make requests indistinguishable from real Chrome/Firefox.

    All requests automatically include:
    - Browser-matching TLS fingerprint (JA3/JA4)
    - HTTP/2 with correct ALPN negotiation
    - Browser-matching header order
    """

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        proxy: str | None = None,
        timeout: int = 30,
    ):
        if not _check_curl_cffi():
            raise ImportError("curl_cffi is required for StealthHTTP")

        from curl_cffi.requests import Session

        self._session = Session(
            impersonate=impersonate,
            proxy=proxy,
            timeout=timeout,
        )
        self._impersonate = impersonate
        logger.debug(f"StealthHTTP created (impersonate={impersonate})")

    def get(self, url: str, **kwargs):
        """HTTP GET with browser TLS fingerprint."""
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        """HTTP POST with browser TLS fingerprint."""
        return self._session.post(url, **kwargs)

    def put(self, url: str, **kwargs):
        """HTTP PUT with browser TLS fingerprint."""
        return self._session.put(url, **kwargs)

    def delete(self, url: str, **kwargs):
        """HTTP DELETE with browser TLS fingerprint."""
        return self._session.delete(url, **kwargs)

    def head(self, url: str, **kwargs):
        """HTTP HEAD with browser TLS fingerprint."""
        return self._session.head(url, **kwargs)

    def close(self):
        """Close the underlying session."""
        try:
            self._session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncStealthHTTP:
    """
    Asynchronous HTTP client with browser TLS fingerprint impersonation.
    Use for concurrent API calls, image downloads, and validation checks.
    """

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        proxy: str | None = None,
        timeout: int = 30,
    ):
        if not _check_curl_cffi():
            raise ImportError("curl_cffi is required for AsyncStealthHTTP")

        from curl_cffi.requests import AsyncSession

        self._session = AsyncSession(
            impersonate=impersonate,
            proxy=proxy,
            timeout=timeout,
        )
        self._impersonate = impersonate
        logger.debug(f"AsyncStealthHTTP created (impersonate={impersonate})")

    async def get(self, url: str, **kwargs):
        """Async HTTP GET with browser TLS fingerprint."""
        return await self._session.get(url, **kwargs)

    async def post(self, url: str, **kwargs):
        """Async HTTP POST with browser TLS fingerprint."""
        return await self._session.post(url, **kwargs)

    async def put(self, url: str, **kwargs):
        """Async HTTP PUT with browser TLS fingerprint."""
        return await self._session.put(url, **kwargs)

    async def delete(self, url: str, **kwargs):
        """Async HTTP DELETE with browser TLS fingerprint."""
        return await self._session.delete(url, **kwargs)

    async def head(self, url: str, **kwargs):
        """Async HTTP HEAD with browser TLS fingerprint."""
        return await self._session.head(url, **kwargs)

    async def close(self):
        """Close the underlying async session."""
        try:
            await self._session.close()
        except Exception:
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
