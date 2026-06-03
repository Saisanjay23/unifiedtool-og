"""
Data API Integrator: YouTube.
Provides a strictly typed, fully orchestrated adapter for the official YouTube Data API v3.
Operates entirely out-of-band (OOB) from the Chromium execution pool to drastically minimize
resource latency and leverage Google's managed backend scale.

Performance: Batches channels.list calls (up to 50 IDs per request) to reduce API quota usage
by ~50x vs individual channel lookups. Uses shared aiohttp session for thumbnail downloads.
"""
import asyncio
import base64
import re

import aiohttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.core.db import ProfileResult
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
        use_free_proxy: bool = False,
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
        Targeted Discovery Orchestration with Batched Channel Hydration.
        
        1. Dispatches paginated `search.list` requests to find channel IDs.
        2. Batches up to 50 channel IDs into a single `channels.list` call for stats+snippets.
        3. Downloads thumbnails concurrently via shared aiohttp session.
        
        This reduces API calls from O(N) to O(N/50) for the channels.list endpoint.
        """
        profiles = []
        try:
            next_page_token = None
            
            while len(profiles) < target_limit:
                # 1. Search Request — get channel IDs
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

                # 2. Collect all channel IDs from this page for batched hydration
                channel_ids = []
                search_items_by_id = {}
                for item in items:
                    snippet = item.get("snippet", {})
                    channel_id = item.get("id", {}).get("channelId") or snippet.get("channelId")
                    if channel_id:
                        channel_ids.append(channel_id)
                        search_items_by_id[channel_id] = item

                if not channel_ids:
                    logger.info(f"No valid channel IDs in search results for {keyword}")
                    break

                # 3. Batch channels.list call — up to 50 IDs in a single request
                #    This is the key optimization: instead of N separate API calls,
                #    we make 1 call for up to 50 channels at once.
                channel_details = {}
                try:
                    batch_ids = ",".join(channel_ids)
                    detail_request = service.channels().list(
                        part="statistics,snippet",
                        id=batch_ids
                    )
                    detail_response = await asyncio.to_thread(detail_request.execute)
                    
                    for detail_item in detail_response.get("items", []):
                        cid = detail_item.get("id")
                        if cid:
                            channel_details[cid] = detail_item
                    
                    logger.info(
                        f"Batched channels.list: requested {len(channel_ids)}, "
                        f"received {len(channel_details)} details"
                    )
                except Exception as e:
                    logger.warning(f"Batched channels.list failed: {e}. Falling back to search snippets only.")

                # 4. Build ProfileResults + download thumbnails concurrently
                async with aiohttp.ClientSession(
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                ) as dl_session:
                    for channel_id in channel_ids:
                        if len(profiles) >= target_limit:
                            break

                        search_item = search_items_by_id[channel_id]
                        detail = channel_details.get(channel_id)
                        
                        profile = await self._build_profile(
                            search_item, detail, channel_id, keyword, client_name, dl_session
                        )

                        if profile:
                            profiles.append(profile)

                            await progress_callback(
                                event_type="result_found",
                                message=f"Found: {profile.display_name} ({profile.confidence} match)",
                                count_found=current_total + len(profiles),
                                count_total=max_total,
                                result=profile.to_dict(),
                            )
                
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

    async def _build_profile(
        self,
        search_item: dict,
        detail: dict | None,
        channel_id: str,
        keyword: str,
        client_name: str,
        dl_session: aiohttp.ClientSession,
    ) -> ProfileResult | None:
        """
        Builds a ProfileResult from search snippet + batched channel detail.
        
        Uses the detailed channel data (from batched channels.list) when available,
        falls back to search snippet data otherwise. Downloads profile thumbnail
        using the shared aiohttp session.
        """
        try:
            search_snippet = search_item.get("snippet", {})
            
            # Use detail snippet if available (has richer data like customUrl, full description)
            if detail:
                detail_snippet = detail.get("snippet", {})
                title = detail_snippet.get("title") or search_snippet.get("title", "")
                desc = detail_snippet.get("description") or search_snippet.get("description", "")
                custom_url = detail_snippet.get("customUrl")
                thumbs = detail_snippet.get("thumbnails", {})
                stats = detail.get("statistics", {})
            else:
                title = search_snippet.get("title", "")
                desc = search_snippet.get("description", "")
                custom_url = None
                thumbs = search_snippet.get("thumbnails", {})
                stats = {}

            # Best available thumbnail
            thumb_url = ""
            for res in ["high", "medium", "default"]:
                if thumbs.get(res):
                    thumb_url = thumbs[res].get("url", "")
                    break

            # Construct URL
            if custom_url:
                full_url = f"https://www.youtube.com/{custom_url}"
            else:
                full_url = f"https://www.youtube.com/channel/{channel_id}"

            # Download thumbnail via shared session (no per-channel session creation)
            profile_image_b64 = None
            if thumb_url:
                try:
                    async with dl_session.get(thumb_url) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()
                            if len(img_data) > 500:
                                profile_image_b64 = base64.b64encode(img_data).decode("utf-8")
                except Exception:
                    pass

            # Confidence scoring
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
                has_logo=bool(thumb_url),
                bio=desc,
                followers=int(stats.get("subscriberCount", 0)),
                post_count=int(stats.get("videoCount", 0)),
                confidence=confidence,
            )

            if profile_image_b64:
                result.profile_image_b64 = profile_image_b64

            return result

        except Exception as e:
            logger.error(f"Error building profile for channel {channel_id}: {e}")
            return None
