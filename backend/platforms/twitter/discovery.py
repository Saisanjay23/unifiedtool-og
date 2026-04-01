"""
OSINT Discovery Engine: Twitter/X.
Orchestrates headless Playwright sessions to horizontally scrape the 'People' search tab.
Fuses DOM scraping with a passive GraphQL `user_cache` interceptor to resolve high-res assets 
and metadata without triggering rate-limiting node expansions.
"""
import asyncio
import random
import time
from urllib.parse import quote
from typing import Optional, List

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.stealth.human import HumanBehavior
from backend.platforms.base import AbstractDiscoverer

logger = get_logger("platforms.twitter.discovery")


class TwitterDiscoverer(AbstractDiscoverer):
    """
    Twitter Discovery Strategy Implementation.
    Executes a hybrid approach: eagerly scrapes visible `UserCell` nodes from the Virtual DOM, 
    while a concurrent background task listens to the CDP network stream to harvest the full 
    GraphQL `SearchTimeline` payload, enabling deep hydration of partial DOM nodes.
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

        from backend.stealth.browser import create_stealth_browser

        pw, browser, context, page = await create_stealth_browser(
            platform="twitter", headless=headless
        )

        try:
            # Validate Login (Legacy logic checks home first)
            try:
                await page.goto("https://x.com/home", timeout=60000)
                await asyncio.sleep(5)
                if "login" in page.url:
                    logger.error("Authentication Required for Twitter Discovery.")
                    await progress_callback(
                        event_type="error",
                        message="Authentication Required. Please log in via Sidebar to get valid cookies.",
                        count_found=0,
                    )
                    return results
            except Exception as e:
                logger.error(f"Failed to load home for validation: {e}")

            scrape_stats = {"count": 0, "rank": 0, "seen_urls": set()}

            for keyword in keywords:
                if self.health.should_pause("twitter"):
                    delay = self.health.get_recommended_delay("twitter")
                    await progress_callback(
                        event_type="rate_limited",
                        message=f"Rate limit approaching, pausing {delay:.0f}s",
                        count_found=len(results),
                    )
                    await asyncio.sleep(delay)

                await progress_callback(
                    event_type="progress",
                    message=f"Searching Twitter via DOM for '{keyword}'...",
                    count_found=len(results),
                    count_total=max_results * len(keywords),
                )

                scrape_stats["rank"] = 0

                found = await self._process_keyword_legacy(
                    page,
                    keyword,
                    max_results,
                    client,
                    progress_callback,
                    len(results),
                    max_results * len(keywords),
                    scrape_stats,
                )

                for profile in found:
                    results.append(profile)

        except Exception as e:
            logger.error(f"Twitter discovery failed: {e}")
        finally:
            await browser.close()
            await pw.stop()

        return results

    async def _process_keyword_legacy(
        self,
        page,
        keyword: str,
        target_limit: int,
        client_name: str,
        progress_callback,
        current_total: int,
        max_total: int,
        scrape_stats: dict,
    ) -> list[ProfileResult]:
        """
        Isolated DOM Extraction Cycle.
        Coordinates asynchronous scrolling and eager evaluation of rendered nodes.
        Hooks into `SearchTimeline` and `UserByScreenName` network endpoints to build a `user_cache`, 
        which is used to transparently upgrade low-res CDN avatars found in the DOM to their original master assets.
        """
        profiles = []

        # 1. Navigate to Search Page (People Tab)
        search_url = f"https://x.com/search?q={quote(keyword)}&src=typed_query&f=user"
        logger.info(f"Navigating to: {search_url}")

        user_cache: dict[str, dict] = {}

        async def _handle_response(response):
            try:
                if "SearchTimeline" in response.url or "UserByScreenName" in response.url or "UserByRestId" in response.url:
                    if response.status == 200:
                        try:
                            json_data = await response.json()
                            
                            # Case 1: SearchTimeline (contains multiple users)
                            if "SearchTimeline" in response.url:
                                instructions = json_data.get("data", {}).get("search_by_raw_query", {}).get("search_timeline", {}).get("timeline", {}).get("instructions", [])
                                for inst in instructions:
                                    if inst.get("type") == "TimelineAddEntries":
                                        for entry in inst.get("entries", []):
                                            item_res = entry.get("content", {}).get("itemContent", {}).get("user_results", {}).get("result", {})
                                            if item_res:
                                                core = item_res.get("core", {})
                                                handle = core.get("screen_name", "") or item_res.get("legacy", {}).get("screen_name", "")
                                                if handle:
                                                    user_cache[handle.lower()] = item_res
                                                    
                            # Case 2: Direct User Lookup
                            else:
                                data = json_data.get("data", {})
                                user_res = data.get("user", {}).get("result", {})
                                if not user_res and "user" in data:
                                    user_res = data["user"].get("result", {})
                                
                                if user_res:
                                    core = user_res.get("core", {})
                                    handle = core.get("screen_name", "") or user_res.get("legacy", {}).get("screen_name", "")
                                    if handle:
                                        user_cache[handle.lower()] = user_res
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", _handle_response)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(5, 8))

            if "/login" in page.url or "/i/flow/login" in page.url:
                logger.error("Redirected to login page during search.")
                return profiles

        except Exception as e:
            logger.error(f"Nav Error: {e}")
            return profiles

        no_change_count = 0
        last_height = 0

        while scrape_stats["rank"] < target_limit:
            cells = await page.locator('div[data-testid="UserCell"]').all()

            if not cells:
                cells = await page.locator('div[data-testid="cellInnerDiv"]').all()

            if len(cells) == 0:
                if await page.locator('text="No results for"').is_visible():
                    break

                await asyncio.sleep(2)
                no_change_count += 1

            new_in_batch = 0

            for cell in cells:
                if scrape_stats["rank"] >= target_limit:
                    break

                try:
                    res = await self._extract_cell_data_legacy(cell, keyword)
                    if not res:
                        continue

                    if res["url"] in scrape_stats["seen_urls"]:
                        continue
                    scrape_stats["seen_urls"].add(res["url"])

                    confidence = "LOW"
                    name_lower = res["name"].lower()
                    kw_lower = keyword.lower()
                    kw_words = kw_lower.split()
                    if kw_lower in name_lower:
                        confidence = "HIGH"
                    elif any(w in name_lower for w in kw_words if len(w) > 2):
                        confidence = "MEDIUM"
                    if confidence == "LOW" and res.get("bio"):
                        bio_lower = res["bio"].lower()
                        if kw_lower in bio_lower:
                            confidence = "MEDIUM"
                        elif any(w in bio_lower for w in kw_words if len(w) > 2):
                            confidence = "MEDIUM"

                    # Get HD version
                    hd_img_url = res.get("img_src", "")
                    
                    # Fallback to cache if missing from DOM
                    if not hd_img_url and res.get("handle"):
                        cached_result = user_cache.get(res["handle"].lower(), {})
                        if cached_result:
                            # Try avatar object
                            avatar = cached_result.get("avatar", {})
                            if avatar and "image_url" in avatar:
                                hd_img_url = avatar["image_url"]
                            else:
                                # Try legacy object
                                hd_img_url = cached_result.get("legacy", {}).get("profile_image_url_https", "")
                            
                    if hd_img_url:
                         import re
                         hd_img_url = re.sub(r"_(normal|mini|bigger)\.", "_400x400.", hd_img_url)

                    profile_result = ProfileResult(
                        platform="twitter",
                        client_name=client_name,
                        keyword=keyword,
                        url=res["url"],
                        username=res["handle"],
                        display_name=res["name"],
                        profile_image_url=hd_img_url,
                        bio=res["bio"],
                        followers=0,
                        is_verified=False,
                        confidence=confidence,
                    )

                    profiles.append(profile_result)
                    scrape_stats["rank"] += 1
                    new_in_batch += 1

                    await progress_callback(
                        event_type="result_found",
                        message=f"Found: @{res['handle']} ({confidence}% match)",
                        count_found=current_total + scrape_stats["rank"],
                        count_total=max_total,
                        result=profile_result.to_dict(),
                    )

                except Exception:
                    pass

            # Scroll Logic
            if new_in_batch == 0:
                no_change_count += 1
            else:
                no_change_count = 0

            if no_change_count >= 5:
                break

            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(random.uniform(2, 4))

            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            last_height = new_height

        return profiles

    async def _extract_cell_data_legacy(self, cell, keyword) -> Optional[dict]:
        """
        Targeted Node Extraction.
        Evaluates a specific `UserCell` DOM fragment to extract handle, bio, and avatar.
        Built with extreme fault-tolerance to handle varying A/B tested cell layouts.
        """
        try:
            link_el = cell.locator('a[href^="/"]').first
            if await link_el.count() == 0:
                return None

            href = await link_el.get_attribute("href")
            if not href:
                return None

            handle = href.split("?")[0].strip("/")
            if handle in ["search", "home", "explore", "notifications"]:
                return None

            full_url = f"https://x.com/{handle}"

            try:
                name_text = await cell.locator(
                    'div[dir="ltr"] > span > span'
                ).first.inner_text()
            except:
                name_text = handle

            img_src = ""
            try:
                # Twitter currently hides img tags in search results for some reason. 
                # But the username is in the UserAvatar-Container-{handle}
                # And we can construct the profile picture url from the GraphQL API or try to find it.
                # A safer way since img is missing is to grab the avatar container style if it has a background image,
                # but if that's missing, we leave it blank, and the frontend will use the default avatar.
                # Since we don't have the GraphQL interceptor attached to the discovery page (only analysis),
                # we'll try to find any img tag, or fallback to empty string.
                # Let's try to find an img first:
                img_el = cell.locator('img[src*="profile_images"]').first
                if await img_el.count() > 0:
                    raw_src = await img_el.get_attribute("src")
                    if raw_src:
                        # Use the original image instead of constrained _400x400 or _normal
                        img_src = raw_src.replace("_normal.", ".").replace("_mini.", ".")
                else:
                    # New Twitter DOM places image in a div, but it might not even render until interacted with
                    # Provide empty string to let it fallback to default avatar on frontend discovery card
                    img_src = ""
            except:
                pass

            bio_text = ""
            try:
                bio_el = cell.locator('div[dir="auto"]').last
                if await bio_el.count() > 0:
                    bio_text = await bio_el.inner_text()
            except:
                pass

            return {
                "url": full_url,
                "handle": handle,
                "name": name_text,
                "img_src": img_src,
                "bio": bio_text,
            }
        except Exception:
            return None
