"""
OSINT Analysis Engine: TikTok.
Three-layer profile extraction:
  1. TikTok Internal API response interception (/api/user/detail/)
  2. SIGI_STATE / __NEXT_DATA__ hydration data extraction
  3. Enhanced DOM fallback with broad selectors
Navigates to individual TikTok profile URLs to extract deep profile data:
bio, followers, following, likes, verified status, profile image, and screenshot.
"""

import asyncio
import base64
import os
import random
import re
from datetime import datetime, timedelta

import requests as req_lib

from backend.core.db import ProfileResult
from backend.core.logger import get_logger
from backend.platforms.base import AbstractAnalyzer
from backend.platforms.utils import is_real_profile_image
from backend.stealth.browser import create_stealth_browser
from backend.stealth.human import HumanBehavior

logger = get_logger("platforms.tiktok.analysis")

# Selectors for dismissing popups — comprehensive list for TikTok's current UI
POPUP_SELECTORS = [
    # Cookie consent
    'button[id="onetrust-accept-btn-handler"]',
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Allow all")',
    'button:has-text("Allow All")',
    'button:has-text("Allow All Cookies")',
    'button:has-text("Decline optional cookies")',
    'button:has-text("Decline optional")',
    # GDPR banner
    'button:has-text("Got it")',
    'a:has-text("Got it")',
    'div[class*="Banner"] button',
    'div[class*="banner"] button:has-text("Got it")',
    # Passkey / security prompts (shown to logged-in users)
    'button:has-text("Maybe later")',
    'button:has-text("Not now")',
    'button:has-text("Skip")',
    'button:has-text("No thanks")',
    'button:has-text("Dismiss")',
    # Notification / push prompt
    'button:has-text("Block")',
    'button:has-text("Later")',
    # Login wall / signup prompts
    '[data-e2e="modal-close-inner-button"]',
    'div[class*="DivCloseIcon"]',
    '[class*="close-button"]',
    'button[aria-label="Close"]',
    '[data-e2e="browse-close"]',
    'div[role="button"][aria-label="Close"]',
    # CAPTCHA close
    '.verify-bar-close',
    '#captcha_close',
    'div[class*="captcha"] button',
    '[class*="verify"] [class*="close"]',
    # Bottom banner
    '[class*="BottomBanner"] button',
    'div[class*="DivBottomBanner"] button',
]


