"""
Provides the asynchronous I/O persistence layer via Motor.
Architectural Decision: We utilize logical partition boundaries by segregating platforms into distinct databases.
This prevents collection locking contention under high scrape loads and sets the foundation for eventual horizontal sharding,
while maintaining a strict, unified bounded context (`ProfileResult`) to ensure aggregation queries remain generic.
"""

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore
from pymongo.errors import PyMongoError, AutoReconnect, ServerSelectionTimeoutError  # type: ignore
from tenacity import (  # type: ignore
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from backend.core.config import settings  # type: ignore
from backend.core.logger import get_logger  # type: ignore

logger = get_logger("db")

# ---- Global client (lazy init) ----
_client: Optional[AsyncIOMotorClient] = None
COLLECTION_NAME = "search_results"
PRESETS_COLLECTION = "keyword_presets"
GLOBAL_CLIENTS_COLLECTION = "clients"  # Store clients globally

SUPPORTED_PLATFORMS = ["facebook", "instagram", "twitter", "youtube", "telegram", "tiktok"]


@dataclass
class ProfileResult:
    """
    The canonical data contract representing a discovered social identity.
    Enforces a strict schema prior to insertion, abstracting the idiosyncrasies
    of individual platform APIs into a guaranteed downstream shape.
    """

    platform: str
    client_name: str
    keyword: str
    url: str
    username: str = ""
    display_name: str = ""
    bio: str = ""
    followers: Optional[int] = None
    following: Optional[int] = None
    post_count: Optional[int] = None
    profile_image_url: str = ""
    profile_image_b64: Optional[str] = None
    is_verified: bool = False
    location: Optional[str] = None
    created_at: Optional[str] = None
    last_active: Optional[str] = None
    status: str = "pending"
    screenshot_b64: Optional[str] = None
    risk_score: int = 0
    has_logo: bool = False
    is_active: bool = False
    has_name_match: bool = False
    last_post_date: Optional[str] = None
    comments: str = ""
    priority: str = "Low"
    original_name: str = ""
    original_feed: str = ""
    entity_type: str = ""
    confidence: str = ""
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    schema_version: int = 2

    def __post_init__(self):
        """
        Minimal URL cleanup — preserve the original URL as-is so that
        copied/exported links match exactly what the user provided.
        Only strip surrounding whitespace.
        """
        if self.url:
            self.url = self.url.strip()

    def to_dict(self) -> dict:
        """
        Calculates and bounds timezone-aware persistence mutations.
        """
        now = datetime.now(timezone.utc)
        doc = asdict(self)  # type: ignore
        if doc["first_seen"] is None:
            doc["first_seen"] = now
        doc["last_seen"] = now
        return doc


def get_client() -> AsyncIOMotorClient:
    """
    Thread-safe connection pool singleton. 
    Guarantees TCP multiplexing and prevents socket leaks during API hot-reloads.
    """
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGO_URI)
        logger.info(f"MongoDB connected to {settings.MONGO_URI}")
    return _client


def get_db(platform: str):
    """Return the database object for a specific platform."""
    return get_client()[settings.get_db_name(platform)]


def get_collection(platform: str, collection_name: str = COLLECTION_NAME):
    """Return a specific collection within a platform's database."""
    return get_db(platform)[collection_name]


