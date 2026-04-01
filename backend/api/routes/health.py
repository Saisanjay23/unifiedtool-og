"""
Health check API routes.
Exposes platform health scores and session status.
"""

from fastapi import APIRouter

from backend.core.health import HealthManager

router = APIRouter(tags=["health"])


@router.get("/health")
async def get_health():
    """Return health scores for all platforms."""
    manager = HealthManager()
    return {
        "status": "operational",
        "tool": "Unified Social Media Tool",
        "version": "2.0.0",
        "platforms": manager.get_all_health(),
    }
