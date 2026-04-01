import asyncio
import base64
import os
from io import BytesIO
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.platforms.base import AbstractDiscoverer

logger = get_logger("platforms.telegram.discovery")


class TelegramDiscoverer(AbstractDiscoverer):
    """
    Discovers Telegram users, channels, and groups by keyword.
    Requires TELEGRAM_API_ID and TELEGRAM_API_HASH in settings.
    """

    async def search(
        self,
        progress_callback,
        client: str,
        keywords: list[str],
        max_results: int = 50,
        headless: bool = True,
        **kwargs,
    ) -> list[ProfileResult]:
        results = []

        if not self.config.TELEGRAM_API_ID or not self.config.TELEGRAM_API_HASH:
            await progress_callback(
                event_type="failed",
                message="Telegram API credentials not configured (TELEGRAM_API_ID, TELEGRAM_API_HASH)",
                count_found=0,
            )
            return results

        try:
            from telethon import TelegramClient
            from telethon.tl.functions.contacts import SearchRequest
        except ImportError:
            await progress_callback(
                event_type="failed",
                message="Telethon not installed. Run: pip install Telethon",
                count_found=0,
            )
            return results

        session_path = os.path.join(self.config.SESSION_PATH, "telegram")
        os.makedirs(self.config.SESSION_PATH, exist_ok=True)

        tg_client = TelegramClient(
            session_path,
            int(self.config.TELEGRAM_API_ID),
            self.config.TELEGRAM_API_HASH,
        )

        try:
            await tg_client.connect()

            # check if we're already authorized
            if not await tg_client.is_user_authorized():
                await progress_callback(
                    event_type="failed",
                    message="Telegram session not authorized. Use the session login flow first.",
                    count_found=0,
                )
                return results

            for keyword in keywords:
                if self.health.should_pause("telegram"):
                    delay = self.health.get_recommended_delay("telegram")
                    await progress_callback(
                        event_type="rate_limited",
                        message=f"Rate limit approaching, waiting {delay:.0f}s",
                        count_found=len(results),
                    )
                    await asyncio.sleep(delay)

                await progress_callback(
                    event_type="progress",
                    message=f"Searching Telegram for '{keyword}'...",
                    count_found=len(results),
                )

                try:
                    search_result = await tg_client(
                        SearchRequest(
                            q=keyword,
                            limit=min(max_results, 100),
                        )
                    )

                    # process users found
                    for user in search_result.users:
                        profile = await self._user_to_profile(tg_client, user, keyword, client)
                        if profile:
                            results.append(profile)
                            await progress_callback(
                                event_type="result_found",
                                message=f"Found user: {profile.display_name}",
                                count_found=len(results),
                                result=profile.to_dict(),
                            )

                    # process chats (groups/channels) found
                    for chat in search_result.chats:
                        profile = await self._chat_to_profile(tg_client, chat, keyword, client)
                        if profile:
                            results.append(profile)
                            await progress_callback(
                                event_type="result_found",
                                message=f"Found {'channel' if hasattr(chat, 'broadcast') and chat.broadcast else 'group'}: {profile.display_name}",
                                count_found=len(results),
                                result=profile.to_dict(),
                            )

                    await self.health.record_request("telegram", success=True)

                except Exception as exc:
                    logger.error(f"Telegram search failed for '{keyword}': {exc}")
                    await self.health.record_request("telegram", success=False)

        finally:
            await tg_client.disconnect()

        return results

    async def _download_profile_photo(self, tg_client, entity) -> tuple[str, str]:
        """
        Download profile photo via Telethon API → returns (b64_data, public_url).
        Falls back to the public t.me URL if the API download fails.
        """
        username = getattr(entity, "username", "") or ""
        public_url = f"https://t.me/i/userpic/320/{username}.jpg" if username else ""

        try:
            # Download photo bytes directly from Telegram API
            photo_bytes = await tg_client.download_profile_photo(
                entity, file=bytes
            )
            if photo_bytes and len(photo_bytes) > 500:
                b64 = base64.b64encode(photo_bytes).decode("utf-8")
                logger.info(f"✅ Downloaded profile photo for @{username or entity.id} ({len(photo_bytes)} bytes)")
                return b64, public_url
            else:
                logger.debug(f"No profile photo available for @{username or entity.id}")
        except Exception as exc:
            logger.warning(f"Profile photo download failed for @{username or entity.id}: {exc}")

        return "", public_url

    async def _user_to_profile(
        self, tg_client, user, keyword: str, client_name: str
    ) -> Optional[ProfileResult]:
        """Convert a Telethon User object to ProfileResult."""
        try:
            username = user.username or ""
            first_name = user.first_name or ""
            last_name = user.last_name or ""
            display_name = f"{first_name} {last_name}".strip()

            if not username and not display_name:
                return None

            url = (
                f"https://t.me/{username}"
                if username
                else f"tg://openmessage?user_id={user.id}"
            )

            # Download profile photo via API
            profile_image_b64, profile_image_url = await self._download_profile_photo(
                tg_client, user
            )

            return ProfileResult(
                platform="telegram",
                client_name=client_name,
                keyword=keyword,
                url=url,
                username=username,
                display_name=display_name,
                bio="",  # bio requires a separate GetFullUser call
                profile_image_url=profile_image_url,
                profile_image_b64=profile_image_b64,
                has_logo=bool(profile_image_b64),
                is_verified=getattr(user, "verified", False),
                last_active=str(user.status)
                if hasattr(user, "status") and user.status
                else None,
            )
        except Exception as exc:
            logger.warning(f"Failed to convert Telegram user: {exc}")
            return None

    async def _chat_to_profile(
        self, tg_client, chat, keyword: str, client_name: str
    ) -> Optional[ProfileResult]:
        """Convert a Telethon Chat/Channel object to ProfileResult."""
        try:
            username = getattr(chat, "username", "") or ""
            title = getattr(chat, "title", "") or ""

            if not username and not title:
                return None

            url = (
                f"https://t.me/{username}" if username else f"tg://channel?id={chat.id}"
            )
            members = getattr(chat, "participants_count", None)

            # Download profile photo via API
            profile_image_b64, profile_image_url = await self._download_profile_photo(
                tg_client, chat
            )

            return ProfileResult(
                platform="telegram",
                client_name=client_name,
                keyword=keyword,
                url=url,
                username=username,
                display_name=title,
                bio=getattr(chat, "about", "") or "",
                followers=members,
                profile_image_url=profile_image_url,
                profile_image_b64=profile_image_b64,
                has_logo=bool(profile_image_b64),
                is_verified=getattr(chat, "verified", False),
            )
        except Exception as exc:
            logger.warning(f"Failed to convert Telegram chat: {exc}")
            return None

