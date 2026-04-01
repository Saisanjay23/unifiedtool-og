"""
OSINT Discovery Engine: TikTok.
Three-layer extraction strategy:
  1. TikTok Internal API response interception (passive network recon)
  2. SIGI_STATE / __NEXT_DATA__ hydration script injection
  3. Enhanced DOM fallback with broad selectors
Uses authenticated session + stealth anti-bot overrides to avoid CAPTCHA.
"""

import asyncio
import base64
import json
import random
import re
from typing import Optional

from backend.core.config import Settings
from backend.core.db import ProfileResult
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.stealth.human import HumanBehavior
from backend.platforms.base import AbstractDiscoverer

logger = get_logger("platforms.tiktok.discovery")

TT_SEARCH_URL = "https://www.tiktok.com/search/user?q={query}"

# Selectors for dismissing popups/cookie banners/GDPR notices/CAPTCHA
POPUP_SELECTORS = [
    # Cookie consent
    'button[id="onetrust-accept-btn-handler"]',
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'div[role="button"]:has-text("Accept all")',
    'button:has-text("Allow all cookies")',
    'button:has-text("Allow all")',
    'button:has-text("Allow All")',
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
    # CAPTCHA overlays
    '.verify-bar-close',
    '#captcha_close',
    'div[class*="captcha"] button',
    '[class*="verify"] [class*="close"]',
    # Bottom banner
    '[class*="BottomBanner"] button',
    'div[class*="DivBottomBanner"] button',
]


