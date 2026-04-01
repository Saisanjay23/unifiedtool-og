import asyncio
import datetime
import random
import os
import json
import sys
import re
import base64
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.stealth.human import HumanBehavior
from backend.platforms.base import AbstractAnalyzer

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
    Analyzes Instagram profiles using the EXACT 20-year principal engineer
    legacy logic. Wraps `async_scrape_profile` to output `ProfileResult`.
    """

    async def analyze(
        self,
        url: str,
        client: str,
        headless: bool = True,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> ProfileResult:

        from backend.stealth.browser import create_stealth_browser

        sem = semaphore or asyncio.Semaphore(1)

        async with sem:
            logger.info(
                f"Starting Instagram analysis for URL: {url} (Client: {client})"
            )

            # --- START EXACT LEGACY CODE (Wrapped) ---

            def get_default_result_dict(url, profile_name="Scrape Failed", comments=""):
                return {
                    "Original Name": "",
                    "Original feed": "",
                    "IMPERSONATED": url,
                    "Profile name": profile_name,
                    "Created Date": "",
                    "Logo (Yes / No)": "No",
                    "Followers": 0,
                    "Active (Yes / No)": "No",
                    "Name (Yes / No)": "No",
                    "Location": "",
                    "Last Post (DD-MM-YYYY) (Optional)": "",
                    "Risk Score": 0,
                    "priority": "Low",
                    "Date": datetime.datetime.now().strftime("%d-%m-%Y"),
                    "Comments": comments,
                    "Screenshot": None,
                    "Validate": False,
                    "profile_picture": "",
                }

            def parse_followers(s):
                if not s:
                    return 0
                s = str(s).lower().replace(",", "").strip()
                try:
                    if "k" in s:
                        return int(float(s.replace("k", "")) * 1000)
                    elif "m" in s:
                        return int(float(s.replace("m", "")) * 1000000)
                    elif "b" in s:
                        return int(float(s.replace("b", "")) * 1000000000)
                    numeric_part = re.sub(r"[^0-9.]", "", s)
                    return int(float(numeric_part)) if numeric_part else 0
                except:
                    return 0

            def calculate_risk(row):
                try:
                    has_name = str(row.get("Name (Yes / No)", "No")).lower() == "yes"
                    has_logo = str(row.get("Logo (Yes / No)", "No")).lower() == "yes"
                    location_txt = str(row.get("Location", "")).strip()
                    has_location = bool(location_txt) and location_txt.lower() != "nan"

                    followers_str = str(row.get("Followers", "0"))
                    try:
                        followers = int(float(str(followers_str).replace(",", "")))
                    except:
                        followers = 0

                    now = datetime.datetime.now(datetime.timezone.utc)

                    def get_months_ago(date_str):
                        if not date_str or str(date_str).lower() in [
                            "nan",
                            "none",
                            "",
                            "not available (instagram restricted)",
                        ]:
                            return 999
                        try:
                            if len(date_str.split("-")) == 2:
                                dt = datetime.datetime.strptime(
                                    date_str, "%m-%Y"
                                ).replace(tzinfo=datetime.timezone.utc)
                            else:
                                dt = datetime.datetime.strptime(
                                    date_str, "%d-%m-%Y"
                                ).replace(tzinfo=datetime.timezone.utc)
                            return (now.year - dt.year) * 12 + (now.month - dt.month)
                        except:
                            return 999

                    created_months = get_months_ago(row.get("Created Date"))
                    posted_months = get_months_ago(
                        row.get("Last Post (DD-MM-YYYY) (Optional)")
                    )

                    is_new_account = created_months <= 6

                    priority = "High" if has_logo else "Low"
                    active_btn_val = str(row.get("Active (Yes / No)", "No")).lower()
                    is_truly_active = active_btn_val.startswith("y")

                    if (
                        has_name
                        and has_logo
                        and is_new_account
                        and is_truly_active
                        and has_location
                        and followers > 100
                    ):
                        return 9, priority
                    is_very_new = created_months <= 1
                    if (
                        has_name
                        and has_logo
                        and is_truly_active
                        and has_location
                        and is_very_new
                    ):
                        return 8, priority
                    if has_name and has_logo and is_truly_active and has_location:
                        return 7, priority
                    if has_name and has_logo and (is_truly_active or is_new_account):
                        return 7, priority
                    if has_name and has_logo:
                        return 6, priority
                    if has_name and is_new_account:
                        return 4, priority
                    if has_name:
                        return 3, priority
                    return 0, priority
                except:
                    return 0, "Low"

            async def extract_location(page, result):
                try:
                    aria_node = page.locator('[aria-label^="Account based in"]')
                    if await aria_node.count() > 0:
                        aria = await aria_node.first.get_attribute("aria-label")
                        loc = aria.replace("Account based in", "").strip()
                        if loc:
                            result["Location"] = loc
                            return
                    label = page.locator('span:has-text("Account based in")')
                    if await label.count() > 0:
                        value = label.locator("xpath=following-sibling::span")
                        loc_text = (await value.first.inner_text()).strip()
                        if loc_text:
                            result["Location"] = loc_text
                except:
                    pass

            async def extract_joined_date(page, result, captured_data):
                # --- Tier 1: Check if network interception already captured date_joined ---
                if captured_data.get("date_joined"):
                    dj = captured_data["date_joined"]
                    try:
                        if isinstance(dj, int):
                            dt = datetime.datetime.fromtimestamp(dj, tz=datetime.timezone.utc)
                            result["Created Date"] = dt.strftime("%m-%Y")
                            result["Comments"] = result.get("Comments", "") + " | JoinedDate:Network"
                            return
                        elif isinstance(dj, str):
                            for fmt in ("%B %Y", "%b %Y", "%Y-%m-%d", "%d-%m-%Y"):
                                try:
                                    dt = datetime.datetime.strptime(dj, fmt)
                                    result["Created Date"] = dt.strftime("%m-%Y")
                                    result["Comments"] = result.get("Comments", "") + " | JoinedDate:Network"
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
                        result["Comments"] = result.get("Comments", "") + " | No options button found"
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
                        # Dismiss the menu and return
                        await page.keyboard.press("Escape")
                        result["Comments"] = result.get("Comments", "") + " | About dialog not found"
                        return

                    await asyncio.sleep(2.5)

                    # Check if network interceptor caught date_joined from the about API call
                    if captured_data.get("date_joined"):
                        dj = captured_data["date_joined"]
                        try:
                            if isinstance(dj, int):
                                dt = datetime.datetime.fromtimestamp(dj, tz=datetime.timezone.utc)
                                result["Created Date"] = dt.strftime("%m-%Y")
                                result["Comments"] = result.get("Comments", "") + " | JoinedDate:NetworkAfterAbout"
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
                            result["Created Date"] = dt.strftime("%m-%Y")
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
                                # Try parent's next element
                                parent = label.first.locator("xpath=..")
                                date_text = (await parent.inner_text()).strip()
                                date_text = date_text.replace("Date joined", "").strip()
                            for fmt in ("%B %Y", "%b %Y", "%B %d, %Y"):
                                try:
                                    dt = datetime.datetime.strptime(date_text, fmt)
                                    result["Created Date"] = dt.strftime("%m-%Y")
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
                                # Look for patterns like "January 2020" or "Jan 2020"
                                m = re.search(
                                    r'(?:joined|since|date).*?([A-Z][a-z]+\s+\d{4})',
                                    dialog_text, re.IGNORECASE
                                )
                                if m:
                                    for fmt in ("%B %Y", "%b %Y"):
                                        try:
                                            dt = datetime.datetime.strptime(m.group(1), fmt)
                                            result["Created Date"] = dt.strftime("%m-%Y")
                                            date_found = True
                                            break
                                        except ValueError:
                                            continue

                                # Also try to find any standalone month-year in dialog
                                if not date_found:
                                    months = re.findall(
                                        r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
                                        dialog_text
                                    )
                                    if months:
                                        dt = datetime.datetime.strptime(months[0], "%B %Y")
                                        result["Created Date"] = dt.strftime("%m-%Y")
                                        date_found = True
                        except Exception:
                            pass

                    if date_found:
                        result["Comments"] = result.get("Comments", "") + " | JoinedDate:AboutDialog"

                    await extract_location(page, result)
                except Exception as e:
                    current_comments = result.get("Comments", "")
                    result["Comments"] = (
                        f"{current_comments} | Joined Date Error: {str(e)}"
                    )
                finally:
                    try:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Escape")
                    except:
                        pass

            async def extract_last_post_date(page, result, captured_data):
                """
                OLD TOOL PROVEN APPROACH — DOM-based last post extraction.
                
                PROBLEM: Pinned posts (up to 3) appear at top of grid, skewing "Last Post" data.
                Network interception can't reliably detect pinned status.
                
                SOLUTION: Open top 4 posts from grid (max 3 pinned + 1 real = guaranteed latest).
                Read <time datetime> from each post detail page. Take MAX = latest real post.
                """
                result["Active (Yes / No)"] = "No"

                # --- Step 1: DOM-based scan of top 4 posts (bypasses pinned posts) ---
                try:
                    all_posts = page.locator("a[href*='/p/'], a[href*='/reel/']")
                    count = await all_posts.count()
                    limit = min(count, 4)  # Top 4: covers 3 pinned + 1 real

                    if limit == 0:
                        logger.info("No posts found in profile grid")
                        result["Comments"] += " | No posts in grid"
                        # Fall through to network interception
                    else:
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

                                        # Wait for the <time> element
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
                            # Sort by timestamp descending — newest first
                            candidates.sort(key=lambda x: x[0], reverse=True)
                            best_ts, best_dt = candidates[0]

                            result["Last Post (DD-MM-YYYY) (Optional)"] = best_dt.strftime("%d-%m-%Y")
                            days_ago = (datetime.datetime.now(datetime.timezone.utc) - best_dt).days
                            result["Active (Yes / No)"] = "Yes" if days_ago <= 180 else "No"
                            result["Comments"] += f" | LastPost:DOM(top{limit}_max) ({days_ago}d)"
                            logger.info(f"IG Last post date (DOM): {best_dt.strftime('%d-%m-%Y')} ({days_ago} days ago)")
                            return

                except Exception as e:
                    logger.warning(f"DOM-based last post extraction failed: {e}")

                # --- Step 2: Fallback to network interception data ---
                if captured_data["max_ts"] > 0:
                    ts = captured_data["max_ts"]
                    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                    days_ago = (datetime.datetime.now(datetime.timezone.utc) - dt).days
                    result["Last Post (DD-MM-YYYY) (Optional)"] = dt.strftime("%d-%m-%Y")
                    result["Active (Yes / No)"] = "Yes" if days_ago <= 180 else "No"
                    result["Comments"] += f" | LastPost:NetworkFallback ({days_ago}d)"
                    logger.info(f"IG Last post date (Network fallback): {dt.strftime('%d-%m-%Y')} ({days_ago} days ago)")
                    return

                # --- Step 3: Scroll to trigger more network data ---
                try:
                    for _ in range(3):
                        await page.mouse.wheel(0, 5000)
                        await asyncio.sleep(1.5)
                        if captured_data["max_ts"] > 0:
                            ts = captured_data["max_ts"]
                            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                            days_ago = (datetime.datetime.now(datetime.timezone.utc) - dt).days
                            result["Last Post (DD-MM-YYYY) (Optional)"] = dt.strftime("%d-%m-%Y")
                            result["Active (Yes / No)"] = "Yes" if days_ago <= 180 else "No"
                            result["Comments"] += f" | LastPost:NetworkScroll ({days_ago}d)"
                            return
                except Exception:
                    pass

                result["Comments"] += " | No last post date found"

            old_result = get_default_result_dict(url, profile_name="")
            error_comments = []

            try:
                pw, browser, context, page = await create_stealth_browser(
                    platform="instagram", headless=headless
                )
                logger.info(f"[{url}] Browser created successfully.")

                captured_data = {"max_ts": 0, "followers": 0, "date_joined": None, "profile_pic_url_hd": None}

                async def capture_graphql(response):
                    try:
                        if response.status == 200 and (
                            "graphql" in response.url
                            or "/feed/" in response.url
                            or "info" in response.url
                        ):
                            try:
                                json_body = await response.json()

                                def recursive_extract_data(obj):
                                    if isinstance(obj, dict):
                                        # Check if this dict IS a post/media node with taken_at
                                        has_taken_at = False
                                        taken_at_val = 0
                                        is_pinned = False

                                        for k, v in obj.items():
                                            # Detect taken_at timestamp
                                            if k in ["taken_at", "taken_at_timestamp"] and isinstance(v, int):
                                                has_taken_at = True
                                                taken_at_val = v
                                            # Detect pinned post indicators
                                            if k in ["timeline_pinned_user_ids", "pinned_for_users"]:
                                                if isinstance(v, list) and len(v) > 0:
                                                    is_pinned = True
                                            if k == "is_pinned" and v:
                                                is_pinned = True

                                        # Only use taken_at if this post is NOT pinned
                                        if has_taken_at and not is_pinned:
                                            if taken_at_val > captured_data["max_ts"]:
                                                captured_data["max_ts"] = taken_at_val
                                                dt_debug = datetime.datetime.fromtimestamp(taken_at_val, tz=datetime.timezone.utc)
                                                logger.info(f"IG Post timestamp accepted: {dt_debug.strftime('%d-%m-%Y')} (unix={taken_at_val})")
                                        elif has_taken_at and is_pinned:
                                            dt_debug = datetime.datetime.fromtimestamp(taken_at_val, tz=datetime.timezone.utc)
                                            logger.info(f"IG PINNED post SKIPPED: {dt_debug.strftime('%d-%m-%Y')} (unix={taken_at_val})")

                                        # Extract other fields regardless of pinned status
                                        for k, v in obj.items():
                                            if k == "edge_followed_by" and isinstance(v, dict):
                                                count = v.get("count", 0)
                                                if count > captured_data["followers"]:
                                                    captured_data["followers"] = count
                                            elif k == "follower_count" and isinstance(v, int):
                                                if v > captured_data["followers"]:
                                                    captured_data["followers"] = v
                                            elif isinstance(v, (dict, list)):
                                                recursive_extract_data(v)
                                            # Capture date_joined from API responses
                                            if k == "date_joined" and isinstance(v, (int, float)):
                                                captured_data["date_joined"] = int(v)
                                            elif k == "date_joined" and isinstance(v, str):
                                                captured_data["date_joined"] = v
                                            # Capture HD Profile Picture
                                            elif k == "profile_pic_url_hd" and isinstance(v, str):
                                                captured_data["profile_pic_url_hd"] = v
                                    elif isinstance(obj, list):
                                        for item in obj:
                                            recursive_extract_data(item)

                                recursive_extract_data(json_body)
                            except:
                                pass
                    except:
                        pass

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

                try:
                    og_title_el = await page.query_selector('meta[property="og:title"]')
                    if og_title_el:
                        og_title = await og_title_el.get_attribute("content")
                        match = re.search(r"^(.*?)\s\(@(.*?)\)", og_title)
                        if match:
                            old_result["Profile name"] = match.group(1).strip()
                            old_result["Name (Yes / No)"] = "Yes"
                        else:
                            old_result["Profile name"] = (
                                og_title.split("•")[0].strip()
                                if "•" in og_title
                                else og_title
                            )
                            old_result["Name (Yes / No)"] = "Yes"
                    else:
                        h2 = await page.query_selector("h2")
                        if h2:
                            old_result["Profile name"] = await h2.inner_text()
                            old_result["Name (Yes / No)"] = "Yes"
                except:
                    pass

                try:
                    if captured_data["followers"] > 0:
                        old_result["Followers"] = captured_data["followers"]
                        old_result["Comments"] += (
                            f" | Followers:Network({old_result['Followers']})"
                        )
                    else:
                        title_node = page.locator(
                            'a[href*="/followers/"] span[title], span[title*=","]'
                        )
                        if await title_node.count() > 0:
                            txt = await title_node.first.get_attribute("title")
                            old_result["Followers"] = parse_followers(txt)
                            old_result["Comments"] += f" | Followers:DOMTitle"
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
                                    old_result["Followers"] = parse_followers(
                                        follower_match.group(1)
                                    )
                                    old_result["Comments"] += (
                                        f" | Followers:Meta(Rounded)"
                                    )
                            else:
                                followers_link = await page.query_selector(
                                    'a[href*="/followers/"]'
                                )
                                if followers_link:
                                    txt = await followers_link.inner_text()
                                    old_result["Followers"] = parse_followers(txt)
                except Exception as e:
                    old_result["Followers"] = 0
                    old_result["Comments"] += f" | FollowerError:{str(e)}"

                try:
                    if captured_data.get("profile_pic_url_hd"):
                        old_result["Logo (Yes / No)"] = "Yes"
                        old_result["profile_picture"] = captured_data["profile_pic_url_hd"]
                        old_result["Comments"] += " | Logo:HD-Network"
                    else:
                        og_image_el = await page.query_selector('meta[property="og:image"]')
                        if og_image_el:
                            old_result["Logo (Yes / No)"] = "Yes"
                            old_result["profile_picture"] = await og_image_el.get_attribute(
                                "content"
                            )
                        else:
                            img = await page.query_selector("header img")
                            if img:
                                old_result["Logo (Yes / No)"] = "Yes"
                                old_result["profile_picture"] = await img.get_attribute(
                                    "src"
                                )
                except:
                    pass

                try:
                    bio_section = await page.query_selector(
                        'div[class*="biography"] span, header + section span'
                    )
                    if bio_section:
                        old_result["bio"] = (await bio_section.inner_text()).strip()
                except Exception:
                    pass

                await asyncio.sleep(3)
                await self._dismiss_ig_popups(page)
                await extract_joined_date(page, old_result, captured_data)

                try:
                    await page.wait_for_timeout(1500)
                    await page.mouse.move(100, 100)
                    for _ in range(3):
                        await page.mouse.wheel(0, 3000)
                        await asyncio.sleep(1.5)
                except:
                    pass

                await self._dismiss_ig_popups(page)
                await extract_last_post_date(page, old_result, captured_data)

                if not old_result.get("Created Date"):
                    old_result["Created Date"] = "Not Available (Instagram Restricted)"

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
                        old_result["Screenshot"] = base64.b64encode(
                            screenshot_bytes
                        ).decode("utf-8")
                except Exception as e:
                    logger.error(f"Instagram screenshot capture error: {e}")
                    # Ultimate fallback
                    try:
                        fallback_bytes = await page.screenshot(type="jpeg", quality=85, timeout=10000)
                        old_result["Screenshot"] = base64.b64encode(fallback_bytes).decode("utf-8")
                    except:
                        old_result["Screenshot"] = None

            except Exception as e:
                error_comments.append(f"Critical error: {type(e).__name__}")
                logger.error(f"Error during Instagram analysis: {e}")

            finally:
                if "browser" in locals() and browser:
                    await browser.close()
                if "pw" in locals() and pw:
                    await pw.stop()

            if not old_result["Profile name"]:
                old_result["Profile name"] = "Scrape Incomplete"

            old_result["Comments"] = (
                old_result.get("Comments", "") + " | " + " | ".join(error_comments)
                if error_comments
                else old_result.get("Comments", "")
            )

            risk, priority = calculate_risk(old_result)
            old_result["Risk Score"], old_result["priority"] = risk, priority

            await self.health.record_request(
                "instagram", success=not bool(error_comments)
            )

            # --- MAP TO NEW BACKEND FORMAT ---
            def extract_username(url: str) -> str:
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

            result = ProfileResult(
                platform="instagram",
                client_name=client,
                keyword="",
                url=url,
                username=extract_username(url),
            )

            result.display_name = old_result.get("Profile name", "")
            result.followers = int(old_result.get("Followers", 0))
            result.location = old_result.get("Location", "")
            result.created_at = old_result.get("Created Date", "")
            result.last_post_date = old_result.get(
                "Last Post (DD-MM-YYYY) (Optional)", ""
            )
            result.last_active = old_result.get("Last Post (DD-MM-YYYY) (Optional)", "")
            result.is_active = (
                str(old_result.get("Active (Yes / No)", "No")).lower().startswith("y")
            )
            result.has_name_match = (
                str(old_result.get("Name (Yes / No)", "No")).lower().startswith("y")
            )
            result.risk_score = old_result.get("Risk Score", 0)
            result.priority = old_result.get("priority", "Low")
            result.screenshot_b64 = old_result.get("Screenshot")
            result.comments = ""
            result.bio = old_result.get("bio", "")

            if (
                old_result.get("profile_picture")
                and "placeholder" not in old_result["profile_picture"]
            ):
                result.has_logo = True
                
                # Use original URL as-is (CDN URLs are signed, don't strip params)
                result.profile_image_url = old_result["profile_picture"]
                
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as dl_session:
                        async with dl_session.get(
                            result.profile_image_url,
                            headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                                "Referer": "https://www.instagram.com/",
                            },
                        ) as resp:
                            if resp.status == 200:
                                img_data = await resp.read()
                                if len(img_data) > 500:
                                    result.profile_image_b64 = base64.b64encode(
                                        img_data
                                    ).decode("utf-8")
                except:
                    pass
            else:
                result.has_logo = False

            return result

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
