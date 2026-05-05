import asyncio
import base64
import datetime
import random
import re

from backend.core.db import ProfileResult
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from backend.platforms.utils import calculate_risk, download_profile_image, parse_followers
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


class InstagramAnalyzer(AbstractAnalyzer):
    """
    Analyzes Instagram profiles using browser-based scraping with
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
        """Core analysis logic — writes directly to ProfileResult."""

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

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if "/accounts/login" in page.url:
                    error_comments.append("Redirected to Login")
                if "/challenge/" in page.url:
                    error_comments.append("Challenge Required — re-login needed")
                    logger.warning(f"[{url}] Instagram challenge redirect detected: {page.url}")
            except Exception:
                error_comments.append("Load Timeout/Error")

            await asyncio.sleep(2)

            # Dismiss any popups ("We suspect automated behavior", cookies, notifications)
            for _ in range(2):
                await self._dismiss_ig_popups(page)
                await asyncio.sleep(0.5)

            # 1. Name — from og:title or h2
            try:
                og_title_el = await page.query_selector('meta[property="og:title"]')
                if og_title_el:
                    og_title = await og_title_el.get_attribute("content")
                    match = re.search(r"^(.*?)\s\(@(.*?)\)", og_title)
                    if match:
                        result.display_name = match.group(1).strip()
                        result.has_name_match = True
                    else:
                        result.display_name = (
                            og_title.split("•")[0].strip()
                            if "•" in og_title
                            else og_title
                        )
                        result.has_name_match = True
                else:
                    h2 = await page.query_selector("h2")
                    if h2:
                        result.display_name = await h2.inner_text()
                        result.has_name_match = True
            except Exception:
                pass

            # 2. Followers — network primary, DOM fallback chain
            try:
                if captured_data["followers"] > 0:
                    result.followers = captured_data["followers"]
                else:
                    title_node = page.locator(
                        'a[href*="/followers/"] span[title], span[title*=","]'
                    )
                    if await title_node.count() > 0:
                        txt = await title_node.first.get_attribute("title")
                        result.followers = parse_followers(txt)
                    else:
                        og_desc_el = await page.query_selector(
                            'meta[property="og:description"]'
                        )
                        if og_desc_el:
                            og_desc = await og_desc_el.get_attribute("content")
                            follower_match = re.search(
                                r"([\d.,kmKM]+)\s+Followers", og_desc
                            )
                            if follower_match:
                                result.followers = parse_followers(
                                    follower_match.group(1)
                                )
                        else:
                            followers_link = await page.query_selector(
                                'a[href*="/followers/"]'
                            )
                            if followers_link:
                                txt = await followers_link.inner_text()
                                result.followers = parse_followers(txt)
            except Exception:
                result.followers = 0

            # 3. Logo / Profile Picture — network HD primary, DOM fallback
            try:
                if captured_data.get("profile_pic_url_hd"):
                    result.has_logo = True
                    result.profile_image_url = captured_data["profile_pic_url_hd"]
                else:
                    og_image_el = await page.query_selector('meta[property="og:image"]')
                    if og_image_el:
                        result.has_logo = True
                        result.profile_image_url = await og_image_el.get_attribute(
                            "content"
                        )
                    else:
                        img = await page.query_selector("header img")
                        if img:
                            result.has_logo = True
                            result.profile_image_url = await img.get_attribute("src")
            except Exception:
                pass

            # 4. Bio
            try:
                bio_section = await page.query_selector(
                    'div[class*="biography"] span, header + section span'
                )
                if bio_section:
                    result.bio = (await bio_section.inner_text()).strip()
            except Exception:
                pass

            # 5. Joined Date — network interception → 'About this account' dialog
            await asyncio.sleep(3)
            await self._dismiss_ig_popups(page)
            await self._extract_joined_date(page, result, captured_data)

            # 6. Scroll to trigger more network data for last post
            try:
                await page.wait_for_timeout(1500)
                await page.mouse.move(100, 100)
                for _ in range(3):
                    await page.mouse.wheel(0, 3000)
                    await asyncio.sleep(1.5)
            except Exception:
                pass

            await self._dismiss_ig_popups(page)

            # 7. Last Post / Active — DOM post scan → network fallback
            await self._extract_last_post_date(page, result, captured_data)

            if not result.created_at:
                result.created_at = "Not Available (Instagram Restricted)"

            # 8. Screenshot
            screenshot_bytes = None
            try:
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1)

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
        """Extract joined date via network interception data or the 'About this account' dialog."""
        # --- Tier 1: Check if network interception already captured date_joined ---
        if captured_data.get("date_joined"):
            dj = captured_data["date_joined"]
            try:
                if isinstance(dj, int):
                    dt = datetime.datetime.fromtimestamp(dj, tz=datetime.timezone.utc)
                    result.created_at = dt.strftime("%m-%Y")
                    return
                elif isinstance(dj, str):
                    for fmt in ("%B %Y", "%b %Y", "%Y-%m-%d", "%d-%m-%Y"):
                        try:
                            dt = datetime.datetime.strptime(dj, fmt)
                            result.created_at = dt.strftime("%m-%Y")
                            return
                        except ValueError:
                            continue
            except Exception:
                pass

        # --- Tier 2: Open 'About this account' dialog ---
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

            await asyncio.sleep(1.5)

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

            await asyncio.sleep(2.5)

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
                await asyncio.sleep(0.3)
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

    async def _extract_last_post_date(self, page, result: ProfileResult, captured_data: dict):
        """
        Extracts last post date primarily from network interception (accurate and fast),
        falling back to DOM-based extraction if network data is missing.
        """
        result.is_active = False

        # --- Step 0: Check known post count from network ---
        if captured_data.get("post_count") == 0:
            logger.info("Profile has 0 posts based on network data.")
            result.last_post_date = "No Posts"
            return

        # --- Step 1: Network interception data (Preferred) ---
        if captured_data.get("max_ts", 0) > 0:
            ts = captured_data["max_ts"]
            dt_local = datetime.datetime.fromtimestamp(ts)  # Local timezone for display
            dt_utc = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)  # UTC for staleness calc
            days_ago = (datetime.datetime.now(datetime.timezone.utc) - dt_utc).days
            result.last_post_date = dt_local.strftime("%d-%m-%Y")
            result.last_active = result.last_post_date
            result.is_active = days_ago <= 180
            logger.info(f"IG Last post date (Network): {dt_local.strftime('%d-%m-%Y')} ({days_ago} days ago)")
            return

        # --- Step 2: DOM-based fallback ---
        try:
            # Look at the article grid specifically to avoid random links
            all_posts = page.locator("article a[href*='/p/'], article a[href*='/reel/']")
            count = await all_posts.count()
            
            if count == 0:
                all_posts = page.locator("main a[href*='/p/'], main a[href*='/reel/']")
                count = await all_posts.count()

            limit = min(count, 4)  # Top 4: covers 3 pinned + 1 real

            if limit == 0:
                logger.info("No posts found in profile grid")
                private_node = page.locator('h2:has-text("This Account is Private"), h2:has-text("This account is private")')
                if await private_node.count() > 0:
                    result.last_post_date = "Private Account"
                else:
                    result.last_post_date = "No Posts"
                return

            candidates = []
            post_page = await page.context.new_page()

            try:
                for i in range(limit):
                    try:
                        post_loc = all_posts.nth(i)
                        href = await post_loc.get_attribute("href")
                        if not href:
                            continue

                        post_url = "https://www.instagram.com" + href if href.startswith("/") else href

                        try:
                            await post_page.goto(post_url, wait_until="domcontentloaded", timeout=25000)

                            try:
                                await post_page.wait_for_selector("time[datetime]", timeout=5000)
                                time_el = post_page.locator("time[datetime]").first
                                iso = await time_el.get_attribute("datetime")
                                if iso:
                                    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                                    ts = int(dt.timestamp())
                                    candidates.append((ts, dt))
                                    logger.info(f"IG Post[{i}] date from DOM: {dt.strftime('%d-%m-%Y')} (href={href})")
                            except Exception:
                                logger.warning(f"IG Post[{i}] no <time> tag found (href={href})")

                            # Anti-ban delay between post page loads
                            await asyncio.sleep(random.uniform(1.0, 2.0))
                        except Exception as e:
                            logger.warning(f"IG Post[{i}] failed to load: {e}")
                    except Exception:
                        pass
            finally:
                await post_page.close()

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_ts, best_dt = candidates[0]

                result.last_post_date = best_dt.strftime("%d-%m-%Y")
                result.last_active = result.last_post_date
                days_ago = (datetime.datetime.now(datetime.timezone.utc) - best_dt).days
                result.is_active = days_ago <= 180
                logger.info(f"IG Last post date (DOM): {best_dt.strftime('%d-%m-%Y')} ({days_ago} days ago)")
                return
            else:
                result.last_post_date = "Date Unknown"

        except Exception as e:
            logger.warning(f"DOM-based last post extraction failed: {e}")
            if not result.last_post_date:
                result.last_post_date = "Error Extracting Date"

    async def _dismiss_ig_popups(self, page):
        """Dismiss Instagram popups (challenges, cookie consent, notifications, login prompts)."""
        for selector in IG_POPUP_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=600):
                    await el.click()
                    logger.info(f"IG popup dismissed via selector: {selector}")
                    await asyncio.sleep(0.5)
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