class TikTokAnalyzer(AbstractAnalyzer):
    """
    TikTok Analysis — Three-Layer Profile Extraction Engine.

    Layer 1: API Interception — passively captures /api/user/detail/ responses
    Layer 2: SIGI_STATE Hydration — extracts React state from the profile page
    Layer 3: DOM Extraction — fallback broad-selector scraping

    Extracts: display name, username, bio, followers, following, likes,
    verified status, profile image, and full-page screenshot.
    """

    async def analyze(
        self,
        url: str,
        client: str,
        headless: bool = True,
        semaphore: asyncio.Semaphore | None = None,
    ) -> ProfileResult:
        """Legacy single-profile analysis. Launches its own browser."""
        sem = semaphore or asyncio.Semaphore(1)
        async with sem:
            # Use authenticated session if available
            session_file = os.path.join(self.config.SESSION_PATH, "tiktok.json")
            if not os.path.exists(session_file):
                session_file = None
            
            pw, browser, context, page = await create_stealth_browser(
                platform="tiktok",
                headless=headless,
                session_file=session_file,
            )
            try:
                return await self._do_analysis(page, url, client, browser_context=context)
            finally:
                if page and not page.is_closed():
                    await page.close()
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()

    async def analyze_with_page(
        self,
        url: str,
        client: str,
        page,
    ) -> ProfileResult:
        """Pool-optimized analysis. Uses a pre-existing stealth page (tab)."""
        return await self._do_analysis(page, url, client)

    async def _do_analysis(
        self,
        page,
        url: str,
        client: str,
        browser_context=None,
    ) -> ProfileResult:
        """Core analysis logic."""
        human = HumanBehavior(platform="tiktok")
        
        # Removed dummy lock to fix parse error
        """Core analysis logic — navigate, extract via 3 layers, screenshot."""
        # Browser lifecycle is managed by the caller
        logger.info(f"[{url}] Analysis starting using provided page.")

        # Extract username from URL for keyword field
        username = ""
        match = re.search(r"/@([^/?]+)", url)
        if match:
            username = match.group(1)

        # ================================================================
        # LAYER 1: Passive API Interception — capture /api/user/detail/
        # ================================================================
        api_profile_data = {}
        api_post_timestamps = []  # Collect video createTime values from item_list

        async def on_response(response):
            """Intercept profile API responses."""
            try:
                resp_url = response.url
                is_profile_api = any(sig in resp_url for sig in [
                    '/api/user/detail',
                    '/api/user/info',
                    'user/detail',
                ])
                is_post_api = any(sig in resp_url for sig in [
                    '/api/post/item_list',
                    'item_list',
                ])
                if response.status == 200:
                    try:
                        data = await response.json()
                        if is_profile_api:
                            self._walk_api_profile(data, api_profile_data)
                        if is_post_api:
                            # Extract video createTime timestamps from item_list
                            self._extract_post_timestamps(data, api_post_timestamps)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        try:
            logger.info(f"Analyzing TikTok profile: {url}")

            # Navigate with retry
            navigation_success = False
            for nav_attempt in range(3):
                try:
                    target_url = url
                    if "lang=" not in target_url:
                        if "?" in target_url:
                            target_url = f"{target_url}&lang=en"
                        else:
                            target_url = f"{target_url}?lang=en"
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                    
                    if "login" in page.url:
                        if "Redirected to Login (Please log in via Session Manager)" not in error_comments:
                            error_comments.append("Redirected to Login (Please log in via Session Manager)")
                            
                    navigation_success = True
                    break
                except Exception as e:
                    logger.warning(f"Navigation attempt {nav_attempt + 1} to {url} failed: {e}")
                    if nav_attempt < 2:
                        await asyncio.sleep(random.uniform(2, 5))

            if not navigation_success:
                logger.error(f"All navigation attempts to {url} failed")
                return self._empty_result(url, client, username)

            await human.pause("page_load")

            # Aggressive popup dismissal
            await self._dismiss_popups(page)

            # Check for CAPTCHA
            captcha_detected = await self._detect_captcha(page)
            if captcha_detected:
                logger.warning(f"CAPTCHA detected on profile page: {url}")
                for retry in range(5):
                    await asyncio.sleep(random.uniform(5, 10))
                    await self._dismiss_popups(page)
                    captcha_detected = await self._detect_captcha(page)
                    if not captcha_detected:
                        logger.info("CAPTCHA cleared after waiting.")
                        break
                if captcha_detected:
                    logger.error("CAPTCHA persists on profile page.")
                    return self._empty_result(url, client, username)

            # Wait for profile content to load
            try:
                await page.wait_for_selector(
                    'h1, h2, [data-e2e="user-title"], [data-e2e="user-subtitle"], [data-e2e="user-bio"], [data-e2e="followers-count"], [data-e2e="user-post-item"]',
                    timeout=15000,
                )
            except Exception:
                logger.debug(f"Profile selectors timed out for {url}, proceeding with SIGI/API data.")

            # Give extra time for full hydration (stats load 3-5s after DOM)
            await asyncio.sleep(2)

            # Scroll down to trigger lazy-loaded content, then back up
            try:
                await page.evaluate("window.scrollBy(0, 600)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.5)
            except Exception:
                pass

            # Dismiss any popups that appeared after scroll
            await self._dismiss_popups(page)

            # Explicitly wait for stats to hydrate
            try:
                await page.wait_for_selector(
                    '[data-e2e="followers-count"], strong[data-e2e="followers-count"], strong[title]',
                    timeout=10000,
                )
            except Exception:
                logger.debug(f"Stats selectors didn't appear for {url}, will use meta fallback.")

            # ================================================================
            # LAYER 2: SIGI_STATE / __NEXT_DATA__ Hydration Extraction
            # ================================================================
            sigi_profile_js = r"""
            () => {
                const result = {
                    displayName: "",
                    username: "",
                    bio: "",
                    followers: "",
                    following: "",
                    likes: "",
                    isVerified: false,
                    imageUrl: "",
                    postCount: "",
                    lastPostDate: "",
                    source: ""
                };
                
                function formatTimestamp(ts) {
                    if (!ts) return "";
                    try {
                        const date = new Date(Number(ts) * 1000);
                        return date.toISOString().split('T')[0];
                    } catch(e) { return ""; }
                }

                
                // Strategy 1: window.SIGI_STATE (primary)
                if (window.SIGI_STATE) {
                    try {
                        const state = window.SIGI_STATE;
                        const userModule = state.UserModule || {};
                        const users = userModule.users || {};
                        const stats = userModule.stats || {};
                        
                        // Get the first (and usually only) user on a profile page
                        const userKeys = Object.keys(users);
                        if (userKeys.length > 0) {
                            // Try to find the matching username from the URL
                            const urlPath = window.location.pathname || "";
                            const urlMatch = urlPath.match(/\/@([^/?]+)/);
                            const targetUser = urlMatch ? urlMatch[1] : userKeys[0];
                            
                            const user = users[targetUser] || users[userKeys[0]] || {};
                            const userStats = stats[targetUser] || stats[userKeys[0]] || {};
                            
                            result.displayName = user.nickname || "";
                            result.username = user.uniqueId || targetUser;
                            result.bio = user.signature || "";
                            result.isVerified = user.verified || false;
                            result.imageUrl = user.avatarLarger || user.avatarMedium || user.avatarThumb || "";
                            
                            // Stats from SIGI_STATE are exact numbers
                            result.followers = String(userStats.followerCount || user.followerCount || "");
                            result.following = String(userStats.followingCount || user.followingCount || "");
                            result.likes = String(userStats.heartCount || userStats.heart || user.heartCount || "");
                            result.postCount = String(userStats.videoCount || user.videoCount || "");
                            result.source = "sigi_state";

                            // Get last post date from ItemModule
                            const items = state.ItemModule || {};
                            const itemKeys = Object.keys(items);
                            if (itemKeys.length > 0) {
                                let latest = 0;
                                for (const k of itemKeys) {
                                    const ts = Number(items[k].createTime || 0);
                                    if (ts > latest) latest = ts;
                                }
                                if (latest > 0) result.lastPostDate = formatTimestamp(latest);
                            }
                        }
                    } catch(e) {}
                }
                
                // Strategy 2: window.__NEXT_DATA__ (newer versions)
                if (!result.username && window.__NEXT_DATA__) {
                    try {
                        const nextData = window.__NEXT_DATA__;
                        const pageProps = nextData.props?.pageProps || {};
                        const userInfo = pageProps.userInfo || pageProps.userData || {};
                        const user = userInfo.user || pageProps.user || {};
                        const stats = userInfo.stats || pageProps.stats || {};
                        
                        if (user.uniqueId || user.id) {
                            result.displayName = user.nickname || "";
                            result.username = user.uniqueId || user.id || "";
                            result.bio = user.signature || "";
                            result.isVerified = user.verified || false;
                            result.imageUrl = user.avatarLarger || user.avatarMedium || "";
                            result.followers = String(stats.followerCount || "");
                            result.following = String(stats.followingCount || "");
                            result.likes = String(stats.heartCount || stats.heart || "");
                            result.postCount = String(stats.videoCount || "");
                            result.source = "next_data";

                            // Search for last post in NextData
                            let latestTS = 0;
                            const itemList = pageProps.itemList || [];
                            for (const item of itemList) {
                                const ts = Number(item.createTime || item.create_time || 0);
                                if (ts > latestTS) latestTS = ts;
                            }
                            if (latestTS > 0) result.lastPostDate = formatTimestamp(latestTS);
                        }
                    } catch(e) {}
                }
                
                // Strategy 3: Parse script tags directly (works even when window vars are blocked)
                // This runs if username is missing OR if lastPostDate is still empty
                if (!result.username || !result.lastPostDate) {
                    try {
                        // Check SIGI_STATE script tag
                        const sigiScript = document.querySelector('script#SIGI_STATE');
                        if (sigiScript && sigiScript.textContent) {
                            const parsed = JSON.parse(sigiScript.textContent);
                            const userModule = parsed.UserModule || {};
                            const users = userModule.users || {};
                            const stats = userModule.stats || {};
                            const userKeys = Object.keys(users);
                            if (userKeys.length > 0 && !result.username) {
                                const user = users[userKeys[0]] || {};
                                const userStats = stats[userKeys[0]] || {};
                                result.displayName = user.nickname || "";
                                result.username = user.uniqueId || userKeys[0];
                                result.bio = user.signature || "";
                                result.isVerified = user.verified || false;
                                result.imageUrl = user.avatarLarger || user.avatarMedium || "";
                                result.followers = String(userStats.followerCount || "");
                                result.following = String(userStats.followingCount || "");
                                result.likes = String(userStats.heartCount || "");
                                result.postCount = String(userStats.videoCount || "");
                                result.source = "script_tag";
                            }
                            // Always try to extract lastPostDate from ItemModule
                            if (!result.lastPostDate) {
                                const items = parsed.ItemModule || {};
                                const itemKeys = Object.keys(items);
                                if (itemKeys.length > 0) {
                                    let latest = 0;
                                    for (const k of itemKeys) {
                                        const ts = Number(items[k].createTime || 0);
                                        if (ts > latest) latest = ts;
                                    }
                                    if (latest > 0) result.lastPostDate = formatTimestamp(latest);
                                }
                            }
                        }
                    } catch(e) {}
                    
                    // Check __NEXT_DATA__ script tag
                    if (!result.lastPostDate || !result.username) {
                        try {
                            const nextScript = document.querySelector('script#__NEXT_DATA__');
                            if (nextScript && nextScript.textContent) {
                                const parsed = JSON.parse(nextScript.textContent);
                                const pageProps = parsed.props?.pageProps || {};
                                if (!result.username) {
                                    const userInfo = pageProps.userInfo || pageProps.userData || {};
                                    const user = userInfo.user || pageProps.user || {};
                                    const stats = userInfo.stats || pageProps.stats || {};
                                    if (user.uniqueId || user.id) {
                                        result.displayName = user.nickname || "";
                                        result.username = user.uniqueId || user.id || "";
                                        result.bio = user.signature || "";
                                        result.isVerified = user.verified || false;
                                        result.imageUrl = user.avatarLarger || user.avatarMedium || "";
                                        result.followers = String(stats.followerCount || "");
                                        result.following = String(stats.followingCount || "");
                                        result.likes = String(stats.heartCount || stats.heart || "");
                                        result.postCount = String(stats.videoCount || "");
                                        result.source = "next_data_script";
                                    }
                                }
                                if (!result.lastPostDate) {
                                    const itemList = pageProps.itemList || [];
                                    let latestTS = 0;
                                    for (const item of itemList) {
                                        const ts = Number(item.createTime || item.create_time || 0);
                                        if (ts > latestTS) latestTS = ts;
                                    }
                                    if (latestTS > 0) result.lastPostDate = formatTimestamp(latestTS);
                                }
                            }
                        } catch(e) {}
                    }

                    // Check __UNIVERSAL_DATA_FOR_WEB__ script tag (newest TikTok format)
                    if (!result.lastPostDate || !result.username) {
                        try {
                            const univScript = document.querySelector('script#__UNIVERSAL_DATA_FOR_WEB__');
                            if (univScript && univScript.textContent) {
                                const parsed = JSON.parse(univScript.textContent);
                                const defaultScope = parsed.__DEFAULT_SCOPE__ || {};
                                
                                // User data
                                const userDetail = defaultScope['webapp.user-detail'] || {};
                                const userInfo = userDetail.userInfo || {};
                                const user = userInfo.user || {};
                                const stats = userInfo.stats || {};
                                
                                if (!result.username && (user.uniqueId || user.id)) {
                                    result.displayName = user.nickname || "";
                                    result.username = user.uniqueId || user.id || "";
                                    result.bio = user.signature || "";
                                    result.isVerified = user.verified || false;
                                    result.imageUrl = user.avatarLarger || user.avatarMedium || "";
                                    result.followers = String(stats.followerCount || "");
                                    result.following = String(stats.followingCount || "");
                                    result.likes = String(stats.heartCount || "");
                                    result.postCount = String(stats.videoCount || "");
                                    result.source = "universal_data";
                                }
                                
                                // Video data for lastPostDate
                                if (!result.lastPostDate) {
                                    const videoDetail = defaultScope['webapp.video-detail'] || {};
                                    const videoList = defaultScope['webapp.user-detail']?.itemList || [];
                                    let latestTS = 0;
                                    for (const item of videoList) {
                                        const ts = Number(item.createTime || 0);
                                        if (ts > latestTS) latestTS = ts;
                                    }
                                    if (latestTS > 0) result.lastPostDate = formatTimestamp(latestTS);
                                }
                            }
                        } catch(e) {}
                    }

                    // Last resort: regex scan ALL script tags for createTime
                    if (!result.lastPostDate) {
                        try {
                            const allScripts = document.querySelectorAll('script');
                            for (const s of allScripts) {
                                const text = s.textContent || '';
                                if (text.includes('createTime') && (text.includes('ItemModule') || text.includes('itemList'))) {
                                    const matches = [...text.matchAll(/"createTime"\s*:\s*"?(\d{10})"?/g)];
                                    let latest = 0;
                                    for (const m of matches) {
                                        const ts = Number(m[1]);
                                        if (ts > latest) latest = ts;
                                    }
                                    if (latest > 0) {
                                        result.lastPostDate = formatTimestamp(latest);
                                        break;
                                    }
                                }
                            }
                        } catch(e) {}
                    }
                }
                
                return result;
            }
            """

            # ================================================================
            # LAYER 3: DOM Extraction — Enhanced fallback
            # ================================================================
            dom_profile_js = r"""
            () => {
                const result = {
                    displayName: "",
                    username: "",
                    bio: "",
                    followers: "",
                    following: "",
                    likes: "",
                    isVerified: false,
                    imageUrl: "",
                    postCount: "",
                    lastPostDate: "",
                };

                // Display name
                const nameSelectors = [
                    '[data-e2e="user-title"]',
                    '[data-e2e="user-name"]',
                    'h1[class*="ShareTitle"]',
                    'h1[class*="UserTitle"]',
                    'span[class*="SpanNickName"]',
                    'h2[class*="UserTitle"]',
                    'h1',
                ];
                for (const sel of nameSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = (el.textContent || "").trim();
                        if (text && text.length > 0 && text.length < 100) {
                            result.displayName = text;
                            break;
                        }
                    }
                }

                // Username  
                const userSelectors = [
                    '[data-e2e="user-subtitle"]',
                    '[data-e2e="user-unique-id"]',
                    'h2[class*="ShareSubTitle"]',
                    'h1[class*="ShareSubTitle"]',
                    'span[class*="SpanUniqueId"]',
                    'h2[data-e2e="user-subtitle"]',
                ];
                for (const sel of userSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = (el.textContent || "").trim().replace(/^@/, '');
                        if (text && text.length > 0) {
                            result.username = text;
                            break;
                        }
                    }
                }

                // Bio
                const bioSelectors = [
                    '[data-e2e="user-bio"]',
                    'h2[class*="UserBio"]',
                    'span[class*="SpanBio"]',
                    'div[class*="DivBio"]',
                    'h2[data-e2e="user-bio"]',
                ];
                for (const sel of bioSelectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = (el.textContent || "").trim();
                        if (text && text.length > 0) {
                            result.bio = text;
                            break;
                        }
                    }
                }

                // Stats via data-e2e (primary)
                const statsMap = {
                    followers: ['[data-e2e="followers-count"] strong', '[data-e2e="followers-count"]', '[data-e2e="follower-count"]'],
                    following: ['[data-e2e="following-count"] strong', '[data-e2e="following-count"]'],
                    likes: ['[data-e2e="likes-count"] strong', '[data-e2e="likes-count"]', '[data-e2e="like-count"]'],
                    postCount: ['[data-e2e="user-post-count"]', '[data-e2e="video-count"]'],
                };
                for (const [key, selectors] of Object.entries(statsMap)) {
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            // Extract text content (e.g., "93.5M")
                            // Prefer textContent as title often contains labels ("Followers") in newer UI
                            let text = (el.textContent || "").trim();
                            
                            // If title exists and looks like a full number (no letters), use it
                            let title = el.getAttribute('title') || "";
                            if (title && /^\d+$/.test(title)) {
                                text = title;
                            }
                            
                            // Clean up "93.5M Followers" -> "93.5M"
                            const cleanVal = text.split(/\s+/)[0];
                            result[key] = cleanVal;
                            break;
                        }
                    }
                }

                // Strategy 2: strong tags inside stat links
                if (!result.followers || !result.following) {
                    const strongEls = document.querySelectorAll('strong[data-e2e], strong[title]');
                    for (const strong of strongEls) {
                        const dataE2e = strong.getAttribute('data-e2e') || "";
                        const val = (strong.getAttribute('title') || strong.textContent || "").trim();
                        
                        if (dataE2e.includes('following') && !result.following) {
                            result.following = val;
                        } else if (dataE2e.includes('follower') && !result.followers) {
                            result.followers = val;
                        } else if (dataE2e.includes('like') && !result.likes) {
                            result.likes = val;
                        }
                    }
                }
                
                if (!result.followers || !result.following) {
                    const statLinks = document.querySelectorAll('a[href*="/@"], div[class*="DivNumber"]');
                    for (const link of statLinks) {
                        const parentText = (link.textContent || "").toLowerCase();
                        const strong = link.querySelector('strong, span[class*="Count"], span[class*="Number"]');
                        const val = strong ? (strong.getAttribute('title') || strong.textContent || "").trim() : "";
                        
                        if (parentText.includes('following') && !result.following && val) {
                            result.following = val;
                        } else if (parentText.includes('follower') && !result.followers && val) {
                            result.followers = val;
                        } else if (parentText.includes('like') && !result.likes && val) {
                            result.likes = val;
                        }
                    }
                }

                // Strategy 3: Text-based fallback (enhanced)
                if (!result.followers || !result.following || !result.likes) {
                    const body = document.body.innerText || "";
                    
                    // Match "78 Following" or "Following 78"
                    const followingMatch1 = body.match(/([\d.]+[KMBkmb]?)\s*Following/i);
                    const followingMatch2 = body.match(/Following\s*([\d.]+[KMBkmb]?)/i);
                    if (followingMatch1 && !result.following) result.following = followingMatch1[1];
                    else if (followingMatch2 && !result.following) result.following = followingMatch2[1];
                    
                    // Match "4.9M Followers" or "Followers 4.9M"
                    const followersMatch1 = body.match(/([\d.]+[KMBkmb]?)\s*Follower/i);
                    const followersMatch2 = body.match(/Follower[s]?\s*([\d.]+[KMBkmb]?)/i);
                    if (followersMatch1 && !result.followers) result.followers = followersMatch1[1];
                    else if (followersMatch2 && !result.followers) result.followers = followersMatch2[1];
                    
                    // Match "51.5M Likes" or "Likes 51.5M"
                    const likesMatch1 = body.match(/([\d.]+[KMBkmb]?)\s*Like/i);
                    const likesMatch2 = body.match(/Like[s]?\s*([\d.]+[KMBkmb]?)/i);
                    if (likesMatch1 && !result.likes) result.likes = likesMatch1[1];
                    else if (likesMatch2 && !result.likes) result.likes = likesMatch2[1];
                }

                // Display name fallback from page title
                if (!result.displayName) {
                    const title = document.title || "";
                    const titleMatch = title.match(/^(.+?)\s*\(@/);
                    if (titleMatch) {
                        result.displayName = titleMatch[1].trim();
                    }
                    const titleMatch2 = title.match(/^(.+?)\s*\(@([^)]+)\)/);
                    if (!result.displayName && titleMatch2) {
                        result.displayName = titleMatch2[1].trim();
                    }
                }
                
                // Username fallback from page title
                if (!result.username) {
                    const title = document.title || "";
                    const userMatch = title.match(/@([\w.]+)/);
                    if (userMatch) result.username = userMatch[1];
                }
                
                // Username fallback from URL
                if (!result.username) {
                    const urlMatch = window.location.pathname.match(/\/@([^/?]+)/);
                    if (urlMatch) result.username = urlMatch[1];
                }

                // Verified badge
                const verifiedSelectors = [
                    'svg[data-e2e="verify-badge"]',
                    '[class*="Verified"]',
                    '[class*="verified-badge"]',
                    '[data-e2e="verified-badge"]',
                    'img[alt*="Verified"]',
                    'img[alt*="verified"]',
                ];
                for (const sel of verifiedSelectors) {
                    if (document.querySelector(sel)) {
                        result.isVerified = true;
                        break;
                    }
                }

                // Profile image
                const imgSelectors = [
                    '[data-e2e="user-avatar"] img',
                    'img[class*="ImgAvatar"]',
                    'img[class*="StyledAvatar"]',
                    'span[class*="SpanAvatar"] img',
                    'div[class*="DivAvatar"] img',
                    'img[class*="Avatar"]',
                    'img[class*="avatar"]',
                ];
                for (const sel of imgSelectors) {
                    const img = document.querySelector(sel);
                    if (img) {
                        const src = img.getAttribute('src') || "";
                        if (src && src.startsWith('http')) {
                            result.imageUrl = src;
                            break;
                        }
                    }
                }

                // Fallback: any large tiktok CDN image
                if (!result.imageUrl) {
                    const imgs = document.querySelectorAll('img');
                    for (const i of imgs) {
                        const src = i.getAttribute('src') || "";
                        if ((src.includes('muscdn') || src.includes('tiktokcdn')) && !src.includes('video')) {
                            const rect = i.getBoundingClientRect();
                            if (rect.width > 60 && rect.height > 60) {
                                result.imageUrl = src;
                                break;
                            }
                        }
                    }
                }

                // Last Post Date from Video Grid
                try {
                    const firstVideo = document.querySelector('[data-e2e="user-post-item"]');
                    if (firstVideo) {
                        const aria = firstVideo.getAttribute('aria-label') || "";
                        if (aria) {
                            const parts = aria.split(',');
                            if (parts.length > 1) {
                                result.lastPostDate = parts[1].trim();
                            }
                        }
                        if (!result.lastPostDate) {
                            const timeEl = firstVideo.querySelector('time');
                            if (timeEl) result.lastPostDate = timeEl.textContent.trim();
                        }
                    }
                } catch(e) {}

                // ============================================================
                // FALLBACK: Extract from <meta> tags (always available)
                // og:description often contains: "156.7M Followers, 1361 Following, 12B Likes"
                // ============================================================
                if (!result.followers || !result.following) {
                    try {
                        const metas = document.querySelectorAll('meta[property="og:description"], meta[name="description"]');
                        for (const meta of metas) {
                            const content = meta.getAttribute('content') || '';
                            if (content.includes('Follower')) {
                                const fMatch = content.match(/([\d.]+[KMBkmb]?)\s*Follower/i);
                                if (fMatch && !result.followers) result.followers = fMatch[1];
                                const fgMatch = content.match(/([\d.]+[KMBkmb]?)\s*Following/i);
                                if (fgMatch && !result.following) result.following = fgMatch[1];
                                const lMatch = content.match(/([\d.]+[KMBkmb]?)\s*Like/i);
                                if (lMatch && !result.likes) result.likes = lMatch[1];
                                break;
                            }
                        }
                    } catch(e) {}
                }

                // FALLBACK: Extract username and displayName from meta/title
                if (!result.displayName) {
                    try {
                        const ogTitle = document.querySelector('meta[property="og:title"]');
                        if (ogTitle) {
                            const ogText = ogTitle.getAttribute('content') || '';
                            const m = ogText.match(/^(.+?)\s*\(@/);
                            if (m) result.displayName = m[1].trim();
                        }
                    } catch(e) {}
                }

                return result;
            }
            """

            # Execute LAYER 2 first (SIGI_STATE — highest fidelity)
            sigi_data = {}
            try:
                sigi_data = await asyncio.wait_for(
                    page.evaluate(sigi_profile_js), timeout=15.0
                )
                if sigi_data.get("username"):
                    logger.info(f"SIGI_STATE extracted profile for @{sigi_data['username']} (source: {sigi_data.get('source', 'unknown')})")
                    logger.info(f"  SIGI stats: followers={sigi_data.get('followers')}, following={sigi_data.get('following')}, lastPost={sigi_data.get('lastPostDate')}")
            except Exception as e:
                logger.debug(f"SIGI_STATE profile extraction failed: {e}")
                sigi_data = {}

            # Execute LAYER 3 (DOM extraction — fallback)
            dom_data = {}
            try:
                dom_data = await asyncio.wait_for(
                    page.evaluate(dom_profile_js), timeout=15.0
                )
                logger.info(f"  DOM stats: followers={dom_data.get('followers')}, following={dom_data.get('following')}, lastPost={dom_data.get('lastPostDate')}")
            except Exception as e:
                logger.warning(f"DOM profile extraction failed: {e}")
                dom_data = {}

            # Process LAYER 1 (API-intercepted data)
            api_data = {}
            if username in api_profile_data:
                raw = api_profile_data[username]
                api_data = {
                    "displayName": raw.get("nickname", ""),
                    "username": raw.get("uniqueId", username),
                    "bio": raw.get("signature", ""),
                    "followers": str(raw.get("followerCount", "")),
                    "following": str(raw.get("followingCount", "")),
                    "likes": str(raw.get("heartCount", raw.get("heart", ""))),
                    "postCount": str(raw.get("videoCount", "")),
                    "isVerified": raw.get("verified", False),
                    "imageUrl": raw.get("avatarLarger", raw.get("avatarMedium", "")),
                    "lastPostDate": "",
                }
                # NOTE: Do NOT use raw["createTime"] here — that's the ACCOUNT
                # creation date, not the last post date. Last post date comes from
                # video/post items only.
                
                logger.info(f"API intercepted profile data for @{username}")

            # Use video post timestamps from item_list API interception
            if api_post_timestamps:
                latest_ts = max(api_post_timestamps)
                if latest_ts > 1000000000:  # Sanity: must be a valid Unix timestamp
                    api_last_post = datetime.fromtimestamp(latest_ts).strftime('%Y-%m-%d')
                    if api_data:
                        api_data["lastPostDate"] = api_last_post
                    else:
                        api_data = {"lastPostDate": api_last_post}
                    logger.info(f"  API item_list: latest video timestamp {latest_ts} → {api_last_post}")

            # ================================================================
            # MERGE: API > SIGI > DOM (priority)
            # ================================================================
            merged = {}
            # DOM first (lowest priority)
            for key, val in dom_data.items():
                if val:
                    merged[key] = val
            # SIGI state (medium priority)
            for key, val in sigi_data.items():
                if val and key != "source":
                    merged[key] = val
            # API intercept (highest priority)
            for key, val in api_data.items():
                if val:
                    merged[key] = val

            data_source = sigi_data.get("source", "") or ("api" if api_data else "dom")
            logger.info(f"Profile data merged for @{username} — primary source: {data_source}")
            logger.info(f"  Merged: followers={merged.get('followers')}, following={merged.get('following')}, lastPost={merged.get('lastPostDate')}, image={bool(merged.get('imageUrl'))}")

            display_name = merged.get("displayName", "") or username
            extracted_username = merged.get("username", "") or username
            bio = merged.get("bio", "")
            is_verified = merged.get("isVerified", False)
            image_url = merged.get("imageUrl", "")

            followers = self._parse_count(merged.get("followers", ""))
            following = self._parse_count(merged.get("following", ""))
            post_count = self._parse_count(merged.get("postCount", ""))
            last_post_date = self._parse_date(merged.get("lastPostDate", ""))

            # ================================================================
            # LAYER 4: Click first video to extract last post date
            # Only runs if all other layers failed to get lastPostDate
            # ================================================================
            if not last_post_date:
                try:
                    logger.info(f"Last post date empty — attempting video click extraction for @{extracted_username}")

                    # Get the first video link URL via JS
                    first_video_url = await asyncio.wait_for(
                        page.evaluate(r"""
                        () => {
                            // Method 1: data-e2e user-post-item links
                            const item = document.querySelector('[data-e2e="user-post-item"] a[href*="/video/"]');
                            if (item) return item.href;
                            
                            // Method 2: any video link in the grid area
                            const links = document.querySelectorAll('a[href*="/video/"]');
                            for (const a of links) {
                                const href = a.href || '';
                                if (href.includes('/video/')) return href;
                            }
                            
                            // Method 3: look for video item divs and click target
                            const gridItem = document.querySelector('[data-e2e="user-post-item"]');
                            if (gridItem) {
                                const anchor = gridItem.closest('a') || gridItem.querySelector('a');
                                if (anchor) return anchor.href;
                            }
                            
                            return null;
                        }
                        """),
                        timeout=5.0,
                    )

                    if first_video_url:
                        logger.info(f"  Found first video URL: {first_video_url}")

                        # Navigate to the video page
                        await page.goto(first_video_url, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(4)

                        # Dismiss any popups on the video page
                        await self._dismiss_popups(page)

                        # Extract the date from the video detail page
                        video_date = await asyncio.wait_for(
                            page.evaluate(r"""
                            () => {
                                // Method 1: Look for the date text near the username/description
                                // TikTok shows dates like "2026-3-25", "5d ago", "2d ago" on video pages
                                const dateSelectors = [
                                    'span[data-e2e="browser-nickname"] + span',
                                    'span[data-e2e="video-create-time"]',
                                    '[class*="SpanOtherInfos"] span',
                                    '[class*="DivBrowserInfo"] span:last-child',
                                    '[class*="InfoContainer"] span:last-child',
                                ];
                                for (const sel of dateSelectors) {
                                    const el = document.querySelector(sel);
                                    if (el) {
                                        const text = (el.textContent || '').trim();
                                        // Check if it looks like a date (contains digits and date-like patterns)
                                        if (text && /\d/.test(text) && (
                                            /ago/i.test(text) ||
                                            /^\d{4}/.test(text) ||
                                            /^\d{1,2}[-\/]\d{1,2}/.test(text) ||
                                            /^\d+[dhm]$/i.test(text)
                                        )) {
                                            return text;
                                        }
                                    }
                                }

                                // Method 2: Search all spans for date-like text
                                const allSpans = document.querySelectorAll('span');
                                for (const span of allSpans) {
                                    const text = (span.textContent || '').trim();
                                    // Match patterns: "2026-3-25", "3-25", "5d ago", "2 days ago"
                                    if (text && (
                                        /^\d{4}-\d{1,2}-\d{1,2}$/.test(text) ||
                                        /^\d{1,2}-\d{1,2}$/.test(text) ||
                                        /^\d+\s*(d|h|m|day|hour|min)\w*\s*ago$/i.test(text) ||
                                        /^\d+[dhm]$/i.test(text)
                                    )) {
                                        return text;
                                    }
                                }

                                // Method 3: Check the page's meta description or title
                                const metaDesc = document.querySelector('meta[property="og:description"]');
                                if (metaDesc) {
                                    const content = metaDesc.getAttribute('content') || '';
                                    // Sometimes contains "posted on YYYY-MM-DD"
                                    const dateMatch = content.match(/(\d{4}-\d{1,2}-\d{1,2})/);
                                    if (dateMatch) return dateMatch[1];
                                }

                                // Method 4: SIGI_STATE on video page contains createTime
                                if (window.SIGI_STATE) {
                                    try {
                                        const items = window.SIGI_STATE.ItemModule || {};
                                        const keys = Object.keys(items);
                                        if (keys.length > 0) {
                                            const ts = Number(items[keys[0]].createTime || 0);
                                            if (ts > 0) {
                                                const d = new Date(ts * 1000);
                                                return d.toISOString().split('T')[0];
                                            }
                                        }
                                    } catch(e) {}
                                }
                                if (window.__NEXT_DATA__) {
                                    try {
                                        const item = window.__NEXT_DATA__?.props?.pageProps?.itemInfo?.itemStruct || {};
                                        const ts = Number(item.createTime || 0);
                                        if (ts > 0) {
                                            const d = new Date(ts * 1000);
                                            return d.toISOString().split('T')[0];
                                        }
                                    } catch(e) {}
                                }

                                return null;
                            }
                            """),
                            timeout=10.0,
                        )

                        if video_date:
                            last_post_date = self._parse_date(video_date)
                            logger.info(f"  ✅ Extracted last post date from video page: {video_date} → {last_post_date}")
                        else:
                            logger.info("  ⚠️ Could not extract date from video page")

                        # Navigate back to the profile page
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(2)
                    else:
                        logger.info(f"  No video links found in the grid for @{extracted_username}")

                except Exception as e:
                    logger.warning(f"  Video click extraction failed: {e}")
                    # Try to navigate back to the profile if we're on a video page
                    try:
                        if "/video/" in page.url:
                            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass

            # Final aggressive popup dismissal before screenshot
            for _ in range(3):
                await self._dismiss_popups(page)
                await asyncio.sleep(0.5)

            # Force-remove any remaining overlays/modals via JS
            try:
                await page.evaluate(r"""
                () => {
                    // Remove elements that block the view
                    const removeSelectors = [
                        '[class*="DivLoginModal"]',
                        '[class*="LoginModal"]',
                        '[class*="login-modal"]',
                        '[class*="DivBottomBanner"]',
                        '[class*="BottomBanner"]',
                        '[class*="DivModal"]',
                        '[role="dialog"]',
                        '[class*="tiktok-cookie"]',
                        '[class*="CookieBanner"]',
                        '[id="login-modal"]',
                        '[class*="DivMask"]',
                        '[class*="Overlay"]',
                        '[class*="overlay"]',
                        '[class*="signup"]',
                        '[class*="DivGuestModeContainer"]',
                        'div[class*="Banner"][class*="Bottom"]',
                    ];
                    for (const sel of removeSelectors) {
                        document.querySelectorAll(sel).forEach(el => {
                            try { el.remove(); } catch(e) {}
                        });
                    }
                    // Also remove any fixed/sticky overlays covering the page
                    document.querySelectorAll('div').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if ((style.position === 'fixed' || style.position === 'sticky') && 
                            style.zIndex > 100 &&
                            el.offsetWidth > window.innerWidth * 0.5 &&
                            el.offsetHeight > window.innerHeight * 0.3) {
                            try { el.remove(); } catch(e) {}
                        }
                    });
                    // Remove any body overflow:hidden that prevents scrolling
                    document.body.style.overflow = 'auto';
                    document.documentElement.style.overflow = 'auto';
                }
                """)
                await asyncio.sleep(0.5)
            except Exception:
                pass

            # Take screenshot
            screenshot_b64 = None
            try:
                screenshot_bytes = await page.screenshot(
                    full_page=False, type="jpeg", quality=75
                )
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                logger.info(f"Screenshot captured for @{extracted_username}")
            except Exception as e:
                logger.warning(f"Screenshot failed for {url}: {e}")

            # Download profile image
            profile_image_b64 = None
            if image_url:
                try:

                    img_resp = await asyncio.to_thread(
                        lambda: req_lib.get(
                            image_url,
                            headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Accept": "image/*,*/*;q=0.8",
                                "Referer": "https://www.tiktok.com/",
                            },
                            timeout=8,
                        )
                    )
                    if img_resp.status_code == 200 and len(img_resp.content) > 500:
                        profile_image_b64 = base64.b64encode(
                            img_resp.content
                        ).decode("utf-8")
                except Exception as e:
                    logger.warning(f"Image download failed: {e}")

            await self.health.record_request("tiktok", success=True)

            return ProfileResult(
                platform="tiktok",
                client_name=client,
                keyword=extracted_username or username,
                url=url,
                username=extracted_username or username,
                display_name=display_name,
                bio=bio,
                followers=followers,
                following=following,
                post_count=post_count,
                is_verified=is_verified,
                profile_image_url=image_url,
                profile_image_b64=profile_image_b64,
                screenshot_b64=screenshot_b64,
                has_logo=is_real_profile_image(url=image_url, image_b64=profile_image_b64),
                entity_type="Creator",
                last_post_date=last_post_date,
            )

        except Exception as exc:
            logger.error(f"TikTok analysis failed for {url}: {exc}")
            await self.health.record_request("tiktok", success=False)
            return self._empty_result(url, client, username)

        finally:
            # Browser is managed by the caller
            pass

    def _walk_api_profile(self, node, api_data: dict):
        """Recursively walk API response JSON looking for user profile data.
        Handles both camelCase and snake_case field naming."""
        if isinstance(node, dict):
            # Detect user objects
            username = (
                node.get("uniqueId")
                or node.get("unique_id")
                or ""
            )
            has_name = "nickname" in node or "id" in node or "uid" in node
            if username and has_name:
                entry = api_data.setdefault(username, {})
                # Map both camelCase and snake_case
                field_map = {
                    "nickname": ["nickname"],
                    "signature": ["signature"],
                    "verified": ["verified", "custom_verify"],
                    "followerCount": ["followerCount", "follower_count"],
                    "followingCount": ["followingCount", "following_count"],
                    "heartCount": ["heartCount", "heart_count", "total_favorited", "heart"],
                    "videoCount": ["videoCount", "video_count", "aweme_count"],
                }
                for target_key, source_keys in field_map.items():
                    for sk in source_keys:
                        if sk in node and node[sk]:
                            entry[target_key] = node[sk]
                            break

                # Avatar (can be dict with url_list or string)
                for av_key in ["avatarLarger", "avatar_larger", "avatarMedium", "avatar_medium", "avatarThumb", "avatar_thumb"]:
                    av = node.get(av_key)
                    if isinstance(av, dict):
                        urls = av.get("url_list", [])
                        if urls:
                            entry["avatarLarger"] = urls[0]
                            break
                    elif isinstance(av, str) and av.startswith("http"):
                        entry["avatarLarger"] = av
                        break

                # Nested stats (commonly found nested deeper in /api/user/detail/ payload)
                stats = node.get("stats", {})
                if isinstance(stats, dict):
                    for target_key, source_keys in {
                        "followerCount": ["followerCount", "follower_count"],
                        "followingCount": ["followingCount", "following_count"],
                        "heartCount": ["heartCount", "heart_count", "heart", "total_favorited"],
                        "videoCount": ["videoCount", "video_count", "aweme_count"],
                    }.items():
                        for sk in source_keys:
                            # It's typical for the API to dump stats as top-level OR under 'stats'
                            if sk in stats and stats[sk] is not None and str(stats[sk]) != "":
                                entry[target_key] = stats[sk]
                                break

            # Recurse
            for value in node.values():
                if isinstance(value, (dict, list)):
                    self._walk_api_profile(value, api_data)

        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    self._walk_api_profile(item, api_data)

    def _extract_post_timestamps(self, node, timestamps: list):
        """Recursively walk API post/item_list response to extract video createTime values."""
        if isinstance(node, dict):
            # If this dict looks like a video/post item (has createTime and desc or id)
            ct = node.get("createTime")
            if ct is not None:
                try:
                    ts = int(ct)
                    # Only accept reasonable timestamps (after 2016, TikTok launch era)
                    if ts > 1451606400:  # 2016-01-01
                        timestamps.append(ts)
                except (ValueError, TypeError):
                    pass
            for value in node.values():
                if isinstance(value, (dict, list)):
                    self._extract_post_timestamps(value, timestamps)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    self._extract_post_timestamps(item, timestamps)

    def _parse_count(self, text: str) -> int | None:
        """Parse counts like '1.2M', '540K', '12500', or raw numbers."""
        if not text:
            return None
        text = str(text).strip().replace(",", "")

        # Try direct integer parse first (SIGI_STATE/API gives exact counts)
        try:
            val = int(float(text))
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass

        # Parse suffixes like K, M, B
        match = re.search(r"([\d.]+)\s*([KMBkmb])?", text)
        if not match:
            return None

        num = float(match.group(1))
        suffix = (match.group(2) or "").upper()

        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        if suffix in multipliers:
            num *= multipliers[suffix]

        return int(num)

    def _parse_date(self, text: str) -> str:
        """
        Normalize TikTok relative dates ("2d ago", "3 days ago", "12-25")
        to ISO format (YYYY-MM-DD) for frontend consumption and risk scoring.
        """
        if not text:
            return ""
        
        text = str(text).lower().strip()
        
        # Already ISO date? (YYYY-MM-DD or YYYY-M-D)
        if re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', text):
            # Normalize to zero-padded YYYY-MM-DD
            parts = text.split('-')
            return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            
        now = datetime.now()
        
        # Handle "2d ago", "2h ago", "10m ago"
        match = re.search(r'(\d+)\s*([mhd])', text)
        if match:
            val = int(match.group(1))
            unit = match.group(2)
            if unit == 'd':
                delta = timedelta(days=val)
            elif unit == 'h':
                delta = timedelta(hours=val)
            else:
                delta = timedelta(minutes=val)
            return (now - delta).strftime("%Y-%m-%d")
            
        # Handle "2 days ago", "3 hours ago"
        match = re.search(r'(\d+)\s*(day|hour|min)', text)
        if match:
            val = int(match.group(1))
            unit = match.group(2)
            if 'day' in unit:
                delta = timedelta(days=val)
            elif 'hour' in unit:
                delta = timedelta(hours=val)
            else:
                delta = timedelta(minutes=val)
            return (now - delta).strftime("%Y-%m-%d")
            
        # Handle "12-25" or "12/25" (current year)
        match = re.search(r'(\d{1,2})[-/](\d{1,2})', text)
        if match:
            m, d = match.groups()
            try:
                return f"{now.year}-{int(m):02d}-{int(d):02d}"
            except ValueError:
                pass
            
        return text

    def _empty_result(self, url: str, client: str, username: str) -> ProfileResult:
        """Return a minimal ProfileResult when extraction fails."""
        return ProfileResult(
            platform="tiktok",
            client_name=client,
            keyword=username,
            url=url,
            username=username,
            display_name=username,
            entity_type="Creator",
        )

    async def _detect_captcha(self, page) -> bool:
        """Detect if TikTok has served a CAPTCHA challenge."""
        try:
            current_url = page.url.lower()
            if any(sig in current_url for sig in ["verify", "captcha", "/challenge/"]):
                return True

            captcha_js = """
            () => {
                const indicators = [
                    document.querySelector('[class*="captcha"]'),
                    document.querySelector('[class*="Captcha"]'),
                    document.querySelector('[class*="verify-wrapper"]'),
                    document.querySelector('[id*="captcha"]'),
                    document.querySelector('.verify-bar-close'),
                    document.querySelector('[class*="secsdk"]'),
                ];
                return indicators.some(el => el !== null);
            }
            """
            result = await asyncio.wait_for(
                page.evaluate(captcha_js), timeout=5.0
            )
            return bool(result)
        except Exception:
            return False

    async def _dismiss_popups(self, page):
        """Dismiss TikTok popups (cookie consent, GDPR banners, login prompts)."""
        for selector in POPUP_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.count() > 0 and await el.is_visible(timeout=100):
                    await el.click()
                    await asyncio.sleep(0.2)
            except Exception:
                pass
