"""
Job management API routes.
Handles creating, monitoring, and cancelling scraping jobs.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.config import settings
from backend.core.jobs import JobManager
from backend.core.health import HealthManager
from backend.core.db import save_result
from backend.core.logger import get_logger

router = APIRouter(tags=["jobs"])
logger = get_logger("api.jobs")


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

        sem = asyncio.Semaphore(settings.MAX_CONCURRENT_PROFILES)

        async def run_analysis(progress_callback, client, keywords, headless=True, **kwargs):
            # for analysis, "keywords" are actually URLs
            urls = keywords
            total = len(urls)

            from urllib.parse import urlparse

            for i, url in enumerate(urls):
                # SSRF Protection: strictly validate URL scheme and block local/internal IP ranges
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or parsed.scheme == "file":
                    logger.warning(f"SSRF attempt blocked. Invalid URL scheme: {url}")
                    continue
                if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                    logger.warning(f"SSRF attempt blocked. Localhost targeted: {url}")
                    continue

                await progress_callback(
                    event_type="progress",
                    message=f"Analyzing {i + 1}/{total}: {url}",
                    count_found=i,
                    count_total=total,
                )

                try:
                    result = await asyncio.wait_for(
                        analyzer.analyze(
                            url=url,
                            client=client,
                            headless=headless,
                            semaphore=sem,
                        ),
                        timeout=settings.REQUEST_TIMEOUT_SEC
                        * 5,  # Allow max ~2.5 mins per profile
                    )
                    # DO NOT save to DB in Analysis mode. Emitted only to WebSocket.
                    
                    # Ensure base64 profile image is present before emitting
                    await _ensure_profile_image_b64(result)

                    await progress_callback(
                        event_type="result_found",
                        message=f"Analyzed: {result.display_name or url}",
                        count_found=i + 1,
                        count_total=total,
                        result=result.to_dict(),
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Analysis timed out for {url}")
                    await progress_callback(
                        event_type="progress",
                        message=f"Timed out analyzing {url}",
                        count_found=i,
                        count_total=total,
                    )
                except Exception as e:
                    logger.error(f"Analysis failed for {url}: {e}")
                    await progress_callback(
                        event_type="progress",
                        message=f"Error analyzing {url}: {str(e)}",
                        count_found=i,
                        count_total=total,
                    )

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
