import asyncio
import base64
import datetime
import random
import re
import os
import io
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from googleapiclient.discovery import build

logger = get_logger("platforms.youtube.analysis")


class YouTubeAnalyzer(AbstractAnalyzer):
    """
    Deep-analyzes YouTube channels using the EXACT 20-year principal engineer
    legacy logic. Wraps `async_scrape_channel` to output `ProfileResult`.
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
            logger.info(f"Starting YouTube analysis for URL: {url} (Client: {client})")

            # --- START EXACT LEGACY CODE (Wrapped) ---
            def get_default_result_dict(url, profile_name="Scrape Failed", comments=""):
                return {
                    "Original Name": "",
                    "Original feed": "",
                    "IMPERSONATED": url,
                    "Profile name": profile_name,
                    "Created Date": "",
                    "Logo (Yes / No)": "No",
                    "Subscribers": 0,
                    "Active (Yes / No)": "No",
                    "Name (Yes / No)": "No",
                    "Location": "",
                    "Last Video (DD-MM-YYYY) (Optional)": "",
                    "Risk Score": 0,
                    "priority": "Low",
                    "Date": datetime.datetime.now().strftime("%d-%m-%Y"),
                    "Comments": comments,
                    "Screenshot": None,
                    "Validate": False,
                    "profile_picture": "",
                    "Bio": "",
                }

            def parse_count(s):
                if isinstance(s, (int, float)):
                    return int(s)
                s = str(s).lower().replace(",", "").strip()
                try:
                    if "k" in s:
                        return int(float(s.replace("k", "")) * 1000)
                    elif "m" in s:
                        return int(float(s.replace("m", "")) * 1_000_000)
                    elif "b" in s:
                        return int(float(s.replace("b", "")) * 1_000_000_000)
                    numeric = re.sub(r"[^0-9.]", "", s)
                    return int(float(numeric)) if numeric else 0
                except:
                    return 0

            def calculate_risk(row):
                try:
                    has_name = str(row.get("Name (Yes / No)", "No")).lower() == "yes"
                    has_logo = str(row.get("Logo (Yes / No)", "No")).lower() == "yes"
                    location_txt = str(row.get("Location", "")).strip()
                    has_location = bool(location_txt) and location_txt.lower() != "nan"

                    subscribers_str = str(row.get("Subscribers", "0"))
                    try:
                        subscribers = int(float(str(subscribers_str).replace(",", "")))
                    except:
                        subscribers = 0

                    now = datetime.datetime.now(datetime.timezone.utc)

                    def get_months_ago(date_str):
                        if not date_str or str(date_str).lower() in [
                            "nan",
                            "none",
                            "",
                            "no",
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
                        row.get("Last Video (DD-MM-YYYY) (Optional)")
                    )

                    is_new_account = created_months <= 6

                    priority = "High" if has_logo else "Low"
                    active_btn_val = str(row.get("Active (Yes / No)", "No")).lower()
                    is_truly_active = active_btn_val.startswith("y") or is_new_account

                    if (
                        has_name
                        and has_logo
                        and is_new_account
                        and is_truly_active
                        and has_location
                        and subscribers > 100
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

            def get_youtube_service():
                return build("youtube", "v3", developerKey=self.config.YOUTUBE_API_KEY)

            def extract_channel_handle_or_id(url):
                url = url.strip().rstrip("/")
                if "@" in url:
                    return "handle", url.split("@")[-1]
                elif "/channel/" in url:
                    return "id", url.split("/channel/")[-1]
                elif "/user/" in url:
                    return "username", url.split("/user/")[-1]
                elif "/c/" in url:
                    return "custom", url.split("/c/")[-1]
                else:
                    return "unknown", url.split("/")[-1]

            async def get_channel_id(service, id_type, value):
                try:
                    if id_type == "id":
                        return value

                    def _sync_api():
                        if id_type == "handle":
                            try:
                                req = service.channels().list(
                                    part="id", forHandle="@" + value
                                )
                                res = req.execute()
                                if res.get("items"):
                                    return res["items"][0]["id"]
                            except:
                                pass

                            req = service.channels().list(part="id", forHandle=value)
                            res = req.execute()
                            if res.get("items"):
                                return res["items"][0]["id"]

                        elif id_type == "username":
                            req = service.channels().list(part="id", forUsername=value)
                            res = req.execute()
                            if res.get("items"):
                                return res["items"][0]["id"]

                        req = service.search().list(
                            part="snippet", type="channel", q=value, maxResults=1
                        )
                        res = req.execute()
                        if res.get("items"):
                            return res["items"][0]["snippet"]["channelId"]

                        return None

                    return await asyncio.to_thread(_sync_api)
                except Exception as e:
                    logger.error(f"Error resolving ID for {value}: {e}")
                    return None

            old_result = get_default_result_dict(url, profile_name="")
            error_comments = []

            service = get_youtube_service()
            channel_id = None

            try:
                id_type, value = extract_channel_handle_or_id(url)
                channel_id = await get_channel_id(service, id_type, value)
            except:
                pass

            try:
                pw, browser, context, page = await create_stealth_browser(
                    platform="youtube", headless=headless
                )

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(2)

                    if not channel_id:
                        try:
                            content = await page.content()
                            match = re.search(
                                r'itemprop="channelId" content="([^"]+)"', content
                            )
                            if match:
                                channel_id = match.group(1)
                            else:
                                match_url = re.search(
                                    r'link rel="canonical" href="https://www.youtube.com/channel/([^"]+)"',
                                    content,
                                )
                                if match_url:
                                    channel_id = match_url.group(1)
                        except Exception as e:
                            error_comments.append(f"Scraping ID failed: {e}")

                    if channel_id:
                        try:

                            def _fetch_channel_data():
                                request = service.channels().list(
                                    part="snippet,statistics,contentDetails,brandingSettings",
                                    id=channel_id,
                                )
                                response = request.execute()

                                ret_data = {}
                                if response.get("items"):
                                    data = response["items"][0]
                                    ret_data["snippet"] = data.get("snippet", {})
                                    ret_data["stats"] = data.get("statistics", {})
                                    ret_data["branding"] = data.get(
                                        "brandingSettings", {}
                                    )

                                    uploads_playlist_id = (
                                        data.get("contentDetails", {})
                                        .get("relatedPlaylists", {})
                                        .get("uploads")
                                    )
                                    if uploads_playlist_id:
                                        try:
                                            pl_request = service.playlistItems().list(
                                                part="snippet",
                                                playlistId=uploads_playlist_id,
                                                maxResults=1,
                                            )
                                            pl_response = pl_request.execute()
                                            if pl_response.get("items"):
                                                ret_data["last_video"] = pl_response[
                                                    "items"
                                                ][0]["snippet"]
                                        except:
                                            pass
                                return ret_data

                            api_data = await asyncio.to_thread(_fetch_channel_data)

                            if api_data:
                                snippet = api_data.get("snippet", {})
                                stats = api_data.get("stats", {})
                                branding = api_data.get("branding", {})
                                last_video = api_data.get("last_video")

                                old_result["Profile name"] = snippet.get(
                                    "title", "Unknown"
                                )
                                if old_result["Profile name"]:
                                    old_result["Name (Yes / No)"] = "Yes"

                                old_result["Bio"] = snippet.get("description", "")

                                published_at = snippet.get("publishedAt")
                                if published_at:
                                    try:
                                        dt = datetime.datetime.fromisoformat(
                                            published_at.replace("Z", "+00:00")
                                        )
                                        old_result["Created Date"] = dt.strftime(
                                            "%d-%m-%Y"
                                        )
                                    except:
                                        pass

                                sub_count = stats.get("subscriberCount", 0)
                                if not stats.get("hiddenSubscriberCount"):
                                    old_result["Subscribers"] = int(sub_count)

                                country_code = snippet.get("country") or (
                                    branding.get("channel") or {}
                                ).get("country")
                                if country_code:
                                    try:
                                        import pycountry

                                        country = pycountry.countries.get(
                                            alpha_2=country_code
                                        )
                                        old_result["Location"] = (
                                            country.name if country else country_code
                                        )
                                    except:
                                        old_result["Location"] = country_code

                                old_result["Logo (Yes / No)"] = "Yes"
                                thumbnails = snippet.get("thumbnails", {})
                                thumb_url = thumbnails.get("high", {}).get(
                                    "url"
                                ) or thumbnails.get("default", {}).get("url", "")
                                if thumb_url:
                                    # Upgrade to HD (800x800) by replacing =s... with =s800
                                    thumb_url = re.sub(r'=s\d+-', '=s800-', thumb_url)
                                    old_result["profile_picture"] = thumb_url

                                if last_video:
                                    p_at = last_video.get("publishedAt")
                                    if p_at:
                                        vid_dt = datetime.datetime.fromisoformat(
                                            p_at.replace("Z", "+00:00")
                                        )
                                        old_result[
                                            "Last Video (DD-MM-YYYY) (Optional)"
                                        ] = vid_dt.strftime("%d-%m-%Y")
                                        now = datetime.datetime.now(
                                            datetime.timezone.utc
                                        )
                                        if (now - vid_dt).days <= 180:
                                            old_result["Active (Yes / No)"] = "Yes"

                            else:
                                error_comments.append("Channel ID found but not in API")
                        except Exception as e:
                            error_comments.append(f"API Fetch Error: {e}")
                    else:
                        error_comments.append(
                            "Could not resolve Channel ID (API & Scrape)"
                        )

                    # Visuals
                    await page.evaluate("window.scrollTo(0, 500)")
                    await asyncio.sleep(0.5)
                    await page.evaluate("window.scrollTo(0, 0)")

                    await page.evaluate(
                        "() => { window.requestAnimationFrame(() => {}); }"
                    )

                    try:
                        await page.mouse.move(
                            random.randint(100, 800), random.randint(100, 800)
                        )
                    except:
                        pass

                    try:
                        await page.wait_for_selector(
                            "div#channel-header, ytd-channel-header-renderer",
                            state="visible",
                            timeout=10000,
                        )
                    except:
                        pass

                    screenshot_bytes = None
                    try:
                        content_el = await page.query_selector("ytd-app")
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

                    if screenshot_bytes:
                        old_result["Screenshot"] = base64.b64encode(
                            screenshot_bytes
                        ).decode("utf-8")

                    # 3. EXACT SUBSCRIBER COUNT (HTML/Regex Scan)
                    try:
                        content = await page.content()
                        patterns = [
                            r'"subscriberCount":"(\d+)"',
                            r'"subscriberCount":(\d+)',
                            r'\\"subscriberCount\\":\\"(\d+)\\"',
                        ]

                        exact_count = 0
                        for pat in patterns:
                            match = re.search(pat, content)
                            if match:
                                try:
                                    val = int(match.group(1))
                                    api_val = old_result.get("Subscribers", 0)
                                    if api_val > 0:
                                        if 0.1 < (val / api_val) < 10.0:
                                            exact_count = val
                                            break
                                    else:
                                        exact_count = val
                                        break
                                except:
                                    pass

                        if exact_count > 0:
                            old_result["Subscribers"] = exact_count
                    except Exception as e:
                        error_comments.append(f"SubCountErr: {e}")

                except Exception as e:
                    error_comments.append(f"Screenshot/Page failed: {e}")

            except Exception as e:
                error_comments.append(f"Browser Error: {e}")
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
                "youtube", success=not bool(error_comments)
            )

            # --- MAP TO NEW BACKEND FORMAT ---
            result = ProfileResult(
                platform="youtube",
                client_name=client,
                keyword="",
                url=url,
                username=channel_id or "",
            )

            result.display_name = old_result.get("Profile name", "")
            result.followers = int(old_result.get("Subscribers", 0))
            result.location = old_result.get("Location", "")
            result.created_at = old_result.get("Created Date", "")
            result.last_post_date = old_result.get(
                "Last Video (DD-MM-YYYY) (Optional)", ""
            )
            result.last_active = old_result.get(
                "Last Video (DD-MM-YYYY) (Optional)", ""
            )
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

            if old_result.get("profile_picture"):
                result.has_logo = True
                result.profile_image_url = old_result["profile_picture"]
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as dl_session:
                        async with dl_session.get(
                            result.profile_image_url,
                            headers={"User-Agent": "Mozilla/5.0"},
                        ) as resp:
                            if resp.status == 200:
                                result.profile_image_b64 = base64.b64encode(
                                    await resp.read()
                                ).decode("utf-8")
                except:
                    pass
            else:
                result.has_logo = False

            return result
