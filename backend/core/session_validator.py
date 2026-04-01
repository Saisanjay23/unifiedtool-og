"""
Session Validator — Cached, lightweight HTTP session verification.

Detects server-side session invalidation by making minimal HTTP requests
to each platform using the saved cookies. Results are cached for 5 minutes
to avoid excessive requests.

Usage:
    validator = SessionValidator()
    result = await validator.validate("facebook")
    # => {"valid": True, "checked_at": "2026-03-24T14:30:00+00:00", "reason": "ok"}
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import requests as req_lib

from backend.core.config import settings
from backend.core.logger import get_logger

logger = get_logger("session_validator")

# Cache TTL in seconds (5 minutes)
CACHE_TTL = 300

# HTTP request timeout in seconds
REQUEST_TIMEOUT = 5

# User-Agent to mimic a real browser
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Platform-specific validation config:
# url: a page that requires login (redirects to login page if session is dead)
# check_type: "redirect" (check if response URL contains login path) or "content" (check response body)
# fail_indicators: URL fragments or body strings that indicate session is expired
PLATFORM_VALIDATORS = {
    "facebook": {
        "url": "https://www.facebook.com/me",
        "check_type": "redirect",
        "fail_indicators": ["/login", "/checkpoint", "login_attempt"],
    },
    "instagram": {
        "url": "https://www.instagram.com/accounts/edit/",
        "check_type": "redirect",
        "fail_indicators": ["/accounts/login", "/challenge"],
    },
    "twitter": {
        "url": "https://x.com/settings/account",
        "check_type": "redirect",
        "fail_indicators": ["/i/flow/login", "/login", "/account/access"],
    },
    # YouTube uses API key, not browser cookies — validated separately
    "youtube": None,
    # Telegram uses API-based auth; no HTTP cookie validation needed
    "telegram": None,
    "tiktok": {
        "url": "https://www.tiktok.com/setting",
        "check_type": "redirect",
        "fail_indicators": ["/login", "/signup"],
    },
}


class SessionValidator:
    """Singleton-pattern session validator with per-platform result caching."""

    _instance = None
    _cache: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._cache = {}
        return cls._instance

    def invalidate(self, platform: str):
        """Clear cached validation result for a platform (call after import/clear)."""
        self._cache.pop(platform, None)
        logger.debug(f"Validation cache invalidated for {platform}")

    def invalidate_all(self):
        """Clear all cached validation results."""
        self._cache.clear()

    async def validate(self, platform: str) -> dict:
        """
        Validate a platform session. Returns cached result if within TTL.

        Returns:
            dict with keys: valid (bool), checked_at (str), reason (str)
        """
        platform = platform.lower()

        # Skip unsupported platforms (YouTube uses API key, Telegram uses API auth)
        if platform not in PLATFORM_VALIDATORS or PLATFORM_VALIDATORS[platform] is None:
            # For YouTube, validate the API key instead
            if platform == "youtube":
                return await self._validate_youtube_api_key()
            return {
                "valid": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "reason": "skip",
            }

        # Check cache
        cached = self._cache.get(platform)
        if cached and (time.time() - cached["_ts"]) < CACHE_TTL:
            return {
                "valid": cached["valid"],
                "checked_at": cached["checked_at"],
                "reason": cached["reason"],
            }

        # Run validation in thread pool to avoid blocking
        try:
            result = await asyncio.to_thread(self._validate_sync, platform)
        except Exception as e:
            logger.warning(f"Session validation error for {platform}: {e}")
            # On error, assume valid (no false negatives from network issues)
            result = {
                "valid": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "reason": "validation_error",
            }

        # Cache the result
        self._cache[platform] = {**result, "_ts": time.time()}
        return result

    def _validate_sync(self, platform: str) -> dict:
        """Synchronous HTTP validation (runs in thread pool)."""
        config = PLATFORM_VALIDATORS[platform]
        now_str = datetime.now(timezone.utc).isoformat()

        # Load cookies from session file
        session_file = os.path.join(settings.SESSION_PATH, f"{platform}.json")
        if not os.path.exists(session_file):
            return {"valid": False, "checked_at": now_str, "reason": "no_session_file"}

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {"valid": False, "checked_at": now_str, "reason": "corrupt_session_file"}

        cookies_list = data.get("cookies", [])
        if not cookies_list:
            return {"valid": False, "checked_at": now_str, "reason": "no_cookies"}

        # Build a requests cookie jar from the Playwright session cookies
        jar = req_lib.cookies.RequestsCookieJar()
        for c in cookies_list:
            name = c.get("name", "")
            value = c.get("value", "")
            domain = c.get("domain", "")
            path = c.get("path", "/")
            if name and value:
                jar.set(name, value, domain=domain, path=path)

        # Make the validation request
        try:
            resp = req_lib.get(
                config["url"],
                cookies=jar,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except req_lib.exceptions.Timeout:
            logger.debug(f"Validation request timed out for {platform}")
            return {"valid": True, "checked_at": now_str, "reason": "timeout"}
        except req_lib.exceptions.ConnectionError:
            logger.debug(f"Validation connection error for {platform}")
            return {"valid": True, "checked_at": now_str, "reason": "connection_error"}
        except Exception as e:
            logger.debug(f"Validation request failed for {platform}: {e}")
            return {"valid": True, "checked_at": now_str, "reason": "request_error"}

        # Check the final URL after redirects
        final_url = resp.url.lower()
        for indicator in config["fail_indicators"]:
            if indicator.lower() in final_url:
                logger.info(
                    f"Session EXPIRED for {platform}: "
                    f"redirected to {final_url} (matched '{indicator}')"
                )
                return {
                    "valid": False,
                    "checked_at": now_str,
                    "reason": f"redirected_to_login",
                }

        # Additional check: some platforms return 401/403 instead of redirecting
        if resp.status_code in (401, 403):
            logger.info(f"Session EXPIRED for {platform}: got HTTP {resp.status_code}")
            return {
                "valid": False,
                "checked_at": now_str,
                "reason": f"http_{resp.status_code}",
            }

        logger.debug(f"Session VALID for {platform} (HTTP {resp.status_code}, url={final_url[:80]})")
        return {"valid": True, "checked_at": now_str, "reason": "ok"}

    async def _validate_youtube_api_key(self) -> dict:
        """Validate YouTube API key by making a lightweight Data API call."""
        now_str = datetime.now(timezone.utc).isoformat()

        if not settings.YOUTUBE_API_KEY:
            return {"valid": False, "checked_at": now_str, "reason": "no_api_key"}

        # Check cache
        cached = self._cache.get("youtube")
        if cached and (time.time() - cached["_ts"]) < CACHE_TTL:
            return {
                "valid": cached["valid"],
                "checked_at": cached["checked_at"],
                "reason": cached["reason"],
            }

        try:
            result = await asyncio.to_thread(self._validate_youtube_sync)
        except Exception as e:
            logger.warning(f"YouTube API key validation error: {e}")
            result = {"valid": True, "checked_at": now_str, "reason": "validation_error"}

        self._cache["youtube"] = {**result, "_ts": time.time()}
        return result

    def _validate_youtube_sync(self) -> dict:
        """Check YouTube API key validity with a minimal API call."""
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            resp = req_lib.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "id", "chart": "mostPopular", "maxResults": 1, "key": settings.YOUTUBE_API_KEY},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                logger.debug("YouTube API key is valid")
                return {"valid": True, "checked_at": now_str, "reason": "ok"}
            elif resp.status_code in (400, 403):
                logger.info(f"YouTube API key invalid or quota exceeded: HTTP {resp.status_code}")
                return {"valid": False, "checked_at": now_str, "reason": f"api_key_invalid_{resp.status_code}"}
            else:
                return {"valid": True, "checked_at": now_str, "reason": "unknown_response"}
        except req_lib.exceptions.Timeout:
            return {"valid": True, "checked_at": now_str, "reason": "timeout"}
        except Exception as e:
            logger.debug(f"YouTube API key validation failed: {e}")
            return {"valid": True, "checked_at": now_str, "reason": "request_error"}
