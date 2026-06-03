"""
Deep Identity Hydration: Twitter/X — ULTRA-PERFORMANCE v2.
Key changes from v1:
- Single-page JS injection: extracts name, followers, bio, location, joined date,
  profile pic, last post, verified badge — all in one page.evaluate() call.
- Network interception kept as primary data source (most accurate for followers,
  created_at, profile image).
- No serial DOM queries. No networkidle waits. No random mouse moves.
- Target: ~5-7s per profile (down from ~15-20s).
"""
import asyncio
import base64
import datetime
import re

from backend.core.db import ProfileResult
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from backend.platforms.utils import (
    calculate_risk,
    download_profile_image,
    is_real_profile_image,
)
from backend.stealth.browser import create_stealth_browser

logger = get_logger("platforms.twitter.analysis")

# ═══════════════════════════════════════════════════════════════════
# SINGLE-PAGE JS INJECTION — extracts all visible data in one shot
# Replaces ~15 individual query_selector calls with one evaluate().
# ═══════════════════════════════════════════════════════════════════

TW_BULK_EXTRACTION_JS = r"""
() => {
    const result = {
        display_name: '',
        username: '',
        followers: 0,
        following: 0,
        bio: '',
        location: '',
        joined_text: '',
        profile_pic_url: '',
        is_verified: false,
        last_post_iso: '',
        tweet_count: 0,
    };

    try {
        // 1. Name & Username from UserName div
        const userNameDiv = document.querySelector('div[data-testid="UserName"]');
        if (userNameDiv) {
            // Display name is the first span > span
            const nameSpan = userNameDiv.querySelector('span > span');
            if (nameSpan) result.display_name = (nameSpan.textContent || '').trim();
            
            // Username is in a span starting with @
            const allSpans = userNameDiv.querySelectorAll('span');
            for (const s of allSpans) {
                const text = (s.textContent || '').trim();
                if (text.startsWith('@')) {
                    result.username = text.substring(1);
                    break;
                }
            }
        }

        // 2. Followers — from followers link
        const followersLink = document.querySelector('a[href$="/verified_followers"], a[href$="/followers"]');
        if (followersLink) {
            const text = (followersLink.textContent || '').replace(/,/g, '').trim();
            const fm = text.match(/([\d.]+)\s*([KkMm]?)/);
            if (fm) {
                let val = parseFloat(fm[1]);
                const suffix = (fm[2] || '').toLowerCase();
                if (suffix === 'k') val *= 1000;
                else if (suffix === 'm') val *= 1000000;
                result.followers = Math.round(val);
            }
        }
        
        // 3. Following
        const followingLink = document.querySelector('a[href$="/following"]');
        if (followingLink) {
            const text = (followingLink.textContent || '').replace(/,/g, '').trim();
            const fm = text.match(/([\d.]+)\s*([KkMm]?)/);
            if (fm) {
                let val = parseFloat(fm[1]);
                const suffix = (fm[2] || '').toLowerCase();
                if (suffix === 'k') val *= 1000;
                else if (suffix === 'm') val *= 1000000;
                result.following = Math.round(val);
            }
        }

        // 4. Bio
        const bioDiv = document.querySelector('div[data-testid="UserDescription"]');
        if (bioDiv) result.bio = (bioDiv.textContent || '').trim();

        // 5. Location
        const locSpan = document.querySelector('span[data-testid="UserLocation"]');
        if (locSpan) {
            // Nested span > span pattern
            const inner = locSpan.querySelector('span span');
            if (inner) result.location = (inner.textContent || '').trim();
            else result.location = (locSpan.textContent || '').trim();
        }

        // 6. Joined date — "Joined Month Year"
        const spans = document.querySelectorAll('span');
        for (const s of spans) {
            const text = (s.textContent || '').trim();
            if (text.startsWith('Joined ') && text.length < 30) {
                result.joined_text = text;
                break;
            }
        }

        // 7. Profile picture
        const pfpSelectors = [
            'img[alt="Opens profile photo"]',
            'a[href$="/photo"] img',
            'div[data-testid="UserAvatar-Container-unknown"] img',
            'img[src*="profile_images"]',
        ];
        for (const sel of pfpSelectors) {
            const img = document.querySelector(sel);
            if (img) {
                const src = img.getAttribute('src') || '';
                if (src && src.includes('http')) {
                    // Upgrade to 400x400 for high-res
                    result.profile_pic_url = src.replace('_normal.', '_400x400.').replace('_mini.', '_400x400.');
                    break;
                }
            }
        }

        // 8. Verified badge
        const verifiedSvg = document.querySelector('[data-testid="UserName"] svg[data-testid="icon-verified"]');
        if (verifiedSvg) result.is_verified = true;

        // 9. Last post date — first non-pinned tweet's <time> tag
        const tweets = document.querySelectorAll('article[data-testid="tweet"]');
        for (const tweet of tweets) {
            // Skip pinned tweets
            const pinnedEl = tweet.querySelector('[data-testid="socialContext"]');
            if (pinnedEl) {
                const pinnedText = (pinnedEl.textContent || '').toLowerCase();
                if (pinnedText.includes('pinned')) continue;
            }
            
            const timeTag = tweet.querySelector('time');
            if (timeTag) {
                const dt = timeTag.getAttribute('datetime');
                if (dt) {
                    result.last_post_iso = dt;
                    break;
                }
            }
        }

    } catch(e) {}

    return result;
}
"""


