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

# ─── Centralized Default Avatar / Placeholder Detection ──────────────────────
#
# Every platform has its own "no profile picture" default image.  The old code
# checked in some platforms but missed others, leading to false `has_logo=True`.
# This single function is the authoritative gate for ALL platforms.

# URL substrings that indicate a default / placeholder avatar
_DEFAULT_AVATAR_URL_PATTERNS = [
    # ── Facebook ──
    "static.xx",            # Facebook CDN static assets (silhouettes)
    "rsrc.php",             # Facebook resource bundles
    "silhouette",           # Generic silhouette pattern
    "guest",                # Guest user
    "default_profile",      # Generic default
    "avatar_empty",         # Empty avatar
    "blank_profile",        # Blank profile
    "1x1",                  # 1x1 pixel placeholder
    "emoji",                # Emoji placeholder
    # ── Twitter / X ──
    "default_profile_images",  # Twitter's official default egg/silhouette
    "default_profile_normal",  # Older Twitter default
    "sticky/default_profile",  # Another Twitter default path
    # ── Instagram ──
    "44884218_345707102882519_2446069589734326272",  # Instagram's default PFP image ID
    "instagram.com/static",  # Instagram static assets
    "cdninstagram.com/v/t51.2885-19/44884218",  # Specific default PFP
    "/anonymousUser",        # Instagram anonymous user
    # ── YouTube ──
    "yt3.ggpht.com/a/default",   # YouTube default channel avatar
    "yt3.ggpht.com/a-/default",  # YouTube default variant
    "yt3.ggpht.com/a/default-user",  # YouTube default user
    # ── TikTok ──
    "musically-maliva",     # Old Musical.ly default avatars
    "tiktok-obj/default",   # TikTok default object
    "default_avatar",       # TikTok default avatar
    # ── General ──
    "placeholder",
    "no-image",
    "no_image",
    "noimage",
    "avatar_default",
    "default-avatar",
    "default_avatar",
    "missing.png",
    "blank.png",
    "empty.png",
    "null",
]


def is_real_profile_image(
    url: str | None = None,
    image_bytes: bytes | None = None,
    image_b64: str | None = None,
) -> bool:
    """
    Determine whether a profile image URL / downloaded data represents a REAL
    user-uploaded profile picture vs. a platform default / placeholder.

    Args:
        url: The profile image URL (checked against known default patterns).
        image_bytes: Raw downloaded image bytes (checked for minimum size).
        image_b64: Base64-encoded image string (checked for minimum size).

    Returns:
        True only if the image is very likely a real, user-uploaded picture.
    """
    # ── Gate 1: URL pattern check ──
    if url:
        url_lower = url.lower()
        # Must be an actual HTTP URL
        if "http" not in url_lower:
            return False
        # Check against known default/placeholder patterns
        for pattern in _DEFAULT_AVATAR_URL_PATTERNS:
            if pattern.lower() in url_lower:
                logger.debug(f"Default avatar detected via URL pattern '{pattern}': {url[:80]}")
                return False

    # ── Gate 2: Image data size check ──
    # Real profile pictures (even small company logos) are almost always >500 bytes.
    # Platform defaults and error responses (transparent PNGs, 1x1 GIFs) are much smaller.
    if image_bytes is not None:
        if len(image_bytes) < 500:
            logger.debug(f"Image too small ({len(image_bytes)} bytes) — likely placeholder")
            return False

    if image_b64 is not None:
        # Base64 is ~33% larger than raw bytes, so 500 bytes ≈ 666 b64 chars
        if len(image_b64) < 700:
            logger.debug(f"Base64 image too small ({len(image_b64)} chars) — likely placeholder")
            return False

    # ── Gate 3: Must have at least a URL or actual image data ──
    if not url and not image_bytes and not image_b64:
        return False

    return True


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


def repair_telegram_session(session_path: str):
    """
    Validates the sqlite schema of the telegram session file to ensure compatibility
    with Telethon's SQLiteSession. Removes the extra 'tmp_auth_key' column from 'sessions'
    table if present, which would otherwise trigger 'ValueError: too many values to unpack (expected 5, got 6)'.
    """
    import os
    import sqlite3

    db_path = session_path
    if not db_path.endswith(".session"):
        db_path += ".session"

    if not os.path.exists(db_path):
        return

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("PRAGMA table_info(sessions)")
        columns = [info[1] for info in c.fetchall()]
        if "tmp_auth_key" in columns:
            logger.info(f"Removing incompatible 'tmp_auth_key' column from {db_path}...")
            c.execute("CREATE TABLE sessions_backup (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer)")
            c.execute("INSERT INTO sessions_backup SELECT dc_id, server_address, port, auth_key, takeout_id FROM sessions")
            c.execute("DROP TABLE sessions")
            c.execute("ALTER TABLE sessions_backup RENAME TO sessions")
            conn.commit()
            logger.info("Successfully repaired telegram.session schema!")
        conn.close()
    except Exception as exc:
        logger.warning(f"Failed to inspect or repair telegram.session at {db_path}: {exc}")

