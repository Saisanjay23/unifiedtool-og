import asyncio
import base64
import datetime
import random
import re
from typing import Any

from backend.core.db import ProfileResult
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from backend.platforms.utils import calculate_risk, download_profile_image, parse_followers
from backend.stealth.browser import create_stealth_browser

logger = get_logger("platforms.twitter.analysis")


class TwitterAnalyzer(AbstractAnalyzer):
    """
    Deep-analyzes Twitter/X profiles using browser-based scraping
    with network interception for reliable data extraction.
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
                platform="twitter", headless=headless,
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
        """Core analysis logic — writes directly to ProfileResult."""
        context = browser_context or page.context

        logger.info(f"Starting Twitter analysis for URL: {url} (Client: {client})")

        # --- Build the result object up front ---
        result = ProfileResult(
            platform="twitter",
            client_name=client,
            keyword="",
            url=url,
            username=self._extract_username(url),
        )
        error_comments = []
        data_store: dict[str, Any] = {
            "followers_count": None,
            "profile_image_url": None,
            "created_at": None,
        }

        # --- Network interception handler ---
        async def _handle_response(response, store: dict[str, Any]) -> None:
            try:
                if (
                    "UserByScreenName" in response.url
                    or "UserByRestId" in response.url
                ):
                    if response.status == 200:
                        try:
                            json_data = await response.json()
                            data = json_data.get("data", {})
                            user_res = data.get("user", {}).get("result", {})

                            if not user_res and "user" in data:
                                user_res = data["user"].get("result", {})

                            if user_res and "legacy" in user_res:
                                legacy = user_res["legacy"]
                                store["followers_count"] = legacy.get(
                                    "followers_count"
                                )
                                # Capture profile image URL from API
                                img_url = ""
                                avatar = user_res.get("avatar", {})
                                if avatar and "image_url" in avatar:
                                    img_url = avatar["image_url"]
                                if not img_url:
                                    img_url = legacy.get("profile_image_url_https", "")

                                if img_url:
                                    # Use _400x400 which is the official high-res size
                                    img_url = img_url.replace("_normal.", "_400x400.").replace("_mini.", "_400x400.")
                                    store["profile_image_url"] = img_url
                                    logger.info(f"Network intercepted profile image URL: {img_url[:80]}...")
                                # Capture created_at from API
                                created_at = legacy.get("created_at", "")
                                if created_at:
                                    store["created_at"] = created_at
                                    logger.info(f"Network intercepted created_at: {created_at}")
                        except Exception as e:
                            logger.warning(f"Failed to parse UserByScreenName response: {e}")
            except Exception:
                pass

        async def _capture_screenshot(page) -> bytes | None:
            try:
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass

                await asyncio.sleep(2)

                await page.mouse.wheel(0, 1)
                await page.mouse.wheel(0, -1)

                await page.evaluate("""
                () => new Promise(resolve => {
                    requestAnimationFrame(() => {
                        requestAnimationFrame(resolve);
                    });
                })
                """)

                screenshot_bytes = None
                try:
                    primary_col = await page.query_selector(
                        'div[data-testid="primaryColumn"]'
                    )
                    if primary_col:
                        bbox = await primary_col.bounding_box()
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
                                timeout=60000,
                            )
                except Exception:
                    pass

                if not screenshot_bytes:
                    screenshot_bytes = await page.screenshot(
                        type="jpeg", quality=85, timeout=15000
                    )

                return screenshot_bytes
            except Exception as e:
                logger.error(f"Screenshot capture failed: {e}")
                # Ultimate fallback: try simple viewport capture
                try:
                    return await page.screenshot(type="jpeg", quality=85, timeout=10000)
                except Exception:
                    return None

        try:
            logger.info(f"[{url}] Analysis starting using provided page.")

            # Stealth Scripts
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            page.on("response", lambda r: _handle_response(r, data_store))

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                await asyncio.sleep(random.uniform(2, 4))
                try:
                    await page.mouse.move(
                        random.randint(100, 500), random.randint(100, 500)
                    )
                except Exception:
                    pass

                await page.wait_for_selector(
                    'div[data-testid="UserName"]', timeout=30000
                )

            except Exception:
                error_comments.append("Name not found (Load Timeout)")

            # Dismiss popups
            for selector in [
                '[aria-label="Close"]',
                'div[role="button"]:has-text("Accept")',
            ]:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=1500):
                        await el.click()
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

            # 1. Name & Handle
            try:
                user_name_el = await page.query_selector(
                    'div[data-testid="UserName"]'
                )
                if user_name_el:
                    name_el = await user_name_el.query_selector("span > span")
                    if name_el:
                        name = (await name_el.inner_text()).strip()
                        result.display_name = name
                        result.has_name_match = True
            except Exception:
                error_comments.append("Name extraction failed")

            # 2. Followers (Hybrid: network → DOM → text search)
            try:
                if data_store["followers_count"] is not None:
                    result.followers = int(data_store["followers_count"])
                else:
                    followers_el = await page.query_selector(
                        "xpath=//a[contains(@href, 'followers')]"
                    )
                    if followers_el:
                        txt = await followers_el.inner_text()
                        result.followers = parse_followers(txt)
                    else:
                        f_el = await page.query_selector(
                            "xpath=//*[contains(text(), 'Followers')]"
                        )
                        if f_el:
                            parent = await f_el.query_selector("..")
                            if parent:
                                txt = await parent.inner_text()
                                result.followers = parse_followers(txt)
                            else:
                                result.followers = 0
                        else:
                            result.followers = 0
            except Exception:
                result.followers = 0

            # 3. Joined Date — Network primary, DOM fallback
            try:
                if data_store.get("created_at"):
                    # Parse Twitter's date format: "Wed Jun 01 12:00:00 +0000 2016"
                    try:
                        joined_dt = datetime.datetime.strptime(
                            data_store["created_at"], "%a %b %d %H:%M:%S %z %Y"
                        )
                        result.created_at = joined_dt.strftime("%m-%Y")
                    except ValueError:
                        pass

                if not result.created_at:
                    # DOM fallback
                    joined_elems = await page.query_selector_all(
                        "xpath=//span[contains(., 'Joined')]"
                    )
                    for el in joined_elems:
                        txt = (await el.inner_text()).strip()
                        if txt.startswith("Joined "):
                            cleaned = txt.replace("Joined", "", 1).strip()
                            for fmt in ("%B %Y", "%b %Y", "%Y"):
                                try:
                                    joined_dt = datetime.datetime.strptime(cleaned, fmt)
                                    result.created_at = joined_dt.strftime("%m-%Y")
                                    break
                                except ValueError:
                                    continue
                            break
            except Exception:
                pass

            # 4. Location
            try:
                loc = await page.query_selector(
                    'span[data-testid="UserLocation"] span span'
                )
                if loc:
                    result.location = (await loc.inner_text()).strip()
                elif loc := await page.query_selector(
                    'span[data-testid="UserLocation"]'
                ):
                    result.location = (await loc.inner_text()).strip()
            except Exception:
                pass

            # 5. Bio
            try:
                bio_el = await page.query_selector(
                    'div[data-testid="UserDescription"]'
                )
                if bio_el:
                    result.bio = (await bio_el.inner_text()).strip()
            except Exception:
                pass

            # 6. Logo — Network interception primary, DOM fallback
            try:
                if data_store.get("profile_image_url") and "default_profile_images" not in data_store["profile_image_url"]:
                    result.has_logo = True
                    result.profile_image_url = data_store["profile_image_url"]
                else:
                    # Fallback: try DOM selectors
                    pfp_el = None
                    for selector in [
                        'img[alt="Opens profile photo"]',
                        'a[href$="/photo"] img',
                        'div[data-testid="UserAvatar-Container-unknown"] img',
                        'div[style*="padding-bottom: 100%"] img',
                        'img[src*="profile_images"]',
                    ]:
                        pfp_el = await page.query_selector(selector)
                        if pfp_el:
                            break

                    if pfp_el:
                        src = await pfp_el.get_attribute("src")
                        if src and "default_profile_images" not in src:
                            src = src.replace("_normal.", "_400x400.")
                            result.has_logo = True
                            result.profile_image_url = src
                        else:
                            result.has_logo = False
                    else:
                        result.has_logo = False
            except Exception:
                result.has_logo = False

            # 7. Last Post / Active
            result.is_active = False
            try:
                try:
                    await page.wait_for_selector(
                        'article[data-testid="tweet"]', timeout=10000
                    )
                    tweets = await page.query_selector_all(
                        'article[data-testid="tweet"]'
                    )
                except Exception:
                    tweets = []

                if tweets:
                    for tweet in tweets:
                        pinned = await tweet.query_selector(
                            "xpath=.//*[contains(text(), 'Pinned')]"
                        )
                        if not pinned:
                            time_tag = await tweet.query_selector("time")
                            if not time_tag:
                                continue

                            dt_str = await time_tag.get_attribute("datetime")
                            if dt_str:
                                dt = datetime.datetime.fromisoformat(
                                    dt_str.replace("Z", "+00:00")
                                )
                                result.last_post_date = dt.strftime("%d-%m-%Y")
                                result.last_active = result.last_post_date

                                now = datetime.datetime.now(datetime.timezone.utc)
                                if (now - dt).days <= 180:
                                    result.is_active = True
                                break
            except Exception:
                pass

            # 8. Verified badge
            try:
                verified = await page.query_selector(
                    '[data-testid="UserName"] svg[data-testid="icon-verified"]'
                )
                result.is_verified = verified is not None
            except Exception:
                result.is_verified = False

            # 9. Screenshot
            screenshot_bytes = await _capture_screenshot(page)
            if screenshot_bytes:
                result.screenshot_b64 = base64.b64encode(
                    screenshot_bytes
                ).decode("utf-8")

        except Exception as e:
            logger.error(f"Critical error during scrape: {e}")
            error_comments.append(f"Critical error: {type(e).__name__}")

        # Finalise
        if not result.display_name:
            result.display_name = "Scrape Incomplete"

        result.comments = " | ".join(error_comments) if error_comments else ""

        calculate_risk(result)

        await self.health.record_request(
            "twitter", success=not bool(error_comments)
        )

        # Download profile image
        if result.has_logo and result.profile_image_url:
            logger.info(f"Profile picture URL found: {result.profile_image_url[:80]}...")
            b64 = await download_profile_image(result.profile_image_url)
            if b64:
                result.profile_image_b64 = b64
                logger.info(f"Profile image downloaded ({len(b64)} chars)")
            else:
                # Playwright fallback — use the browser context already open
                try:
                    if context:
                        img_page = await context.new_page()
                        try:
                            resp = await img_page.goto(result.profile_image_url, timeout=10000)
                            if resp and resp.status == 200:
                                img_bytes = await img_page.screenshot(full_page=True, type="jpeg", quality=80)
                                result.profile_image_b64 = base64.b64encode(img_bytes).decode("utf-8")
                                logger.info("Profile image captured via Playwright fallback")
                        finally:
                            await img_page.close()
                except Exception as e2:
                    logger.warning(f"Playwright fallback also failed: {e2}")

        return result

    @staticmethod
    def _extract_username(url: str) -> str:
        """Extract Twitter/X username from URL."""
        match = re.search(r"(?:twitter|x)\.com/(@?[a-zA-Z0-9_]+)", url)
        if match:
            username = match.group(1).lstrip("@")
            if username.lower() not in (
                "home",
                "search",
                "explore",
                "notifications",
                "messages",
                "i",
                "settings",
            ):
                return username
        return ""
