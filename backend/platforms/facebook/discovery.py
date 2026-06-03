"""
OSINT Discovery Engine: Facebook.
Orchestrates headless Playwright sessions to execute horizontal keyword scraping across the DOM.
Implements aggressive CDN manipulation (URL rewriting) and GraphQL response interception
to extract high-value metadata (true follower counts, 2048px uncompressed profile assets)
without triggering additional network requests.
"""

import asyncio
import json
import random
import re

from backend.core.config import settings

from backend.core.db import ProfileResult
from backend.core.logger import get_logger
from backend.platforms.base import AbstractDiscoverer
from backend.stealth.human import HumanBehavior

logger = get_logger("platforms.facebook.discovery")

FB_SEARCH_PEOPLE_URL = "https://www.facebook.com/search/people/?q={query}"
FB_SEARCH_PAGES_URL = "https://www.facebook.com/search/pages/?q={query}"

# ordered list of selectors to try for dismissing popups
POPUP_SELECTORS = [
    'div[aria-label="Close"]',
    'div[aria-label="Decline optional cookies"]',
    'button:has-text("Not Now")',
    'div[role="button"]:has-text("Not Now")',
    'div[role="button"]:has-text("Close")',
    '[data-testid="cookie-policy-manage-dialog-accept-button"]',
]


def _upgrade_image_url(url: str) -> str:
    """
    CDN Asset Escalation Pipeline.
    Strips downstream compression algorithms, physical dimension constraints, and
    crop-box coordinates directly from the Akamai/FB CDN URL structures.
    Forces the edge server to deliver the raw, uncompressed master asset (1080p+).
    """
    if not url:
        return url
    # Replace small/tiny suffixes with normal/large
    upgraded = re.sub(r"_[st](\d*)\.(jpg|jpeg|png|webp)", r"_n\1.\2", url)
    # Remove size constraints like /s120x120/ or /p50x50/
    upgraded = re.sub(r"/[sp]\d+x\d+/", "/", upgraded)
    # Remove crop boxes like /c0.0.120.120a/
    upgraded = re.sub(r"/c[\d.]+a?/", "/", upgraded)
    # Remove crop param /cp0/
    upgraded = re.sub(r"/cp\d+/", "/", upgraded)
    # Remove width params like /w_n_/ or /w\d+/
    upgraded = re.sub(r"/w_?\d*_?/", "/", upgraded)
    # Clean up double slashes (but not in https://)
    upgraded = re.sub(r"(?<!:)/{2,}", "/", upgraded)
    # Upgrade width/height query parameters if present
    upgraded = re.sub(r"&w=\d+", "&w=1080", upgraded)
    upgraded = re.sub(r"&h=\d+", "&h=1080", upgraded)
    upgraded = re.sub(r"&width=\d+", "&width=1080", upgraded)
    upgraded = re.sub(r"&height=\d+", "&height=1080", upgraded)
    
    return upgraded


