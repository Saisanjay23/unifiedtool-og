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
