"""
Stealth browser factory for the Unified Social Media Tool.
Launches Playwright Chromium with full anti-detection patching:
fingerprint injection, stealth JS overrides, session state loading.
"""

import asyncio
import os

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from backend.core.config import settings
from backend.core.fs import atomic_write_json
from backend.core.logger import get_logger
from backend.stealth.free_proxy import get_working_free_proxy
from backend.stealth.fingerprint import (
    DeviceProfileManager,
    get_audio_noise_script,
    get_canvas_noise_script,
    get_navigator_override_script,
    get_webgl_spoof_script,
)

logger = get_logger("stealth.browser")


# Chrome launch args tuned for stealth + rendering stability
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--disable-component-update",
    # WebRTC leak prevention: force WebRTC to respect proxy settings,
    # preventing local/real IP address leaks through STUN/TURN requests.
    # Inspired by Scrapling's block_webrtc feature.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
]


async def create_stealth_browser(
    platform: str,
    headless: bool = True,
    session_file: str | None = None,
    block_ads: bool = False,
    use_free_proxy: bool = False,
) -> tuple[Playwright, Browser, BrowserContext, Page]:
    """
    Launch a fully stealth-patched Chromium browser.

    Returns (playwright, browser, context, page).
    The caller is responsible for closing the browser when done:
        await browser.close()
        await playwright.stop()

    Args:
        platform: platform name (used for device profile selection)
        headless: run headless or visible
        session_file: path to Playwright storage state JSON (logged-in session)
        block_ads: if True, block requests to known ad/tracker domains
                   (safe for discovery, avoid for analysis where full page rendering matters)
    """
    # load (or create) the persistent device profile for this platform
    profile_mgr = DeviceProfileManager(platform)
    profile = profile_mgr.get_profile()
    viewport = profile_mgr.get_viewport()
    user_agent = profile_mgr.get_user_agent()

    # resolve session state file path
    if session_file is None:
        possible_path = os.path.join(settings.SESSION_PATH, f"{platform}.json")
        if os.path.exists(possible_path):
            session_file = possible_path

    # launch the browser
    pw = await async_playwright().start()
    try:
        launch_args = list(STEALTH_ARGS) + [
            f"--window-size={viewport['width']},{viewport['height']}",
        ]

        launch_opts = {
            "headless": headless,
            "args": launch_args,
        }

        # Route through proxy — supports both single proxy and rotation
        proxy_url = None
        logger.info(f"{platform}: use_free_proxy={use_free_proxy}")
        if use_free_proxy:
            proxy_url = await get_working_free_proxy()
            if proxy_url:
                launch_opts["proxy"] = {"server": proxy_url}
                logger.info(f"{platform}: Using free public proxy {proxy_url}")
            else:
                logger.warning(f"{platform}: use_free_proxy requested but no working proxies found.")
        
        if not proxy_url:
            proxy_rotator = settings.get_proxy_rotator()
            if proxy_rotator:
                proxy_url = proxy_rotator.next()
                launch_opts["proxy"] = {"server": proxy_url}
                logger.info(f"{platform}: Using rotated proxy {proxy_url} (pool of {proxy_rotator.count})")
            elif settings.PROXY_URL:
                proxy_url = settings.PROXY_URL
                launch_opts["proxy"] = {"server": proxy_url}
                logger.info(f"{platform}: Using proxy {proxy_url}")

        # DNS-over-HTTPS: when using a proxy, route DNS through Cloudflare's DoH
        # to prevent DNS leaks revealing which domains we're scraping.
        # Inspired by Scrapling's dns_over_https feature.
        if proxy_url:
            launch_args.append(
                "--dns-over-https-templates=https://cloudflare-dns.com/dns-query"
            )
            logger.debug(f"{platform}: DNS-over-HTTPS enabled (leak prevention)")

        browser = await pw.chromium.launch(**launch_opts)
    except Exception as exc:
        logger.error(f"Failed to launch browser: {exc}")
        await pw.stop()
        raise exc

    # build context options
    context_opts = {
        "viewport": viewport,
        "user_agent": user_agent,
        "locale": "en-US",
        "timezone_id": "Asia/Kolkata",
        "color_scheme": "dark",
    }

    # load saved session state if available
    if session_file and os.path.exists(session_file):
        context_opts["storage_state"] = session_file
        logger.info(f"{platform}: Loading session from {session_file}")
    else:
        logger.info(f"{platform}: Starting without saved session")

    context = await browser.new_context(**context_opts)

    # apply stealth JS patches to every new page
    # IMPORTANT: Twitter and Facebook detect canvas/WebGL/audio prototype overrides
    # as "privacy extension" behavior and break ("Something went wrong" on Twitter,
    # infinite loading spinner on Facebook). Only apply navigator override for these.
    # Instagram REMOVED from aggressive stealth — canvas/WebGL/audio prototype overrides
    # break Instagram's React hydration, causing blank pages (body renders empty, 11KB screenshots).
    # Same issue that was previously fixed for Twitter and Facebook.
    AGGRESSIVE_STEALTH_PLATFORMS = {"tiktok", "youtube", "telegram"}

    stealth_scripts = [
        get_navigator_override_script(profile),  # Always apply (webdriver=false, etc.)
        # Document visibility spoofing — prevent platforms from detecting
        # background/hidden/headless tabs and throttling or blocking scraping.
        # Inspired by Scrapling's global visibility override.
        # Applied to ALL platforms (TikTok's extended version below adds event blocking too).
        """
        (function() {
            Object.defineProperty(document, 'visibilityState', {
                get: function() { return 'visible'; },
                configurable: true
            });
            Object.defineProperty(document, 'hidden', {
                get: function() { return false; },
                configurable: true
            });
        })();
        """,
    ]

    if platform in AGGRESSIVE_STEALTH_PLATFORMS:
        stealth_scripts.extend([
            get_canvas_noise_script(),
            get_webgl_spoof_script(profile),
            get_audio_noise_script(),
        ])
        logger.debug(f"{platform}: Full stealth fingerprint spoofing enabled")
    else:
        logger.debug(f"{platform}: Using minimal stealth (navigator + visibility) to avoid detection")

    for script in stealth_scripts:
        await context.add_init_script(script)

    # TikTok-specific anti-bot overrides — neutralize byted_acrawler SDK,
    # performance timing leaks, document visibility checks, and bot detection scripts
    if platform == "tiktok":
        tiktok_stealth_script = """
        (function() {
            // 1. Mock byted_acrawler — TikTok's primary bot detection SDK
            // If this object is missing or returns invalid tokens, TikTok flags the session
            if (!window.byted_acrawler) {
                window.byted_acrawler = {
                    init: function(opts) { return this; },
                    sign: function(params) {
                        // Return a plausible-looking signature (TikTok validates format, not cryptographic validity during page loads)
                        return { 'X-Bogus': Array.from({length: 28}, () => 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'[Math.floor(Math.random() * 62)]).join('') };
                    },
                    frontierSign: function(url) { return url; },
                };
            }

            // 2. Suppress performance.timing automation signatures
            // Headless browsers have timing anomalies (zero connectEnd, identical loadEventStart)
            // that TikTok's slardar telemetry flags
            try {
                const origGetEntries = performance.getEntriesByType;
                performance.getEntriesByType = function(type) {
                    const entries = origGetEntries.call(this, type);
                    if (type === 'navigation' && entries.length > 0) {
                        const e = entries[0];
                        // Add realistic jitter to timing entries
                        const noise = () => Math.random() * 50 + 10;
                        if (e.connectEnd === 0) {
                            Object.defineProperty(e, 'connectEnd', { value: noise(), configurable: true });
                        }
                    }
                    return entries;
                };
            } catch(e) {}

            // 3. Override document.visibilityState — TikTok throttles/blocks hidden tabs
            Object.defineProperty(document, 'visibilityState', {
                get: function() { return 'visible'; },
                configurable: true
            });
            Object.defineProperty(document, 'hidden', {
                get: function() { return false; },
                configurable: true
            });

            // 4. Prevent Page Visibility API events from firing
            const origAddEventListener = document.addEventListener;
            document.addEventListener = function(type, listener, options) {
                if (type === 'visibilitychange') return; // swallow visibility listeners
                return origAddEventListener.call(this, type, listener, options);
            };

            // 5. Mock TikTok's encryption/signing namespace (used in newer versions)
            if (!window._TikTok) {
                window._TikTok = { isBot: false, isCrawler: false };
            }

            // 6. WebSocket keep-alive spoofing — TikTok monitors WS heartbeat patterns
            const origWebSocket = window.WebSocket;
            window.WebSocket = function(url, protocols) {
                const ws = new origWebSocket(url, protocols);
                // Don't block WebSocket, but ensure it doesn't leak automation state
                return ws;
            };
            window.WebSocket.prototype = origWebSocket.prototype;
            window.WebSocket.CONNECTING = origWebSocket.CONNECTING;
            window.WebSocket.OPEN = origWebSocket.OPEN;
            window.WebSocket.CLOSING = origWebSocket.CLOSING;
            window.WebSocket.CLOSED = origWebSocket.CLOSED;
        })();
        """
        await context.add_init_script(tiktok_stealth_script)

        # DONT BLOCK TikTok's bot detection and telemetry resources!
        # Context: We initially blocked `acrawler` and `slardar`, but TikTok
        # heavily relies on `acrawler` to generate the client-side `X-Bogus`
        # and `msToken` parameters for almost all internal protected APIs.
        # If we block them, the page loads but the frontend won't make the search 
        # API request, or it makes an unsigned request that fails, bypassing both
        # our network interception and the hydration loop!
        
        logger.debug("tiktok: Anti-bot stealth scripts applied (acrawler NOT blocked intentionally to allow X-Bogus generation)")

    # also try playwright-stealth if installed (skip for Twitter/Facebook — conflicts with their integrity checks)
    if platform not in ("twitter", "facebook", "instagram"):
        try:
            from playwright_stealth import stealth_async

            page = await context.new_page()
            await stealth_async(page)
            logger.debug(f"{platform}: playwright-stealth patches applied")
        except ImportError:
            page = await context.new_page()
            logger.debug(
                f"{platform}: playwright-stealth not installed, using custom patches only"
            )
    else:
        page = await context.new_page()
        logger.debug(f"{platform}: Skipping playwright-stealth (causes detection on this platform)")

    # set default navigation timeout
    page.set_default_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
    page.set_default_navigation_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)

    # Ad/tracker domain blocking — opt-in via block_ads parameter.
    # Blocks requests at the domain level (not resource-type level),
    # which is safer and doesn't break platform JS bundles.
    # Inspired by Scrapling's block_ads feature.
    if block_ads:
        from backend.stealth.blocklist import is_blocked_domain

        async def _ad_block_handler(route):
            if is_blocked_domain(route.request.url):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _ad_block_handler)
        logger.debug(f"{platform}: Ad/tracker domain blocking enabled")

    # Resource blocking DISABLED globally.
    # The catch-all route handler (page.route("**/*", ...)) was interfering with
    # platform JS bundles, causing Twitter's "Something went wrong" error and
    # Facebook's infinite loading spinner. The memory savings from blocking
    # media/font resources are not worth the reliability cost.
    # NOTE: Domain-level ad blocking (above) is safe because it only blocks
    # third-party tracker domains, not platform resources.

    logger.info(
        f"{platform}: Browser ready (headless={headless}, "
        f"profile={profile['name']}, viewport={viewport['width']}x{viewport['height']}, "
        f"block_ads={block_ads})"
    )

    return pw, browser, context, page


