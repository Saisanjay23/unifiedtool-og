import asyncio
import base64
import datetime
import random
import os
import json
import sys
import re
from typing import Optional, Dict, Any

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.stealth.human import HumanBehavior
from backend.platforms.base import AbstractAnalyzer

logger = get_logger("platforms.twitter.analysis")


class TwitterAnalyzer(AbstractAnalyzer):
    """
    Deep-analyzes Twitter/X profiles using the EXACT 20-year principal engineer
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
            logger.info(f"Starting Twitter analysis for URL: {url} (Client: {client})")

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
                            "no",
                            "not available (twitter restricted)",
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

            async def _handle_response(response, data_store: Dict[str, Any]) -> None:
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

            async def _capture_screenshot(page) -> Optional[bytes]:
                try:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except:
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
                    except:
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
                    except:
                        return None

            old_result = get_default_result_dict(url, profile_name="")
            error_comments = []
            data_store = {"followers_count": None, "profile_image_url": None, "created_at": None}

            try:
                pw, browser, context, page = await create_stealth_browser(
                    platform="twitter", headless=headless
                )
                logger.info(f"[{url}] Browser created successfully.")

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
                    except:
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
                    except:
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
                            old_result["Profile name"] = name
                            old_result["Name (Yes / No)"] = "Yes"
                except:
                    error_comments.append("Name extraction failed")

                # 2. Followers (Hybrid)
                try:
                    if data_store["followers_count"] is not None:
                        old_result["Followers"] = int(data_store["followers_count"])
                        old_result["Comments"] += (
                            f" | Followers:Network({old_result['Followers']})"
                        )
                    else:
                        followers_el = await page.query_selector(
                            "xpath=//a[contains(@href, 'followers')]"
                        )
                        if followers_el:
                            txt = await followers_el.inner_text()
                            old_result["Followers"] = parse_followers(txt)
                            old_result["Comments"] += f" | Followers:DOM"
                        else:
                            f_el = await page.query_selector(
                                "xpath=//*[contains(text(), 'Followers')]"
                            )
                            if f_el:
                                parent = await f_el.query_selector("..")
                                if parent:
                                    txt = await parent.inner_text()
                                    old_result["Followers"] = parse_followers(txt)
                                    old_result["Comments"] += f" | Followers:TextSearch"
                                else:
                                    old_result["Followers"] = 0
                            else:
                                old_result["Followers"] = 0
                except:
                    old_result["Followers"] = 0

                # 3. Joined Date — Network primary, DOM fallback
                try:
                    if data_store.get("created_at"):
                        # Parse Twitter's date format: "Wed Jun 01 12:00:00 +0000 2016"
                        try:
                            joined_dt = datetime.datetime.strptime(
                                data_store["created_at"], "%a %b %d %H:%M:%S %z %Y"
                            )
                            old_result["Created Date"] = joined_dt.strftime("%m-%Y")
                            old_result["Comments"] += " | JoinedDate:Network"
                        except ValueError:
                            pass

                    if not old_result["Created Date"]:
                        # DOM fallback
                        joined_elems = await page.query_selector_all(
                            "xpath=//span[contains(., 'Joined')]"
                        )
                        joined_text = None
                        for el in joined_elems:
                            txt = (await el.inner_text()).strip()
                            if txt.startswith("Joined "):
                                joined_text = txt
                                break

                        if joined_text:
                            cleaned = joined_text.replace("Joined", "", 1).strip()
                            for fmt in ("%B %Y", "%b %Y", "%Y"):
                                try:
                                    joined_dt = datetime.datetime.strptime(cleaned, fmt)
                                    old_result["Created Date"] = joined_dt.strftime("%m-%Y")
                                    break
                                except ValueError:
                                    continue
                except Exception:
                    pass

                # 4. Location
                try:
                    loc = await page.query_selector(
                        'span[data-testid="UserLocation"] span span'
                    )
                    if loc:
                        old_result["Location"] = (await loc.inner_text()).strip()
                    elif loc := await page.query_selector(
                        'span[data-testid="UserLocation"]'
                    ):
                        old_result["Location"] = (await loc.inner_text()).strip()
                except:
                    pass

                # 5. Bio
                try:
                    bio_el = await page.query_selector(
                        'div[data-testid="UserDescription"]'
                    )
                    if bio_el:
                        old_result["Bio"] = (await bio_el.inner_text()).strip()
                except:
                    old_result["Bio"] = ""

                # 6. Logo — Network interception primary, DOM fallback
                try:
                    if data_store.get("profile_image_url") and "default_profile_images" not in data_store["profile_image_url"]:
                        # Best source: API response always has the correct URL
                        old_result["Logo (Yes / No)"] = "Yes"
                        old_result["profile_picture"] = data_store["profile_image_url"]
                        old_result["Comments"] += " | Logo:Network"
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
                                old_result["Logo (Yes / No)"] = "Yes"
                                old_result["profile_picture"] = src
                                old_result["Comments"] += " | Logo:DOM"
                            else:
                                old_result["Logo (Yes / No)"] = "No"
                        else:
                            old_result["Logo (Yes / No)"] = "No"
                except Exception:
                    old_result["Logo (Yes / No)"] = "No"

                # 7. Last Post / Active
                old_result["Active (Yes / No)"] = "No"
                try:
                    try:
                        await page.wait_for_selector(
                            'article[data-testid="tweet"]', timeout=10000
                        )
                        tweets = await page.query_selector_all(
                            'article[data-testid="tweet"]'
                        )
                    except:
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
                                    old_result["Last Post (DD-MM-YYYY) (Optional)"] = (
                                        dt.strftime("%d-%m-%Y")
                                    )

                                    now = datetime.datetime.now(datetime.timezone.utc)
                                    delta = now - dt
                                    if delta.days <= 180:
                                        old_result["Active (Yes / No)"] = "Yes"
                                    break
                except:
                    pass

                # Verified badge
                try:
                    verified = await page.query_selector(
                        '[data-testid="UserName"] svg[data-testid="icon-verified"]'
                    )
                    old_result["is_verified"] = verified is not None
                except Exception:
                    old_result["is_verified"] = False

                # 8. Screenshot
                screenshot_bytes = await _capture_screenshot(page)
                if screenshot_bytes:
                    old_result["Screenshot"] = base64.b64encode(
                        screenshot_bytes
                    ).decode("utf-8")
                else:
                    old_result["Screenshot"] = None

            except Exception as e:
                logger.error(f"Critical error during scrape: {e}")
                error_comments.append(f"Critical error: {type(e).__name__}")

            finally:
                if "browser" in locals() and browser:
                    await browser.close()
                if "pw" in locals() and pw:
                    await pw.stop()

            if not old_result.get("Profile name"):
                old_result["Profile name"] = "Scrape Incomplete"

            old_result["Comments"] = (
                old_result.get("Comments", "") + " | " + " | ".join(error_comments)
                if error_comments
                else old_result.get("Comments", "")
            )

            risk, priority = calculate_risk(old_result)
            old_result["Risk Score"], old_result["priority"] = risk, priority

            await self.health.record_request(
                "twitter", success=not bool(error_comments)
            )

            # --- MAP TO NEW BACKEND FORMAT ---
            def extract_username(url: str) -> str:
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

            result = ProfileResult(
                platform="twitter",
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
            result.bio = old_result.get("Bio", "")
            result.is_verified = old_result.get("is_verified", False)

            if (
                old_result.get("profile_picture")
                and "default_profile_images" not in old_result["profile_picture"]
            ):
                result.has_logo = True
                result.profile_image_url = old_result["profile_picture"]
                logger.info(f"Profile picture URL found: {result.profile_image_url[:80]}...")
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as dl_session:
                        async with dl_session.get(
                            result.profile_image_url,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                result.profile_image_b64 = base64.b64encode(
                                    await resp.read()
                                ).decode("utf-8")
                                logger.info(f"Profile image downloaded via aiohttp ({len(result.profile_image_b64)} chars)")
                            else:
                                logger.warning(f"aiohttp download failed with status {resp.status}")
                except Exception as e:
                    logger.warning(f"aiohttp download failed: {e}, trying Playwright fallback")
                    # Playwright fallback — use the browser context already open
                    try:
                        if "context" in locals() and context:
                            img_page = await context.new_page()
                            try:
                                resp = await img_page.goto(result.profile_image_url, timeout=10000)
                                if resp and resp.status == 200:
                                    img_bytes = await img_page.screenshot(full_page=True, type="jpeg", quality=80)
                                    result.profile_image_b64 = base64.b64encode(img_bytes).decode("utf-8")
                                    logger.info(f"Profile image captured via Playwright fallback")
                            finally:
                                await img_page.close()
                    except Exception as e2:
                        logger.warning(f"Playwright fallback also failed: {e2}")
            else:
                result.has_logo = False
                logger.info(f"No profile picture found (profile_picture='{old_result.get('profile_picture', '')}')")

            return result
