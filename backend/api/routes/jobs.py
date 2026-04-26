"""
Job management API routes.
Handles creating, monitoring, and cancelling scraping jobs.
"""

import asyncio
import random

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.config import settings
from backend.core.health import HealthManager
from backend.core.jobs import JobManager
from backend.core.logger import get_logger

router = APIRouter(tags=["jobs"])
logger = get_logger("api.jobs")
OFFICIAL_API_ANALYSIS_PLATFORMS = {"youtube", "telegram"}


class CreateJobRequest(BaseModel):
    platform: str
    mode: str  # "discovery" or "analysis"
    client: str
    keywords: list[str]
    max_results: int = 50
    headless: bool = True
    search_type: str = "people"  # "people", "pages", or "both" (discovery only)


@router.post("/jobs")
async def create_job(req: CreateJobRequest):
    """Create a new scraping job (discovery or analysis)."""
    if req.platform not in ["facebook", "instagram", "twitter", "youtube", "telegram", "tiktok"]:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {req.platform}")

    if req.mode not in ("discovery", "analysis"):
        raise HTTPException(
            status_code=400, detail="Mode must be 'discovery' or 'analysis'"
        )

    if not req.client.strip():
        raise HTTPException(status_code=400, detail="Client name is required")

    if not req.keywords:
        raise HTTPException(
            status_code=400, detail="At least one keyword/URL is required"
        )

    health = HealthManager()
    manager = JobManager()

    # build the async run function based on platform and mode
    run_fn = _build_run_function(
        platform=req.platform,
        mode=req.mode,
        health=health,
    )

    if run_fn is None:
        raise HTTPException(status_code=500, detail="Failed to initialize scraper")

    job_id = await manager.create_job(
        platform=req.platform,
        mode=req.mode,
        client=req.client,
        keywords=req.keywords,
        run_fn=run_fn,
        config={
            "max_results": req.max_results,
            "headless": req.headless,
            "search_type": req.search_type,
        },
    )

    logger.info(f"Job created via API: {job_id}")
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs")
async def list_jobs():
    """List all jobs with their current status."""
    manager = JobManager()
    return {"jobs": manager.get_all_jobs()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get the status of a specific job."""
    manager = JobManager()
    job = manager.get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job.to_dict()


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a running job."""
    manager = JobManager()
    cancelled = await manager.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=404, detail=f"Job {job_id} not found or already finished"
        )
    return {"job_id": job_id, "status": "cancelled"}


import base64


async def _ensure_profile_image_b64(result_obj):
    """
    Universally download the profile image to base64 if it's missing.
    This bypasses frontend CORS/CORP blocks for CDN URLs (e.g. Facebook, Instagram).
    """
    img_url = getattr(result_obj, "profile_image_url", None)
    img_b64 = getattr(result_obj, "profile_image_b64", None)
    if img_url and not img_b64:
        # Don't try to download data URIs or empty strings
        if img_url.startswith("data:") or len(img_url) < 10:
            return
            
        import requests as req_lib
        try:
            # Determine platform context for referrer
            plat = getattr(result_obj, "platform", "facebook")
            referer_map = {
                "facebook": "https://www.facebook.com/",
                "instagram": "https://www.instagram.com/",
                "twitter": "https://x.com/",
                "youtube": "https://www.youtube.com/",
                "telegram": "https://web.telegram.org/",
                "tiktok": "https://www.tiktok.com/",
            }
            referer = referer_map.get(plat, f"https://www.{plat}.com/")
                
            img_resp = await asyncio.to_thread(
                lambda: req_lib.get(
                    img_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": referer,
                        "Sec-Fetch-Dest": "image",
                        "Sec-Fetch-Mode": "no-cors",
                        "Sec-Fetch-Site": "cross-site",
                    },
                    timeout=8
                )
            )

            if img_resp.status_code == 200 and len(img_resp.content) > 500:
                result_obj.profile_image_b64 = base64.b64encode(img_resp.content).decode("utf-8")
                result_obj.has_logo = True  # Globally signify we have an image
            else:
                logger.warning(f"Universal image downloader got HTTP {img_resp.status_code} "
                               f"Length {len(img_resp.content)} for {img_url[:50]}...")
        except Exception as e:
            logger.warning(f"Universal image downloader failed for {img_url[:50]}: {e}")

