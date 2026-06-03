import asyncio
import base64
import datetime
import random
import re

import pycountry
from googleapiclient.discovery import build

from backend.core.db import ProfileResult
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from backend.platforms.utils import calculate_risk, download_profile_image, is_real_profile_image
from backend.stealth.browser import create_stealth_browser

logger = get_logger("platforms.youtube.analysis")


class YouTubeAnalyzer(AbstractAnalyzer):
    """
    Deep-analyzes YouTube channels using the Data API v3 for metadata
    and Playwright for screenshots and exact subscriber counts.
    Writes directly to ProfileResult — no legacy dict conversion.
    """

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
                platform="youtube", headless=headless,
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
        """
        Core analysis logic — PERFORMANCE OPTIMIZED.
        Key speed wins over previous version:
        1. API fetch runs fully in parallel with browser navigation (not sequentially)
        2. Fixed sleeps eliminated (2s + 0.5s = 2.5s saved)
        3. Channel header wait reduced from 10s to 4s
        4. Expensive page.content() subscriber scrape skipped when API already has the count
        5. Profile image download runs in parallel with screenshot
        """

        logger.info(f"Starting YouTube analysis for URL: {url} (Client: {client})")

        # --- Build the result object up front ---
        result = ProfileResult(
            platform="youtube",
            client_name=client,
            keyword="",
            url=url,
        )
        error_comments = []

        service = self._get_youtube_service()
        channel_id = None
        api_data_task = None

        # Resolve channel ID from URL (fast, no network)
        try:
            id_type, value = self._extract_channel_handle_or_id(url)
            channel_id = await self._get_channel_id(service, id_type, value)
            if channel_id:
                # Fire API fetch immediately — runs in parallel with browser navigation
                api_data_task = asyncio.create_task(
                    self._fetch_channel_data(service, channel_id)
                )
        except Exception:
            pass

        try:
            logger.info(f"[{url}] Analysis starting using provided page.")

            try:
                # Navigate — don't add fixed sleep, use event-driven waits instead
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # If we couldn't resolve channel ID from URL, scrape it from the page
                if not channel_id:
                    try:
                        content = await page.content()
                        match = re.search(
                            r'itemprop="channelId" content="([^"]+)"', content
                        )
                        if match:
                            channel_id = match.group(1)
                        else:
                            match_url = re.search(
                                r'link rel="canonical" href="https://www.youtube.com/channel/([^"]+)"',
                                content,
                            )
                            if match_url:
                                channel_id = match_url.group(1)
                        if channel_id and api_data_task is None:
                            api_data_task = asyncio.create_task(
                                self._fetch_channel_data(service, channel_id)
                            )
                    except Exception as e:
                        error_comments.append(f"Scraping ID failed: {e}")

                # Process API data (should already be done — it was running in parallel)
                if channel_id:
                    result.username = channel_id
                    try:
                        api_data = (
                            await api_data_task
                            if api_data_task is not None
                            else await self._fetch_channel_data(service, channel_id)
                        )

                        if api_data:
                            snippet = api_data.get("snippet", {})
                            stats = api_data.get("stats", {})
                            branding = api_data.get("branding", {})
                            last_video = api_data.get("last_video")

                            result.display_name = snippet.get("title", "Unknown")
                            if result.display_name:
                                result.has_name_match = True

                            result.bio = snippet.get("description", "")

                            published_at = snippet.get("publishedAt")
                            if published_at:
                                try:
                                    dt = datetime.datetime.fromisoformat(
                                        published_at.replace("Z", "+00:00")
                                    )
                                    result.created_at = dt.strftime("%d-%m-%Y")
                                except Exception:
                                    pass

                            sub_count = stats.get("subscriberCount", 0)
                            if not stats.get("hiddenSubscriberCount"):
                                result.followers = int(sub_count)

                            # Location from country code
                            country_code = snippet.get("country") or (
                                branding.get("channel") or {}
                            ).get("country")
                            if country_code:
                                try:
                                    country = pycountry.countries.get(
                                        alpha_2=country_code
                                    )
                                    result.location = (
                                        country.name if country else country_code
                                    )
                                except Exception:
                                    result.location = country_code

                            # Profile picture
                            thumbnails = snippet.get("thumbnails", {})
                            thumb_url = thumbnails.get("high", {}).get(
                                "url"
                            ) or thumbnails.get("default", {}).get("url", "")
                            if thumb_url:
                                # Upgrade to HD (800x800) by replacing =s... with =s800
                                thumb_url = re.sub(r'=s\d+-', '=s800-', thumb_url)
                                if is_real_profile_image(url=thumb_url):
                                    result.has_logo = True
                                    result.profile_image_url = thumb_url
                                else:
                                    result.has_logo = False
                            else:
                                result.has_logo = False

                            # Last video / active
                            if last_video:
                                p_at = last_video.get("publishedAt")
                                if p_at:
                                    vid_dt = datetime.datetime.fromisoformat(
                                        p_at.replace("Z", "+00:00")
                                    )
                                    result.last_post_date = vid_dt.strftime("%d-%m-%Y")
                                    result.last_active = result.last_post_date
                                    now = datetime.datetime.now(
                                        datetime.timezone.utc
                                    )
                                    if (now - vid_dt).days <= 180:
                                        result.is_active = True

                        else:
                            error_comments.append("Channel ID found but not in API")
                    except Exception as e:
                        error_comments.append(f"API Fetch Error: {e}")
                else:
                    error_comments.append(
                        "Could not resolve Channel ID (API & Scrape)"
                    )

                # Wait for channel header to render (reduced from 10s → 4s)
                try:
                    await page.wait_for_selector(
                        "div#channel-header, ytd-channel-header-renderer",
                        state="visible",
                        timeout=4000,
                    )
                except Exception:
                    pass

                # Trigger paint: single scroll + mouse jitter (no fixed sleeps)
                try:
                    await page.evaluate("window.scrollTo(0, 300)")
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.mouse.move(
                        random.randint(100, 800), random.randint(100, 800)
                    )
                except Exception:
                    pass

                # Start profile image download in parallel with screenshot
                image_task = None
                if result.has_logo and result.profile_image_url:
                    image_task = asyncio.create_task(
                        download_profile_image(result.profile_image_url)
                    )

                # Screenshot
                screenshot_bytes = None
                try:
                    content_el = await page.query_selector("ytd-app")
                    if content_el:
                        bbox = await content_el.bounding_box()
                        if bbox:
                            screenshot_bytes = await page.screenshot(
                                clip={
                                    "x": bbox["x"],
                                    "y": bbox["y"],
                                    "width": bbox["width"],
                                    "height": min(1000, bbox["height"]),
                                },
                                type="jpeg",
                                quality=85,
                            )
                except Exception:
                    pass

                if not screenshot_bytes:
                    screenshot_bytes = await page.screenshot(
                        full_page=False, type="jpeg", quality=85
                    )

                if screenshot_bytes:
                    result.screenshot_b64 = base64.b64encode(
                        screenshot_bytes
                    ).decode("utf-8")

                # Exact subscriber count from page HTML — ONLY when API didn't
                # provide it (hidden subscriber count). Skipping this saves ~2s
                # because page.content() on YouTube transfers 2-5MB of HTML.
                if not result.followers:
                    try:
                        content = await page.content()
                        patterns = [
                            r'"subscriberCount":"(\d+)"',
                            r'"subscriberCount":(\d+)',
                            r'\"subscriberCount\":\"(\d+)\"',
                        ]

                        for pat in patterns:
                            match = re.search(pat, content)
                            if match:
                                try:
                                    val = int(match.group(1))
                                    result.followers = val
                                    break
                                except Exception:
                                    pass
                    except Exception as e:
                        error_comments.append(f"SubCountErr: {e}")

                # Await profile image download (was running in parallel with screenshot)
                if image_task:
                    try:
                        b64 = await image_task
                        if b64:
                            result.profile_image_b64 = b64
                    except Exception:
                        pass

            except Exception as e:
                error_comments.append(f"Screenshot/Page failed: {e}")

        except Exception as e:
            error_comments.append(f"Browser Error: {e}")

        # Finalise
        if not result.display_name:
            result.display_name = "Scrape Incomplete"

        result.comments = " | ".join(error_comments) if error_comments else ""

        calculate_risk(result)

        await self.health.record_request(
            "youtube", success=not bool(error_comments)
        )

        # Download profile image (fallback if parallel download wasn't started)
        if result.has_logo and result.profile_image_url and not result.profile_image_b64:
            b64 = await download_profile_image(result.profile_image_url)
            if b64:
                result.profile_image_b64 = b64

        return result

    # --- Helper methods ---

    def _get_youtube_service(self):
        return build("youtube", "v3", developerKey=self.config.YOUTUBE_API_KEY)

    @staticmethod
    def _extract_channel_handle_or_id(url: str):
        url = url.strip().rstrip("/")
        if "@" in url:
            return "handle", url.split("@")[-1]
        elif "/channel/" in url:
            return "id", url.split("/channel/")[-1]
        elif "/user/" in url:
            return "username", url.split("/user/")[-1]
        elif "/c/" in url:
            return "custom", url.split("/c/")[-1]
        else:
            return "unknown", url.split("/")[-1]

    @staticmethod
    async def _get_channel_id(service, id_type: str, value: str) -> str | None:
        try:
            if id_type == "id":
                return value

            def _sync_api():
                if id_type == "handle":
                    try:
                        req = service.channels().list(
                            part="id", forHandle="@" + value
                        )
                        res = req.execute()
                        if res.get("items"):
                            return res["items"][0]["id"]
                    except Exception:
                        pass

                    req = service.channels().list(part="id", forHandle=value)
                    res = req.execute()
                    if res.get("items"):
                        return res["items"][0]["id"]

                elif id_type == "username":
                    req = service.channels().list(part="id", forUsername=value)
                    res = req.execute()
                    if res.get("items"):
                        return res["items"][0]["id"]

                req = service.search().list(
                    part="snippet", type="channel", q=value, maxResults=1
                )
                res = req.execute()
                if res.get("items"):
                    return res["items"][0]["snippet"]["channelId"]

                return None

            return await asyncio.to_thread(_sync_api)
        except Exception as e:
            logger.error(f"Error resolving ID for {value}: {e}")
            return None

    @staticmethod
    async def _fetch_channel_data(service, resolved_channel_id: str) -> dict:
        def _fetch():
            request = service.channels().list(
                part="snippet,statistics,contentDetails,brandingSettings",
                id=resolved_channel_id,
            )
            response = request.execute()

            ret_data = {}
            if response.get("items"):
                data = response["items"][0]
                ret_data["snippet"] = data.get("snippet", {})
                ret_data["stats"] = data.get("statistics", {})
                ret_data["branding"] = data.get("brandingSettings", {})

                uploads_playlist_id = (
                    data.get("contentDetails", {})
                    .get("relatedPlaylists", {})
                    .get("uploads")
                )
                if uploads_playlist_id:
                    try:
                        pl_request = service.playlistItems().list(
                            part="snippet",
                            playlistId=uploads_playlist_id,
                            maxResults=1,
                        )
                        pl_response = pl_request.execute()
                        if pl_response.get("items"):
                            ret_data["last_video"] = pl_response["items"][0][
                                "snippet"
                            ]
                    except Exception:
                        pass
            return ret_data

        return await asyncio.to_thread(_fetch)
