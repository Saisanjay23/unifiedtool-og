"""
Browser Pool for batch analysis operations.
Instead of launching a fresh Chromium process per profile (~5-8s overhead each),
this pool launches ONE browser and creates/reuses multiple tabs (pages) from the
same BrowserContext. All tabs share cookies/session, so authentication carries over.

Usage:
    pool = await BrowserPool.create("facebook", headless=True, max_pages=3)
    try:
        page = await pool.acquire_page()
        # ... do work with page ...
        await pool.release_page(page)
    finally:
        await pool.shutdown()
"""

import asyncio

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from backend.core.config import settings
from backend.core.logger import get_logger

logger = get_logger("stealth.browser_pool")


class BrowserPool:
    """
    Reusable browser + page pool for batch analysis.
    Eliminates the catastrophic overhead of launching a new Chromium
    process for every single profile URL.
    """

    def __init__(self):
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._available_pages: asyncio.Queue[Page] = asyncio.Queue()
        self._all_pages: list[Page] = []
        self._max_pages: int = 3
        self._platform: str = ""
        self._headless: bool = True
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def create(
        cls,
        platform: str,
        headless: bool = True,
        max_pages: int = 3,
    ) -> "BrowserPool":
        """
        Factory method. Launches one stealth browser and pre-creates
        the specified number of tabs ready for analysis.
        """
        pool = cls()
        pool._platform = platform
        pool._max_pages = max_pages
        pool._headless = headless

        from backend.stealth.browser import create_stealth_browser

        # Create the first page via the standard stealth pipeline
        # (this handles all fingerprint injection, stealth scripts, etc.)
        pw, browser, context, first_page = await create_stealth_browser(
            platform=platform,
            headless=headless,
        )

        pool._pw = pw
        pool._browser = browser
        pool._context = context

        # The first page is ready
        pool._all_pages.append(first_page)
        await pool._available_pages.put(first_page)

        # Pre-create additional tabs from the same context
        # They inherit all stealth init scripts from the context
        for i in range(max_pages - 1):
            try:
                page = await context.new_page()
                page.set_default_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
                page.set_default_navigation_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
                pool._all_pages.append(page)
                await pool._available_pages.put(page)
            except Exception as exc:
                logger.warning(f"Failed to create pool page {i+2}: {exc}")
                break

        logger.info(
            f"{platform}: BrowserPool created with {len(pool._all_pages)} tabs "
            f"(headless={headless})"
        )
        return pool

    async def _recreate_browser(self):
        """Shutdown the dead browser and launch a fresh one, repopulating the pool."""
        logger.info(f"{self._platform}: Self-healing: recreating browser process...")
        # 1. Clear old queue
        while not self._available_pages.empty():
            try:
                self._available_pages.get_nowait()
            except asyncio.QueueEmpty:
                break

        # 2. Shutdown old browser safely
        try:
            for page in list(self._all_pages):
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

        self._all_pages.clear()

        # 3. Create fresh browser
        from backend.stealth.browser import create_stealth_browser
        try:
            pw, browser, context, first_page = await create_stealth_browser(
                platform=self._platform,
                headless=self._headless,
            )
            self._pw = pw
            self._browser = browser
            self._context = context

            self._all_pages.append(first_page)
            await self._available_pages.put(first_page)

            for i in range(self._max_pages - 1):
                try:
                    page = await context.new_page()
                    page.set_default_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
                    page.set_default_navigation_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
                    self._all_pages.append(page)
                    await self._available_pages.put(page)
                except Exception as exc:
                    logger.warning(f"Failed to create pool page {i+2} during restart: {exc}")
                    break
            
            logger.info(f"{self._platform}: BrowserPool successfully self-healed and restarted.")
        except Exception as e:
            logger.error(f"{self._platform}: Critical error during self-healing: {e}")

    async def acquire_page(self, timeout: float = 120.0) -> Page:
        """
        Get a page from the pool. Blocks until one is available.
        If the page was closed/crashed, creates a replacement.
        """
        if not self._browser or not self._browser.is_connected():
            logger.warning(f"{self._platform}: Browser disconnected! Triggering self-healing restart...")
            async with self._lock:
                await self._recreate_browser()

        try:
            page = await asyncio.wait_for(self._available_pages.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"{self._platform}: Timeout waiting for available page! Re-initializing pool...")
            async with self._lock:
                await self._recreate_browser()
            page = await asyncio.wait_for(self._available_pages.get(), timeout=10.0)

        # Validate the page is still alive
        page_ok = False
        try:
            if not page.is_closed():
                await asyncio.wait_for(page.evaluate("1 + 1"), timeout=2.0)
                page_ok = True
        except Exception:
            pass

        if not page_ok:
            logger.warning(f"{self._platform}: Pool page dead, creating replacement")
            async with self._lock:
                try:
                    self._all_pages.remove(page)
                except ValueError:
                    pass
                
                try:
                    page = await self._create_fresh_page()
                except Exception as exc:
                    logger.warning(f"{self._platform}: Failed to create replacement page ({exc}). Restarting browser...")
                    await self._recreate_browser()
                    page = await asyncio.wait_for(self._available_pages.get(), timeout=10.0)

        return page

    async def release_page(self, page: Page):
        """
        Return a page to the pool after use.
        Navigates to blank to clear state, then re-queues.
        """
        if self._closed:
            return

        try:
            if page.is_closed():
                # Don't return dead pages, create a replacement
                async with self._lock:
                    try:
                        self._all_pages.remove(page)
                    except ValueError:
                        pass
                    replacement = await self._create_fresh_page()
                    await self._available_pages.put(replacement)
                return

            # Navigate to blank to clear page state (fast, no network)
            await page.goto("about:blank", wait_until="commit", timeout=5000)
            await self._available_pages.put(page)
        except Exception as exc:
            logger.warning(f"{self._platform}: Error releasing page: {exc}")
            # Create replacement
            try:
                async with self._lock:
                    try:
                        self._all_pages.remove(page)
                    except ValueError:
                        pass
                    replacement = await self._create_fresh_page()
                    await self._available_pages.put(replacement)
            except Exception:
                pass

    async def _create_fresh_page(self) -> Page:
        """Create a new page in the existing context."""
        page = await self._context.new_page()
        page.set_default_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
        page.set_default_navigation_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
        self._all_pages.append(page)
        logger.debug(f"{self._platform}: Created replacement pool page")
        return page

    async def shutdown(self):
        """Close everything. Call when the analysis job is done."""
        if self._closed:
            return
        self._closed = True

        # Close all pages
        for page in self._all_pages:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass

        # Close browser
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass

        # Stop playwright
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

        logger.info(f"{self._platform}: BrowserPool shut down")

    @property
    def page_count(self) -> int:
        return len(self._all_pages)