async def init_indexes():
    """
    Ensures O(log N) lookup complexity for critical read paths.
    Idempotent operation; safe for execution during application bootstrap.
    """
    for platform in SUPPORTED_PLATFORMS:
        coll = get_collection(platform)
        presets = get_collection(platform, PRESETS_COLLECTION)

        # primary lookup: client + url for upsert
        await coll.create_index(
            [("client_name", 1), ("url", 1)],
            unique=True,
            name="idx_client_url_unique",
        )
        # filtering by client + status
        await coll.create_index(
            [("client_name", 1), ("status", 1)],
            name="idx_client_status",
        )
        # sorting by newest
        await coll.create_index(
            [("last_seen", -1)],
            name="idx_last_seen_desc",
        )
        # keyword search within a client
        try:
            await coll.drop_index("idx_client_keyword")
        except PyMongoError:
            pass

        await coll.create_index(
            [("client_name", 1), ("keywords", 1)],
            name="idx_client_keywords",
        )
        # preset lookup (unique per client + platform + preset_name)
        await presets.create_index(
            [("client_name", 1), ("platform", 1), ("preset_name", 1)],
            unique=True,
            name="idx_preset_client_platform_name",
        )

        logger.info(f"Indexes ensured for {platform}")

    # Global Client Index
    global_db = get_client()[
        settings.get_db_name("facebook")
    ]  # Use facebook DB as the "master" for globals
    await global_db[GLOBAL_CLIENTS_COLLECTION].create_index(
        [("name", 1)], unique=True, name="idx_global_client_name"
    )
    logger.info("Global client index ensured")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(
        (
            PyMongoError,
            AutoReconnect,
            ServerSelectionTimeoutError,
            ConnectionRefusedError,
        )
    ),
    reraise=True,
)
async def save_client(name: str) -> bool:
    """
    Establishes a global tenant identifier. Uses upsert semantics to ensure atomicity.
    """
    global_db = get_client()[settings.get_db_name("facebook")]
    try:
        await global_db[GLOBAL_CLIENTS_COLLECTION].update_one(
            {"name": name},
            {"$setOnInsert": {"name": name, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to save global client {name}: {e}")
        return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(
        (
            PyMongoError,
            AutoReconnect,
            ServerSelectionTimeoutError,
            ConnectionRefusedError,
        )
    ),
    reraise=True,
)
async def save_result(result: ProfileResult) -> tuple[str, bool]:
    """
    Executes an atomic upsert to guarantee exactly-once processing semantics for the composite key (client_name, url).
    Mutates array fields (like `keywords`) via `$addToSet` to track many-to-one search origins without document duplication.
    Returns: (document_id, is_new_record)
    """
    coll = get_collection(result.platform)
    doc = result.to_dict()
    now = doc["last_seen"]

    update_result = await coll.update_one(
        {"client_name": result.client_name, "url": result.url},
        {
            "$set": {
                "display_name": doc["display_name"],
                "username": doc["username"],
                "bio": doc["bio"],
                "followers": doc["followers"],
                "following": doc["following"],
                "post_count": doc["post_count"],
                "profile_image_url": doc["profile_image_url"],
                "profile_image_b64": doc["profile_image_b64"],
                "is_verified": doc["is_verified"],
                "location": doc["location"],
                "created_at": doc["created_at"],
                "last_active": doc["last_active"],
                "last_post_date": doc["last_post_date"],
                "screenshot_b64": doc["screenshot_b64"],
                "risk_score": doc.get("risk_score", 0),
                "has_logo": doc.get("has_logo", False),
                "is_active": doc.get("is_active", False),
                "confidence": doc.get("confidence", ""),
                "entity_type": doc.get("entity_type", ""),
                "last_seen": now,
                "schema_version": doc["schema_version"],
            },
            "$setOnInsert": {
                "platform": doc["platform"],
                "client_name": doc["client_name"],
                "url": doc["url"],
                "status": doc["status"],
                "first_seen": now,
            },
            "$addToSet": {"keywords": doc["keyword"]},
        },
        upsert=True,
    )

    is_new = update_result.upserted_id is not None
    doc_id = str(update_result.upserted_id or "")

    if not is_new:
        # fetch the existing doc id for the response
        existing = await coll.find_one(
            {"client_name": result.client_name, "url": result.url},
            {"_id": 1},
        )
        doc_id = str(existing["_id"]) if existing else ""

    return doc_id, is_new


async def bulk_save_results(results: list[ProfileResult]) -> int:
    """Save multiple results, returns count of documents saved."""
    saved_count = 0
    for result in results:
        try:
            _, is_new = await save_result(result)
            saved_count += 1  # type: ignore
        except Exception as exc:
            logger.warning(f"Failed to save result {result.url}: {exc}")
    return saved_count


async def get_results(
    client: str,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[datetime] = None,
    keyword: Optional[str] = None,
    confidence: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    Executes a scatter-gather query pattern.
    If no bounds are provided, the query maps across all logically sharded platform databases,
    reducing and sorting the aggregate payload in-memory.
    Returns: A tuple of (hydrated_results, absolute_unpaginated_count).
    """
    platforms_to_search = [platform] if platform else SUPPORTED_PLATFORMS
    all_results = []
    total_count = 0

    for plat in platforms_to_search:
        coll = get_collection(plat)

        query: dict[str, Any] = {"client_name": client}
        if status:
            query["status"] = status
        if since:
            query["last_seen"] = {"$gte": since}
        if keyword:
            query["$or"] = [
                {"keywords": keyword},
                {"keyword": keyword}
            ]
        if confidence:
            query["confidence"] = confidence

        # Get total count for pagination
        total_count += await coll.count_documents(query)

        cursor = (
            coll.find(query)
            .sort("last_seen", -1)
            .limit(offset + limit)
        )
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            doc["platform"] = plat
            all_results.append(doc)

    # re-sort across platforms by last_seen (newest first)
    all_results.sort(key=lambda d: d.get("last_seen", datetime.min), reverse=True)
    return all_results[offset : offset + limit], total_count  # type: ignore


async def get_result_full(doc_id: str, platform: str) -> Optional[dict]:
    """Fetch a single result with all fields (including images)."""
    from bson import ObjectId  # type: ignore

    coll = get_collection(platform)
    doc = await coll.find_one({"_id": ObjectId(doc_id)})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


async def update_status(doc_id: str, platform: str, new_status: str) -> bool:
    """Update the status (pending/approved/rejected) of a record."""
    from bson import ObjectId  # type: ignore

    coll = get_collection(platform)
    result = await coll.update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": {"status": new_status, "last_seen": datetime.now(timezone.utc)}},
    )
    return result.modified_count > 0


EDITABLE_FIELDS = {
    "has_logo",
    "is_active",
    "has_name_match",
    "risk_score",
    "priority",
    "comments",
    "original_name",
    "original_feed",
    "last_post_date",
}


async def update_fields(doc_id: str, platform: str, fields: dict) -> bool:
    """
    Executes a partial document mutation. 
    Guarded by an explicit whitelist (`EDITABLE_FIELDS`) to prevent arbitrary injection or schema pollution.
    """
    from bson import ObjectId  # type: ignore

    # only allow whitelisted fields
    safe_fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not safe_fields:
        return False

    safe_fields["last_seen"] = datetime.now(timezone.utc)

    coll = get_collection(platform)
    result = await coll.update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": safe_fields},
    )
    return result.modified_count > 0


async def get_all_clients() -> list[str]:
    """Return a sorted list of all unique client names across all platforms and global db."""
    clients = set()

    # Get from global collection first
    try:
        global_db = get_client()[settings.get_db_name("facebook")]
        cursor = global_db[GLOBAL_CLIENTS_COLLECTION].find({}, {"name": 1})
        async for doc in cursor:
            clients.add(doc["name"])
    except Exception as e:
        logger.error(f"Failed to read global clients: {e}")

    # Fallback/Merge with actual results (in case some old data exists)
    for plat in SUPPORTED_PLATFORMS:
        coll = get_collection(plat)
        distinct = await coll.distinct("client_name")
        clients.update(distinct)

    return sorted(clients)


async def delete_client(client: str) -> int:
    """Delete all results for a client across all platforms, and remove globally. Returns total deleted."""
    total_deleted = 0
    for plat in SUPPORTED_PLATFORMS:
        coll = get_collection(plat)
        result = await coll.delete_many({"client_name": client})
        total_deleted += result.deleted_count

        # also clean up presets
        presets = get_collection(plat, PRESETS_COLLECTION)
        await presets.delete_many({"client_name": client})

    # Delete from global collection
    try:
        global_db = get_client()[settings.get_db_name("facebook")]
        await global_db[GLOBAL_CLIENTS_COLLECTION].delete_one({"name": client})
    except Exception as e:
        logger.error(f"Failed to delete global client {client}: {e}")

    logger.info(f"Deleted {total_deleted} records for client '{client}'")
    return total_deleted


async def save_keyword_preset(
    client: str, platform: str, keywords: list[str], preset_name: str = "default"
) -> bool:
    """Save or update a named keyword preset for a client/platform."""
    coll = get_collection(platform, PRESETS_COLLECTION)
    await coll.update_one(
        {"client_name": client, "platform": platform, "preset_name": preset_name},
        {
            "$set": {
                "keywords": keywords,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "client_name": client,
                "platform": platform,
                "preset_name": preset_name,
            },
        },
        upsert=True,
    )
    return True


async def get_keyword_presets(client: str, platform: str) -> list[dict]:
    """Get all saved keyword presets for a client/platform."""
    coll = get_collection(platform, PRESETS_COLLECTION)
    cursor = coll.find(
        {"client_name": client, "platform": platform},
        {"preset_name": 1, "keywords": 1, "updated_at": 1},
    ).sort("updated_at", -1)
    results = []
    async for doc in cursor:
        results.append(
            {
                "id": str(doc["_id"]),
                "preset_name": doc.get("preset_name", "default"),
                "keywords": doc.get("keywords", []),
            }
        )
    return results


async def get_keywords_for_client(
    client: str, platform: Optional[str] = None
) -> list[str]:
    """Return unique keywords associated with a client's results."""
    all_keywords = set()
    platforms_to_check = [platform] if platform else SUPPORTED_PLATFORMS

    for plat in platforms_to_check:
        coll = get_collection(plat)
        pipeline = [
            {"$match": {"client_name": client}},
            {"$unwind": "$keywords"},
            {"$group": {"_id": "$keywords"}},
            {"$sort": {"_id": 1}},
        ]
        async for doc in coll.aggregate(pipeline):
            if doc["_id"]:
                all_keywords.add(doc["_id"])

    return sorted(all_keywords)


async def get_known_urls_for_client(
    client: str, platform: Optional[str] = None
) -> list[str]:
    """Return all known URLs for a client (across all statuses) for dedup."""
    all_urls = set()
    platforms_to_check = [platform] if platform else SUPPORTED_PLATFORMS

    for plat in platforms_to_check:
        coll = get_collection(plat)
        async for doc in coll.find(
            {"client_name": client},
            {"url": 1, "_id": 0},
        ):
            if doc.get("url"):
                all_urls.add(doc["url"])

    return list(all_urls)


async def get_health_stats(platform: str) -> dict:
    """Return request counts and timing for health monitoring."""
    coll = get_collection(platform)
    total = await coll.count_documents({})
    pending = await coll.count_documents({"status": "pending"})
    approved = await coll.count_documents({"status": "approved"})
    rejected = await coll.count_documents({"status": "rejected"})

    return {
        "platform": platform,
        "total_results": total,
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
    }


async def close_connection():
    """
    Synchronizes teardown of the connection pool. 
    Critical for preventing unhandled socket exceptions during SIGTERM/pod termination.
    """
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("MongoDB connection closed")
