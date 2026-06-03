"""
Deep Identity Hydration: Instagram — ULTRA-PERFORMANCE v2.
Key changes from v1:
- Single-page JS injection: extracts name, followers, bio, profile pic, last post
  dates all in one page.evaluate() call — zero extra navigations.
- Network interception kept as primary data source (most accurate).
- 'About this account' dialog opened ONLY when network didn't provide date_joined.
- No scroll loops. No post-page navigations. No fixed sleeps.
- Target: ~5-8s per profile (down from ~20-30s).
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

logger = get_logger("platforms.instagram.analysis")

# Selectors for dismissing Instagram popups/challenges/overlays
IG_POPUP_SELECTORS = [
    # "We suspect automated behavior" challenge dismiss
    'button:has-text("Dismiss")',
    'button:has-text("OK")',
    'button:has-text("Close")',
    # Cookie consent / GDPR
    'button:has-text("Accept")',
    'button:has-text("Accept All")',
    'button:has-text("Accept all cookies")',
    'button:has-text("Allow essential and optional cookies")',
    'button:has-text("Allow all cookies")',
    # "Turn on Notifications" / "Save Login Info" prompts
    'button:has-text("Not Now")',
    'button:has-text("Not now")',
    'button:has-text("Skip")',
    'button:has-text("Cancel")',
    'button:has-text("Maybe Later")',
    # "This Was Me" / "Verify" identity confirmation
    'button:has-text("This Was Me")',
    'button:has-text("This was me")',
    # Generic dialog close buttons
    'div[role="dialog"] button[aria-label="Close"]',
    'div[role="dialog"] div[role="button"]:has(svg[aria-label="Close"])',
    'svg[aria-label="Close"]',
]

# ═══════════════════════════════════════════════════════════════════
# SINGLE-PAGE JS INJECTION — extracts all visible data in one shot
# No extra navigations, no scrolling, no IPC round-trips.
# ═══════════════════════════════════════════════════════════════════

IG_BULK_EXTRACTION_JS = r"""
() => {
    const result = {
        display_name: '',
        username: '',
        followers: 0,
        bio: '',
        profile_pic_url: '',
        post_dates: [],
        is_private: false,
        is_verified: false,
        post_count: -1,
    };

    try {
        // 1. Name & Username from og:title — "Display Name (@username)"
        const ogTitle = document.querySelector('meta[property="og:title"]');
        if (ogTitle) {
            const content = (ogTitle.getAttribute('content') || '').trim();
            const match = content.match(/^(.*?)\s\(@(.*?)\)/);
            if (match) {
                result.display_name = match[1].trim();
                result.username = match[2].trim();
            } else {
                // Fallback: "name • Instagram photos and videos"
                result.display_name = content.split('•')[0].trim();
            }
        }
        if (!result.display_name) {
            const h2 = document.querySelector('h2');
            if (h2) result.display_name = (h2.textContent || '').trim();
        }

        // 2. Followers from og:description — "123 Followers, 45 Following, 67 Posts"
        const ogDesc = document.querySelector('meta[property="og:description"]');
        if (ogDesc) {
            const desc = (ogDesc.getAttribute('content') || '');
            const fm = desc.match(/([\d.,kmKM]+)\s+Followers/i);
            if (fm) {
                let val = fm[1].replace(/,/g, '');
                if (/k/i.test(val)) val = parseFloat(val) * 1000;
                else if (/m/i.test(val)) val = parseFloat(val) * 1000000;
                else val = parseInt(val);
                result.followers = Math.round(val) || 0;
            }
            // Post count from description
            const pm = desc.match(/([\d.,]+)\s+Posts/i);
            if (pm) {
                result.post_count = parseInt(pm[1].replace(/,/g, '')) || -1;
            }
        }

        // 3. Followers from title attribute (exact count, more reliable)
        const titleFollowers = document.querySelector('a[href*="/followers/"] span[title]');
        if (titleFollowers) {
            const titleVal = (titleFollowers.getAttribute('title') || '').replace(/,/g, '');
            const parsed = parseInt(titleVal);
            if (parsed > 0) result.followers = parsed;
        }

        // 4. Profile picture from og:image
        const ogImage = document.querySelector('meta[property="og:image"]');
        if (ogImage) {
            result.profile_pic_url = (ogImage.getAttribute('content') || '').trim();
        }
        // Fallback: header img
        if (!result.profile_pic_url) {
            const headerImg = document.querySelector('header img');
            if (headerImg) {
                result.profile_pic_url = (headerImg.getAttribute('src') || '').trim();
            }
        }

        // 5. Bio from header section
        const bioSelectors = [
            'div[class*="biography"] span',
            'header + section span',
            'header section > div > span',
        ];
        for (const sel of bioSelectors) {
            const el = document.querySelector(sel);
            if (el) {
                const text = (el.textContent || '').trim();
                if (text && text.length > 2 && !text.match(/^\d+$/)) {
                    result.bio = text;
                    break;
                }
            }
        }

        // 6. Last post dates from <time datetime> elements in the grid
        //    Instagram renders time tags on posts visible in the grid
        const timeEls = document.querySelectorAll('time[datetime]');
        for (const t of timeEls) {
            const dt = t.getAttribute('datetime');
            if (dt) result.post_dates.push(dt);
        }

        // 7. Private account detection
        const privateTexts = ['This Account is Private', 'This account is private'];
        for (const pt of privateTexts) {
            if (document.body && document.body.innerText && document.body.innerText.includes(pt)) {
                result.is_private = true;
                break;
            }
        }

        // 8. Verified badge
        const verifiedSvg = document.querySelector('svg[aria-label="Verified"]');
        if (verifiedSvg) result.is_verified = true;

    } catch(e) {}

    return result;
}
"""


class InstagramAnalyzer(AbstractAnalyzer):
    """
    Analyzes Instagram profiles using single-page JS injection with
    network interception for reliable data extraction.
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
                platform="instagram", headless=headless,
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
        2. Network interception captures GraphQL data (followers, date_joined, last post ts)
        3. Dismiss popups (parallel, fast)
        4. page.evaluate(IG_BULK_EXTRACTION_JS) — extracts everything from DOM in one shot
        5. Merge: network data preferred, JS data as fallback
        6. 'About this account' dialog — ONLY if network didn't provide date_joined
        7. Screenshot
        """

        logger.info(
            f"Starting Instagram analysis for URL: {url} (Client: {client})"
        )

        # --- Build the result object up front ---
        result = ProfileResult(
            platform="instagram",
            client_name=client,
            keyword="",
            url=url,
            username=self._extract_username(url),
        )
        error_comments = []

        # Network interception data store
        captured_data = {
            "max_ts": 0,
            "followers": 0,
            "date_joined": None,
            "profile_pic_url_hd": None,
            "post_count": -1,
        }

        async def capture_graphql(response):
            try:
                if response.status == 200 and (
                    "graphql" in response.url
                    or "/feed/" in response.url
                    or "info" in response.url
                ):
                    try:
                        json_body = await response.json()

                        def recursive_extract_data(obj, current_user=None):
                            if isinstance(obj, dict):
                                user_ctx = current_user
                                
                                # Update user context if this node defines an owner/user
                                for user_key in ["user", "owner"]:
                                    if user_key in obj and isinstance(obj[user_key], dict) and "username" in obj[user_key]:
                                        user_ctx = obj[user_key]["username"]
                                        break
                                
                                # If the node itself is a user node
                                if "username" in obj and "id" in obj:
                                    user_ctx = obj["username"]

                                has_taken_at = False
                                taken_at_val = 0
                                is_pinned = False

                                for k, v in obj.items():
                                    if k in ["taken_at", "taken_at_timestamp"] and isinstance(v, int):
                                        has_taken_at = True
                                        taken_at_val = v
                                    if k in ["timeline_pinned_user_ids", "pinned_for_users"]:
                                        if isinstance(v, list) and len(v) > 0:
                                            is_pinned = True
                                    if k == "is_pinned" and v:
                                        is_pinned = True

                                if has_taken_at and not is_pinned:
                                    is_valid_user = True
                                    if user_ctx and result.username:
                                        if user_ctx.lower() != result.username.lower():
                                            is_valid_user = False
                                    
                                    if is_valid_user:
                                        if taken_at_val > captured_data["max_ts"]:
                                            captured_data["max_ts"] = taken_at_val
                                            dt_debug = datetime.datetime.fromtimestamp(taken_at_val)
                                            logger.info(f"IG Post timestamp accepted: {dt_debug.strftime('%d-%m-%Y')} (unix={taken_at_val})")

                                for k, v in obj.items():
                                    if k == "edge_followed_by" and isinstance(v, dict):
                                        count = v.get("count", 0)
                                        if count > captured_data["followers"]:
                                            captured_data["followers"] = count
                                    elif k == "follower_count" and isinstance(v, int):
                                        if v > captured_data["followers"]:
                                            captured_data["followers"] = v
                                    elif k in ["edge_owner_to_timeline_media", "edge_felix_video_timeline"] and isinstance(v, dict):
                                        count = v.get("count")
                                        if isinstance(count, int):
                                            captured_data["post_count"] = count
                                        recursive_extract_data(v, user_ctx)
                                    elif k == "media_count" and isinstance(v, int):
                                        captured_data["post_count"] = v
                                    elif isinstance(v, (dict, list)):
                                        recursive_extract_data(v, user_ctx)
                                    
                                    if k == "date_joined" and isinstance(v, (int, float)):
                                        captured_data["date_joined"] = int(v)
                                    elif k == "date_joined" and isinstance(v, str):
                                        captured_data["date_joined"] = v
                                    elif k == "profile_pic_url_hd" and isinstance(v, str):
                                        captured_data["profile_pic_url_hd"] = v

                            elif isinstance(obj, list):
                                for item in obj:
                                    recursive_extract_data(item, current_user)

                        recursive_extract_data(json_body)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            logger.info(f"[{url}] Analysis starting using provided page.")

            page.on("response", capture_graphql)

            # ── Step 1: Navigate ──
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if "/accounts/login" in page.url:
                    error_comments.append("Redirected to Login")
                if "/challenge/" in page.url:
                    error_comments.append("Challenge Required — re-login needed")
                    logger.warning(f"[{url}] Instagram challenge redirect detected: {page.url}")
            except Exception:
                error_comments.append("Load Timeout/Error")

            # ── Step 2: Dismiss popups (parallel, fast) ──
            await self._dismiss_ig_popups(page)

            # ── Step 3: Brief wait for network interception to fire ──
            # Instagram's GraphQL responses arrive within ~1-2s of domcontentloaded
            await asyncio.sleep(1.5)

            # ── Step 4: Single JS injection — extract everything from DOM ──
            js_data = {}
            try:
                js_data = await page.evaluate(IG_BULK_EXTRACTION_JS) or {}
            except Exception as e:
                logger.warning(f"JS extraction failed: {e}")

            # ── Step 5: Merge data — network preferred, JS fallback ──

            # 5a. Name
            if js_data.get("display_name"):
                result.display_name = js_data["display_name"]
                result.has_name_match = True

            # 5b. Followers — network > JS (title attr) > JS (og:desc)
            if captured_data["followers"] > 0:
                result.followers = captured_data["followers"]
            elif js_data.get("followers", 0) > 0:
                result.followers = js_data["followers"]
            else:
                result.followers = 0

            # 5c. Profile Picture — network HD > JS (og:image)
            pfp_url = None
            if captured_data.get("profile_pic_url_hd"):
                pfp_url = captured_data["profile_pic_url_hd"]
            elif js_data.get("profile_pic_url"):
                pfp_url = js_data["profile_pic_url"]

            if pfp_url and is_real_profile_image(url=pfp_url):
                result.has_logo = True
                result.profile_image_url = pfp_url
            else:
                result.has_logo = False

            # 5d. Bio
            if js_data.get("bio"):
                result.bio = js_data["bio"]

            # 5e. Verified
            if js_data.get("is_verified"):
                result.is_verified = True

            # ── Step 6: Joined Date — network primary, 'About' dialog conditional fallback ──
            await self._extract_joined_date(page, result, captured_data)

            # ── Step 7: Last Post Date — network primary, JS DOM fallback ──
            await self._extract_last_post_date(page, result, captured_data, js_data)

            if not result.created_at:
                result.created_at = "Not Available (Instagram Restricted)"

            # ── Step 8: Screenshot ──
            screenshot_bytes = None
            try:
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.3)

                header = await page.query_selector("main header")
                if header:
                    main = await page.query_selector("main")
                    if main:
                        bbox = await main.bounding_box()
                        if bbox:
                            clip = {
                                "x": bbox["x"],
                                "y": bbox["y"],
                                "width": bbox["width"],
                                "height": min(bbox["height"], 1000),
                            }
                            screenshot_bytes = await page.screenshot(
                                clip=clip, type="jpeg", quality=85
                            )
                if not screenshot_bytes:
                    screenshot_bytes = await page.screenshot(type="jpeg", quality=85, timeout=15000)

                if screenshot_bytes:
                    result.screenshot_b64 = base64.b64encode(
                        screenshot_bytes
                    ).decode("utf-8")
            except Exception as e:
                logger.error(f"Instagram screenshot capture error: {e}")
                # Ultimate fallback
                try:
                    fallback_bytes = await page.screenshot(type="jpeg", quality=85, timeout=10000)
                    result.screenshot_b64 = base64.b64encode(fallback_bytes).decode("utf-8")
                except Exception:
                    pass

        except Exception as e:
            error_comments.append(f"Critical error: {type(e).__name__}")
            logger.error(f"Error during Instagram analysis: {e}")

        # Finalise
        if not result.display_name:
            result.display_name = "Scrape Incomplete"

        result.comments = " | ".join(error_comments) if error_comments else ""

        calculate_risk(result)

        # Record selector hits/misses to HealthManager
        try:
            from backend.core.health import HealthManager
            health_mgr = HealthManager()
            await health_mgr.record_selector_hit("instagram", "display_name", result.has_name_match)
            await health_mgr.record_selector_hit("instagram", "followers", result.followers > 0)
            await health_mgr.record_selector_hit("instagram", "profile_image", result.has_logo)
            has_created = bool(result.created_at and "Restricted" not in result.created_at)
            await health_mgr.record_selector_hit("instagram", "created_at", has_created)
            has_last_post = bool(result.last_post_date and "Unknown" not in result.last_post_date and "No Posts" not in result.last_post_date)
            await health_mgr.record_selector_hit("instagram", "last_post_date", has_last_post)
        except Exception as e:
            logger.debug(f"Failed to record selector hits: {e}")

        await self.health.record_request(
            "instagram", success=not bool(error_comments)
        )

        # Download profile image
        if (
            result.has_logo
            and result.profile_image_url
            and "placeholder" not in result.profile_image_url
        ):
            b64 = await download_profile_image(
                result.profile_image_url,
                referer="https://www.instagram.com/",
                extra_headers={
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            if b64:
                result.profile_image_b64 = b64

        return result

    # --- Private helper methods ---

    async def _extract_joined_date(self, page, result: ProfileResult, captured_data: dict):
        """
        Extract joined date via network interception data.
        Opens 'About this account' dialog ONLY if network didn't provide date_joined.
        """
        # --- Tier 1: Check if network interception already captured date_joined ---
        if captured_data.get("date_joined"):
            dj = captured_data["date_joined"]
            try:
                if isinstance(dj, int):
                    dt = datetime.datetime.fromtimestamp(dj, tz=datetime.timezone.utc)
                    result.created_at = dt.strftime("%m-%Y")
                    logger.info(f"IG joined date from network: {result.created_at}")
                    return
                elif isinstance(dj, str):
                    for fmt in ("%B %Y", "%b %Y", "%Y-%m-%d", "%d-%m-%Y"):
                        try:
                            dt = datetime.datetime.strptime(dj, fmt)
                            result.created_at = dt.strftime("%m-%Y")
                            logger.info(f"IG joined date from network (str): {result.created_at}")
                            return
                        except ValueError:
                            continue
            except Exception:
                pass

        # --- Tier 2: Open 'About this account' dialog (conditional fallback) ---
        logger.info("Network didn't provide date_joined, trying 'About this account' dialog...")
        try:
            # Try multiple ways to open the options/more menu
            clicked = False
            for selector in [
                'svg[aria-label="Options"]',
                'svg[aria-label="Settings"]',
                'svg[aria-label="More options"]',
                'div[role="button"]:has(svg[aria-label="Options"])',
                'button:has(svg[aria-label="Options"])',
                'header button:last-of-type',
                'header svg[aria-label]',
            ]:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    try:
                        el = loc.first
                        # For SVG elements, click their parent button
                        if selector.startswith('svg') or selector.startswith('header svg'):
                            el = el.locator("xpath=ancestor::*[self::button or @role='button'][1]")
                            if await el.count() == 0:
                                el = loc.first.locator("xpath=..")
                        await el.click()
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                # Fallback: try all header buttons
                header_buttons = page.locator("header button")
                if await header_buttons.count() > 0:
                    await header_buttons.nth(-1).click()
                    clicked = True

            if not clicked:
                return

            await asyncio.sleep(1.0)

            # Try to find and click 'About this account'
            about_clicked = False
            for text in ["About this account", "About this profile", "About This Account"]:
                about_btn = page.locator(f"text={text}")
                if await about_btn.count() > 0:
                    await about_btn.first.click()
                    about_clicked = True
                    break

            if not about_clicked:
                # Try matching by role
                menu_items = page.locator('div[role="dialog"] button, div[role="menu"] div[role="menuitem"]')
                count = await menu_items.count()
                for i in range(count):
                    item_text = await menu_items.nth(i).inner_text()
                    if "about" in item_text.lower():
                        await menu_items.nth(i).click()
                        about_clicked = True
                        break

            if not about_clicked:
                await page.keyboard.press("Escape")
                return

            await asyncio.sleep(1.5)

            # Check if network interceptor caught date_joined from the about API call
            if captured_data.get("date_joined"):
                dj = captured_data["date_joined"]
                try:
                    if isinstance(dj, int):
                        dt = datetime.datetime.fromtimestamp(dj, tz=datetime.timezone.utc)
                        result.created_at = dt.strftime("%m-%Y")
                        return
                except Exception:
                    pass

            # Try multiple DOM selectors to find the joined date
            date_found = False

            # Method 1: aria-label
            aria_node = page.locator('[aria-label^="Date joined"]')
            if await aria_node.count() > 0:
                aria = await aria_node.first.get_attribute("aria-label")
                m = re.search(r"Date joined\s+([A-Za-z]+\s+\d{4})", aria)
                if m:
                    dt = datetime.datetime.strptime(m.group(1), "%B %Y")
                    result.created_at = dt.strftime("%m-%Y")
                    date_found = True

            # Method 2: span text matching
            if not date_found:
                label = page.locator('span:has-text("Date joined")')
                if await label.count() > 0:
                    # Try sibling span first
                    value = label.locator("xpath=following-sibling::span")
                    if await value.count() > 0:
                        date_text = (await value.first.inner_text()).strip()
                    else:
                        parent = label.first.locator("xpath=..")
                        date_text = (await parent.inner_text()).strip()
                        date_text = date_text.replace("Date joined", "").strip()
                    for fmt in ("%B %Y", "%b %Y", "%B %d, %Y"):
                        try:
                            dt = datetime.datetime.strptime(date_text, fmt)
                            result.created_at = dt.strftime("%m-%Y")
                            date_found = True
                            break
                        except ValueError:
                            continue

            # Method 3: Full dialog text scan for month-year patterns
            if not date_found:
                try:
                    dialog = page.locator('div[role="dialog"]')
                    if await dialog.count() > 0:
                        dialog_text = await dialog.first.inner_text()
                        m = re.search(
                            r'(?:joined|since|date).*?([A-Z][a-z]+\s+\d{4})',
                            dialog_text, re.IGNORECASE
                        )
                        if m:
                            for fmt in ("%B %Y", "%b %Y"):
                                try:
                                    dt = datetime.datetime.strptime(m.group(1), fmt)
                                    result.created_at = dt.strftime("%m-%Y")
                                    date_found = True
                                    break
                                except ValueError:
                                    continue

                        if not date_found:
                            months = re.findall(
                                r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
                                dialog_text
                            )
                            if months:
                                dt = datetime.datetime.strptime(months[0], "%B %Y")
                                result.created_at = dt.strftime("%m-%Y")
                                date_found = True
                except Exception:
                    pass

            # Also extract location from the about dialog
            await self._extract_location(page, result)

        except Exception as e:
            logger.warning(f"Joined date extraction error: {e}")
        finally:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _extract_location(self, page, result: ProfileResult):
        """Extract location from the 'About this account' dialog."""
        try:
            aria_node = page.locator('[aria-label^="Account based in"]')
            if await aria_node.count() > 0:
                aria = await aria_node.first.get_attribute("aria-label")
                loc = aria.replace("Account based in", "").strip()
                if loc:
                    result.location = loc
                    return
            label = page.locator('span:has-text("Account based in")')
            if await label.count() > 0:
                value = label.locator("xpath=following-sibling::span")
                loc_text = (await value.first.inner_text()).strip()
                if loc_text:
                    result.location = loc_text
        except Exception:
            pass

    async def _extract_last_post_date(
        self, page, result: ProfileResult, captured_data: dict, js_data: dict
    ):
        """
        Extracts last post date — ULTRA-FAST.
        Priority: network interception → JS DOM <time> tags → no-post detection.
        No scroll loops. No post page navigation.
        """
        result.is_active = False

        # --- Step 0: Check known post count ---
        post_count = captured_data.get("post_count", -1)
        if post_count == -1 and js_data.get("post_count", -1) >= 0:
            post_count = js_data["post_count"]

        if post_count == 0:
            logger.info("Profile has 0 posts based on data.")
            result.last_post_date = "No Posts"
            return

        # --- Step 1: Network interception data (Preferred — most accurate) ---
        if captured_data.get("max_ts", 0) > 0:
            ts = captured_data["max_ts"]
            dt_local = datetime.datetime.fromtimestamp(ts)
            dt_utc = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            days_ago = (datetime.datetime.now(datetime.timezone.utc) - dt_utc).days
            result.last_post_date = dt_local.strftime("%d-%m-%Y")
            result.last_active = result.last_post_date
            result.is_active = days_ago <= 180
            logger.info(f"IG Last post date (Network): {dt_local.strftime('%d-%m-%Y')} ({days_ago} days ago)")
            return

        # --- Step 2: JS DOM <time datetime> tags (Fast fallback — no navigation) ---
        post_dates = js_data.get("post_dates", [])
        if post_dates:
            candidates = []
            for iso_str in post_dates:
                try:
                    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    candidates.append(dt)
                except Exception:
                    continue

            if candidates:
                candidates.sort(reverse=True)
                best_dt = candidates[0]
                result.last_post_date = best_dt.strftime("%d-%m-%Y")
                result.last_active = result.last_post_date
                days_ago = (datetime.datetime.now(datetime.timezone.utc) - best_dt).days
                result.is_active = days_ago <= 180
                logger.info(f"IG Last post date (JS DOM): {best_dt.strftime('%d-%m-%Y')} ({days_ago} days ago)")
                return

        # --- Step 3: Private account or no posts ---
        if js_data.get("is_private"):
            result.last_post_date = "Private Account"
        else:
            result.last_post_date = "Date Unknown"

    async def _dismiss_ig_popups(self, page):
        """Dismiss Instagram popups — parallel, fire-and-forget."""
        tasks = []
        for selector in IG_POPUP_SELECTORS:
            tasks.append(self._try_dismiss_popup(page, selector))
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _try_dismiss_popup(page, selector):
        """Try to click a single popup selector, swallow errors."""
        try:
            el = page.locator(selector).first
            if await el.count() > 0 and await el.is_visible(timeout=100):
                await el.click()
                logger.info(f"IG popup dismissed via selector: {selector}")
        except Exception:
            pass

    @staticmethod
    def _extract_username(url: str) -> str:
        """Extract Instagram username from URL."""
        match = re.search(r"instagram\.com/([a-zA-Z0-9._]+)", url)
        if match:
            username = match.group(1)
            if username.lower() not in (
                "p",
                "reel",
                "stories",
                "explore",
                "accounts",
                "direct",
            ):
                return username
        return ""
