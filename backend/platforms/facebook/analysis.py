"""
Deep Identity Hydration: Facebook.
Executes multi-phase extraction heuristics (OpenGraph Meta -> JSON-LD -> Native DOM -> GraphQL Interception)
to synthesize a comprehensive and deterministic profile footprint despite extreme DOM volatility
and AB-tested interface layouts.
"""
import asyncio
import base64
import datetime
import json
import random
import re
import os
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.stealth.human import HumanBehavior
from backend.platforms.base import AbstractAnalyzer

logger = get_logger("platforms.facebook.analysis")

POPUP_CLOSE_SELECTORS = [
    'div[aria-label="Close"]',
    'div[role="button"][aria-label="Close"]',
    'div[aria-label="Not Now"]',
    'span:has-text("Not Now")',
    'span:has-text("Block")',
    'div[aria-label="Decline"]',
    'div[aria-label="Allow"]',
    'div[data-testid="cookie-policy-manage-dialog"]',
]


async def _handle_blocking_popups(page):
    """
    DOM Interstitial Mitigation.
    Employs aggressive keyboard and click events to clear non-deterministic overlays
    (GDPR consent, login walls, guided tours) that otherwise occlude target hydration zones.
    """
    try:
        # Quickest fix for many modals
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        # Check for explicit buttons
        for sel in POPUP_CLOSE_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=1000)
                    await asyncio.sleep(0.5)
            except Exception:
                pass  # Element gone or not actionable
    except Exception:
        pass


def parse_followers(s: str) -> int:
    """Normalizes highly volatile human-readable engagement metrics (e.g., '1.2M', '45K') into strict integers."""
    if not s:
        return 0
    s = str(s).lower().replace(",", "").strip()
    try:
        if "k" in s:
            return int(float(s.replace("k", "")) * 1000)
        elif "m" in s:
            return int(float(s.replace("m", "")) * 1000000)
        elif "b" in s:
            return int(float(s.replace("b", "")) * 1_000_000_000)
        numeric = re.sub(r"[^0-9.]", "", s)
        return int(float(numeric)) if numeric else 0
    except Exception:
        return 0


def parse_date_robust(date_str):
    """Helper: Parse ANY date string to DD-MM-YYYY"""
    if not date_str:
        return None
    date_str = str(date_str).strip()

    # Pre-clean
    clean = re.sub(
        r"(\d+)(st|nd|rd|th)", r"\1", date_str
    )  # Remove st/nd/rd/th
    clean = (
        clean.replace(" at ", " ").replace(",", "").replace(".", "")
    )

    formats = [
        "%d %B %Y",  # 31 January 2020
        "%B %d %Y",  # January 31 2020
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%B %Y",  # May 2015
        "%Y",  # 2015
    ]

    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(clean, fmt)
            if dt.year >= 2004:
                return dt
        except:
            pass

    return None

