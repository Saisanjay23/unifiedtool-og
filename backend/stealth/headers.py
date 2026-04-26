"""
Platform-specific HTTP header generation for the Unified Social Media Tool.
Produces realistic browser header sets that match the device fingerprint.
Used primarily for API-based scraping (Instagram, YouTube).
"""

import random

from backend.core.logger import get_logger

logger = get_logger("stealth.headers")


# Common accept-language values weighted by frequency
ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
]

# Instagram-specific app ID (public, embedded in their frontend JS)
IG_APP_ID = "936619743392459"


class HeaderManager:
    """
    Generates realistic HTTP headers for a given platform and device profile.
    Headers rotate slightly between requests while maintaining consistency
    with the device fingerprint.
    """

    def __init__(self, platform: str, device_profile: dict):
        self.platform = platform
        self.profile = device_profile
        self._accept_lang = random.choice(ACCEPT_LANGUAGES)

    def _base_headers(self) -> dict:
        """Headers common to all platforms."""
        return {
            "User-Agent": self.profile["user_agent"],
            "Accept-Language": self._accept_lang,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "sec-ch-ua": self.profile.get("sec_ch_ua", ""),
            "sec-ch-ua-mobile": self.profile.get("sec_ch_ua_mobile", "?0"),
            "sec-ch-ua-platform": self.profile.get("sec_ch_ua_platform", '"Windows"'),
        }

    def get_headers(
        self, endpoint: str = "", csrf_token: str = "", session_id: str = ""
    ) -> dict:
        """
        Return the full header set for the current platform and endpoint.

        Args:
            endpoint: the specific API endpoint being called
            csrf_token: CSRF token if available from cookies
            session_id: session ID if available from cookies
        """
        headers = self._base_headers()

        if self.platform == "instagram":
            headers.update(self._instagram_headers(endpoint, csrf_token, session_id))
        elif self.platform == "facebook":
            headers.update(self._facebook_headers(endpoint))
        elif self.platform == "twitter":
            headers.update(self._twitter_headers(endpoint))
        elif self.platform == "youtube":
            headers.update(self._youtube_headers())
        else:
            # generic web request headers
            headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            )

        return headers

    def _instagram_headers(
        self, endpoint: str, csrf_token: str, session_id: str
    ) -> dict:
        """Instagram-specific headers mimicking the web app."""
        h = {
            "Accept": "*/*",
            "X-IG-App-ID": IG_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com",
        }

        if csrf_token:
            h["X-CSRFToken"] = csrf_token

        if session_id:
            h["Cookie"] = f"sessionid={session_id}; csrftoken={csrf_token}"

        # search endpoint uses specific headers
        if "search" in endpoint.lower() or "topsearch" in endpoint.lower():
            h["X-IG-WWW-Claim"] = "0"

        return h

    def _facebook_headers(self, endpoint: str) -> dict:
        """Facebook-specific headers."""
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _twitter_headers(self, endpoint: str) -> dict:
        """Twitter/X headers."""
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _youtube_headers(self) -> dict:
        """YouTube headers for API-style requests."""
        return {
            "Accept": "application/json, text/plain, */*",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
        }
