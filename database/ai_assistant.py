from datetime import datetime, timezone

from database.mongo import get_database

COLLECTION = "seller_ai_assistant"


def collection():
    return get_database()[COLLECTION]


async def initialize_ai_assistant_indexes():
    await collection().create_index("owner_id", unique=True)


async def get_ai_settings(owner_id: int) -> dict:
    row = await collection().find_one({"owner_id": int(owner_id)})
    if row:
        return row
    return {
        "owner_id": int(owner_id),
        "enabled": False,
    }


async def set_ai_enabled(owner_id: int, enabled: bool) -> dict:
    now = datetime.now(timezone.utc)
    await collection().update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": {
                "enabled": bool(enabled),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return await get_ai_settings(owner_id)
