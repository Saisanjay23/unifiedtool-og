"""
Telegram profile/channel analysis via Telethon API.
Extracts detailed metadata including full user info, channel stats,
profile photos, last message, and OSINT risk score.
"""

import asyncio
import base64
import datetime
import os
import re

from playwright.async_api import async_playwright
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.users import GetFullUserRequest

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from backend.platforms.utils import calculate_risk as _calculate_risk_shared
from backend.stealth.browser import create_stealth_browser

logger = get_logger("platforms.telegram.analysis")


class TelegramAnalyzer(AbstractAnalyzer):
    """Deep-analyzes Telegram users and channels with full OSINT field population."""

    def __init__(self, config: Settings, health: HealthManager):
        super().__init__(config, health)
        self._client_lock = asyncio.Lock()
        self._client: TelegramClient | None = None

    async def _get_shared_client(self) -> TelegramClient:
        """Reuse one connected Telethon client per analyzer/job for speed and stability."""
        async with self._client_lock:
            if self._client is not None and self._client.is_connected():
                return self._client

            if not self.config.TELEGRAM_API_ID or not self.config.TELEGRAM_API_HASH:
                raise RuntimeError("Telegram API credentials not configured")

            try:
                api_id = int(self.config.TELEGRAM_API_ID)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid API ID format") from exc

            session_path = os.path.join(self.config.SESSION_PATH, "telegram")
            client = TelegramClient(
                session_path,
                api_id,
                self.config.TELEGRAM_API_HASH,
            )
            await client.connect()

            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError("Session not authorized")

            self._client = client
            return client

    async def close(self):
        """Release the shared Telethon client once the job finishes."""
        async with self._client_lock:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                finally:
                    self._client = None

    async def analyze(
        self,
        url: str,
        client: str,
        headless: bool = True,
        semaphore: asyncio.Semaphore | None = None,
    ) -> ProfileResult:
        """Legacy single-profile analysis. Launches its own browser."""
        sem = semaphore or asyncio.Semaphore(1)
        async with sem:
            pw, browser, context, page = await create_stealth_browser(
                platform="telegram", headless=headless,
            )
            try:
                return await self._do_analysis(page, url, client, browser_context=context)
            finally:
                if page and not page.is_closed():
                    await page.close()
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()
                await self.close()

    async def analyze_with_page(
        self,
        url: str,
        client: str,
        page,
    ) -> ProfileResult:
        """Pool-optimized analysis. Uses a pre-existing stealth page (tab)."""
        return await self._do_analysis(page, url, client)

    async def _do_analysis(
        self,
        page,
        url: str,
        client: str,
        browser_context=None,
    ) -> ProfileResult:
        """Core analysis logic."""
        # Note: 'page' is used for web preview screenshot.
        
        error_comments = []
        result = ProfileResult(
            platform="telegram",
            client_name=client,
            keyword="",
            url=url,
        )

        if not self.config.TELEGRAM_API_ID or not self.config.TELEGRAM_API_HASH:
            logger.warning("Telegram API credentials not configured")
            result.comments = "API credentials not configured"
            return result

        try:
            # Check if Telethon is available (already imported, but keeping structure)
            import telethon
        except ImportError:
            logger.error("Telethon not installed")
            result.comments = "Telethon not installed"
            return result

        try:
            tg_client = await self._get_shared_client()

            username = self._extract_username(url)
            if not username:
                logger.warning(f"Could not extract username from URL: {url}")
                result.comments = "Invalid URL"
                return result

            entity = await tg_client.get_entity(username)

            if hasattr(entity, "broadcast") or hasattr(entity, "megagroup"):
                result_task = self._analyze_channel(
                    tg_client, entity, url, client, error_comments
                )
            else:
                result_task = self._analyze_user(
                    tg_client, entity, url, client, error_comments
                )

            web_username = username if isinstance(username, str) else None
            screenshot_task = (
                self._capture_screenshot_with_page(page, web_username)
                if web_username
                else asyncio.sleep(0, result=None)
            )
            photo_task = self._download_photo(tg_client, entity)
            last_message_task = self._get_last_message_info(
                tg_client, entity, error_comments
            )

            result, photo_b64, screenshot_b64, last_message_info = await asyncio.gather(
                result_task,
                photo_task,
                screenshot_task,
                last_message_task,
            )

            if photo_b64:
                result.profile_image_b64 = photo_b64
                result.has_logo = True

            if screenshot_b64:
                result.screenshot_b64 = screenshot_b64

            if last_message_info:
                result.last_post_date = last_message_info["last_post_date"]
                result.last_active = last_message_info["last_active"]
                result.is_active = last_message_info["is_active"]

            # Calculate OSINT risk score
            result.comments = ""
            self._calculate_risk(result)

            await self.health.record_request("telegram", success=True)

        except RuntimeError as exc:
            logger.warning(f"Telegram analysis unavailable for {url}: {exc}")
            result.comments = str(exc)
            await self.health.record_request("telegram", success=False)
        except Exception as exc:
            logger.error(f"Telegram analysis failed for {url}: {exc}")
            result.comments = f"Critical error: {type(exc).__name__}"
            await self.health.record_request("telegram", success=False)

        return result

    async def _analyze_user(
        self, tg_client, user, url: str, client_name: str, error_comments: list
    ) -> ProfileResult:
        """Analyze a Telegram user for full profile data."""
        try:
            full = await tg_client(GetFullUserRequest(user))
            full_user = full.full_user

            first_name = user.first_name or ""
            last_name = user.last_name or ""
            display_name = f"{first_name} {last_name}".strip()

            return ProfileResult(
                platform="telegram",
                client_name=client_name,
                keyword="",
                url=url,
                username=user.username or "",
                display_name=display_name,
                bio=full_user.about or "",
                is_verified=getattr(user, "verified", False),
                has_name_match=bool(display_name),
                entity_type="user",
            )

        except Exception as exc:
            error_comments.append(f"Full user fetch failed: {str(exc)[:30]}")
            return ProfileResult(
                platform="telegram",
                client_name=client_name,
                keyword="",
                url=url,
                username=getattr(user, "username", "") or "",
                display_name=f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip(),
                has_name_match=True,
            )

    async def _analyze_channel(
        self, tg_client, channel, url: str, client_name: str, error_comments: list
    ) -> ProfileResult:
        """Analyze a Telegram channel/group for full details."""
        try:
            full = await tg_client(GetFullChannelRequest(channel))
            full_chat = full.full_chat

            # Created date
            created_at = ""
            if hasattr(channel, "date") and channel.date:
                created_at = channel.date.strftime("%d-%m-%Y")

            return ProfileResult(
                platform="telegram",
                client_name=client_name,
                keyword="",
                url=url,
                username=getattr(channel, "username", "") or "",
                display_name=getattr(channel, "title", "") or "",
                bio=getattr(full_chat, "about", "") or "",
                followers=getattr(full_chat, "participants_count", None),
                is_verified=getattr(channel, "verified", False),
                created_at=created_at,
                has_name_match=bool(getattr(channel, "title", "")),
                entity_type="channel"
                if getattr(channel, "broadcast", False)
                else "group",
            )

        except Exception as exc:
            error_comments.append(f"Full channel fetch failed: {str(exc)[:30]}")
            return ProfileResult(
                platform="telegram",
                client_name=client_name,
                keyword="",
                url=url,
                username=getattr(channel, "username", "") or "",
                display_name=getattr(channel, "title", "") or "",
                has_name_match=True,
            )

    async def _get_last_message_info(
        self, tg_client, entity, error_comments: list
    ) -> dict | None:
        """Get last message metadata for Active/Last Post detection."""
        try:
            messages = await tg_client.get_messages(entity, limit=1)
            if messages and len(messages) > 0:
                msg = messages[0]
                if msg and msg.date:
                    date_str = msg.date.strftime("%d-%m-%Y")
                    now = datetime.datetime.now(datetime.timezone.utc)
                    return {
                        "last_post_date": date_str,
                        "last_active": date_str,
                        "is_active": (now - msg.date).days <= 180,
                    }
        except Exception as exc:
            error_comments.append(f"Last msg: {str(exc)[:20]}")
        return None

    async def _download_photo(self, tg_client, entity) -> str | None:
        """Download and base64-encode the entity's profile photo."""
        try:
            photo_bytes = await tg_client.download_profile_photo(entity, bytes)
            if photo_bytes:
                return base64.b64encode(photo_bytes).decode("utf-8")
        except Exception as exc:
            logger.debug(f"Photo download failed: {exc}")
        return None

    async def _capture_screenshot_with_page(self, page, username: str) -> str | None:
        """Capture screenshot of the public t.me/<username> web preview using the provided page."""
        try:
            tme_url = f"https://t.me/{username}"
            await page.goto(tme_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)  # let the page render

            screenshot_bytes = await page.screenshot(
                full_page=False, type="jpeg", quality=85
            )

            if screenshot_bytes:
                return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as exc:
            logger.debug(f"Screenshot capture failed for @{username}: {exc}")
        return None

    async def _capture_screenshot(self, username: str, headless: bool = True) -> str | None:
        """Legacy standalone screenshot capture."""
        pw = None
        browser = None
        try:

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=headless)
            page = await browser.new_page(
                viewport={"width": 1280, "height": 900}
            )
            return await self._capture_screenshot_with_page(page, username)
        finally:
            if browser:
                await browser.close()
            if pw:
                await pw.stop()

    def _calculate_risk(self, result: ProfileResult):
        """Calculate risk score 3-9 — delegates to shared implementation."""
        _calculate_risk_shared(result)

    def _extract_username(self, url: str):
        """Extract username (str) or entity ID (int) from t.me URL or raw input."""
        user_id_match = re.search(r"(?:user_id|id)=(\d+)", url)
        if user_id_match:
            return int(user_id_match.group(1))

        match = re.search(r"(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)", url)
        if match:
            return match.group(1)
        if url.startswith("@"):
            return url[1:]
        if re.match(r"^[a-zA-Z0-9_]+$", url):
            return url
        return None
