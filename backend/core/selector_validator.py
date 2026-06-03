"""
Selector Validator Service.
Runs the platform analyzers against known public reference accounts to verify
DOM selector and network API interception integrity in real-time.
"""

import asyncio
import re
from typing import Dict, Any

from backend.core.db import ProfileResult
from backend.core.logger import get_logger

logger = get_logger("selector_validator")

# Stable public handles used as validation references
PLATFORM_REFERENCE_URLS = {
    "facebook": "https://www.facebook.com/facebook",
    "instagram": "https://www.instagram.com/instagram",
    "twitter": "https://x.com/x",
}

class SelectorValidator:
    """Runs selector validation suites for social platform analyzers."""

    @staticmethod
    def _create_analyzer(platform: str):
        """Lazy imports and instantiates the proper platform analyzer."""
        from backend.core.config import settings
        from backend.core.health import HealthManager
        health = HealthManager()

        if platform == "facebook":
            from backend.platforms.facebook.analysis import FacebookAnalyzer
            return FacebookAnalyzer(config=settings, health=health)
        elif platform == "instagram":
            from backend.platforms.instagram.analysis import InstagramAnalyzer
            return InstagramAnalyzer(config=settings, health=health)
        elif platform == "twitter":
            from backend.platforms.twitter.analysis import TwitterAnalyzer
            return TwitterAnalyzer(config=settings, health=health)
        else:
            raise ValueError(f"Unsupported platform for selector validation: {platform}")

    async def validate_platform(self, platform: str) -> Dict[str, Any]:
        """
        Scrapes the reference profile for a platform and checks the integrity
        of the extracted metadata fields.
        """
        platform = platform.lower()
        if platform not in PLATFORM_REFERENCE_URLS:
            return {
                "platform": platform,
                "success": False,
                "error": f"No reference URL configured for platform '{platform}'"
            }

        url = PLATFORM_REFERENCE_URLS[platform]
        logger.info(f"Running selector validation for {platform} against {url}...")

        try:
            analyzer = self._create_analyzer(platform)
            # Run the scraper
            result: ProfileResult = await analyzer.analyze(url, client="SelectorValidationCheck")
            
            # Compile validation metrics
            metrics = {}
            overall_success = True

            # 1. Display Name Check
            name_val = result.display_name
            name_ok = bool(
                name_val and 
                name_val.strip() and 
                name_val not in ("Unknown", "Scrape Incomplete", "Unknown User")
            )
            metrics["display_name"] = {
                "value": name_val,
                "status": "pass" if name_ok else "fail",
                "details": "Parsed display name successfully" if name_ok else "Name resolved as default/unknown"
            }
            if not name_ok:
                overall_success = False

            # 2. Followers Check
            followers_val = result.followers or 0
            followers_ok = followers_val > 0
            metrics["followers"] = {
                "value": followers_val,
                "status": "pass" if followers_ok else "fail",
                "details": f"Parsed {followers_val} followers" if followers_ok else "Followers parsed as 0 or empty"
            }
            if not followers_ok:
                overall_success = False

            # 3. Profile Image Check (Logo Detection)
            pfp_url = result.profile_image_url
            logo_ok = bool(result.has_logo and pfp_url and "placeholder" not in pfp_url.lower())
            metrics["profile_image"] = {
                "value": pfp_url or "None",
                "status": "pass" if logo_ok else "warning",
                "details": "Profile picture logo extracted successfully" if logo_ok else "No custom logo detected (monogram or placeholder)"
            }
            # Treat as warning: does not fail the overall validation, as a profile might lack a picture or be restricted

            # 4. Created At Date Check
            created_val = result.created_at
            created_ok = bool(
                created_val and 
                created_val not in ("No", "Not Available (Restricted)", "Not Available (Instagram Restricted)", "Not Available (Twitter Restricted)", "")
            )
            # Verify date format looks like MM-YYYY or DD-MM-YYYY
            if created_ok and not re.match(r"^\d{2}-\d{4}$|^\d{2}-\d{2}-\d{4}$", created_val):
                created_ok = False
                details = f"Invalid date format: '{created_val}'"
            else:
                details = f"Account created date: {created_val}" if created_ok else "Created date is missing/restricted"

            metrics["created_at"] = {
                "value": created_val or "Not Available",
                "status": "pass" if created_ok else "fail",
                "details": details
            }
            if not created_ok:
                overall_success = False

            # 5. Last Post Date Check
            last_post_val = result.last_post_date
            last_post_ok = bool(
                last_post_val and 
                last_post_val not in ("Date Unknown", "Private Account", "")
            )
            metrics["last_post_date"] = {
                "value": last_post_val or "None",
                "status": "pass" if last_post_ok else "warning",
                "details": f"Last post date: {last_post_val}" if last_post_ok else "Last post date unavailable/unknown"
            }
            # Warnings do not fail the overall validation run

            return {
                "platform": platform,
                "url": url,
                "success": overall_success,
                "metrics": metrics,
                "error": None
            }

        except Exception as e:
            logger.error(f"Selector validation failed for {platform}: {e}", exc_info=True)
            return {
                "platform": platform,
                "url": url,
                "success": False,
                "metrics": {},
                "error": f"Scraper execution crashed: {type(e).__name__} ({str(e)})"
            }