async def save_session_state(context: BrowserContext, platform: str):
    """
    Save the current browser context storage state (cookies + localStorage)
    to disk for reuse in future sessions.
    """
    os.makedirs(settings.SESSION_PATH, exist_ok=True)
    session_path = os.path.join(settings.SESSION_PATH, f"{platform}.json")

    storage = await context.storage_state()
    atomic_write_json(session_path, storage, indent=2)

    logger.info(f"{platform}: Session state saved to {session_path}")
    return session_path


async def create_visible_login_browser(
    platform: str,
    start_url: str,
    verify_cookie: str | None = None,
    timeout_seconds: int = 600,
) -> bool:
    """
    Open a VISIBLE (non-headless) browser for the user to manually log in.
    Waits for either a specific cookie to appear or the timeout to expire.
    Saves the session state on success.

    Args:
        platform: platform name
        start_url: the login page URL
        verify_cookie: cookie name that confirms successful login (e.g. "c_user" for Facebook)
        timeout_seconds: max wait time for login

    Returns:
        True if login was successful and session was saved.
    """
    pw, browser, context, page = await create_stealth_browser(
        platform=platform,
        headless=False,
        session_file=None,  # fresh context for login
    )

    try:
        await page.goto(start_url, wait_until="domcontentloaded")
        logger.info(
            f"{platform}: Login browser opened at {start_url}. Waiting for user to log in..."
        )

        # poll for the verification cookie
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        while True:
            elapsed = loop.time() - start_time
            if elapsed > timeout_seconds:
                logger.warning(f"{platform}: Login timeout after {timeout_seconds}s")
                return False

            cookies = await context.cookies()

            if verify_cookie:
                # check for specific auth cookie
                found = any(c["name"] == verify_cookie for c in cookies)
                if found:
                    logger.info(
                        f"{platform}: Login verified (cookie '{verify_cookie}' found)"
                    )
                    await save_session_state(context, platform)
                    return True
            else:
                # generic check: if there are >5 cookies after the user interacted, assume logged in
                if len(cookies) > 5 and elapsed > 10:
                    logger.info(
                        f"{platform}: Login assumed successful ({len(cookies)} cookies)"
                    )
                    await save_session_state(context, platform)
                    return True

            await asyncio.sleep(2)

    finally:
        await browser.close()
        await pw.stop()