async def _extract_profile_picture(page, result_dict, error_comments):
    """
    Polymorphic Asset Extraction Pipeline.
    Prioritizes deterministic structural metadata (Strategy A/B) before degrading
    to heuristic visual scraping (Strategy C). Contains strict filtering to prevent
    poisoning the dataset with generic platform placeholders.
    """
    try:
        # Strategy 0: SVG image elements (Primary — current Facebook DOM pattern 2025+)
        # Facebook renders profile pictures inside <svg><g><image xlink:href="..."/></g></svg>
        # The profile picture is ~168x168px at y>60 (below navbar).
        # Must skip: navbar avatar (40x40 at y~8), hidden elements (no bounding box).
        try:
            svg_images = await page.query_selector_all("svg image")
            for svg_img in svg_images[:15]:
                href = await svg_img.get_attribute("xlink:href") or await svg_img.get_attribute("href") or ""
                if not (href and "scontent" in href and "http" in href):
                    continue
                blocklist = ["static.xx", "rsrc.php", "silhouette", "emoji", "guest", "default_profile"]
                if any(b in href for b in blocklist):
                    continue
                # MUST have a visible bounding box, be large (>=100px), and below navbar (y>60)
                try:
                    box = await svg_img.bounding_box()
                except Exception:
                    box = None
                if not box or box["width"] < 100 or box["y"] < 60:
                    continue
                
                # --- UPGRADE TO HD ---
                best_url = href.replace("&amp;", "&")
                try:
                    upg = re.sub(r'p\d+x\d+/', '', best_url)
                    upg = re.sub(r's\d+x\d+/', '', upg)
                    upg = re.sub(r'c\d+\.\d+\.\d+\.\d+/', '', upg)
                    upg = re.sub(r'&w=\d+', '&w=1080', upg)
                    upg = re.sub(r'&h=\d+', '&h=1080', upg)
                    upg = re.sub(r'&width=\d+', '&width=1080', upg)
                    upg = re.sub(r'&height=\d+', '&height=1080', upg)
                    result_dict["profile_picture"] = upg
                except:
                    result_dict["profile_picture"] = best_url
                return  # Found the actual profile picture
        except Exception:
            pass


        def _is_valid_pfp(url):
            if not url or "http" not in url:
                return False
            if "emoji" in url:
                return False
            # Strict Anti-Placeholder Filters
            blocklist = [
                "static.xx",
                "rsrc.php",
                "silhouette",
                "guest",
                "default_profile",
                "avatar_empty",
                "blank_profile",
                "1x1",
            ]
            if any(x in url for x in blocklist):
                return False
            return True

        def _get_resolution_score(url):
            score = 0
            # Explicit high-res markers
            if "s2048x2048" in url:
                score += 2000
            elif "s960x960" in url:
                score += 960
            elif "s720x720" in url:
                score += 720
            elif "s480x480" in url:
                score += 480

            # Penalize "p" sizes (thumbnails) severely if looking for HD
            if "p320x320" in url:
                score += 100
            elif "p100x100" in url:
                score -= 500
            elif "s100x100" in url:
                score -= 500

            # HD indicators
            if "original" in url:
                score += 50
            if "full" in url:
                score += 50

            # Clean URL (no resizing params) often means original/public which is good
            if (
                "scontent" in url
                and not re.search(r"s\d+x\d+", url)
                and not re.search(r"p\d+x\d+", url)
            ):
                score += 800

            return score

        # Strategy A: OpenGraph Meta (Highest Priority)
        try:
            og_img_el = page.locator('meta[property="og:image"]').first
            if await og_img_el.count() > 0:
                og_val = await og_img_el.get_attribute("content")
                if _is_valid_pfp(og_val):
                    # OG Images are designed for sharing and usually high res / persistent
                    best_url = og_val.replace("&amp;", "&")
                    
                    try:
                        upg = re.sub(r'p\d+x\d+/', '', best_url)
                        upg = re.sub(r's\d+x\d+/', '', upg)
                        upg = re.sub(r'c\d+\.\d+\.\d+\.\d+/', '', upg)
                        upg = re.sub(r'&w=\d+', '&w=1080', upg)
                        upg = re.sub(r'&h=\d+', '&h=1080', upg)
                        upg = re.sub(r'&width=\d+', '&width=1080', upg)
                        upg = re.sub(r'&height=\d+', '&height=1080', upg)
                        result_dict["profile_picture"] = upg
                    except:
                        result_dict["profile_picture"] = best_url
                        
                    return  # Stop immediately if we have the Gold Standard
        except:
            pass

        candidates = []

        # Strategy B: JSON-LD (Fallback)
        try:
            json_ld_scripts = await page.query_selector_all(
                'script[type="application/ld+json"]'
            )
            for script in json_ld_scripts:
                try:
                    data = json.loads(await script.text_content())
                    if "image" in data:
                        img_val = data["image"]
                        if isinstance(img_val, dict):
                            img_val = img_val.get("contentUrl") or img_val.get("url")
                        if _is_valid_pfp(img_val):
                            candidates.append(img_val)
                except:
                    pass
        except:
            pass

        # Strategy C: DOM Scan (Fallback)
        try:
            images = await page.locator("img, image").all()
            for img in images[:40]:
                try:
                    # Explicitly skip cover photos
                    if (
                        await img.get_attribute("data-imgperflogname")
                        == "profileCoverPhoto"
                    ):
                        continue

                    src = await img.get_attribute("src")
                    if not src:
                        src = await img.get_attribute("xlink:href")

                    if src and "scontent" in src and _is_valid_pfp(src):
                        score_boost = 0
                        # Boost images inside SVG masks (standard FB profile style)
                        if await img.evaluate(
                            'el => el.tagName.toLowerCase() === "image"'
                        ):
                            score_boost += 5000

                        candidates.append((src, score_boost))
                except:
                    continue
        except:
            pass

        # Selection Logic
        if candidates:
            # unique by url
            seen = set()
            unique_candidates = []
            for url, boost in candidates:
                if url not in seen:
                    seen.add(url)
                    unique_candidates.append((url, boost))

            sorted_candidates = sorted(
                unique_candidates,
                key=lambda x: _get_resolution_score(x[0]) + x[1],
                reverse=True,
            )
            best_url = sorted_candidates[0][0]
            
            # --- UPGRADE TO HD ---
            try:
                upg = re.sub(r'p\d+x\d+/', '', best_url)
                upg = re.sub(r's\d+x\d+/', '', upg)
                upg = re.sub(r'c\d+\.\d+\.\d+\.\d+/', '', upg)
                upg = re.sub(r'&w=\d+', '&w=1080', upg)
                upg = re.sub(r'&h=\d+', '&h=1080', upg)
                upg = re.sub(r'&width=\d+', '&width=1080', upg)
                upg = re.sub(r'&height=\d+', '&height=1080', upg)
                result_dict["profile_picture"] = upg
            except:
                result_dict["profile_picture"] = best_url

    except Exception as e:
        error_comments.append(f"PFP-Err: {str(e)[:10]}")


