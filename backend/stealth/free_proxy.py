"""
Free Proxy fetcher for bypassing region blocks (e.g. TikTok).
Downloads proxies from public sources and tests them concurrently.
Uses SOCKS5 proxies first (more reliable for browser automation),
then falls back to HTTP proxies.
"""
import asyncio
import random
import aiohttp
from backend.core.logger import get_logger

logger = get_logger("stealth.free_proxy")

PROXY_SOURCES = [
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
]

async def _fetch_proxy_list() -> list[str]:
    """Fetch raw proxy lists from GitHub."""
    proxies = []
    seen = set()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for proto, source in PROXY_SOURCES:
            try:
                logger.info(f"Fetching proxy list: {source}")
                async with session.get(source) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        count = 0
                        for line in text.splitlines():
                            line = line.strip()
                            if line and ":" in line:
                                full = f"{proto}://{line}"
                                if full not in seen:
                                    seen.add(full)
                                    proxies.append(full)
                                    count += 1
                        logger.info(f"Got {count} proxies from {source}")
            except Exception as e:
                logger.warning(f"Failed to fetch proxies from {source}: {e}")
    
    random.shuffle(proxies)
    return proxies

async def _test_proxy(proxy_url: str, session: aiohttp.ClientSession) -> str | None:
    """Test if a proxy can successfully connect to TikTok."""
    try:
        async with session.get(
            "https://www.tiktok.com/", 
            proxy=proxy_url, 
            timeout=aiohttp.ClientTimeout(total=5),
            ssl=False,
        ) as resp:
            # Any response (even 403/404) means the proxy can reach TikTok
            logger.debug(f"Proxy {proxy_url} returned status {resp.status}")
            if resp.status < 500:
                return proxy_url
    except Exception as e:
        logger.debug(f"Proxy {proxy_url} failed: {type(e).__name__}")
    return None

async def get_working_free_proxy() -> str | None:
    """
    Fetch and find a working free proxy.
    Tests batches of proxies concurrently to find one fast.
    """
    logger.info("=" * 50)
    logger.info("FREE PROXY: Starting proxy search for TikTok region bypass")
    logger.info("=" * 50)
    
    proxies = await _fetch_proxy_list()
    if not proxies:
        logger.warning("FREE PROXY: Could not fetch any free proxies from any source.")
        return None
        
    logger.info(f"FREE PROXY: Loaded {len(proxies)} proxies total. Testing connectivity to TikTok...")
    
    # Test in batches of 30 concurrently, stop at first success
    batch_size = 30
    max_batches = 10  # Don't test more than 300 proxies
    batches_tested = 0
    
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(proxies), batch_size):
            if batches_tested >= max_batches:
                break
            batches_tested += 1
            batch = proxies[i:i + batch_size]
            logger.info(f"FREE PROXY: Testing batch {batches_tested} ({len(batch)} proxies)...")
            
            tasks = [_test_proxy(p, session) for p in batch]
            results = await asyncio.gather(*tasks)
            
            working = [p for p in results if p is not None]
            if working:
                chosen = random.choice(working)
                logger.info(f"FREE PROXY: SUCCESS! Found {len(working)} working proxies. Using: {chosen}")
                return chosen
            else:
                logger.info(f"FREE PROXY: Batch {batches_tested} had 0 working proxies, trying next batch...")
                
    logger.warning(f"FREE PROXY: FAILED — Tested {min(len(proxies), batch_size * max_batches)} proxies, none could reach TikTok.")
    return None
