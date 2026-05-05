"""
Ad/tracker domain blocklist for the Unified Social Media Tool.
Blocks requests to known advertising, analytics, and tracking domains
during scraping to improve performance and reduce fingerprinting exposure.

Inspired by Scrapling's block_ads feature (~3,500 domains) but curated
to a safe subset that won't break social media platform functionality.

Usage:
    Used internally by create_stealth_browser() when block_ads=True.
"""

from urllib.parse import urlparse

from backend.core.logger import get_logger

logger = get_logger("stealth.blocklist")

# Curated ad/tracker domains — safe to block on social media platforms.
# These are analytics, advertising, and tracking services that platforms
# load but don't require for core page functionality.
# Subdomains are automatically matched (e.g., "doubleclick.net" blocks "ad.doubleclick.net").
AD_TRACKER_DOMAINS: set[str] = {
    # Google Ads & Analytics
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "analytics.google.com",
    "adservice.google.com",
    "pagead2.googlesyndication.com",
    "googletagmanager.com",
    "googletagservices.com",
    # Amazon Ads
    "amazonads.com",
    "amazon-adsystem.com",
    "aax.amazon-adsystem.com",
    # General Analytics/Tracking
    "scorecardresearch.com",
    "quantserve.com",
    "hotjar.com",
    "clarity.ms",
    "newrelic.com",
    "nr-data.net",
    "sentry.io",
    "bugsnag.com",
    # Marketing/Attribution
    "optimizely.com",
    "segment.com",
    "segment.io",
    "mixpanel.com",
    "amplitude.com",
    "heap.io",
    "heapanalytics.com",
    "branch.io",
    "app.link",
    "adjust.com",
    "appsflyer.com",
    # Ad Networks
    "moatads.com",
    "adsrvr.org",
    "criteo.com",
    "criteo.net",
    "taboola.com",
    "outbrain.com",
    "revcontent.com",
    "mgid.com",
    "adnxs.com",
    "rubiconproject.com",
    "pubmatic.com",
    "openx.net",
    "casalemedia.com",
    "indexexchange.com",
    "sharethrough.com",
    # Tracking pixels
    "bat.bing.com",
    "ct.pinterest.com",
    "snap.licdn.com",
    "linkedin.com/px",
    # Consent/Cookie management
    "cookiebot.com",
    "onetrust.com",
    "trustarc.com",
    "cookielaw.org",
}


def is_blocked_domain(url: str) -> bool:
    """
    Check if a URL's domain matches any blocked ad/tracker domain.
    Matches both exact domains and subdomains.

    Examples:
        is_blocked_domain("https://ad.doubleclick.net/pixel") → True
        is_blocked_domain("https://www.facebook.com/page") → False
    """
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False

        hostname = hostname.lower()
        for domain in AD_TRACKER_DOMAINS:
            if hostname == domain or hostname.endswith(f".{domain}"):
                return True
        return False
    except Exception:
        return False
