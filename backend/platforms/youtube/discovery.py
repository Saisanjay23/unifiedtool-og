"""
Data API Integrator: YouTube.
Provides a strictly typed, fully orchestrated adapter for the official YouTube Data API v3.
Operates entirely out-of-band (OOB) from the Chromium execution pool to drastically minimize
resource latency and leverage Google's managed backend scale.
"""
import base64
import asyncio
import requests
import re
from typing import Optional, List

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.platforms.base import AbstractDiscoverer

logger = get_logger("platforms.youtube.discovery")


class YouTubeDiscoverer(AbstractDiscoverer):
    """
    Data API Adapter for YouTube Discovery.
    Replaces brittle DOM scraping with deterministic, paginated REST payloads.
    Requires an authorized GCP Service Account key injected via the environment configuration.
    """

    async def search(
        self,
        progress_callback,
        client: str,
        keywords: list[str],
        max_results: int = 50,
        headless: bool = True,  # Unused for API
        **kwargs,
    ) -> list[ProfileResult]:

        results = []

        if not self.config.YOUTUBE_API_KEY:
            logger.error("Missing YOUTUBE_API_KEY")
            await progress_callback(
                event_type="error",
                message="Missing YouTube API Key.",
                count_found=0,
            )
            return results

        try:
            service = build("youtube", "v3", developerKey=self.config.YOUTUBE_API_KEY)
        except Exception as e:
            logger.error(f"Failed to build YouTube Service: {e}")
            return results

        # Process keywords
        for keyword in keywords:
            if self.health.should_pause("youtube"):
                delay = self.health.get_recommended_delay("youtube")
                await progress_callback(
                    event_type="rate_limited",
                    message=f"Rate limit approaching, pausing {delay:.0f}s",
                    count_found=len(results),
                )
                await asyncio.sleep(delay)

            await progress_callback(
                event_type="progress",
                message=f"Searching YouTube API for '{keyword}'...",
                count_found=len(results),
                count_total=max_results * len(keywords),
            )

            try:
                found = await self._process_keyword(
                    service,
                    keyword,
                    max_results,
                    client,
                    progress_callback,
                    len(results),
                    max_results * len(keywords),
                )

                for profile in found:
                    results.append(profile)

            except Exception as e:
                logger.error(f"Error processing {keyword}: {e}")

        return results

    async def _process_keyword(
        self,
        service,
        keyword: str,
        target_limit: int,
        client_name: str,
        progress_callback,
        current_total: int,
        max_total: int,
    ) -> list[ProfileResult]:
        """
        Targeted Discovery Orchestration.
        Dispatches paginated `search.list` requests to the v3 REST API. Asynchronously
        offloads network IO to the `asyncio.to_thread` executor pool to prevent blocking
        the primary FastAPI reactor core.
        """
        profiles = []
        try:
            next_page_token = None
            
            while len(profiles) < target_limit:
                # 1. Search Request
                request = service.search().list(
                    q=keyword,
                    type="channel",
                    part="snippet",
                    maxResults=min(target_limit - len(profiles), 50),  # API max is 50
                    pageToken=next_page_token
                )
                search_response = await asyncio.to_thread(request.execute)

                items = search_response.get("items", [])
                if not items:
                    logger.info(f"No more channels found for {keyword}")
                    break

                # Iterate
                for item in items:
                    if len(profiles) >= target_limit:
                        break

                    # Process Single Channel
                    profile = await self._process_single_channel(
                        service, item, keyword, client_name
                    )

                    if profile:
                        profiles.append(profile)

                        await progress_callback(
                            event_type="result_found",
                            message=f"Found: {profile.display_name} ({profile.confidence}% match)",
                            count_found=current_total + len(profiles),
                            count_total=max_total,
                            result=profile.to_dict(),
                        )
                        # Respectful delay
                        await asyncio.sleep(0.1)
                
                next_page_token = search_response.get("nextPageToken")
                if not next_page_token:
                    break

            await self.health.record_request("youtube", success=True)

        except HttpError as e:
            logger.error(f"YouTube API HttpError: {e}")
            await self.health.record_request("youtube", success=False)
        except Exception as e:
            logger.error(f"YouTube Loop Error: {e}")

        return profiles

    async def _process_single_channel(
        self, service, item, keyword, client_name
    ) -> Optional[ProfileResult]:
        """
        Auxiliary Data Hydration Pipeline.
        Since the primary `search.list` endpoint omits engagement metrics (like sub counts),
        a secondary decoupled request is sent to `channels.list` to enrich the `ProfileResult` context.
        """
        try:
            snippet = item.get("snippet", {})
            channel_id = snippet.get("channelId")
            if not channel_id:
                return None

            title = snippet.get("title", "")
            desc = snippet.get("description", "")

            # Thumbnails
            thumbs = snippet.get("thumbnails", {})
            thumb_url = ""
            for res in ["high", "medium", "default"]:
                if thumbs.get(res):
                    thumb_url = thumbs[res].get("url", "")
                    break

            # 2. Get Statistics (Subscribers, etc.)
            stats = {}
            custom_url = None
            try:
                stats_response = await asyncio.to_thread(
                    service.channels()
                    .list(part="statistics,snippet", id=channel_id)
                    .execute
                )
                stats_items = stats_response.get("items", [])
                stats = stats_items[0].get("statistics", {}) if stats_items else {}

                # Merge snippet from detail
                if stats_items:
                    detail_snippet = stats_items[0].get("snippet", {})
                    if detail_snippet.get("description"):
                        desc = detail_snippet.get("description")
                    custom_url = detail_snippet.get("customUrl")
            except Exception as e:
                logger.warning(f"Stats fetch failed for {channel_id}: {e}")

            # Construct URL
            if custom_url:
                full_url = f"https://www.youtube.com/{custom_url}"
            else:
                full_url = f"https://www.youtube.com/channel/{channel_id}"

            # 3. Download Image & Convert to Base64
            profile_image_b64 = None
            if thumb_url:
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as dl_session:
                        async with dl_session.get(
                            thumb_url, headers={"User-Agent": "Mozilla/5.0"}
                        ) as resp:
                            if resp.status == 200:
                                b64_img = base64.b64encode(await resp.read()).decode(
                                    "utf-8"
                                )
                                profile_image_b64 = b64_img
                except:
                    pass

            # Confidence
            confidence = "LOW"
            name_lower = title.lower()
            kw_lower = keyword.lower()
            kw_words = kw_lower.split()
            if kw_lower in name_lower:
                confidence = "HIGH"
            elif any(w in name_lower for w in kw_words if len(w) > 2):
                confidence = "MEDIUM"
            if confidence == "LOW" and desc:
                bio_lower = desc.lower()
                if kw_lower in bio_lower:
                    confidence = "MEDIUM"
                elif any(w in bio_lower for w in kw_words if len(w) > 2):
                    confidence = "MEDIUM"

            # Result Object
            # Set to HD resolution (800x800) by replacing =s...
            thumb_url = re.sub(r'=s\d+-', '=s800-', thumb_url)
            
            result = ProfileResult(
                platform="youtube",
                client_name=client_name,
                keyword=keyword,
                url=full_url,
                username=custom_url or channel_id,
                display_name=title,
                profile_image_url=thumb_url,
                bio=desc,
                followers=int(stats.get("subscriberCount", 0)),
                post_count=int(stats.get("videoCount", 0)),
                confidence=confidence,
            )

            if profile_image_b64:
                result.profile_image_b64 = profile_image_b64

            return result

        except Exception as e:
            logger.error(f"Error processing channel {item.get('id', 'Unknown')}: {e}")
            return None