class TwitterAnalyzer(AbstractAnalyzer):
    """
    Deep-analyzes Twitter/X profiles using single-page JS injection
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
        """
        Core analysis logic — ULTRA-PERFORMANCE v2.

        Flow:
        1. page.goto() + domcontentloaded
        2. Network interception captures UserByScreenName API data
        3. Wait for UserName selector (reduced timeout: 15s)
        4. Dismiss popups (fast)
        5. page.evaluate(TW_BULK_EXTRACTION_JS) — extract everything in one shot
        6. Merge: network data preferred, JS data as fallback
        7. Screenshot (simplified, no networkidle)
        """
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
        data_store = {
            "followers_count": None,
            "profile_image_url": None,
            "created_at": None,
        }

        # --- Network interception handler ---
        async def _handle_response(response):
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
                                data_store["followers_count"] = legacy.get(
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
                                    data_store["profile_image_url"] = img_url
                                    logger.info(f"Network intercepted profile image URL: {img_url[:80]}...")
                                # Capture created_at from API
                                created_at = legacy.get("created_at", "")
                                if created_at:
                                    data_store["created_at"] = created_at
                                    logger.info(f"Network intercepted created_at: {created_at}")
                        except Exception as e:
                            logger.warning(f"Failed to parse UserByScreenName response: {e}")
            except Exception:
                pass

        try:
            logger.info(f"[{url}] Analysis starting using provided page.")

            # Stealth Scripts
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            page.on("response", lambda r: asyncio.ensure_future(_handle_response(r)))

            # ── Step 1: Navigate ──
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                # Wait for UserName to appear (reduced timeout for speed)
                await page.wait_for_selector(
                    'div[data-testid="UserName"]', timeout=15000
                )

            except Exception:
                error_comments.append("Name not found (Load Timeout)")

            # ── Step 2: Dismiss popups (fast) ──
            for selector in [
                '[aria-label="Close"]',
                'div[role="button"]:has-text("Accept")',
            ]:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0 and await el.is_visible(timeout=100):
                        await el.click()
                except Exception:
                    pass

            # ── Step 3: Brief wait for network interception ──
            # Twitter's UserByScreenName API fires almost immediately
            await asyncio.sleep(1.0)

            # ── Step 4: Single JS injection — extract everything from DOM ──
            js_data = {}
            try:
                js_data = await page.evaluate(TW_BULK_EXTRACTION_JS) or {}
            except Exception as e:
                logger.warning(f"JS extraction failed: {e}")

            # ── Step 5: Merge data — network preferred, JS fallback ──

            # 5a. Name
            if js_data.get("display_name"):
                result.display_name = js_data["display_name"]
                result.has_name_match = True

            # 5b. Followers — network > JS
            if data_store["followers_count"] is not None:
                result.followers = int(data_store["followers_count"])
            elif js_data.get("followers", 0) > 0:
                result.followers = js_data["followers"]
            else:
                result.followers = 0

            # 5c. Joined Date — network primary, JS DOM fallback
            if data_store.get("created_at"):
                # Parse Twitter's date format: "Wed Jun 01 12:00:00 +0000 2016"
                try:
                    joined_dt = datetime.datetime.strptime(
                        data_store["created_at"], "%a %b %d %H:%M:%S %z %Y"
                    )
                    result.created_at = joined_dt.strftime("%m-%Y")
                except ValueError:
                    pass

            if not result.created_at and js_data.get("joined_text"):
                # Parse "Joined Month Year"
                cleaned = js_data["joined_text"].replace("Joined", "", 1).strip()
                for fmt in ("%B %Y", "%b %Y", "%Y"):
                    try:
                        joined_dt = datetime.datetime.strptime(cleaned, fmt)
                        result.created_at = joined_dt.strftime("%m-%Y")
                        break
                    except ValueError:
                        continue

            # 5d. Location
            if js_data.get("location"):
                result.location = js_data["location"]

            # 5e. Bio
            if js_data.get("bio"):
                result.bio = js_data["bio"]

            # 5f. Logo — network > JS
            pfp_url = None
            if data_store.get("profile_image_url") and is_real_profile_image(url=data_store["profile_image_url"]):
                pfp_url = data_store["profile_image_url"]
                result.has_logo = True
                result.profile_image_url = pfp_url
            elif js_data.get("profile_pic_url") and is_real_profile_image(url=js_data["profile_pic_url"]):
                pfp_url = js_data["profile_pic_url"]
                result.has_logo = True
                result.profile_image_url = pfp_url
            else:
                result.has_logo = False

            # 5g. Last Post / Active
            result.is_active = False
            if js_data.get("last_post_iso"):
                try:
                    dt = datetime.datetime.fromisoformat(
                        js_data["last_post_iso"].replace("Z", "+00:00")
                    )
                    result.last_post_date = dt.strftime("%d-%m-%Y")
                    result.last_active = result.last_post_date
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if (now - dt).days <= 180:
                        result.is_active = True
                except Exception:
                    pass

            # 5h. Verified badge
            if js_data.get("is_verified"):
                result.is_verified = True
            else:
                result.is_verified = False

            # ── Step 6: Screenshot (simplified — no networkidle wait) ──
            screenshot_bytes = await self._capture_screenshot(page)
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

        # Record selector hits/misses to HealthManager
        try:
            from backend.core.health import HealthManager
            health_mgr = HealthManager()
            await health_mgr.record_selector_hit("twitter", "display_name", result.has_name_match)
            await health_mgr.record_selector_hit("twitter", "followers", result.followers > 0)
            await health_mgr.record_selector_hit("twitter", "profile_image", result.has_logo)
            has_created = bool(result.created_at and "Restricted" not in result.created_at)
            await health_mgr.record_selector_hit("twitter", "created_at", has_created)
            await health_mgr.record_selector_hit("twitter", "last_post_date", bool(result.last_post_date))
        except Exception as e:
            logger.debug(f"Failed to record selector hits: {e}")

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
    async def _capture_screenshot(page) -> bytes | None:
        """Simplified screenshot — no networkidle wait, no random mouse moves."""
        try:
            await asyncio.sleep(0.5)

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
                            timeout=15000,
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
            try:
                return await page.screenshot(type="jpeg", quality=85, timeout=10000)
            except Exception:
                return None

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
