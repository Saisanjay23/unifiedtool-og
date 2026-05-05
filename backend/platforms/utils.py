"""
Shared utility functions for platform analyzers.
Consolidates duplicated logic (follower parsing, date parsing, risk scoring,
image downloading) into a single module for reuse across all platforms.
"""

import base64
import datetime
import re

import aiohttp

from backend.core.db import ProfileResult
from backend.core.logger import get_logger

logger = get_logger("platforms.utils")

# Default user-agent for image downloads
_DL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)


def parse_followers(s) -> int:
    """
    Normalise human-readable engagement metrics (e.g. '1.2M', '45K', '3,200')
    into strict integers.  Returns 0 on any failure.
    """
    if isinstance(s, (int, float)):
        return int(s)
    if not s:
        return 0
    s = str(s).lower().replace(",", "").strip()
    try:
        if "k" in s:
            return int(float(s.replace("k", "")) * 1_000)
        elif "m" in s:
            return int(float(s.replace("m", "")) * 1_000_000)
        elif "b" in s:
            return int(float(s.replace("b", "")) * 1_000_000_000)
        numeric = re.sub(r"[^0-9.]", "", s)
        return int(float(numeric)) if numeric else 0
    except Exception:
        return 0


def parse_date_robust(date_str) -> datetime.datetime | None:
    """
    Parse ANY date string (including relative '14 years ago') to a datetime.
    Returns None when the string cannot be parsed.
    """
    if not date_str:
        return None
    date_str = str(date_str).strip()

    low = date_str.lower()
    
    # Relative formats
    if "year" in low and "ago" in low:
        m = re.search(r"(\d+)\s+years?", low)
        if m:
            return datetime.datetime.now() - datetime.timedelta(days=365 * int(m.group(1)))
    if "month" in low and "ago" in low:
        m = re.search(r"(\d+)\s+months?", low)
        if m:
            return datetime.datetime.now() - datetime.timedelta(days=30 * int(m.group(1)))
    if "day" in low and "ago" in low:
        m = re.search(r"(\d+)\s+days?", low)
        if m:
            return datetime.datetime.now() - datetime.timedelta(days=int(m.group(1)))
            
    # Facebook short relatives: '15 hrs', '2 h', '45 mins', '10 m'
    if re.search(r"(\d+)\s*(?:hrs?|h)\b", low):
        m = re.search(r"(\d+)\s*(?:hrs?|h)\b", low)
        if m:
            return datetime.datetime.now() - datetime.timedelta(hours=int(m.group(1)))
    if re.search(r"(\d+)\s*(?:mins?|m)\b", low):
        m = re.search(r"(\d+)\s*(?:mins?|m)\b", low)
        if m:
            return datetime.datetime.now() - datetime.timedelta(minutes=int(m.group(1)))
    if "yesterday" in low:
        return datetime.datetime.now() - datetime.timedelta(days=1)
    if "just now" in low:
        return datetime.datetime.now()

    # Clean up common fluff
    clean = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str)
    clean = clean.split(" at ")[0].strip()  # "8 April at 21:25" -> "8 April"
    clean = clean.replace(",", "").replace(".", "")

    # If it's just "Day Month" (e.g. "8 April"), append current year
    if re.match(r"^\d{1,2}\s+[A-Za-z]+$", clean):
        clean += f" {datetime.datetime.now().year}"

    formats = [
        "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y",
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
        "%B %Y", "%b %Y", "%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(clean, fmt)
            if dt.year >= 2004:
                return dt
        except ValueError:
            pass
    return None


def _get_months_ago(date_str: str | None) -> int:
    """Return how many months ago a date string is.
    Accepts any format that parse_date_robust() can handle (DD-MM-YYYY,
    ISO, relative, etc.) so risk scoring works across all platforms."""
    if not date_str or str(date_str).lower() in ("nan", "none", "", "no",
                                                   "not available (instagram restricted)",
                                                   "not available (twitter restricted)"):
        return 999
    now = datetime.datetime.now(datetime.timezone.utc)

    # First try the most common DD-MM-YYYY and MM-YYYY formats (fast path)
    try:
        parts = str(date_str).split("-")
        if len(parts) == 2:
            dt = datetime.datetime.strptime(date_str, "%m-%Y").replace(
                tzinfo=datetime.timezone.utc
            )
            return (now.year - dt.year) * 12 + (now.month - dt.month)
        elif len(parts) == 3:
            dt = datetime.datetime.strptime(date_str, "%d-%m-%Y").replace(
                tzinfo=datetime.timezone.utc
            )
            return (now.year - dt.year) * 12 + (now.month - dt.month)
    except Exception:
        pass

    # Fallback: use the robust parser for any other format (ISO, relative, etc.)
    dt = parse_date_robust(date_str)
    if dt:
        # Make timezone-aware if needed
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (now.year - dt.year) * 12 + (now.month - dt.month)

    return 999


def calculate_risk(result: ProfileResult) -> None:
    """
    Calculate OSINT risk score (0-9) and priority directly on a ProfileResult.
    Mutates ``result.risk_score``, ``result.priority``, and ``result.is_active``.

    Scoring ladder (highest match wins):
        9  — name + logo + new (<6 mo) + active + location + followers > 100
        8  — name + logo + active + location + very new (<1 mo)
        7  — name + logo + active + location
        7  — name + logo + (active OR new)
        6  — name + logo
        4  — name + new
        3  — name only
        0  — nothing
    """
    has_name = result.has_name_match
    has_logo = result.has_logo
    has_location = bool(
        result.location and result.location.lower() not in ("nan", "none", "")
    )
    followers = result.followers or 0

    created_months = _get_months_ago(result.created_at)
    posted_months = _get_months_ago(result.last_post_date)

    is_new = created_months <= 6
    is_very_new = created_months <= 1
    is_active_post = posted_months <= 6

    result.is_active = result.is_active or is_active_post or is_new
    result.priority = "High" if has_logo else "Low"

    if has_name and has_logo and is_new and result.is_active and has_location and followers > 100:
        score = 9
    elif has_name and has_logo and result.is_active and has_location and is_very_new:
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
    else:
        score = 0

    result.risk_score = score


async def download_profile_image(
    image_url: str,
    *,
    referer: str = "",
    extra_headers: dict | None = None,
) -> str | None:
    """
    Download an image from *image_url* and return its base64-encoded string.
    Returns ``None`` on any failure.
    """
    if not image_url:
        return None
    headers = {"User-Agent": _DL_USER_AGENT}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                image_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 500:  # skip tiny error placeholders
                        return base64.b64encode(data).decode("utf-8")
    except Exception as exc:
        logger.debug(f"Image download failed for {image_url[:80]}: {exc}")
    return None
