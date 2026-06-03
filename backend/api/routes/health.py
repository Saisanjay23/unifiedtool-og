"""
Health check API routes.
Exposes platform health scores and session status.
"""

from fastapi import APIRouter

from backend.core.config import settings
from backend.core.health import HealthManager
from backend.core.runtime import get_runtime_info

router = APIRouter(tags=["health"])


@router.get("/health")
async def get_health():
    """Return health scores for all platforms."""
    manager = HealthManager()
    runtime = get_runtime_info()
    return {
        "status": "operational",
        "tool": "Unified Social Media Tool",
        "version": "2.0.0",
        "platforms": manager.get_all_health(),
        "analysis_concurrent_tabs": settings.ANALYSIS_CONCURRENT_TABS,
        "runtime": runtime,
    }


@router.post("/health/selectors/validate")
async def validate_selectors(platform: str | None = None):
    """
    Validate the selector and API interception health of platform scrapers.
    If 'platform' is provided, validates only that platform. Otherwise, validates
    all supported platforms (facebook, instagram, twitter).
    """
    from fastapi import HTTPException
    from backend.core.selector_validator import SelectorValidator
    
    validator = SelectorValidator()
    platforms_to_check = ["facebook", "instagram", "twitter"]
    
    if platform:
        p_clean = platform.lower()
        if p_clean not in platforms_to_check:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported platform: {platform}. Supported: {', '.join(platforms_to_check)}"
            )
        platforms_to_check = [p_clean]
        
    results = {}
    for p in platforms_to_check:
        results[p] = await validator.validate_platform(p)
        
    return {
        "status": "success",
        "results": results
    }
