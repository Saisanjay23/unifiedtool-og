"""
Keyword presets API routes.
CRUD operations for saved keyword sets per client/platform.
Inspired by old tool's saved_searches MongoDB collection.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.db import (
    save_keyword_preset,
    get_keyword_presets,
    get_collection,
    SUPPORTED_PLATFORMS,
)
from backend.core.logger import get_logger

router = APIRouter(tags=["presets"])
logger = get_logger("api.presets")


class PresetCreate(BaseModel):
    preset_name: str
    keywords: list[str]


@router.get("/presets/{client}/{platform}")
async def list_presets(client: str, platform: str):
    """List all keyword presets for a client/platform."""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    presets = await get_keyword_presets(client, platform)
    return {"client": client, "platform": platform, "presets": presets}


@router.post("/presets/{client}/{platform}")
async def create_preset(client: str, platform: str, body: PresetCreate):
    """Create or update a keyword preset."""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    if not body.preset_name.strip():
        raise HTTPException(status_code=400, detail="Preset name cannot be empty")

    if not body.keywords:
        raise HTTPException(status_code=400, detail="Keywords list cannot be empty")

    await save_keyword_preset(client, platform, body.keywords, body.preset_name)
    logger.info(f"Preset '{body.preset_name}' saved for {client}/{platform}")

    return {"status": "saved", "preset_name": body.preset_name}


@router.delete("/presets/{client}/{platform}/{preset_name}")
async def delete_preset(client: str, platform: str, preset_name: str):
    """Delete a keyword preset."""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    # delete from the keyword_presets collection
    coll = get_collection(platform, "keyword_presets")
    result = await coll.delete_one(
        {
            "client_name": client,
            "preset_name": preset_name,
        }
    )

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"Preset '{preset_name}' not found")

    return {"status": "deleted", "preset_name": preset_name}
