import asyncio
import json
import random
import time
from urllib.parse import quote
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.stealth.fingerprint import DeviceProfileManager
from backend.stealth.headers import HeaderManager
from backend.stealth.human import HumanBehavior
from backend.platforms.base import AbstractDiscoverer

logger = get_logger("platforms.instagram.discovery")


class InstagramDiscoverer(AbstractDiscoverer):
    """
    Android Private API Emulation Layer.
    Bypasses aggressive GraphQL rate-limiting by masquerading as a legacy Instagram Android native client (v155.0.0.37.107).
    This endpoint (`api/v1/users/search`) requires a valid hijacked `sessionid` but returns highly structured, 
    low-latency JSON without triggering DOM bot-detection heuristics.
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

        # Exact Headers from User Request (Legacy)
        legacy_headers = {
            "User-Agent": "Instagram 155.0.0.37.107 Android",
            "X-IG-App-ID": "936619743392459",
            "Accept": "*/*",
            "Accept-Language": "en-US",
            "X-Requested-With": "XMLHttpRequest",
            "X-IG-WWW-Claim": "0",
            "X-ASBD-ID": "198387",
        }

        # Try to get session cookies
        session_id, _ = self._load_session_cookies()
        if not session_id:
            logger.error(
                "Authentication Required for Instagram API. Please log in via Sidebar."
            )
            await progress_callback(
                event_type="error",
                message="Authentication Required. Please log in via Sidebar (Interactive) to get sessionid.",
                count_found=0,
            )
            return results

        import requests

        session = requests.Session()
        session.headers.update(legacy_headers)
        session.headers.update({"Cookie": f"sessionid={session_id}"})

        for keyword in keywords:
            if self.health.should_pause("instagram"):
                delay = self.health.get_recommended_delay("instagram")
                await progress_callback(
                    event_type="rate_limited",
                    message=f"Rate limit approaching, pausing {delay:.0f}s",
                    count_found=len(results),
                )
                await asyncio.sleep(delay)

            await progress_callback(
                event_type="progress",
                message=f"Searching Instagram API for '{keyword}'...",
                count_found=len(results),
                count_total=max_results * len(keywords),
            )

            try:
                found = await self._search_keyword_legacy(
                    session,
                    keyword,
                    max_results,
                    client,
                    session_id,
                    progress_callback,
                    len(results),
                    max_results * len(keywords),
                )

                for profile in found:
                    results.append(profile)

            except Exception as e:
                logger.error(f"Error in processing keyword {keyword}: {e}")

        return results

    async def _search_keyword_legacy(
        self,
        session,
        keyword: str,
        target_limit: int,
        client_name: str,
        session_id: str,
        progress_callback,
        current_total: int,
        max_total: int,
    ) -> list[ProfileResult]:
        """
        API Gateway Execution Cycle.
        Performs synchronous network calls mapped into the `asyncio.to_thread` execution pool. 
        Filters the raw JSON graph to construct unified `ProfileResult` identities.
        """
        profiles = []

        try:
            url = f"https://i.instagram.com/api/v1/users/search/?q={quote(keyword)}"

            # Retry with exponential backoff on rate-limit / auth errors
            response = None
            for attempt in range(3):
                response = await asyncio.to_thread(session.get, url)
                logger.info(f"API Search Status Code: {response.status_code} (attempt {attempt + 1})")

                if response.status_code == 200:
                    break

                # Challenge redirect detection
                if response.status_code in (302, 301) or "/challenge/" in response.url:
                    logger.error("Instagram challenge detected — session may need refresh. Re-login via Sidebar.")
                    await progress_callback(
                        event_type="error",
                        message="Instagram challenge detected. Please re-login via Sidebar (Interactive) to refresh session.",
                        count_found=current_total,
                    )
                    return profiles

                if response.status_code in (429, 401, 403):
                    backoff = (2 ** attempt) * random.uniform(5, 10)
                    logger.warning(f"Rate limited / auth error ({response.status_code}). Backing off {backoff:.0f}s...")
                    await progress_callback(
                        event_type="rate_limited",
                        message=f"Instagram API returned {response.status_code}, backing off {backoff:.0f}s",
                        count_found=current_total,
                    )
                    await asyncio.sleep(backoff)
                else:
                    break  # Unknown error, don't retry

            if not response or response.status_code != 200:
                logger.error(
                    "Instagram may have blocked the API request or the session is invalid."
                )
                return profiles

            data = response.json()
            users = data.get("users", [])

            if not users:
                logger.info("No users found in API.")
                return profiles

            current_count = 0

            for index, user_wrap in enumerate(users):
                if current_count >= target_limit:
                    break

                try:
                    user = user_wrap.get(
                        "user", user_wrap
                    )  # Sometimes wrapped in 'user' key

                    # Check filter
                    username = user.get("username", "").lower()
                    full_name = user.get("full_name", "").lower()
                    if keyword.lower() not in username and keyword.lower() not in full_name:
                        continue

                    # Fetch detailed info (Async)
                    user_info = await self._get_user_info_legacy(session, username)
                    bio = user_info.get("biography", "")
                    followers = user_info.get(
                        "follower_count", user.get("follower_count", 0)
                    )

                    created_at = ""
                    try:
                        import datetime

                        timeline = user_info.get("edge_owner_to_timeline_media", {}).get(
                            "edges", []
                        )
                        if timeline:
                            ts = timeline[0].get("node", {}).get("taken_at_timestamp")
                            if ts:
                                created_at = datetime.datetime.fromtimestamp(ts).strftime(
                                    "%d-%m-%Y"
                                )
                    except:
                        pass

                    # --- Robust Image Extraction & Download Pipeline ---
                    import re
                    import base64
                    import requests as req_lib
                
                    candidate_urls = []
                    # 1. HD URL from detailed profile info
                    hd_url = user_info.get("profile_pic_url_hd")
                    if hd_url: candidate_urls.append(hd_url)
                
                    # 2. HD nested info
                    hd_info = user_info.get("hd_profile_pic_url_info", {})
                    if isinstance(hd_info, dict) and hd_info.get("url"):
                        candidate_urls.append(hd_info["url"])
                
                    # 3. HD versions list
                    for v in user_info.get("hd_profile_pic_versions", []):
                        if isinstance(v, dict) and v.get("url"):
                            candidate_urls.append(v["url"])
                
                    # 4. Standard URLs from detailed info & search results
                    for k in ["profile_pic_url", "profile_pic_url_medium", "profile_pic_url_small"]:
                        val = user_info.get(k) or user.get(k)
                        if val: candidate_urls.append(val)
                
                    # Deduplicate
                    unique_urls = []
                    for u in candidate_urls:
                        if u and u not in unique_urls: unique_urls.append(u)
                
                    logger.info(f"@{username}: {len(unique_urls)} candidate image URLs collected")
                
                    profile_picture_b64 = None
                    profile_pic_url = unique_urls[0] if unique_urls else ""
                
                    # Download with Browser Headers
                    for idx, try_url in enumerate(unique_urls):
                        if profile_picture_b64: break
                        try:
                            logger.info(f"  [{idx+1}/{len(unique_urls)}] Downloading image from: {try_url[:60]}...")
                            # Use requests directly in thread to avoid any aiohttp session issues for CDN
                            img_resp = await asyncio.to_thread(
                                lambda: req_lib.get(
                                    try_url,
                                    headers={
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                                        "Accept-Language": "en-US,en;q=0.9",
                                        "Referer": "https://www.instagram.com/",
                                        "Sec-Fetch-Dest": "image",
                                        "Sec-Fetch-Mode": "no-cors",
                                        "Sec-Fetch-Site": "cross-site",
                                    },
                                    timeout=8
                                )
                            )
                        
                            if img_resp.status_code == 200 and len(img_resp.content) > 500:
                                profile_picture_b64 = base64.b64encode(img_resp.content).decode("utf-8")
                                profile_pic_url = try_url
                                logger.info(f"  ✅ SUCCESS: @{username} image saved ({len(img_resp.content)} bytes)")
                            else:
                                logger.warning(f"  ❌ FAIL: HTTP {img_resp.status_code}, length {len(img_resp.content)}")
                        except Exception as e:
                            logger.error(f"  ⚠️ ERROR: @{username} download failed: {str(e)}")

                    full_url = f"https://www.instagram.com/{username}/"
                
                    # Standardize to always have trailing slash for deduplication 
                    # or always have NO trailing slash. The DB uses the exact string. Let's use with trailing slash.
                    full_name_text = user.get("full_name", "") or username

                    confidence = "LOW"
                    name_lower = full_name_text.lower()
                    kw_lower = keyword.lower()
                    kw_words = kw_lower.split()
                    if kw_lower in name_lower:
                        confidence = "HIGH"
                    elif any(w in name_lower for w in kw_words if len(w) > 2):
                        confidence = "MEDIUM"
                    if confidence == "LOW" and bio:
                        bio_lower = bio.lower()
                        if kw_lower in bio_lower:
                            confidence = "MEDIUM"
                        elif any(w in bio_lower for w in kw_words if len(w) > 2):
                            confidence = "MEDIUM"

                    profile_result = ProfileResult(
                        platform="instagram",
                        client_name=client_name,
                        keyword=keyword,
                        url=full_url,
                        username=username,
                        display_name=full_name_text,
                        bio=bio,
                        followers=followers,
                        is_verified=user.get("is_verified", False),
                        created_at=created_at,
                        profile_image_url=profile_pic_url,
                        profile_image_b64=profile_picture_b64,
                        confidence=confidence,
                    )

                    profiles.append(profile_result)
                    current_count += 1

                    await progress_callback(
                        event_type="result_found",
                        message=f"Found: @{username} ({confidence}% match)",
                        count_found=current_total + current_count,
                        count_total=max_total,
                        result=profile_result.to_dict(),
                    )

                    # Randomized delay to avoid bot-pattern detection
                    await asyncio.sleep(random.uniform(4, 8))

                except Exception as e:
                    logger.error(f"Error processing user {index}: {e}")
                    continue
            await self.health.record_request("instagram", success=True)

        except Exception as e:
            logger.error(f"Error searching for similar profiles: {e}")
            await self.health.record_request("instagram", success=False)

        return profiles

    async def _get_user_info_legacy(self, session, username: str) -> dict:
        """
        Secondary Hydration Pipeline.
        Hits the web endpoints to pull auxiliary timeline metrics not exposed by the base `v1/users/search` endpoint.
        """
        try:
            url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={quote(username)}"
            response = await asyncio.to_thread(session.get, url)

            if response.status_code == 200:
                data = response.json()
                user_data = data.get("data", {}).get("user", {})
                # Debug: log available image keys
                img_keys = [k for k in user_data.keys() if "pic" in k.lower() or "image" in k.lower() or "photo" in k.lower()]
                logger.info(f"@{username} web_profile_info keys with 'pic/image': {img_keys}")
                return user_data
            else:
                logger.warning(f"@{username} web_profile_info returned status {response.status_code}")
            return {}
        except Exception as e:
            logger.error(f"Error fetching user info: {e}")
            return {}

    def _load_session_cookies(self) -> tuple[str, str]:
        """
        File-System Session Ingestion.
        Deserializes Playwright session states to hijack the `sessionid` and `csrftoken` cookies, 
        injecting them into standard Python `requests` objects to bypass interactive authentication walls.
        """
        import os

        session_path = os.path.join(self.config.SESSION_PATH, "instagram.json")

        if not os.path.exists(session_path):
            return "", ""

        try:
            with open(session_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            session_id = ""
            csrf_token = ""
            for cookie in state.get("cookies", []):
                if cookie.get("name") == "sessionid":
                    session_id = cookie.get("value", "")
                elif cookie.get("name") == "csrftoken":
                    csrf_token = cookie.get("value", "")

            return session_id, csrf_token

        except Exception as exc:
            logger.warning(f"Failed to load Instagram session cookies: {exc}")
            return "", ""