def _build_run_function(platform: str, mode: str, health: HealthManager):
    """
    Create the async run function for a given platform and mode.
    Each run function follows the signature expected by JobManager:
      async def run_fn(progress_callback, client, keywords, **config)
    """
    if mode == "discovery":
        discoverer = _get_discoverer(platform, health)
        if discoverer is None:
            return None

        async def run_discovery(
            progress_callback,
            client,
            keywords,
            max_results=50,
            headless=True,
            search_type="people",
        ):

            async def wrapped_cb(event_type, **kw):
                from backend.core.db import ProfileResult, save_result

                if event_type == "result_found" and "result" in kw:
                    try:
                        # Reconstruct model to save it
                        pr = ProfileResult(**kw["result"])
                        await _ensure_profile_image_b64(pr)
                        # sync the modified base64 back to the dictionary for the websocket
                        if pr.profile_image_b64:
                            kw["result"]["profile_image_b64"] = pr.profile_image_b64

                        doc_id, _ = await save_result(pr)
                        # Inject _id into the dictionary sent to frontend
                        kw["result"]["id"] = doc_id
                        kw["result"]["_id"] = doc_id
                    except Exception as e:
                        logger.error(f"Failed to save result inline: {e}")

                await progress_callback(event_type, **kw)

            try:
                results = await asyncio.wait_for(
                    discoverer.search(
                        progress_callback=wrapped_cb,
                        client=client,
                        keywords=keywords,
                        max_results=max_results,
                        headless=headless,
                        search_type=search_type,
                    ),
                    timeout=settings.REQUEST_TIMEOUT_SEC * (20 if max_results >= 9999 else 10) * max(1, len(keywords)),
                )
            except asyncio.TimeoutError:
                logger.error(f"Discovery job timed out for keywords: {keywords}")
                raise Exception(
                    "Discovery timed out. Target platform is too slow or unresponsive."
                )

        return run_discovery

    elif mode == "analysis":
        analyzer = _get_analyzer(platform, health)
        if analyzer is None:
            return None

        async def run_analysis(progress_callback, client, keywords, headless=True, **kwargs):
            from urllib.parse import urlparse

            from backend.stealth.browser_pool import BrowserPool

            urls = keywords
            completed = 0
            progress_lock = asyncio.Lock()
            is_official_api_platform = platform in OFFICIAL_API_ANALYSIS_PLATFORMS

            # Validate all URLs first (SSRF protection)
            valid_urls = []
            for url in urls:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or parsed.scheme == "file":
                    logger.warning(f"SSRF attempt blocked. Invalid URL scheme: {url}")
                    continue
                if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                    logger.warning(f"SSRF attempt blocked. Localhost targeted: {url}")
                    continue
                valid_urls.append(url)

            if not valid_urls:
                return

            total = len(valid_urls)
            parallelism = (
                settings.ANALYSIS_API_CONCURRENT_TABS
                if is_official_api_platform
                else settings.ANALYSIS_CONCURRENT_TABS
            )
            parallelism = max(1, int(parallelism))
            parallelism = min(parallelism, total)
            inter_batch_delay = (
                settings.ANALYSIS_API_INTER_PROFILE_DELAY
                if is_official_api_platform
                else settings.ANALYSIS_INTER_PROFILE_DELAY
            )

            # Launch ONE browser with multiple tabs
            pool = await BrowserPool.create(
                platform=platform,
                headless=headless,
                max_pages=parallelism,
            )

            try:
                # Process profiles in parallel batches
                batch_size = parallelism

                for batch_start in range(0, len(valid_urls), batch_size):
                    batch_urls = valid_urls[batch_start:batch_start + batch_size]

                    async def analyze_one(url, idx):
                        nonlocal completed
                        page = await pool.acquire_page()
                        try:
                            async with progress_lock:
                                await progress_callback(
                                    event_type="progress",
                                    message=f"Analyzing {idx + 1}/{total}: {url}",
                                    count_found=completed,
                                    count_total=total,
                                )

                            result = await asyncio.wait_for(
                                analyzer.analyze_with_page(
                                    url=url,
                                    client=client,
                                    page=page,
                                ),
                                timeout=settings.REQUEST_TIMEOUT_SEC * 3,
                            )

                            await _ensure_profile_image_b64(result)
                            async with progress_lock:
                                completed += 1
                                current_completed = completed
                                await progress_callback(
                                    event_type="result_found",
                                    message=f"Analyzed: {result.display_name or url}",
                                    count_found=current_completed,
                                    count_total=total,
                                    result=result.to_dict(),
                                )
                        except asyncio.TimeoutError:
                            logger.error(f"Analysis timed out for {url}")
                            async with progress_lock:
                                await progress_callback(
                                    event_type="progress",
                                    message=f"Timed out analyzing {url}",
                                    count_found=completed,
                                    count_total=total,
                                )
                        except Exception as e:
                            logger.error(f"Analysis failed for {url}: {e}")
                            async with progress_lock:
                                await progress_callback(
                                    event_type="progress",
                                    message=f"Error analyzing {url}: {str(e)}",
                                    count_found=completed,
                                    count_total=total,
                                )
                        finally:
                            await pool.release_page(page)

                    # Run batch concurrently
                    tasks = [
                        analyze_one(url, batch_start + j)
                        for j, url in enumerate(batch_urls)
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)

                    # Anti-ban: small delay between batches
                    if batch_start + batch_size < len(valid_urls):
                        if inter_batch_delay > 0:
                            delay = inter_batch_delay + random.uniform(0.5, 1.5)
                            await asyncio.sleep(delay)

            finally:
                await pool.shutdown()
                close_method = getattr(analyzer, "close", None)
                if callable(close_method):
                    try:
                        await close_method()
                    except Exception as exc:
                        logger.warning(f"Failed to close analyzer for {platform}: {exc}")

        return run_analysis

    return None