class FacebookDiscoverer(AbstractDiscoverer):
    """
    Facebook Discovery Strategy Implementation.
    Fuses DOM scraping with passive network reconnaissance (GraphQL interception) to minimize total request volume.
    Employs `HumanBehavior` injections to randomize interaction cadences, reducing fingerprinting risks.
    """

    async def search(
        self,
        progress_callback,
        client: str,
        keywords: list[str],
        max_results: int = 50,
        headless: bool = True,
        search_type: str = "people",
        use_free_proxy: bool = False,
        **kwargs,
    ) -> list[ProfileResult]:
        from backend.stealth.browser import create_stealth_browser

        results = []
        seen_urls = set()  # Global dedup across all keywords & search types
        graphql_data = {}
        human = HumanBehavior(platform="facebook")

        logger.info(
            f"Starting Facebook discovery for {len(keywords)} keywords (Client: {client})"
        )
        pw, browser, context, page = await create_stealth_browser(
            platform="facebook",
            headless=headless,
            use_free_proxy=use_free_proxy,
        )
        logger.info("Browser created successfully for discovery.")

        # Passive Network Recon: Hook into the CDP (Chrome DevTools Protocol) stream
        # This allows us to silently extract deeply embedded profile data without clicking into individual profiles.
        async def on_response(response):
            try:
                url = response.url
                if "graphql" in url.lower() or "api/graphql" in url.lower():
                    if response.status == 200:
                        try:
                            body = await response.text()
                            self._parse_graphql_response(body, graphql_data)
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", on_response)

        try:
            for keyword in keywords:
                logger.info(f"Processing keyword: '{keyword}'")
                if self.health.should_pause("facebook"):
                    delay = self.health.get_recommended_delay("facebook")
                    logger.warning(
                        f"Rate limit approaching, pausing {delay:.0f}s before '{keyword}'"
                    )
                    await progress_callback(
                        event_type="rate_limited",
                        message=f"Rate limit approaching, pausing {delay:.0f}s",
                        count_found=len(results),
                    )
                    await asyncio.sleep(delay)

                # Determine which search types to run
                search_passes = []
                if search_type in ("pages", "both"):
                    search_passes.append(("pages", FB_SEARCH_PAGES_URL))
                if search_type in ("people", "both"):
                    search_passes.append(("people", FB_SEARCH_PEOPLE_URL))

                for entity_type_label, search_url_template in search_passes:
                    logger.info(f"Searching {entity_type_label} for '{keyword}'...")
                    await progress_callback(
                        event_type="progress",
                        message=f"Searching Facebook {entity_type_label} for '{keyword}'...",
                        count_found=len(results),
                        count_total=max_results * len(keywords),
                    )

                    # --- RESILIENCY CHECK: Ensure Browser is Alive ---
                    try:
                        if page.is_closed():
                            raise Exception("Page is closed")
                        # A simple evaluate to prove the Node connection is alive
                        await asyncio.wait_for(page.evaluate("1 + 1"), timeout=3.0)
                    except Exception as reset_e:
                        logger.warning(f"Browser session died ({reset_e}). Attempting to recreate context...")
                        try:
                            await browser.close()
                        except:
                            pass
                        try:
                            await pw.stop()
                        except:
                            pass
                            
                        # Re-create stealth browser
                        pw, browser, context, page = await create_stealth_browser(
                            platform="facebook",
                            headless=headless,
                        )
                        page.on("response", on_response)
                        logger.info("Successfully recreated browser context for recovery.")
                    # -------------------------------------------------

                    try:
                        found = await self._search_keyword(
                            page,
                            keyword,
                            max_results,
                            client,
                            human,
                            graphql_data,
                            search_url_template,
                            entity_type_label,
                            progress_callback,
                            len(results),
                            max_results * len(keywords),
                            seen_urls,
                        )

                        logger.info(
                            f"Found {len(found)} results in {entity_type_label} for '{keyword}'"
                        )
                        results.extend(found)
                    except Exception as e:
                        logger.error(f"Error searching {entity_type_label} for '{keyword}': {e}")
                        await progress_callback(
                            event_type="failed",
                            message=f"Failed searching {entity_type_label} for '{keyword}': {e}",
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
        graphql_data: dict,
        search_url_template: str,
        entity_type_label: str,
        progress_callback,
        current_total: int,
        max_total: int,
        seen_urls: set,
    ) -> list[ProfileResult]:
        """
        Isolated DOM Extraction Cycle - REWRITTEN FOR MAXIMUM RESILIENCY.
        Relies on injected JS to extract data in bulk, eliminating fragile Node IPC timeouts.
        Enforces a strict absolute timeout per keyword to guarantee the job NEVER hangs.
        Uses a shared seen_urls set to prevent duplicates across keywords and search types.
        """
        profiles = []
        
        # Absolute hard timeout per keyword (e.g., 5 minutes max)
        keyword_timeout = 300
        start_time = asyncio.get_event_loop().time()

        is_scrape_all = max_results >= 9999
        max_empty_scrolls = 15 if is_scrape_all else 6

        try:
            search_url = search_url_template.format(query=keyword)
            # Robust navigation wrapper
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.error(f"Navigation to {search_url} timed out: {e}")
                return profiles

            await human.pause("page_load")
            await self._dismiss_popups(page)

            # --- LOGIN WALL DETECTION ---
            current_url = page.url.lower()
            if "/login" in current_url or "/checkpoint" in current_url:
                logger.error(f"Facebook redirected to login/checkpoint page: {page.url}")
                await progress_callback(
                    event_type="error",
                    message="Facebook session expired. Please log in via Sidebar to refresh cookies.",
                    count_found=current_total + len(profiles),
                    count_total=max_total,
                )
                return profiles

            # Wait for actual search results to render, but don't crash if they don't
            try:
                await page.wait_for_selector(
                    'div[role="feed"], div[role="article"], div[role="listitem"]',
                    timeout=15000,
                )
            except Exception:
                logger.debug(f"Wait for feed selector timed out for '{keyword}', proceeding anyway.")
                # Speed-mode-aware fallback wait (was hardcoded 5s)
                _mode = settings.DISCOVERY_SPEED_MODE
                await asyncio.sleep(1.0 if _mode == "aggressive" else 2.0 if _mode == "balanced" else 5.0)

            # Speed-mode-aware post-render settle (was hardcoded 2s)
            _mode = settings.DISCOVERY_SPEED_MODE
            await asyncio.sleep(0.3 if _mode == "aggressive" else 0.8 if _mode == "balanced" else 2.0)

            empty_scrolls = 0
            last_scroll_height = 0
            profiles_since_last_break = 0

            # Core extraction JS payload
            # This runs entirely in the browser context, eliminating Python <-> JS bridge timeouts.
            extraction_js = """
            () => {
                const NOISE_TEXTS = new Set([
                    "add friend", "follow", "following", "like", "liked", "message", "share", 
                    "send message", "report", "block", "more", "see more", "view profile", 
                    "public figure", "musician/band", "community", "personal blog", 
                    "product/service", "local business", "company", "nonprofit organization", 
                    "education", "government organization", "just now", "see all", "photos", 
                    "videos", "about", "friends", "likes", "reviews", "posts", "events", 
                    "mentions", "check-ins", "mutual friend", "mutual friends", "suggested for you"
                ]);
                
                const skipSegments = [
                    "/search/", "/stories/", "/photo", "/hashtag/", "/events/", "/groups/", 
                    "/watch/", "/reel/", "/login", "/checkpoint", "/marketplace/", "/gaming/"
                ];

                const results = [];
                
                let cards = [];
                const feed = document.querySelector('div[role="feed"]');
                if (feed) {
                    cards = Array.from(feed.children);
                }
                if (cards.length === 0) {
                    cards = Array.from(document.querySelectorAll('div[role="article"], div[role="listitem"]'));
                }

                for (const card of cards) {
                    try {
                        let profileUrl = "";
                        let linkDisplayName = "";
                        let imageUrl = "";
                        let displayName = "";

                        // 1. URL & Link Text
                        const links = Array.from(card.querySelectorAll('a[href]'));
                        for (const link of links) {
                            let href = link.getAttribute('href') || "";
                            if (!href.includes('facebook.com/') && !href.includes('profile.php?id=') && !href.startsWith('/')) continue;
                            
                            if (href.startsWith('/')) href = "https://www.facebook.com" + href;
                            
                            let clean = href.split('?')[0].replace(/\\/$/, '');
                            if (href.includes('profile.php')) {
                                const urlObj = new URL(href, window.location.origin);
                                const id = urlObj.searchParams.get('id');
                                if (id) clean = `https://www.facebook.com/profile.php?id=${id}`;
                            }

                            if (skipSegments.some(skip => clean.toLowerCase().includes(skip))) continue;
                            if (["https://www.facebook.com", "https://facebook.com", "https://m.facebook.com"].includes(clean)) continue;

                            const text = (link.textContent || "").trim();
                            if (text && text.length > 1 && text.length < 100) {
                                const textLower = text.toLowerCase();
                                if (!NOISE_TEXTS.has(textLower) && !textLower.startsWith('http')) {
                                    if (!profileUrl) {
                                        profileUrl = clean;
                                        linkDisplayName = text;
                                    } else if (!linkDisplayName) {
                                        linkDisplayName = text;
                                    }
                                }
                            } else if (!profileUrl) {
                                profileUrl = clean;
                            }
                        }

                        if (!profileUrl) continue;

                        // 2. Display Name (Heading preferred)
                        const heading = card.querySelector('h2, h3, strong');
                        if (heading) {
                            const text = (heading.textContent || "").trim();
                            if (text && text.length > 1 && text.length < 100 && !NOISE_TEXTS.has(text.toLowerCase())) {
                                displayName = text;
                            }
                        }
                        if (!displayName) displayName = linkDisplayName;

                        // 3. Image URL
                        const img = card.querySelector('image, img');
                        if (img) {
                            imageUrl = img.getAttribute('xlink:href') || img.getAttribute('src') || "";
                        }

                        results.push({
                            url: profileUrl,
                            displayName: displayName,
                            imageUrl: imageUrl
                        });
                    } catch (e) {
                         // silently skip bad cards
                    }
                }
                return results;
            }
            """

            while len(profiles) < max_results and empty_scrolls < max_empty_scrolls:
                # -----------------------------------------------------------
                # 0. HARD TIMEOUT CHECK
                # -----------------------------------------------------------
                if asyncio.get_event_loop().time() - start_time > keyword_timeout:
                    logger.warning(f"Absolute timeout reached for keyword '{keyword}' ({keyword_timeout}s). Halting extraction.")
                    break

                # -----------------------------------------------------------
                # 1. BULK EVALUATE DOM
                # -----------------------------------------------------------
                new_found = 0
                try:
                    # Execute the payload with a hard timeout to prevent hanging
                    extracted_cards = await asyncio.wait_for(
                        page.evaluate(extraction_js), 
                        timeout=15.0
                    )
                except Exception as e:
                    logger.warning(f"JS Extraction payload failed or timed out: {e}")
                    extracted_cards = []

                # -----------------------------------------------------------
                # 2. PROCESS EXTRACTED CARDS (Safe Python Side)
                # -----------------------------------------------------------
                for card_data in extracted_cards:
                    if len(profiles) >= max_results:
                        break

                    profile_url = card_data.get("url")
                    if not profile_url or profile_url in seen_urls:
                        continue
                        
                    display_name = card_data.get("displayName", "")
                    raw_image_url = card_data.get("imageUrl", "")
                    
                    # Safe username extraction
                    username = profile_url.rstrip("/").split("/")[-1]
                    if "profile.php?id=" in profile_url:
                        uid_match = re.search(r"id=(\d+)", profile_url)
                        if uid_match:
                            username = uid_match.group(1)

                    # GraphQL overrides
                    gql_entry = graphql_data.get(username, {})
                    if gql_entry.get("name"):
                        display_name = gql_entry["name"]
                        
                    hd_image_url = gql_entry.get("hd_image") or _upgrade_image_url(raw_image_url)

                    # Image downloading is deferred to the frontend or done extremely passively to avoid blocking

                    profile = ProfileResult(
                        platform="facebook",
                        client_name=client_name,
                        keyword=keyword,
                        url=profile_url,
                        username=username,
                        display_name=display_name,
                        profile_image_url=hd_image_url,
                        profile_image_b64=None,
                        has_logo=bool(hd_image_url),
                        entity_type="Page" if entity_type_label == "pages" else "Person",
                    )

                    profiles.append(profile)
                    seen_urls.add(profile_url)
                    new_found += 1
                    profiles_since_last_break += 1

                    await progress_callback(
                        event_type="result_found",
                        message=f"Found: {profile.display_name}",
                        count_found=current_total + len(profiles),
                        count_total=max_total,
                        result=profile.to_dict(),
                    )

                # -----------------------------------------------------------
                # 3. ANTI-BAN: Reading pauses & mouse jitter
                # Speed-mode-aware: threshold and pause duration adjust
                # -----------------------------------------------------------
                _mode = settings.DISCOVERY_SPEED_MODE
                _break_threshold = 20 if _mode == "stealth" else 40 if _mode == "balanced" else 80
                if profiles_since_last_break >= _break_threshold:
                    profiles_since_last_break = 0
                    if _mode != "aggressive":  # skip jitter entirely in aggressive
                        try:
                            await asyncio.wait_for(human.mouse_jitter(page, count=random.randint(1, 3)), timeout=5.0)
                        except Exception:
                            pass
                    if _mode == "stealth":
                        pause_secs = random.uniform(3, 8)
                    elif _mode == "balanced":
                        pause_secs = random.uniform(1, 3)
                    else:
                        pause_secs = random.uniform(0.3, 0.8)
                    await asyncio.sleep(pause_secs)

                # -----------------------------------------------------------
                # 4. DETECT END-OF-RESULTS vs. NEED MORE SCROLLING
                # -----------------------------------------------------------
                if new_found == 0:
                    empty_scrolls += 1

                    # Try clicking "See more results" / pagination buttons Safely
                    # NOTE: Must use pure JS text matching — Playwright's :has-text()
                    # pseudo-selector does NOT work inside page.evaluate/document.querySelector
                    see_more_clicked = False
                    try:
                        clicked = await asyncio.wait_for(
                            page.evaluate("""
                                () => {
                                    const phrases = ['see more results', 'see more', 'show more results'];
                                    // Check buttons and role=button divs
                                    const candidates = [
                                        ...document.querySelectorAll('div[role="button"]'),
                                        ...document.querySelectorAll('a'),
                                        ...document.querySelectorAll('span'),
                                        ...document.querySelectorAll('button'),
                                    ];
                                    for (const el of candidates) {
                                        const text = (el.textContent || '').trim().toLowerCase();
                                        if (phrases.some(p => text === p || text.startsWith(p))) {
                                            if (el.offsetParent !== null) {
                                                el.click();
                                                return true;
                                            }
                                        }
                                    }
                                    return false;
                                }
                            """),
                            timeout=5.0
                        )
                        if clicked:
                            # Smart wait: wait for scroll height to change (content loading)
                            # instead of a dumb fixed 3s sleep
                            try:
                                _pre_height = await page.evaluate("() => document.body.scrollHeight")
                                await page.wait_for_function(
                                    f"() => document.body.scrollHeight > {_pre_height}",
                                    timeout=3000,
                                )
                            except Exception:
                                # Fallback: speed-mode-aware fixed wait
                                _mode = settings.DISCOVERY_SPEED_MODE
                                await asyncio.sleep(0.5 if _mode == "aggressive" else 1.0 if _mode == "balanced" else 3.0)
                            see_more_clicked = True
                            empty_scrolls = max(0, empty_scrolls - 2)
                    except Exception:
                        pass

                    if not see_more_clicked:
                        # Check if page height stopped growing
                        try:
                            current_height = await asyncio.wait_for(
                                page.evaluate("() => document.body.scrollHeight"),
                                timeout=5.0
                            )
                            if current_height == last_scroll_height and empty_scrolls >= 3:
                                logger.debug(f"End of feed detected at scroll height {current_height}")
                                break
                            last_scroll_height = current_height
                        except Exception:
                            pass

                    if is_scrape_all and empty_scrolls > 3:
                        _mode = settings.DISCOVERY_SPEED_MODE
                        _multiplier = 0.3 if _mode == "aggressive" else 0.7 if _mode == "balanced" else 1.5
                        extra_wait = min(empty_scrolls * _multiplier, 5 if _mode != "stealth" else 10)
                        await asyncio.sleep(extra_wait)
                else:
                    empty_scrolls = 0

                # -----------------------------------------------------------
                # 5. SCROLL DOWN — Variable distance for anti-ban
                # -----------------------------------------------------------
                scroll_distance = None  
                if is_scrape_all:
                    try:
                        viewport = page.viewport_size or {"width": 1920, "height": 1080}
                        vh = viewport["height"]
                        scroll_distance = int(vh * random.uniform(0.3, 1.0))
                    except Exception:
                        pass

                try:
                    await asyncio.wait_for(human.human_scroll(page, direction="down", distance=scroll_distance), timeout=10.0)
                except Exception as e:
                    logger.debug(f"Scroll timeout: {e}")
                    
                await human.pause("scroll")

                # Speed-mode-aware jitter probability: 15% stealth, 5% balanced, 0% aggressive
                _mode = settings.DISCOVERY_SPEED_MODE
                _jitter_prob = 0.15 if _mode == "stealth" else 0.05 if _mode == "balanced" else 0.0
                if _jitter_prob > 0 and random.random() < _jitter_prob:
                    try:
                        await asyncio.wait_for(human.mouse_jitter(page, count=random.randint(1, 3)), timeout=5.0)
                    except Exception:
                        pass

                await human.maybe_take_break()
                await self._dismiss_popups(page)

            await self.health.record_request("facebook", success=True)

        except Exception as exc:
            logger.error(f"Facebook search logic failed critically for '{keyword}': {exc}")
            await self.health.record_request("facebook", success=False)

        return profiles

    def _parse_graphql_response(self, body: str, graphql_data: dict):
        """
        Try to extract user data from intercepted GraphQL response bodies.
        Facebook GraphQL responses can contain follower counts and HD profile images.
        """
        try:
            # GraphQL responses may contain multiple JSON blobs separated by newlines
            for line in body.split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    self._walk_graphql_node(data, graphql_data)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    def _walk_graphql_node(self, node, graphql_data: dict):
        """Recursively walk a JSON structure looking for user profile data."""
        if isinstance(node, dict):
            # look for user objects with both a name and a profile URL
            if "name" in node and "url" in node:
                url = node.get("url", "")
                if "facebook.com/" in url:
                    username = url.rstrip("/").split("/")[-1]
                    entry = graphql_data.setdefault(username, {})
                    entry["name"] = node.get("name", "")

                    # try various field names for follower count
                    for key in (
                        "follower_count",
                        "followers_count",
                        "friends_count",
                        "subscriber_count",
                    ):
                        if key in node:
                            try:
                                entry["followers"] = int(node[key])
                            except (ValueError, TypeError):
                                pass

            # Extract HD profile picture from GraphQL
            # Facebook GraphQL uses various keys for profile images
            for img_key in (
                "profile_picture",
                "profilePicLarge",
                "profilePicMedium",
                "profile_pic_large",
                "pic_big",
                "pic_large",
            ):
                if img_key in node and isinstance(node[img_key], dict):
                    uri = node[img_key].get("uri") or node[img_key].get("url")
                    if uri and "fbcdn" in uri:
                        # Try to associate with a user
                        name = node.get("name")
                        url = node.get("url", "")
                        if url and "facebook.com/" in url:
                            username = url.rstrip("/").split("/")[-1]
                            entry = graphql_data.setdefault(username, {})
                            entry["hd_image"] = uri

            # Also check for profile_picture at the current level with a URI
            if isinstance(node.get("profile_picture"), str):
                # Sometimes it's a direct URL string
                pic_url = node["profile_picture"]
                if "fbcdn" in pic_url:
                    url = node.get("url", "")
                    if url and "facebook.com/" in url:
                        username = url.rstrip("/").split("/")[-1]
                        entry = graphql_data.setdefault(username, {})
                        entry["hd_image"] = pic_url

            # recurse
            for value in node.values():
                self._walk_graphql_node(value, graphql_data)

        elif isinstance(node, list):
            for item in node:
                self._walk_graphql_node(item, graphql_data)

    async def _dismiss_popups(self, page):
        """Dismiss Facebook popups (cookie consent, login wall, notifications)."""
        for selector in POPUP_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=1000):
                    await el.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass
