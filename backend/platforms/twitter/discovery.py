"""
OSINT Discovery Engine: Twitter/X.
Orchestrates headless Playwright sessions to horizontally scrape the 'People' search tab.
Fuses DOM scraping with a passive GraphQL `user_cache` interceptor to resolve high-res assets 
and metadata without triggering rate-limiting node expansions.
"""
import asyncio
import random
from urllib.parse import quote

from backend.core.config import settings
from backend.core.db import ProfileResult
from backend.core.logger import get_logger
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
        use_free_proxy: bool = False,
        **kwargs,
    ) -> list[ProfileResult]:

        results = []

        from backend.stealth.browser import create_stealth_browser

        pw, browser, context, page = await create_stealth_browser(
            platform="twitter", headless=headless, use_free_proxy=use_free_proxy
        )

        try:
            # Validate Login (Legacy logic checks home first)
            try:
                await page.goto("https://x.com/home", timeout=60000)
                # Smart wait: wait for Twitter to render (login redirect or home feed)
                # instead of a fixed 5s sleep
                _mode = settings.DISCOVERY_SPEED_MODE
                try:
                    await page.wait_for_selector(
                        'a[href="/home"], a[href="/login"]',
                        timeout=8000,
                    )
                except Exception:
                    await asyncio.sleep(1.0 if _mode == "aggressive" else 2.0 if _mode == "balanced" else 5.0)
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
                                                legacy = item_res.get("legacy", {})
                                                handle = legacy.get("screen_name", "")
                                                if handle:
                                                    user_cache[handle.lower()] = item_res
                                                    
                            # Case 2: Direct User Lookup
                            else:
                                data = json_data.get("data", {})
                                user_res = data.get("user", {}).get("result", {})
                                if not user_res and "user" in data:
                                    user_res = data["user"].get("result", {})
                                
                                if user_res:
                                    legacy = user_res.get("legacy", {})
                                    handle = legacy.get("screen_name", "")
                                    if handle:
                                        user_cache[handle.lower()] = user_res
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", _handle_response)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            # Smart wait: wait for UserCell elements to render instead of fixed 5-8s
            _mode = settings.DISCOVERY_SPEED_MODE
            try:
                await page.wait_for_selector(
                    'div[data-testid="UserCell"], div[data-testid="cellInnerDiv"]',
                    timeout=10000,
                )
            except Exception:
                await asyncio.sleep(1.0 if _mode == "aggressive" else 2.0 if _mode == "balanced" else 5.0)

            # Detect and recover from "Something went wrong" error page
            for retry in range(3):
                body_text = ""
                try:
                    body_text = await page.evaluate("() => (document.body?.innerText || '').substring(0, 300)")
                except Exception:
                    pass

                if "something went wrong" in body_text.lower():
                    logger.warning(f"Twitter showed 'Something went wrong' (attempt {retry+1}/3), retrying...")
                    # Try clicking the "Try again" button
                    try:
                        try_again = page.locator('text="Try again"').first
                        if await try_again.is_visible(timeout=3000):
                            await try_again.click()
                            # Speed-mode-aware retry wait (was hardcoded 5s)
                            _mode = settings.DISCOVERY_SPEED_MODE
                            await asyncio.sleep(1.0 if _mode == "aggressive" else 2.0 if _mode == "balanced" else 5.0)
                            continue
                    except Exception:
                        pass
                    # If no button, do a full reload
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    # Speed-mode-aware reload wait (was hardcoded 5-8s)
                    _mode = settings.DISCOVERY_SPEED_MODE
                    await asyncio.sleep(random.uniform(1, 2) if _mode == "aggressive" else random.uniform(2, 4) if _mode == "balanced" else random.uniform(5, 8))
                else:
                    break  # Page loaded successfully

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

                # Check for "Something went wrong" mid-scrape
                try:
                    body_text = await page.evaluate("() => (document.body?.innerText || '').substring(0, 300)")
                    if "something went wrong" in body_text.lower():
                        logger.warning("Twitter 'Something went wrong' mid-scrape, clicking Try again...")
                        try:
                            try_again = page.locator('text="Try again"').first
                            if await try_again.is_visible(timeout=2000):
                                await try_again.click()
                                _mode = settings.DISCOVERY_SPEED_MODE
                                await asyncio.sleep(1.0 if _mode == "aggressive" else 2.0 if _mode == "balanced" else 5.0)
                                continue
                        except Exception:
                            pass
                except Exception:
                    pass

                _mode = settings.DISCOVERY_SPEED_MODE
                await asyncio.sleep(0.3 if _mode == "aggressive" else 0.8 if _mode == "balanced" else 2.0)
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

                    # Get HD version & enrich from GraphQL cache
                    hd_img_url = res.get("img_src", "")
                    cached_result = user_cache.get(res.get("handle", "").lower(), {})
                    cached_legacy = cached_result.get("legacy", {}) if cached_result else {}
                    
                    # Fallback to cache if missing from DOM
                    if not hd_img_url and cached_legacy:
                        hd_img_url = cached_legacy.get("profile_image_url_https", "")
                            
                    if hd_img_url:
                         import re
                         hd_img_url = re.sub(r"_(normal|mini|bigger)\.", "_400x400.", hd_img_url)

                    # Enrich followers from GraphQL cache (instead of hardcoded 0)
                    followers_count = 0
                    if cached_legacy:
                        followers_count = cached_legacy.get("followers_count", 0) or 0

                    # Enrich verified status from GraphQL cache
                    is_verified = False
                    if cached_legacy:
                        is_verified = cached_legacy.get("verified", False) or cached_result.get("is_blue_verified", False)

                    profile_result = ProfileResult(
                        platform="twitter",
                        client_name=client_name,
                        keyword=keyword,
                        url=res["url"],
                        username=res["handle"],
                        display_name=res["name"],
                        profile_image_url=hd_img_url,
                        has_logo=bool(hd_img_url),
                        bio=res["bio"],
                        followers=followers_count,
                        is_verified=is_verified,
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

            # Smart wait: wait for DOM to update after scroll instead of fixed 2-4s
            _mode = settings.DISCOVERY_SPEED_MODE
            _pre_height = await page.evaluate("document.body.scrollHeight")
            try:
                await page.wait_for_function(
                    f"() => document.body.scrollHeight > {_pre_height}",
                    timeout=3000 if _mode == "aggressive" else 5000 if _mode == "balanced" else 8000,
                )
            except Exception:
                # Content didn't grow — might be end of results or slow load
                await asyncio.sleep(0.5 if _mode == "aggressive" else 1.0 if _mode == "balanced" else 2.0)

            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3 if _mode == "aggressive" else 0.8 if _mode == "balanced" else 2.0)
            last_height = new_height

        return profiles

    async def _extract_cell_data_legacy(self, cell, keyword) -> dict | None:
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
            except Exception:
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
            except Exception:
                pass

            bio_text = ""
            try:
                bio_el = cell.locator('div[dir="auto"]').last
                if await bio_el.count() > 0:
                    bio_text = await bio_el.inner_text()
            except Exception:
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
