"""
Client management API routes.
Handles client CRUD and keyword preset management.
"""


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.db import (
    SUPPORTED_PLATFORMS,
    delete_client,
    get_all_clients,
    get_keyword_presets,
    save_client,
    save_keyword_preset,
)
from backend.core.logger import get_logger

router = APIRouter(tags=["clients"])
logger = get_logger("api.clients")


class CreateClientRequest(BaseModel):
    name: str


class SavePresetsRequest(BaseModel):
    keywords: list[str]


@router.get("/clients")
async def list_clients():
    """List all unique client names across platforms."""
    clients = await get_all_clients()
    return {"clients": clients, "count": len(clients)}


@router.post("/clients")
async def create_client(req: CreateClientRequest):
    """
    Create a new client. Clients are implicitly created when saving results,
    but this endpoint allows pre-creation via the UI.
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Client name cannot be empty")

    existing = await get_all_clients()
    if name in existing:
        raise HTTPException(status_code=409, detail=f"Client '{name}' already exists")

    # Save to global client collection
    success = await save_client(name)
    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to create client in database"
        )

    logger.info(f"Client created: {name}")
    return {"name": name, "status": "created"}


@router.delete("/clients/{name}")
async def remove_client(name: str):
    """Delete a client and all their results across all platforms."""
    # Check the client exists before deleting
    existing = await get_all_clients()
    if name not in existing:
        raise HTTPException(status_code=404, detail=f"Client '{name}' not found")

    deleted_count = await delete_client(name)

    logger.info(f"Client deleted: {name} ({deleted_count} records)")
    return {"name": name, "deleted_count": deleted_count}


@router.get("/clients/{client}/presets/{platform}")
async def get_presets(client: str, platform: str):
    """Get saved keyword presets for a client/platform combination."""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    keywords = await get_keyword_presets(client, platform)
    return {"client": client, "platform": platform, "keywords": keywords}


@router.put("/clients/{client}/presets/{platform}")
async def save_presets(client: str, platform: str, req: SavePresetsRequest):
    """Save keyword presets for a client/platform combination."""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    await save_keyword_preset(client, platform, req.keywords)
    return {"client": client, "platform": platform, "keywords": req.keywords}