class TikTokDiscoverer(AbstractDiscoverer):
    """
    TikTok Discovery — Three-Layer Extraction Engine.

    Layer 1: Passive API Interception — captures rich JSON from TikTok's internal
             search API responses during normal page rendering.
    Layer 2: SIGI_STATE Hydration — extracts the complete React hydration state
             embedded in every TikTok page.
    Layer 3: DOM Extraction — fallback broad-selector DOM scraping.

    All layers feed into a unified dedup pipeline that merges data by username.
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
        from backend.stealth.browser import create_stealth_browser

        results = []
        # Shared data store for API interception (keyed by username)
        api_captured_data = {}
        human = HumanBehavior(platform="tiktok")

        logger.info(
            f"Starting TikTok discovery for {len(keywords)} keywords (Client: {client})"
        )

        # Use authenticated session if available — critical for avoiding bot detection
        import os
        session_file = os.path.join(self.config.SESSION_PATH, "tiktok.json")
        if not os.path.exists(session_file):
            session_file = None
            logger.warning("TikTok: No saved session found. Running unauthenticated (higher CAPTCHA risk).")
        else:
            logger.info("TikTok: Using saved session for authenticated discovery.")

        pw, browser, context, page = await create_stealth_browser(
            platform="tiktok",
            headless=headless,
            session_file=session_file,
        )
        logger.info("Browser created successfully for TikTok discovery.")

        try:
            for keyword in keywords:
                logger.info(f"Processing keyword: '{keyword}'")
                if self.health.should_pause("tiktok"):
                    delay = self.health.get_recommended_delay("tiktok")
                    logger.warning(
                        f"Rate limit approaching, pausing {delay:.0f}s before '{keyword}'"
                    )
                    await progress_callback(
                        event_type="rate_limited",
                        message=f"Rate limit approaching, pausing {delay:.0f}s",
                        count_found=len(results),
                    )
                    await asyncio.sleep(delay)

                await progress_callback(
                    event_type="progress",
                    message=f"Searching TikTok users for '{keyword}'...",
                    count_found=len(results),
                    count_total=max_results * len(keywords),
                )

                # --- RESILIENCY CHECK: Ensure Browser is Alive ---
                try:
                    if page.is_closed():
                        raise Exception("Page is closed")
                    await asyncio.wait_for(page.evaluate("1 + 1"), timeout=3.0)
                except Exception as reset_e:
                    logger.warning(
                        f"Browser session died ({reset_e}). Attempting to recreate context..."
                    )
                    try:
                        await browser.close()
                    except:
                        pass
                    try:
                        await pw.stop()
                    except:
                        pass

                    pw, browser, context, page = await create_stealth_browser(
                        platform="tiktok",
                        headless=headless,
                        session_file=session_file,
                    )
                    page.on("response", on_response)
                    logger.info(
                        "Successfully recreated browser context for recovery."
                    )

                try:
                    found = await self._search_keyword(
                        page,
                        keyword,
                        max_results,
                        client,
                        human,
                        api_captured_data,
                        progress_callback,
                        len(results),
                        max_results * len(keywords),
                    )
                    logger.info(
                        f"Found {len(found)} results for '{keyword}'"
                    )
                    results.extend(found)
                except Exception as e:
                    logger.error(f"Error searching for '{keyword}': {e}")
                    await progress_callback(
                        event_type="failed",
                        message=f"Failed searching for '{keyword}': {e}",
                        count_found=len(results),
                        count_total=max_results * len(keywords),
                    )
                    continue

        finally:
            await browser.close()
            await pw.stop()

        return results

    async def _search_keyword(
        self,
        page,
        keyword: str,
        max_results: int,
        client_name: str,
        human: HumanBehavior,
        api_captured_data: dict,
        progress_callback,
        current_total: int,
        max_total: int,
    ) -> list[ProfileResult]:
        """
        Multi-layer extraction cycle for TikTok user search.
        Combines API interception, SIGI_STATE hydration, and DOM extraction.
        """
        profiles = []
        seen_urls = set()

        # ================================================================
        # LAYER 1: Passive Network Recon — Intercept TikTok Internal API
        # ================================================================
        async def on_response(response):
            """
            Hook into all network responses to passively capture TikTok's
            internal API data. This is the highest-fidelity data source.
            """
            try:
                url = response.url
                # Log all API-like responses for debugging
                if '/api/' in url:
                    logger.debug(f"Network response: {url[:120]} (status={response.status})")

                # TikTok search API endpoints
                is_search_api = any(sig in url for sig in [
                    '/api/search/',
                    '/api/user/search',
                    'search/user/full',
                    'search/general/full',
                    '/api/recommend/',
                ])
                if is_search_api and response.status == 200:
                    try:
                        body = await response.text()
                        logger.info(f"API INTERCEPTED: {url[:100]} (body length={len(body)})")
                        self._parse_api_search_response(body, api_captured_data)
                        logger.info(f"API capture state: {len(api_captured_data)} users captured so far")
                    except Exception as e:
                        logger.warning(f"API response parse failed for {url[:80]}: {e}")
            except Exception as e:
                logger.debug(f"on_response error: {e}")

        page.on("response", on_response)

        # Hard timeout per keyword (5 minutes max)
        keyword_timeout = 300
        start_time = asyncio.get_event_loop().time()

        is_scrape_all = max_results >= 9999
        max_empty_scrolls = 15 if is_scrape_all else 10

        try:
            search_url = TT_SEARCH_URL.format(query=keyword)

            # Navigate with retries — TikTok sometimes returns blank on first load
            navigation_success = False
            for nav_attempt in range(3):
                try:
                    await page.goto(
                        search_url, wait_until="domcontentloaded", timeout=60000
                    )
                    navigation_success = True
                    break
                except Exception as e:
                    logger.warning(f"Navigation attempt {nav_attempt + 1} failed: {e}")
                    if nav_attempt < 2:
                        await asyncio.sleep(random.uniform(2, 5))

            if not navigation_success:
                logger.error(f"All navigation attempts to {search_url} failed")
                return profiles

            await human.pause("page_load")

            # Aggressive popup dismissal — run multiple passes
            for _ in range(3):
                await self._dismiss_popups(page)
                await asyncio.sleep(1)

            # Check for CAPTCHA page
            captcha_detected = await self._detect_captcha(page)
            if captcha_detected:
                logger.warning("CAPTCHA detected on TikTok search page!")
                await progress_callback(
                    event_type="warning",
                    message="TikTok CAPTCHA detected. Waiting for it to clear...",
                    count_found=current_total + len(profiles),
                    count_total=max_total,
                )
                # Wait and retry — sometimes CAPTCHAs auto-dismiss
                for retry in range(5):
                    await asyncio.sleep(random.uniform(5, 10))
                    await self._dismiss_popups(page)
                    captcha_detected = await self._detect_captcha(page)
                    if not captcha_detected:
                        logger.info("CAPTCHA cleared after waiting.")
                        break
                if captcha_detected:
                    logger.error("CAPTCHA persists. Skipping this keyword.")
                    await progress_callback(
                        event_type="error",
                        message="TikTok CAPTCHA could not be cleared. Try logging in via Sidebar or solving manually in non-headless mode.",
                        count_found=current_total + len(profiles),
                        count_total=max_total,
                    )
                    return profiles

            # Wait for content to load — generous timeout
            try:
                await page.wait_for_selector(
                    '[data-e2e="search_user-item"], [data-e2e="search-user-container"], a[href*="/@"]',
                    timeout=15000,
                )
            except Exception:
                logger.debug(
                    f"Wait for user container timed out for '{keyword}', proceeding with API/SIGI data."
                )
                await asyncio.sleep(5)

            # Give page extra time for API responses to fire and SIGI_STATE to populate
            await asyncio.sleep(5)

            # One more popup dismissal after content loads
            await self._dismiss_popups(page)

            # ================================================================
            # LAYER 2: SIGI_STATE / __NEXT_DATA__ Hydration Extraction
            # ================================================================
            sigi_state_js = """
            () => {
                const results = [];
                
                // Strategy 1: window.SIGI_STATE (TikTok's primary hydration state)
                if (window.SIGI_STATE) {
                    try {
                        const state = window.SIGI_STATE;
                        // Search results are in SearchResult or UserModule
                        const searchState = state.SearchResult || state.SearchUserResult || {};
                        const userModule = state.UserModule || {};
                        const users = userModule.users || {};
                        const stats = userModule.stats || {};
                        
                        // Try to get user list from search results
                        let userIds = [];
                        if (searchState.searchUsers) {
                            userIds = searchState.searchUsers || [];
                        }
                        // Also try raw data entries
                        for (const [key, user] of Object.entries(users)) {
                            try {
                                const userStats = stats[key] || {};
                                results.push({
                                    url: 'https://www.tiktok.com/@' + (user.uniqueId || key),
                                    displayName: user.nickname || user.uniqueId || key,
                                    username: user.uniqueId || key,
                                    imageUrl: user.avatarLarger || user.avatarMedium || user.avatarThumb || '',
                                    bio: user.signature || '',
                                    followersText: String(userStats.followerCount || user.followerCount || ''),
                                    likesText: String(userStats.heartCount || userStats.heart || ''),
                                    followingText: String(userStats.followingCount || ''),
                                    isVerified: user.verified || false,
                                    source: 'sigi_state'
                                });
                            } catch(e) {}
                        }
                    } catch(e) {}
                }
                
                // Strategy 2: window.__NEXT_DATA__ (newer TikTok versions)
                if (window.__NEXT_DATA__ && results.length === 0) {
                    try {
                        const nextData = window.__NEXT_DATA__;
                        const pageProps = nextData.props?.pageProps || {};
                        const itemList = pageProps.searchData?.searchUsers || 
                                        pageProps.userList || 
                                        pageProps.data?.userList || [];
                        
                        for (const item of itemList) {
                            const user = item.userInfo?.user || item.user || item;
                            const stats = item.userInfo?.stats || item.stats || {};
                            if (user && (user.uniqueId || user.id)) {
                                results.push({
                                    url: 'https://www.tiktok.com/@' + (user.uniqueId || user.id),
                                    displayName: user.nickname || user.uniqueId || user.id || '',
                                    username: user.uniqueId || user.id || '',
                                    imageUrl: user.avatarLarger || user.avatarMedium || '',
                                    bio: user.signature || '',
                                    followersText: String(stats.followerCount || ''),
                                    likesText: String(stats.heartCount || stats.heart || ''),
                                    followingText: String(stats.followingCount || ''),
                                    isVerified: user.verified || false,
                                    source: 'next_data'
                                });
                            }
                        }
                    } catch(e) {}
                }
                
                // Strategy 3: script tag extraction (if globals were cleaned up)
                if (results.length === 0) {
                    try {
                        const sigiScript = document.querySelector('script#SIGI_STATE, script#__NEXT_DATA__');
                        if (sigiScript) {
                            const parsed = JSON.parse(sigiScript.textContent);
                            // Recurse through the structure looking for user objects
                            const findUsers = (obj, depth) => {
                                if (depth > 8 || !obj) return;
                                if (typeof obj === 'object') {
                                    if (obj.uniqueId && (obj.nickname || obj.id)) {
                                        results.push({
                                            url: 'https://www.tiktok.com/@' + obj.uniqueId,
                                            displayName: obj.nickname || obj.uniqueId,
                                            username: obj.uniqueId,
                                            imageUrl: obj.avatarLarger || obj.avatarMedium || '',
                                            bio: obj.signature || '',
                                            followersText: String(obj.followerCount || ''),
                                            likesText: '',
                                            isVerified: obj.verified || false,
                                            source: 'script_tag'
                                        });
                                    }
                                    for (const val of Object.values(obj)) {
                                        if (typeof val === 'object') findUsers(val, depth + 1);
                                    }
                                    if (Array.isArray(obj)) {
                                        for (const item of obj) findUsers(item, depth + 1);
                                    }
                                }
                            };
                            findUsers(parsed, 0);
                        }
                    } catch(e) {}
                }
                
                return results;
            }
            """

            # ================================================================
            # LAYER 3: DOM Extraction — Enhanced fallback
            # ================================================================
            extraction_js = """
            () => {
                const results = [];
                
                // TikTok user search cards — multiple selector strategies
                let cards = [];
                
                // Strategy 1: data-e2e attribute selectors (most reliable when present)
                cards = Array.from(document.querySelectorAll('[data-e2e="search_user-item"]'));
                
                // Strategy 2: Children of the search container
                if (cards.length === 0) {
                    const container = document.querySelector('[data-e2e="search-user-container"]');
                    if (container) {
                        cards = Array.from(container.children);
                    }
                }
                
                // Strategy 3: Grid layout container
                if (cards.length === 0) {
                    cards = Array.from(document.querySelectorAll('div[class*="SearchGridLayout"] > div, div[class*="DivUserCardContainer"], div[class*="UserListCard"]'));
                }
                
                // Strategy 4: Any container with /@username links that looks like a card
                if (cards.length === 0) {
                    const links = document.querySelectorAll('a[href*="/@"]');
                    const parentSet = new Set();
                    links.forEach(link => {
                        let el = link.parentElement;
                        for (let i = 0; i < 6 && el; i++) {
                            if (el.offsetHeight > 60 && el.offsetWidth > 200) {
                                parentSet.add(el);
                                break;
                            }
                            el = el.parentElement;
                        }
                    });
                    cards = Array.from(parentSet);
                }

                for (const card of cards) {
                    try {
                        let profileUrl = "";
                        let displayName = "";
                        let username = "";
                        let imageUrl = "";
                        let bio = "";
                        let followersText = "";
                        let likesText = "";
                        let isVerified = false;

                        // 1. Profile URL & Username
                        const links = Array.from(card.querySelectorAll('a[href*="/@"]'));
                        if (links.length === 0) {
                            const cardLink = card.closest('a[href*="/@"]') || card.querySelector('a');
                            if (cardLink && cardLink.href && cardLink.href.includes('/@')) {
                                links.push(cardLink);
                            }
                        }
                        
                        for (const link of links) {
                            const href = link.getAttribute('href') || link.href || "";
                            if (href.includes('/@')) {
                                profileUrl = href.startsWith('http') ? href : 'https://www.tiktok.com' + href;
                                const match = href.match(/\/@([^/?]+)/);
                                if (match) username = match[1];
                                break;
                            }
                        }

                        if (!profileUrl) continue;

                        // 2. Display Name
                        const nameSelectors = [
                            'p[data-e2e="search-card-user-nickname"]',
                            'p[data-e2e="search-user-name"]',
                            'span[class*="SpanNickName"]',
                            'p[class*="PTitle"]',
                            'p[class*="Nickname"]',
                            'span[class*="Nickname"]',
                        ];
                        for (const sel of nameSelectors) {
                            const el = card.querySelector(sel);
                            if (el) {
                                const text = (el.textContent || "").trim();
                                if (text && text.length > 0 && text.length < 100) {
                                    displayName = text;
                                    break;
                                }
                            }
                        }
                        
                        if (!displayName) {
                            const pElements = card.querySelectorAll('p, span, h3, h4');
                            for (const p of pElements) {
                                const text = (p.textContent || "").trim();
                                if (text && text.length > 1 && text.length < 80 && 
                                    !text.startsWith('@') && !text.includes('Follow') &&
                                    !text.includes('Follower') && !text.includes('Like') &&
                                    !text.match(/^[\d.]+[KMBkmb]?$/) &&
                                    text !== username) {
                                    displayName = text;
                                    break;
                                }
                            }
                        }
                        
                        if (!displayName) displayName = username;

                        // 3. Username text
                        const userSelectors = [
                            'p[data-e2e="search-card-user-unique-id"]',
                            'p[data-e2e="search-user-unique-id"]',
                            'span[class*="SpanUniqueId"]',
                            'p[class*="UniqueId"]',
                        ];
                        for (const sel of userSelectors) {
                            const el = card.querySelector(sel);
                            if (el) {
                                const text = (el.textContent || "").trim().replace(/^@/, '');
                                if (text && !username) {
                                    username = text;
                                    break;
                                }
                            }
                        }

                        // 4. Profile Image
                        const imgSelectors = [
                            'img[class*="ImgAvatar"]',
                            'img[class*="avatar"]',
                            'img[class*="Avatar"]',
                            'img[class*="UserAvatar"]',
                            'span[class*="Avatar"] img',
                            'div[class*="Avatar"] img',
                            'img',
                        ];
                        for (const sel of imgSelectors) {
                            const img = card.querySelector(sel);
                            if (img) {
                                const src = img.getAttribute('src') || "";
                                if (src && src.startsWith('http')) {
                                    imageUrl = src;
                                    break;
                                }
                            }
                        }

                        // 5. Bio/Description
                        const descSelectors = [
                            'p[data-e2e="search-user-desc"]',
                            'p[data-e2e="search-card-desc"]',
                            'p[class*="PDesc"]',
                            'p[class*="Desc"]',
                        ];
                        for (const sel of descSelectors) {
                            const el = card.querySelector(sel);
                            if (el) {
                                bio = (el.textContent || "").trim();
                                if (bio) break;
                            }
                        }

                        // 6. Stats
                        const allText = card.textContent || "";
                        const fMatch = allText.match(/([\d,.]+[KMBkmb]?)\s*[Ff]ollower/);
                        if (fMatch) followersText = fMatch[1] + " Followers";
                        const lMatch = allText.match(/([\d,.]+[KMBkmb]?)\s*[Ll]ike/);
                        if (lMatch) likesText = lMatch[1] + " Likes";

                        // 7. Verified badge
                        const verifiedSelectors = [
                            'svg[class*="Verified"]',
                            'svg[data-e2e="verify-badge"]',
                            '[class*="verified"]',
                            '[class*="Verified"]',
                            'img[alt*="verified"]',
                            'img[alt*="Verified"]',
                        ];
                        for (const sel of verifiedSelectors) {
                            if (card.querySelector(sel)) {
                                isVerified = true;
                                break;
                            }
                        }

                        results.push({
                            url: profileUrl.split('?')[0],
                            displayName: displayName,
                            username: username,
                            imageUrl: imageUrl,
                            bio: bio,
                            followersText: followersText,
                            likesText: likesText,
                            isVerified: isVerified,
                            source: 'dom'
                        });
                    } catch (e) {
                        // silently skip bad cards
                    }
                }
                return results;
            }
            """

            empty_scrolls = 0
            last_scroll_height = 0
            profiles_since_last_break = 0

            while len(profiles) < max_results and empty_scrolls < max_empty_scrolls:
                # 0. HARD TIMEOUT CHECK
                if asyncio.get_event_loop().time() - start_time > keyword_timeout:
                    logger.warning(
                        f"Absolute timeout reached for keyword '{keyword}' ({keyword_timeout}s). Halting."
                    )
                    break

                # 1. LAYER 2: SIGI_STATE extraction (highest priority)
                await asyncio.sleep(2)
                new_found = 0

                try:
                    sigi_results = await asyncio.wait_for(
                        page.evaluate(sigi_state_js), timeout=15.0
                    )
                    if sigi_results:
                        logger.info(f"SIGI_STATE/NEXT_DATA extracted {len(sigi_results)} users (source: hydration)")
                except Exception as e:
                    logger.debug(f"SIGI_STATE extraction failed: {e}")
                    sigi_results = []

                # 2. LAYER 1: Process API-intercepted data
                api_results = []
                for uname, data in api_captured_data.items():
                    api_results.append({
                        "url": f"https://www.tiktok.com/@{uname}",
                        "displayName": data.get("nickname", uname),
                        "username": uname,
                        "imageUrl": data.get("avatarLarger", data.get("avatarMedium", "")),
                        "bio": data.get("signature", ""),
                        "followersText": str(data.get("followerCount", "")),
                        "likesText": str(data.get("heartCount", "")),
                        "isVerified": data.get("verified", False),
                        "source": "api_intercept",
                    })

                if api_results:
                    logger.info(f"API interception captured {len(api_results)} users")

                # 3. LAYER 3: DOM extraction (fallback)
                try:
                    dom_results = await asyncio.wait_for(
                        page.evaluate(extraction_js), timeout=15.0
                    )
                    logger.debug(f"DOM extraction found {len(dom_results)} cards")
                except Exception as e:
                    logger.warning(f"DOM extraction failed: {e}")
                    dom_results = []

                # 4. MERGE all layers — API > SIGI > DOM (priority order)
                all_extracted = {}
                # DOM first (lowest priority, gets overwritten)
                for item in dom_results:
                    uname = item.get("username", "")
                    if uname:
                        all_extracted[uname] = item
                # SIGI state (medium priority, overwrites DOM)
                for item in sigi_results:
                    uname = item.get("username", "")
                    if uname:
                        existing = all_extracted.get(uname, {})
                        # Merge: SIGI overwrites empty fields
                        for key, val in item.items():
                            if val and (not existing.get(key) or key != "source"):
                                existing[key] = val
                        existing["source"] = item.get("source", "sigi_state")
                        all_extracted[uname] = existing
                # API intercept (highest priority, overwrites everything)
                for item in api_results:
                    uname = item.get("username", "")
                    if uname:
                        existing = all_extracted.get(uname, {})
                        for key, val in item.items():
                            if val and (not existing.get(key) or key != "source"):
                                existing[key] = val
                        existing["source"] = item.get("source", "api_intercept")
                        all_extracted[uname] = existing

                # 5. PROCESS merged results
                for card_data in all_extracted.values():
                    if len(profiles) >= max_results:
                        break

                    profile_url = card_data.get("url")
                    if not profile_url or profile_url in seen_urls:
                        continue

                    username = card_data.get("username", "")
                    display_name = card_data.get("displayName", "") or username
                    raw_image_url = card_data.get("imageUrl", "")
                    bio = card_data.get("bio", "")
                    is_verified = card_data.get("isVerified", False)
                    source = card_data.get("source", "unknown")

                    # Parse followers count
                    followers = self._parse_follower_count(
                        card_data.get("followersText", "")
                    )

                    profile = ProfileResult(
                        platform="tiktok",
                        client_name=client_name,
                        keyword=keyword,
                        url=profile_url,
                        username=username,
                        display_name=display_name,
                        bio=bio,
                        followers=followers,
                        is_verified=is_verified,
                        profile_image_url=raw_image_url,
                        profile_image_b64=None,
                        has_logo=bool(raw_image_url),
                        entity_type="Creator",
                    )

                    profiles.append(profile)
                    seen_urls.add(profile_url)
                    new_found += 1
                    profiles_since_last_break += 1

                    await progress_callback(
                        event_type="result_found",
                        message=f"Found: @{username} ({display_name}) [{source}]",
                        count_found=current_total + len(profiles),
                        count_total=max_total,
                        result=profile.to_dict(),
                    )

                # 6. ANTI-BAN: Reading pauses & mouse jitter
                if profiles_since_last_break >= 20:
                    profiles_since_last_break = 0
                    try:
                        await asyncio.wait_for(
                            human.mouse_jitter(page, count=random.randint(2, 5)),
                            timeout=5.0,
                        )
                    except Exception:
                        pass
                    pause_secs = random.uniform(3, 8)
                    await asyncio.sleep(pause_secs)

                # 7. DETECT END-OF-RESULTS
                if new_found == 0:
                    empty_scrolls += 1

                    # Check if page height stopped growing
                    try:
                        current_height = await asyncio.wait_for(
                            page.evaluate("() => document.body.scrollHeight"),
                            timeout=5.0,
                        )
                        if (
                            current_height == last_scroll_height
                            and empty_scrolls >= 3
                        ):
                            logger.debug(
                                f"End of feed detected at scroll height {current_height}"
                            )
                            break
                        last_scroll_height = current_height
                    except Exception:
                        pass

                    if is_scrape_all and empty_scrolls > 3:
                        extra_wait = min(empty_scrolls * 1.5, 10)
                        await asyncio.sleep(extra_wait)
                else:
                    empty_scrolls = 0

                # 8. SCROLL DOWN
                scroll_distance = None
                if is_scrape_all:
                    try:
                        viewport = page.viewport_size or {
                            "width": 1920,
                            "height": 1080,
                        }
                        vh = viewport["height"]
                        scroll_distance = int(vh * random.uniform(0.3, 1.0))
                    except Exception:
                        pass

                try:
                    await asyncio.wait_for(
                        human.human_scroll(
                            page, direction="down", distance=scroll_distance
                        ),
                        timeout=10.0,
                    )
                except Exception as e:
                    logger.debug(f"Scroll timeout: {e}")

                await human.pause("scroll")

                if random.random() < 0.15:
                    try:
                        await asyncio.wait_for(
                            human.mouse_jitter(
                                page, count=random.randint(1, 3)
                            ),
                            timeout=5.0,
                        )
                    except Exception:
                        pass

                await human.maybe_take_break()
                await self._dismiss_popups(page)

            await self.health.record_request("tiktok", success=True)

        except Exception as exc:
            logger.error(
                f"TikTok search logic failed critically for '{keyword}': {exc}"
            )
            await self.health.record_request("tiktok", success=False)

        return profiles

    def _parse_api_search_response(self, body: str, api_data: dict):
        """
        Parse TikTok's internal search API response.

        TikTok's /api/search/user/full/ returns:
        {
            "user_list": [
                {
                    "user_info": {
                        "unique_id": "username",
                        "nickname": "Display Name",
                        "uid": "123",
                        "sec_uid": "...",
                        "follower_count": 43200,
                        "total_favorited": 362900,
                        "signature": "bio text",
                        "avatar_thumb": { "url_list": ["https://..."] },
                        ...
                    }
                }
            ],
            "cursor": 12,
            "has_more": 1
        }
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return

        # Direct parse of the known structure (highest reliability)
        user_list = data.get("user_list", [])
        if isinstance(user_list, list) and user_list:
            for item in user_list:
                try:
                    user_info = item.get("user_info", item)
                    if not isinstance(user_info, dict):
                        continue

                    # TikTok API uses snake_case: unique_id, follower_count, etc.
                    username = (
                        user_info.get("unique_id")
                        or user_info.get("uniqueId")
                        or ""
                    )
                    if not username:
                        continue

                    entry = api_data.setdefault(username, {})

                    # Nickname / display name
                    entry["nickname"] = (
                        user_info.get("nickname")
                        or entry.get("nickname", "")
                    )

                    # Bio
                    entry["signature"] = (
                        user_info.get("signature")
                        or entry.get("signature", "")
                    )

                    # Verified
                    if user_info.get("custom_verify") or user_info.get("verified"):
                        entry["verified"] = True
                    elif "verified" not in entry:
                        entry["verified"] = False

                    # Follower count (snake_case: follower_count)
                    for fc_key in ("follower_count", "followerCount"):
                        if fc_key in user_info and user_info[fc_key]:
                            entry["followerCount"] = int(user_info[fc_key])
                            break

                    # Likes / total favorited (snake_case: total_favorited)
                    for lk_key in ("total_favorited", "heartCount", "heart_count", "heart"):
                        if lk_key in user_info and user_info[lk_key]:
                            entry["heartCount"] = int(user_info[lk_key])
                            break

                    # Following count
                    for fw_key in ("following_count", "followingCount"):
                        if fw_key in user_info and user_info[fw_key]:
                            entry["followingCount"] = int(user_info[fw_key])
                            break

                    # Video count
                    for vc_key in ("video_count", "videoCount", "aweme_count"):
                        if vc_key in user_info and user_info[vc_key]:
                            entry["videoCount"] = int(user_info[vc_key])
                            break

                    # Avatar — TikTok returns avatar_thumb as { url_list: [...] }
                    for av_key in ("avatar_larger", "avatarLarger", "avatar_medium", "avatarMedium", "avatar_thumb", "avatarThumb"):
                        av = user_info.get(av_key)
                        if isinstance(av, dict):
                            urls = av.get("url_list", [])
                            if urls:
                                entry["avatarLarger"] = urls[0]
                                break
                        elif isinstance(av, str) and av.startswith("http"):
                            entry["avatarLarger"] = av
                            break

                    logger.debug(
                        f"API intercepted user: @{username} "
                        f"(followers={entry.get('followerCount', '?')}, "
                        f"likes={entry.get('heartCount', '?')})"
                    )
                except Exception as e:
                    logger.debug(f"Error parsing user_list item: {e}")
                    continue

            logger.info(f"API search response parsed: {len(user_list)} users in user_list")
            return

        # Fallback: recursive walk for unknown/nested structures
        self._walk_api_node(data, api_data)

    def _walk_api_node(self, node, api_data: dict):
        """Recursively walk a JSON structure looking for TikTok user profile data.
        Handles both camelCase (SIGI_STATE) and snake_case (API) field naming."""
        if isinstance(node, dict):
            # Detect user objects by presence of username-like fields
            username = (
                node.get("uniqueId")
                or node.get("unique_id")
                or ""
            )
            has_name = "nickname" in node or "id" in node or "uid" in node
            if username and has_name:
                entry = api_data.setdefault(username, {})
                # Map both camelCase and snake_case fields
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

                # Avatar handling (can be dict with url_list or string)
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

                # Nested stats object
                stats = node.get("stats", {})
                if isinstance(stats, dict):
                    for target_key, source_keys in {
                        "followerCount": ["followerCount", "follower_count"],
                        "followingCount": ["followingCount", "following_count"],
                        "heartCount": ["heartCount", "heart_count", "heart"],
                        "videoCount": ["videoCount", "video_count"],
                    }.items():
                        for sk in source_keys:
                            if sk in stats and stats[sk]:
                                entry[target_key] = stats[sk]
                                break

                logger.debug(f"API walked user: @{username}")

            # Recurse into all values
            for value in node.values():
                if isinstance(value, (dict, list)):
                    self._walk_api_node(value, api_data)

        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    self._walk_api_node(item, api_data)

    def _parse_follower_count(self, text: str) -> Optional[int]:
        """Parse follower counts like '1.2M Followers', '540K', '12.5K Followers', or raw numbers like '1234567'."""
        if not text:
            return None
        text = text.strip().replace(",", "")

        # Try direct integer parse first (API responses give exact counts)
        try:
            val = int(float(text))
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass

        match = re.match(r"([\d.]+)\s*([KMBkmb])?", text)
        if not match:
            return None

        num = float(match.group(1))
        suffix = (match.group(2) or "").upper()

        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        if suffix in multipliers:
            num *= multipliers[suffix]

        return int(num)

    async def _detect_captcha(self, page) -> bool:
        """Detect if TikTok has served a CAPTCHA challenge page."""
        try:
            # Check URL for captcha/verify indicators
            current_url = page.url.lower()
            if any(sig in current_url for sig in ["verify", "captcha", "/challenge/"]):
                return True

            # Check for CAPTCHA DOM elements
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
        """Dismiss TikTok popups (cookie consent, GDPR banners, login prompts, CAPTCHA overlays)."""
        for selector in POPUP_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=800):
                    await el.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass
