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
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer

logger = get_logger("platforms.telegram.analysis")


class TelegramAnalyzer(AbstractAnalyzer):
    """Deep-analyzes Telegram users and channels with full OSINT field population."""

    async def analyze(
        self,
        url: str,
        client: str,
        headless: bool = True,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> ProfileResult:
        sem = semaphore or asyncio.Semaphore(1)

        async with sem:
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
                from telethon import TelegramClient
                from telethon.tl.functions.users import GetFullUserRequest
                from telethon.tl.functions.channels import GetFullChannelRequest
                from telethon.tl.functions.messages import GetHistoryRequest
            except ImportError:
                logger.error("Telethon not installed")
                result.comments = "Telethon not installed"
                return result

            session_path = os.path.join(self.config.SESSION_PATH, "telegram")

            try:
                api_id = int(self.config.TELEGRAM_API_ID)
            except (TypeError, ValueError):
                logger.warning("Invalid TELEGRAM_API_ID format")
                result.comments = "Invalid API ID format"
                return result

            tg_client = TelegramClient(
                session_path,
                api_id,
                self.config.TELEGRAM_API_HASH,
            )

            try:
                await tg_client.connect()

                if not await tg_client.is_user_authorized():
                    logger.warning("Telegram session not authorized")
                    result.comments = "Session not authorized"
                    return result

                username = self._extract_username(url)
                if not username:
                    logger.warning(f"Could not extract username from URL: {url}")
                    result.comments = "Invalid URL"
                    return result

                entity = await tg_client.get_entity(username)

                if hasattr(entity, "broadcast") or hasattr(entity, "megagroup"):
                    result = await self._analyze_channel(
                        tg_client, entity, url, client, error_comments
                    )
                else:
                    result = await self._analyze_user(
                        tg_client, entity, url, client, error_comments
                    )

                # Download profile photo
                photo_b64 = await self._download_photo(tg_client, entity)
                if photo_b64:
                    result.profile_image_b64 = photo_b64
                    result.has_logo = True

                # Capture screenshot of t.me web preview
                web_username = username if isinstance(username, str) else None
                if web_username:
                    screenshot_b64 = await self._capture_screenshot(web_username, headless)
                    if screenshot_b64:
                        result.screenshot_b64 = screenshot_b64

                # Get last message for Active/Last Post detection
                await self._check_last_message(
                    tg_client, entity, result, error_comments
                )

                # Calculate OSINT risk score
                result.comments = ""
                self._calculate_risk(result)

                await self.health.record_request("telegram", success=True)

            except Exception as exc:
                logger.error(f"Telegram analysis failed for {url}: {exc}")
                result.comments = f"Critical error: {type(exc).__name__}"
                await self.health.record_request("telegram", success=False)
            finally:
                await tg_client.disconnect()

            return result

    async def _analyze_user(
        self, tg_client, user, url: str, client_name: str, error_comments: list
    ) -> ProfileResult:
        """Analyze a Telegram user for full profile data."""
        from telethon.tl.functions.users import GetFullUserRequest

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
        from telethon.tl.functions.channels import GetFullChannelRequest

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

    async def _check_last_message(
        self, tg_client, entity, result: ProfileResult, error_comments: list
    ):
        """Check last message date for Active/Last Post detection."""
        try:
            messages = await tg_client.get_messages(entity, limit=1)
            if messages and len(messages) > 0:
                msg = messages[0]
                if msg and msg.date:
                    result.last_post_date = msg.date.strftime("%d-%m-%Y")
                    result.last_active = msg.date.strftime("%d-%m-%Y")
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if (now - msg.date).days <= 180:
                        result.is_active = True
        except Exception as exc:
            error_comments.append(f"Last msg: {str(exc)[:20]}")

    async def _download_photo(self, tg_client, entity) -> Optional[str]:
        """Download and base64-encode the entity's profile photo."""
        try:
            photo_bytes = await tg_client.download_profile_photo(entity, bytes)
            if photo_bytes:
                return base64.b64encode(photo_bytes).decode("utf-8")
        except Exception as exc:
            logger.debug(f"Photo download failed: {exc}")
        return None

    async def _capture_screenshot(self, username: str, headless: bool = True) -> Optional[str]:
        """Capture screenshot of the public t.me/<username> web preview using Playwright."""
        pw = None
        browser = None
        try:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=headless)
            page = await browser.new_page(
                viewport={"width": 1280, "height": 900}
            )

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
        finally:
            if browser:
                await browser.close()
            if pw:
                await pw.stop()
        return None

    def _calculate_risk(self, result: ProfileResult):
        """Calculate risk score 3-9 using old tool's exact logic."""
        has_name = result.has_name_match
        has_logo = result.has_logo
        has_location = bool(
            result.location and result.location.lower() not in ("nan", "none", "")
        )
        followers = result.followers or 0
        now = datetime.datetime.now()

        def get_months_ago(date_str):
            if not date_str or str(date_str).lower() in ("nan", "none", "", "no"):
                return 999
            try:
                parts = str(date_str).split("-")
                if len(parts) == 2:
                    dt = datetime.datetime.strptime(date_str, "%m-%Y")
                else:
                    dt = datetime.datetime.strptime(date_str, "%d-%m-%Y")
                return (now.year - dt.year) * 12 + (now.month - dt.month)
            except Exception:
                return 999

        created_months = get_months_ago(result.created_at)
        posted_months = get_months_ago(result.last_post_date)

        is_new = created_months <= 6
        is_very_new = created_months <= 1

        result.is_active = result.is_active or is_new
        result.priority = "High" if has_logo else "Low"

        score = 0
        if (
            has_name
            and has_logo
            and is_new
            and result.is_active
            and has_location
            and followers > 100
        ):
            score = 9
        elif (
            has_name and has_logo and result.is_active and has_location and is_very_new
        ):
            score = 8
        elif has_name and has_logo and result.is_active and has_location:
            score = 7
        elif has_name and has_logo and (result.is_active or is_new):
            score = 7
        elif has_name and has_logo:
            score = 6
        elif has_name and is_new:
            score = 4
        elif has_name:
            score = 3
        result.risk_score = score

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
