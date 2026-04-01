"""
Stealth browser factory for the Unified Social Media Tool.
Launches Playwright Chromium with full anti-detection patching:
fingerprint injection, stealth JS overrides, session state loading.
"""

import asyncio
import os
import sys
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from backend.core.config import settings
from backend.core.logger import get_logger
from backend.stealth.fingerprint import (
    DeviceProfileManager,
    get_canvas_noise_script,
    get_webgl_spoof_script,
    get_audio_noise_script,
    get_navigator_override_script,
)

logger = get_logger("stealth.browser")

# event loop policy for Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


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
]


async def create_stealth_browser(
    platform: str,
    headless: bool = True,
    session_file: Optional[str] = None,
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
        launch_opts = {
            "headless": headless,
            "args": STEALTH_ARGS
            + [f"--window-size={viewport['width']},{viewport['height']}"],
        }

        # Route through proxy if configured (enables access when platforms are blocked)
        if settings.PROXY_URL:
            launch_opts["proxy"] = {"server": settings.PROXY_URL}
            logger.info(f"{platform}: Using proxy {settings.PROXY_URL}")

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
    stealth_scripts = [
        get_navigator_override_script(profile),
        get_canvas_noise_script(),
        get_webgl_spoof_script(profile),
        get_audio_noise_script(),
    ]
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
        
        logger.debug("tiktok: Anti-bot stealth scripts applied (acrawler NOT blocked intentionally to allow X-Bogus generation)")    # also try playwright-stealth if installed
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

    # set default navigation timeout
    page.set_default_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)
    page.set_default_navigation_timeout(settings.REQUEST_TIMEOUT_SEC * 1000)

    # Aggressive memory optimization: Block heavy media in headless mode
    # IMPORTANT: images and CSS must NOT be blocked — they are required
    # for screenshots and profile picture capture during analysis
    if headless:

        async def block_resources(route):
            resource_type = route.request.resource_type
            if resource_type in ("media", "font"):
                await route.abort()
            else:
                await route.continue_()

        # Apply resource blocker to all routes
        await page.route("**/*", block_resources)

    logger.info(
        f"{platform}: Browser ready (headless={headless}, "
        f"profile={profile['name']}, viewport={viewport['width']}x{viewport['height']})"
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
    import json

    with open(session_path, "w", encoding="utf-8") as f:
        json.dump(storage, f, indent=2)

    logger.info(f"{platform}: Session state saved to {session_path}")
    return session_path


async def create_visible_login_browser(
    platform: str,
    start_url: str,
    verify_cookie: Optional[str] = None,
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
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
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