class FacebookAnalyzer(AbstractAnalyzer):
    """
    Deterministic Facebook Hydration Engine.
    Employs defense-in-depth parsing strategies to extract PII across rapidly shifting UI variants.
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
            # Result dictionary exactly like old tool
            old_result = {
                "Profile name": "",
                "Name (Yes / No)": "No",
                "Followers": 0,
                "Location": "",
                "Logo (Yes / No)": "No",
                "Created Date": "No",
                "Last Post (DD-MM-YYYY) (Optional)": "",
                "Active (Yes / No)": "No",
                "Screenshot": None,
                "profile_picture": "",
            }
            error_comments = []

            # The final returned object for the new backend
            result = ProfileResult(
                platform="facebook",
                client_name=client,
                keyword="",
                url=url,
                username=self._extract_username(url) or "",
            )

            logger.info(f"Starting Facebook analysis for URL: {url} (Client: {client})")
            pw, browser, context, page = await create_stealth_browser(
                platform="facebook",
                headless=headless,
            )
            logger.info(f"[{url}] Browser created successfully.")

            try:
                # =========================================================================
                # LEGACY HYDRATION LOGIC (Retained for proven heuristic stability)
                # =========================================================================
                await page.add_init_script(
                    """Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"""
                )

                # Network Interception Storage
                captured_network_data = {
                    "followers": 0,
                    "page_created": None,
                    "joined": None,
                }

                async def _intercept_response(response):
                    """
                    Passive GraphQL Interception Hook.
                    Subscribes to the immediate CDP network stream, extracting deeply nested,
                    authoritative timestamps and counts without touching the volatile DOM layer.
                    """
                    try:
                        if "graphql" in response.url and response.status == 200:
                            text = await response.text()

                            # Followers/Friends (exact integer)
                            f_match = re.search(r'"follower_count":\s*(\d+)', text)
                            if f_match:
                                captured_network_data["followers"] = max(
                                    captured_network_data["followers"],
                                    int(f_match.group(1)),
                                )
                            f_match2 = re.search(r'"friend_count":\s*(\d+)', text)
                            if f_match2:
                                captured_network_data["followers"] = max(
                                    captured_network_data["followers"],
                                    int(f_match2.group(1)),
                                )

                            # Page Created Date (Profile-specific keys ONLY)
                            for pattern in [
                                r'"page_created":\s*(\d{10})',
                                r'"page_created_time":\s*(\d{10})',
                                r'"founding_date":\s*(\d{10})',
                            ]:
                                pc_match = re.search(pattern, text)
                                if pc_match:
                                    captured_network_data["page_created"] = int(
                                        pc_match.group(1)
                                    )
                                    break

                            # Text-based creation date
                            pc_match2 = re.search(
                                r"Page created on\s+([A-Za-z0-9 ,]+)", text
                            )
                            if pc_match2 and not captured_network_data["page_created"]:
                                captured_network_data["page_created"] = pc_match2.group(
                                    1
                                )

                            # Joined Date (for People - Profile-specific keys ONLY)
                            for pattern in [
                                r'"registration_time":\s*(\d{10})',
                                r'"join_date":\s*(\d{10})',
                                r'"profile_creation_time":\s*(\d{10})',
                            ]:
                                jd_match = re.search(pattern, text)
                                if jd_match and not captured_network_data["joined"]:
                                    captured_network_data["joined"] = int(
                                        jd_match.group(1)
                                    )
                                    break
                    except:
                        pass

                page.on("response", _intercept_response)

                # Navigate
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                    # Anti-Bot: Quick delay (reduced for speed)
                    await asyncio.sleep(random.uniform(1, 2))
                    await HumanBehavior(platform="facebook").mouse_jitter(page)

                    # Wait for something significant. Profile name usually in h1
                    try:
                        await page.wait_for_selector("h1", timeout=8000)
                    except:
                        # Might be a login wall or captcha
                        if "login" in page.url:
                            error_comments.append("Redirected to Login")
                except Exception as e:
                    error_comments.append("Page Load Timeout")
                    # Continue to try scraping what we have

                # 0. Extraction: Profile Picture (Critical for Logo Check)
                await _extract_profile_picture(page, old_result, error_comments)

                # 1. Profile Name - ROBUST MULTI-STRATEGY EXTRACTION
                try:
                    profile_name = None

                    # STRATEGY A: OpenGraph Meta (Most Reliable for Pages)
                    try:
                        og_title = await page.locator(
                            'meta[property="og:title"]'
                        ).first.get_attribute("content")
                        if og_title and len(og_title) > 1 and len(og_title) < 100:
                            # Filter out generic titles and UI elements
                            blocklist = [
                                "facebook",
                                "log in",
                                "sign up",
                                "watch",
                                "meta",
                                "home",
                                "notifications",
                                "messenger",
                                "menu",
                                "search",
                                "marketplace",
                                "groups",
                                "gaming",
                                "video",
                                "feeds",
                                "events",
                                "pages",
                                "friends",
                                "profile",
                                "settings",
                                "help",
                                "privacy",
                            ]
                            if not any(
                                og_title.strip().lower() == b for b in blocklist
                            ):
                                profile_name = og_title.strip()
                    except:
                        pass

                    # STRATEGY B: JSON-LD Name (Gold Standard)
                    if not profile_name:
                        try:
                            json_ld_scripts = await page.query_selector_all(
                                'script[type="application/ld+json"]'
                            )
                            for script in json_ld_scripts:
                                try:
                                    content = await script.text_content()
                                    data = json.loads(content)
                                    if "name" in data and data["name"]:
                                        candidate = str(data["name"]).strip()
                                        if len(candidate) > 1 and len(candidate) < 100:
                                            profile_name = candidate
                                            break
                                except:
                                    continue
                        except:
                            pass

                    # STRATEGY C: H1 with Validation
                    if not profile_name:
                        try:
                            h1_elements = await page.query_selector_all("h1")
                            for h1 in h1_elements:
                                try:
                                    text = (await h1.inner_text()).strip()
                                    # Validate: Not empty, not too long, not generic
                                    if text and len(text) > 1 and len(text) < 100:
                                        blocklist = [
                                            "facebook",
                                            "log in",
                                            "sign up",
                                            "watch",
                                            "meta",
                                            "home",
                                            "error",
                                            "notifications",
                                            "messenger",
                                            "menu",
                                            "search",
                                            "marketplace",
                                            "groups",
                                            "gaming",
                                            "video",
                                            "feeds",
                                            "events",
                                            "pages",
                                            "friends",
                                            "profile",
                                            "settings",
                                            "help",
                                            "privacy",
                                        ]
                                        if not any(
                                            text.strip().lower() == b for b in blocklist
                                        ):
                                            profile_name = text
                                            break
                                except:
                                    continue
                        except:
                            pass

                    # STRATEGY D: URL Parsing (Last Resort)
                    if not profile_name:
                        try:
                            # Extract from URL like facebook.com/pagename or profile.php?id=123
                            from urllib.parse import urlparse

                            parsed = urlparse(url)
                            path = parsed.path.strip("/")
                            if path and path not in ["profile.php", "pages", "groups"]:
                                # Clean username from URL
                                clean_name = (
                                    path.split("/")[0]
                                    .replace(".", " ")
                                    .replace("-", " ")
                                    .title()
                                )
                                if len(clean_name) > 1:
                                    profile_name = f"[URL] {clean_name}"
                        except:
                            pass

                    if profile_name:
                        old_result["Profile name"] = profile_name
                        old_result["Name (Yes / No)"] = "Yes"
                    else:
                        old_result["Profile name"] = "Unknown"
                        old_result["Name (Yes / No)"] = "No"

                except Exception:
                    error_comments.append("Name extraction failed")

                # 2. Followers (and Friends) - EXACT COUNT STRATEGY
                try:
                    # We first look for raw numbers in attributes or JSON.
                    body_content = await page.content()

                    # Pattern A: InteractionCount in JSON-LD (Best for Pages)
                    # "interactionCount":"123456"
                    f_count = 0
                    json_matches = re.findall(
                        r'"userInteractionCount":\s*"?(\d+)"?', body_content
                    )
                    if json_matches:
                        # Usually the largest one is followers/likes
                        f_count = max([int(x) for x in json_matches])

                    # Pattern B: Title attributes for exact numbers (Common in FB UI)
                    # <span title="1,234,567">1.2M</span>
                    if f_count == 0:
                        # Scan for large numbers in titles near "followers" or "friends" keywords
                        # This is a bit heuristic. We seek elements with title="1,234"
                        try:
                            elements_with_title = await page.query_selector_all(
                                "[title]"
                            )
                            for el in elements_with_title:
                                t_val = await el.get_attribute("title")
                                if t_val and re.match(r"^[\d,]+$", t_val):
                                    val = int(t_val.replace(",", ""))
                                    if val > 100:  # filter out small noise
                                        f_count = max(f_count, val)
                        except:
                            pass

                    if f_count > 0:
                        old_result["Followers"] = f_count
                    else:
                        # Fallback to Text Scraping (Rounded)
                        body_text = await page.inner_text("body")
                        followers_match = re.search(
                            r"([\d,.]+K?M?)\s+followers", body_text, re.IGNORECASE
                        )
                        likes_match = re.search(
                            r"([\d,.]+K?M?)\s+likes", body_text, re.IGNORECASE
                        )
                        friends_match = re.search(
                            r"([\d,.]+K?M?)\s+friends", body_text, re.IGNORECASE
                        )

                        if followers_match:
                            old_result["Followers"] = parse_followers(
                                followers_match.group(1)
                            )
                        elif likes_match:
                            old_result["Followers"] = parse_followers(
                                likes_match.group(1)
                            )
                        elif friends_match:
                            old_result["Followers"] = parse_followers(
                                friends_match.group(1)
                            )

                except:
                    old_result["Followers"] = 0

                # Followers Backup: Network Interception Data
                if (
                    old_result["Followers"] == 0
                    and captured_network_data["followers"] > 0
                ):
                    old_result["Followers"] = captured_network_data["followers"]

                # 3. Location & Basic Info via JSON-LD
                try:
                    # Look for schema.org data
                    json_ld_scripts = await page.query_selector_all(
                        'script[type="application/ld+json"]'
                    )
                    for script in json_ld_scripts:
                        try:
                            content = await script.text_content()
                            data = json.loads(content)
                            if "address" in data:
                                addr = data["address"]
                                if isinstance(addr, dict) and "addressLocality" in addr:
                                    old_result["Location"] = addr["addressLocality"]
                            if not old_result["Profile name"] and "name" in data:
                                old_result["Profile name"] = data["name"]
                        except:
                            continue

                    # Fallback Location
                    if not old_result["Location"]:
                        try:
                            # Get body text
                            body_text_head = await page.inner_text("body")

                            loc_match = re.search(
                                r"(Lives in|From)\s+([^\n]+)", body_text_head
                            )
                            if loc_match:
                                old_result["Location"] = loc_match.group(2).strip()
                            else:
                                lines = body_text_head.split("\n")
                                for line in lines[:50]:
                                    line = line.strip()
                                    if "," in line and len(line) < 50:
                                        parts = line.split(",")
                                        if (
                                            len(parts) >= 2
                                            and parts[0].strip().isalpha()
                                            and parts[-1].strip().isalpha()
                                        ):
                                            old_result["Location"] = line
                                            break
                        except:
                            pass
                except Exception as e:
                    error_comments.append(f"Loc-Err: {str(e)[:20]}")

                # 4. Logo
                # Fix: Rely on the robust image extraction from step 3
                if (
                    old_result.get("profile_picture")
                    and "placeholder" not in old_result["profile_picture"]
                ):
                    old_result["Logo (Yes / No)"] = "Yes"
                else:
                    old_result["Logo (Yes / No)"] = "No"

                # 5. Creation Date - INDUSTRY STANDARD (Network -> Modal -> JSON-LD -> Text)
                try:
                    found_date = None

                    # STRATEGY 0: DEEP SOURCE SCAN (Ultimate Robust - Reads Raw Data)
                    try:
                        page_source = await page.content()

                        source_patterns = [
                            # Profile/Page-specific Unix timestamps (10 digits)
                            (r'"page_created_time":\s*(\d{10})', "unix"),
                            (r'"founding_date":\s*(\d{10})', "unix"),
                            (r'"registration_time":\s*(\d{10})', "unix"),
                            (r'"profile_creation_time":\s*(\d{10})', "unix"),
                            (r'"join_time":\s*(\d{10})', "unix"),
                            # ISO dates (from JSON-LD - these are profile-specific)
                            (r'"dateCreated":\s*"(\d{4}-\d{2}-\d{2})', "iso"),
                            (r'"foundingDate":\s*"(\d{4}-\d{2}-\d{2})', "iso"),
                            # Text dates in source (profile-specific phrases)
                            (r"Page created on\s+([A-Za-z]+ \d{1,2},? \d{4})", "text"),
                            (
                                r"Joined Facebook on\s+([A-Za-z]+ \d{1,2},? \d{4})",
                                "text",
                            ),
                            (
                                r"Joined\s+([A-Za-z]+ \d{4})",
                                "text",
                            ),  # "Joined May 2015"
                        ]

                        best_timestamp = None

                        for pattern, date_type in source_patterns:
                            matches = re.findall(pattern, page_source)
                            for m in matches:
                                try:
                                    if date_type == "unix":
                                        ts = int(
                                            m[-1]
                                        )  # Use last group for combined regexes
                                        # Validate range (2004 - now)
                                        if (
                                            1072915200
                                            < ts
                                            < datetime.datetime.now().timestamp()
                                            + 86400
                                        ):
                                            if (
                                                best_timestamp is None
                                                or ts < best_timestamp
                                            ):
                                                best_timestamp = (
                                                    ts  # Take earliest (creation)
                                                )
                                    elif date_type == "iso":
                                        dt = datetime.datetime.strptime(m, "%Y-%m-%d")
                                        if dt.year >= 2004:
                                            found_date = dt.strftime("%d-%m-%Y")
                                            break
                                    elif date_type == "text":
                                        dt = parse_date_robust(m)
                                        if dt:
                                            found_date = dt.strftime("%d-%m-%Y")
                                            break
                                except:
                                    pass
                            if found_date:
                                break

                        if not found_date and best_timestamp:
                            found_date = datetime.datetime.fromtimestamp(
                                best_timestamp
                            ).strftime("%d-%m-%Y")
                    except:
                        pass

                    # Use network-intercepted data as fallback (if source scan didn't find anything)
                    if not found_date and captured_network_data.get("page_created"):
                        val = captured_network_data["page_created"]
                        if isinstance(val, int):
                            found_date = datetime.datetime.fromtimestamp(val).strftime(
                                "%d-%m-%Y"
                            )
                        else:
                            dt = parse_date_robust(val)
                            if dt:
                                found_date = dt.strftime("%d-%m-%Y")

                    if not found_date and captured_network_data.get("joined"):
                        val = captured_network_data["joined"]
                        if isinstance(val, int):
                            found_date = datetime.datetime.fromtimestamp(val).strftime(
                                "%d-%m-%Y"
                            )

                    # STRATEGY 1: MODAL CLICK (PRIMARY - confirmed working via live testing)
                    # Modal shows: "Created: August 17, 2015" or "Joined: February 4, 2004"
                    if not found_date:
                        try:
                            # Find correct H1 (not "Notifications" from sidebar)
                            h1_elements = await page.query_selector_all("h1")
                            target_h1 = None
                            blocklist = [
                                "notifications",
                                "facebook",
                                "log in",
                                "menu",
                                "home",
                                "watch",
                                "marketplace",
                                "groups",
                                "gaming",
                            ]

                            for h1 in h1_elements:
                                try:
                                    txt = (await h1.inner_text()).strip().lower()
                                    is_visible = await h1.is_visible()
                                    if is_visible and txt and txt not in blocklist:
                                        target_h1 = h1
                                        break
                                except:
                                    pass

                            if target_h1:
                                await target_h1.click(force=True, timeout=3000)
                                await asyncio.sleep(2)

                                # Find the modal with profile info
                                modals = page.locator('div[role="dialog"]')
                                count = await modals.count()

                                for i in range(count):
                                    m_loc = modals.nth(i)
                                    if await m_loc.is_visible():
                                        modal_text = await m_loc.inner_text()

                                        # Check if this modal has date info (not login popup)
                                        # For People: "Joined" | For Pages: "Created"
                                        has_date_info = (
                                            "Created" in modal_text
                                            or "Joined" in modal_text
                                            or re.search(r"\b20[0-2]\d\b", modal_text)
                                        )  # Any year 2000-2029

                                        if has_date_info:
                                            # Parse modal text for date
                                            # PEOPLE patterns first (Joined), then PAGES (Created)
                                            modal_patterns = [
                                                # PRIMARY: Exact format from live testing
                                                r"Joined\s+Facebook:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Joined Facebook: November 20, 2018
                                                r"Joined\s+Facebook:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Joined Facebook: 20 November 2018
                                                # Other People patterns
                                                r"Joined\s+Facebook\s+in\s+([A-Za-z]+\s+\d{4})",  # Joined Facebook in February 2004
                                                r"Joined\s+Facebook\s+in\s+(\d{4})",  # Joined Facebook in 2004
                                                r"Joined\s+in\s+([A-Za-z]+\s+\d{4})",  # Joined in February 2004
                                                r"Joined:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Joined: February 4, 2004
                                                r"Joined:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Joined: 4 February 2004
                                                r"Joined\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Joined February 4, 2004
                                                r"Joined\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Joined 4 February 2004
                                                r"Joined\s+([A-Za-z]+\s+\d{4})",  # Joined February 2004
                                                r"Joined\s+(\d{4})",  # Joined 2004
                                                # Pages: "Created" patterns
                                                r"Created:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Created: August 17, 2015
                                                r"Created:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Created: 17 August 2015
                                                r"Created:\s*([A-Za-z]+\s+\d{4})",  # Created: August 2015
                                                r"Created\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Created August 17, 2015
                                                r"Created\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Created 17 August 2015
                                            ]

                                            for pat in modal_patterns:
                                                m = re.search(
                                                    pat, modal_text, re.IGNORECASE
                                                )
                                                if m:
                                                    date_str = m.group(1).strip()
                                                    # Handle year-only case
                                                    if re.match(r"^\d{4}$", date_str):
                                                        found_date = f"01-01-{date_str}"
                                                    else:
                                                        dt = parse_date_robust(date_str)
                                                        if dt and dt.year >= 2004:
                                                            found_date = dt.strftime(
                                                                "%d-%m-%Y"
                                                            )
                                                    if found_date:
                                                        break
                                            break

                                # Close modal
                                try:
                                    await page.keyboard.press("Escape")
                                except:
                                    pass
                        except:
                            pass

                    # STRATEGY 2: PAGE TRANSPARENCY (Fallback for Pages)
                    if not found_date:
                        try:
                            if "profile.php?id=" in url:
                                transparency_url = (
                                    url.rstrip("/") + "&sk=about_profile_transparency"
                                )
                            else:
                                transparency_url = (
                                    url.rstrip("/") + "/about_profile_transparency"
                                )

                            await page.goto(
                                transparency_url,
                                wait_until="domcontentloaded",
                                timeout=12000,
                            )
                            await asyncio.sleep(0.5)

                            body_text = await page.inner_text("body")

                            # Patterns for standalone dates
                            patterns = [
                                r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # 23 July 2009
                                r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # July 23, 2009
                            ]

                            for pat in patterns:
                                m = re.search(pat, body_text, re.IGNORECASE)
                                if m:
                                    date_str = m.group(1).strip()
                                    dt = parse_date_robust(date_str)
                                    if dt and dt.year >= 2004:
                                        found_date = dt.strftime("%d-%m-%Y")
                                        break
                        except:
                            pass

                    # STRATEGY 3: INTRO / ABOUT (For People & Groups - Fallback)
                    if not found_date:
                        try:
                            # Go to About Section (Correctly handling ID vs Username)
                            if "profile.php?id=" in url:
                                about_url = url.rstrip("/") + "&sk=about"
                            else:
                                about_url = url.rstrip("/") + "/about"

                            # Try accessing About
                            await page.goto(
                                about_url, wait_until="domcontentloaded", timeout=12000
                            )
                            await asyncio.sleep(0.5)

                            about_text = await page.inner_text("body")

                            # "Joined [Date]" Patterns (Comprehensive)
                            joined_patterns = [
                                r"Joined\s+(?:Facebook\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Joined 12 May 2015
                                r"Joined\s+(?:Facebook\s+)?([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Joined May 12, 2015
                                r"Joined\s+(?:Facebook\s+)?([A-Za-z]+\s+\d{4})",  # Joined May 2015
                                r"Joined\s+(?:Facebook\s+)?(\d{4})",  # Joined 2015
                                r"Started\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",  # Started 1 August 2023
                                r"Started\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",  # Started August 1, 2023
                                r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*\n?\s*Joined",  # 12 May 2015\nJoined
                            ]

                            for pat in joined_patterns:
                                m = re.search(pat, about_text, re.IGNORECASE)
                                if m:
                                    dt = parse_date_robust(m.group(1))
                                    if dt:
                                        found_date = dt.strftime("%d-%m-%Y")
                                        # If we only got YYYY, that's fine, parse_date handles it (defaults to 1 Jan or just YYYY check)
                                        if len(m.group(1)) == 4:  # Just Year
                                            found_date = (
                                                f"01-01-{m.group(1)}"  # Standardize
                                            )
                                        break
                        except:
                            pass

                    # STRATEGY 5: JSON-LD (Metadata - Fallback)
                    if not found_date:
                        try:
                            jsons = await page.query_selector_all(
                                'script[type="application/ld+json"]'
                            )
                            for s in jsons:
                                txt = await s.text_content()
                                if '"dateCreated"' in txt or '"foundingDate"' in txt:
                                    m = re.search(
                                        r'"(dateCreated|foundingDate)":"([^"]+)"', txt
                                    )
                                    if m:
                                        # ISO format: 2009-02-04T...
                                        raw = m.group(2).split("T")[0]
                                        dt = parse_date_robust(raw)
                                        if dt:
                                            found_date = dt.strftime("%d-%m-%Y")
                        except:
                            pass

                    # Final Assignment
                    if found_date:
                        old_result["Created Date"] = found_date
                    else:
                        old_result["Created Date"] = "No"

                    # Return to main profile if we navigated away
                    if "about" in page.url or "transparency" in page.url:
                        await page.goto(url, wait_until="domcontentloaded")

                except Exception as e:
                    error_comments.append(f"Date-Err: {str(e)[:20]}")
                    old_result["Created Date"] = "No"

                # 6. Last Post / Active - EXACT DATE UTILS STRATEGY
                old_result["Active (Yes / No)"] = "No"
                try:
                    # STRATEGY 0: DEEP SOURCE SCAN (MAX TIMESTAMP)
                    # User noticed 'creation_time' regex was finding post dates. We leverage this!
                    # We scan ALL timestamps in the source and take the LATEST one.
                    try:
                        page_content = await page.content()
                        # Find all unix timestamps associated with creation/publish
                        # "creation_time":1678901234 or "publish_time":1678901234
                        timestamps = []
                        for pat in [
                            r'"creation_time":\s*(\d{10})',
                            r'"publish_time":\s*(\d{10})',
                            r'data-utime="(\d{10})"',
                        ]:
                            matches = re.findall(pat, page_content)
                            for m in matches:
                                try:
                                    timestamps.append(int(m))
                                except:
                                    pass

                        if timestamps:
                            # Filter valid range (2004 - Now)
                            valid_ts = []
                            now_ts = datetime.datetime.now().timestamp()
                            min_ts = datetime.datetime(2004, 1, 1).timestamp()

                            for ts in timestamps:
                                if (
                                    min_ts < ts <= now_ts + 86400
                                ):  # allow 1 day future drift
                                    valid_ts.append(ts)

                            if valid_ts:
                                # The LATEST timestamp is the Last Post/Activity
                                max_ts = max(valid_ts)
                                last_dt = datetime.datetime.fromtimestamp(max_ts)

                                old_result["Last Post (DD-MM-YYYY) (Optional)"] = (
                                    last_dt.strftime("%d-%m-%Y")
                                )
                                if (datetime.datetime.now() - last_dt).days <= 180:
                                    old_result["Active (Yes / No)"] = "Yes"
                    except Exception as e:
                        error_comments.append(f"DeepScanErr: {str(e)[:10]}")

                    if not old_result["Last Post (DD-MM-YYYY) (Optional)"]:
                        # Quick check for feed, minimal wait
                        try:
                            await page.wait_for_selector(
                                'div[role="feed"]', timeout=3000
                            )
                        except:
                            pass
                        await asyncio.sleep(random.uniform(0.5, 1))

                        feed_selector = "div[role='feed']"
                        if await page.locator(feed_selector).count() == 0:
                            feed_selector = "div[role='main']"

                        if await page.locator(feed_selector).count() > 0:
                            posts = page.locator(feed_selector).first.locator(
                                "div[role='article']"
                            )
                            count = await posts.count()

                            found_post = False
                            for i in range(min(count, 5)):
                                post = posts.nth(i)

                                # Check PINNED
                                try:
                                    if "Pinned" in (await post.inner_text())[:100]:
                                        continue
                                except:
                                    pass

                                best_dt = None

                                # STRATEGY 1: Unix Timestamp (Golden Source - DOM Level)
                                try:
                                    utime_el = await post.locator("[data-utime]").first
                                    if await utime_el.count() > 0:
                                        ts = await utime_el.get_attribute("data-utime")
                                        if ts:
                                            best_dt = datetime.datetime.fromtimestamp(
                                                int(ts)
                                            )
                                except:
                                    pass

                                if not best_dt:
                                    # STRATEGY 2: Link Scan (Title/Aria)
                                    try:
                                        links = await post.locator("a").all()
                                    except:
                                        continue

                                    for link in links:
                                        txt = await link.inner_text()
                                        aria = await link.get_attribute("aria-label")
                                        title = await link.get_attribute("title")

                                        candidates = [
                                            c for c in [title, aria, txt] if c
                                        ]

                                        for date_str in candidates:
                                            if len(date_str) > 50:
                                                continue

                                            # Absolute First
                                            dt = parse_date_robust(date_str)
                                            if dt:
                                                best_dt = dt
                                                break

                                            # Relative Fallback
                                            now = datetime.datetime.now()
                                            if (
                                                re.search(
                                                    r"^\d+\s*(h|m|s|min|hr|mins|hrs)$",
                                                    date_str,
                                                )
                                                or "Just now" in date_str
                                            ):
                                                best_dt = now
                                            elif "Yesterday" in date_str:
                                                best_dt = now - datetime.timedelta(
                                                    days=1
                                                )
                                            elif re.search(
                                                r"^(\d+)\s*d", date_str
                                            ):  # 2d
                                                v = int(
                                                    re.search(
                                                        r"^(\d+)\s*d", date_str
                                                    ).group(1)
                                                )
                                                best_dt = now - datetime.timedelta(
                                                    days=v
                                                )
                                            elif re.search(
                                                r"^(\d+)\s*w", date_str
                                            ):  # 1w
                                                v = int(
                                                    re.search(
                                                        r"^(\d+)\s*w", date_str
                                                    ).group(1)
                                                )
                                                best_dt = now - datetime.timedelta(
                                                    weeks=v
                                                )
                                            elif re.search(
                                                r"^(\d+)\s*y", date_str
                                            ):  # 1y
                                                v = int(
                                                    re.search(
                                                        r"^(\d+)\s*y", date_str
                                                    ).group(1)
                                                )
                                                best_dt = now - datetime.timedelta(
                                                    days=v * 365
                                                )

                                            if best_dt:
                                                break
                                        if best_dt:
                                            break

                                if best_dt and best_dt.year >= 2004:
                                    old_result["Last Post (DD-MM-YYYY) (Optional)"] = (
                                        best_dt.strftime("%d-%m-%Y")
                                    )
                                    if (datetime.datetime.now() - best_dt).days <= 180:
                                        old_result["Active (Yes / No)"] = "Yes"
                                    found_post = True
                                    break

                            if not found_post:
                                error_comments.append("No posts found")
                        else:
                            error_comments.append("NoFeed/MainFound")

                    # MOBILE FALLBACK
                    if not old_result["Last Post (DD-MM-YYYY) (Optional)"]:
                        try:
                            context_mobile = await browser.new_context(
                                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                                viewport={"width": 390, "height": 844},
                            )
                            page_mobile = await context_mobile.new_page()
                            await page_mobile.goto(
                                url, wait_until="networkidle", timeout=30000
                            )
                            await asyncio.sleep(3)

                            if await page_mobile.locator("article").count() > 0:
                                m_links = (
                                    await page_mobile.locator("article")
                                    .first.locator("a")
                                    .all()
                                )
                                for m_link in m_links[:5]:
                                    txt = await m_link.inner_text()
                                    if not txt:
                                        continue

                                    dt = parse_date_robust(txt)
                                    if not dt:
                                        now = datetime.datetime.now()
                                        if (
                                            re.search(r"^\d+\s*(h|m|d|min|hr|w)$", txt)
                                            or "Just now" in txt
                                        ):
                                            dt = now
                                        elif "Yesterday" in txt:
                                            dt = now - datetime.timedelta(days=1)

                                    if dt and dt.year >= 2004:
                                        old_result[
                                            "Last Post (DD-MM-YYYY) (Optional)"
                                        ] = dt.strftime("%d-%m-%Y")
                                        if (datetime.datetime.now() - dt).days <= 180:
                                            old_result["Active (Yes / No)"] = "Yes"
                                        break
                            await context_mobile.close()
                        except Exception as ex:
                            error_comments.append(f"MobileFallErr: {str(ex)[:10]}")

                except Exception as e:
                    error_comments.append(f"Act-Err: {str(e)[:20]}")

                # 7. Screenshot
                try:
                    await _handle_blocking_popups(page)
                    await page.evaluate("window.scrollTo(0, 500)")
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.evaluate(
                        "() => { window.requestAnimationFrame(() => {}); }"
                    )

                    screenshot_bytes = None
                    try:
                        content_el = await page.query_selector('div[role="main"]')
                        if content_el:
                            bbox = await content_el.bounding_box()
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
                                )
                    except:
                        pass

                    if not screenshot_bytes:
                        screenshot_bytes = await page.screenshot(
                            full_page=False, type="jpeg", quality=85
                        )

                    old_result["Screenshot"] = screenshot_bytes

                except Exception as e:
                    old_result["Screenshot"] = None

                # Download profile picture via a new browser tab (shares cookies)
                # In-page fetch() is blocked by CORS; navigating a new tab works
                if (
                    old_result.get("profile_picture")
                    and "placeholder" not in old_result["profile_picture"]
                    and page
                    and not page.is_closed()
                ):
                    img_page = None
                    try:
                        img_page = await page.context.new_page()
                        img_resp = await img_page.goto(
                            old_result["profile_picture"],
                            wait_until="load",
                            timeout=15000,
                        )
                        if img_resp and img_resp.ok:
                            img_bytes = await img_resp.body()
                            if img_bytes and len(img_bytes) > 100:
                                old_result["profile_picture_b64"] = base64.b64encode(
                                    img_bytes
                                ).decode("utf-8")
                    except Exception:
                        pass
                    finally:
                        if img_page and not img_page.is_closed():
                            await img_page.close()

            except Exception as e:
                error_comments.append(f"Critical error: {type(e).__name__}")

            finally:
                if page and not page.is_closed():
                    await page.close()
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()

            # =========================================================================
            # MAP OLD DICTIONARY TO NEW PROFILE RESULT TYPE
            # =========================================================================
            if not old_result["Profile name"]:
                old_result["Profile name"] = "Scrape Incomplete"

            result.display_name = old_result["Profile name"]
            result.has_name_match = old_result["Name (Yes / No)"] == "Yes"
            result.followers = old_result["Followers"]
            result.location = old_result["Location"]

            # Explicit logo logic matching old code
            if (
                old_result.get("profile_picture")
                and "placeholder" not in old_result["profile_picture"]
            ):
                result.has_logo = True
                result.profile_image_url = old_result["profile_picture"]
                # Use the base64 data downloaded via browser context (before close)
                if old_result.get("profile_picture_b64"):
                    result.profile_image_b64 = old_result["profile_picture_b64"]
            else:
                result.has_logo = False

            result.created_at = old_result.get("Created Date", "")
            result.last_post_date = old_result.get(
                "Last Post (DD-MM-YYYY) (Optional)", ""
            )
            result.is_active = old_result["Active (Yes / No)"] == "Yes"
            result.priority = old_result.get("priority", "Low")

            if old_result["Screenshot"]:
                result.screenshot_b64 = base64.b64encode(
                    old_result["Screenshot"]
                ).decode("utf-8")

            result.comments = ""

            # Calculate Risk using the exact old tool logic ported to new variables
            self._calculate_risk(result)

            await self.health.record_request("facebook", success=True)
            return result

    def _calculate_risk(self, result: ProfileResult):
        """Calculate risk score 3-9 using old tool's exact logic."""
        has_name = result.has_name_match
        has_logo = result.has_logo
        has_location = bool(
            result.location and result.location.lower() not in ("nan", "none", "")
        )
        followers = result.followers or 0

        now = datetime.datetime.now()

        def get_months_ago(date_str):
            if not date_str or str(date_str).lower() in ("nan", "none", "", "no"):
                return 999
            try:
                parts = str(date_str).split("-")
                if len(parts) == 2:
                    dt = datetime.datetime.strptime(date_str, "%m-%Y")
                else:
                    dt = datetime.datetime.strptime(date_str, "%d-%m-%Y")
                return (now.year - dt.year) * 12 + (now.month - dt.month)
            except Exception:
                return 999

        created_months = get_months_ago(result.created_at)
        posted_months = get_months_ago(result.last_post_date)

        is_new = created_months <= 6
        is_very_new = created_months <= 1
        is_active_post = posted_months <= 6

        # Active: use post date if available, else fallback to new account
        result.is_active = is_active_post or is_new
        result.priority = "High" if has_logo else "Low"

        score = 0
        if (
            has_name
            and has_logo
            and is_new
            and result.is_active
            and has_location
            and followers > 100
        ):
            score = 9
        elif (
            has_name and has_logo and result.is_active and has_location and is_very_new
        ):
            score = 8
        elif has_name and has_logo and result.is_active and has_location:
            score = 7
        elif has_name and has_logo and (result.is_active or is_new):
            score = 7
        elif has_name and has_logo:
            score = 6
        elif has_name and is_new:
            score = 4
        elif has_name:
            score = 3

        result.risk_score = score

    def _extract_username(self, url: str) -> Optional[str]:
        """Extract username/user ID from Facebook URL."""
        if not url:
            return None
        # Clean the URL first
        clean = url.split("?")[0].rstrip("/")
        # Reject non-facebook URLs
        if "facebook.com" not in clean:
            return None
        # Try profile.php?id= pattern
        id_match = re.search(r"profile\.php\?id=(\d+)", url)
        if id_match:
            return id_match.group(1)
        # Get the last path segment
        parts = clean.split("/")
        if len(parts) < 4:
            return None
        candidate = parts[-1]
        # Reject common non-profile segments
        reject_patterns = [
            "search",
            "stories",
            "photo",
            "groups",
            "events",
            "pages",
            "marketplace",
            "watch",
            "gaming",
            "login",
            "recover",
            "checkpoint",
            "help",
            "settings",
            "privacy",
            "policies",
            "rsrc",
            "static",
            "ajax",
            "api",
            "graphql",
            "bundle",
            "worker",
            "manifest",
            "sw",
            "serviceworker",
        ]
        if any(p in candidate.lower() for p in reject_patterns):
            return None
        # Reject if it looks like a JS/CSS file
        if re.search(
            r"\.(js|css|png|jpg|gif|woff|svg|bundle)$", candidate, re.IGNORECASE
        ):
            return None
        # Reject overly long strings (likely not a username)
        if len(candidate) > 50:
            return None
        # Must look like a valid Facebook username: letters, numbers, dots
        if re.match(r"^[a-zA-Z0-9.]+$", candidate):
            return candidate
        return None