def _get_discoverer(platform: str, health: HealthManager):
    """Instantiate a platform-specific discoverer."""
    if platform == "facebook":
        from backend.platforms.facebook.discovery import FacebookDiscoverer

        return FacebookDiscoverer(config=settings, health=health)
    elif platform == "instagram":
        from backend.platforms.instagram.discovery import InstagramDiscoverer

        return InstagramDiscoverer(config=settings, health=health)
    elif platform == "twitter":
        from backend.platforms.twitter.discovery import TwitterDiscoverer

        return TwitterDiscoverer(config=settings, health=health)
    elif platform == "youtube":
        from backend.platforms.youtube.discovery import YouTubeDiscoverer

        return YouTubeDiscoverer(config=settings, health=health)
    elif platform == "telegram":
        from backend.platforms.telegram.discovery import TelegramDiscoverer

        return TelegramDiscoverer(config=settings, health=health)
    elif platform == "tiktok":
        from backend.platforms.tiktok.discovery import TikTokDiscoverer

        return TikTokDiscoverer(config=settings, health=health)
    return None


def _get_analyzer(platform: str, health: HealthManager):
    """Instantiate a platform-specific analyzer."""
    if platform == "facebook":
        from backend.platforms.facebook.analysis import FacebookAnalyzer

        return FacebookAnalyzer(config=settings, health=health)
    elif platform == "instagram":
        from backend.platforms.instagram.analysis import InstagramAnalyzer

        return InstagramAnalyzer(config=settings, health=health)
    elif platform == "twitter":
        from backend.platforms.twitter.analysis import TwitterAnalyzer

        return TwitterAnalyzer(config=settings, health=health)
    elif platform == "youtube":
        from backend.platforms.youtube.analysis import YouTubeAnalyzer

        return YouTubeAnalyzer(config=settings, health=health)
    elif platform == "telegram":
        from backend.platforms.telegram.analysis import TelegramAnalyzer

        return TelegramAnalyzer(config=settings, health=health)
    elif platform == "tiktok":
        from backend.platforms.tiktok.analysis import TikTokAnalyzer

        return TikTokAnalyzer(config=settings, health=health)
    return None
