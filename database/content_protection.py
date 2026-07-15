from datetime import datetime, timezone
from typing import Any, Dict

from database.mongo import get_database

COLLECTION = "seller_content_protection_settings"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
}


def collection():
    return get_database()[COLLECTION]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def initialize_content_protection_indexes() -> None:
    await collection().create_index([("owner_id", 1)], unique=True)


async def ensure_content_protection_settings(owner_id: int) -> Dict[str, Any]:
    owner_id = int(owner_id)
    now = utc_now()
    await collection().update_one(
        {"owner_id": owner_id},
        {
            "$setOnInsert": {
                "owner_id": owner_id,
                **DEFAULT_SETTINGS,
                "created_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    return await collection().find_one({"owner_id": owner_id}) or {
        "owner_id": owner_id,
        **DEFAULT_SETTINGS,
    }


async def get_content_protection_settings(owner_id: int) -> Dict[str, Any]:
    return await ensure_content_protection_settings(owner_id)


async def set_content_protection_enabled(owner_id: int, enabled: bool) -> Dict[str, Any]:
    owner_id = int(owner_id)
    now = utc_now()
    await collection().update_one(
        {"owner_id": owner_id},
        {
            "$set": {
                "enabled": bool(enabled),
                "updated_at": now,
            },
            "$setOnInsert": {
                "owner_id": owner_id,
                "created_at": now,
            },
        },
        upsert=True,
    )
    return await get_content_protection_settings(owner_id)
