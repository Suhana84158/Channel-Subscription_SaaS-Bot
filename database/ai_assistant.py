from datetime import datetime, timezone

from database.mongo import get_database

SETTINGS_COLLECTION = "seller_ai_assistant"
EVENTS_COLLECTION = "seller_ai_events"
UNANSWERED_COLLECTION = "seller_ai_unanswered"
SESSIONS_COLLECTION = "seller_ai_sessions"


def settings_collection():
    return get_database()[SETTINGS_COLLECTION]


def events_collection():
    return get_database()[EVENTS_COLLECTION]


def unanswered_collection():
    return get_database()[UNANSWERED_COLLECTION]


def sessions_collection():
    return get_database()[SESSIONS_COLLECTION]


async def initialize_ai_assistant_indexes():
    await settings_collection().create_index("owner_id", unique=True)
    await events_collection().create_index([("owner_id", 1), ("created_at", -1)])
    await events_collection().create_index([("owner_id", 1), ("user_id", 1), ("created_at", -1)])
    await unanswered_collection().create_index([("owner_id", 1), ("normalized_question", 1)], unique=True)
    await unanswered_collection().create_index([("owner_id", 1), ("last_asked_at", -1)])
    await sessions_collection().create_index([("owner_id", 1), ("user_id", 1)], unique=True)


async def get_ai_settings(owner_id: int) -> dict:
    row = await settings_collection().find_one({"owner_id": int(owner_id)})
    return row or {"owner_id": int(owner_id), "enabled": False, "mode": "balanced"}


async def set_ai_enabled(owner_id: int, enabled: bool) -> dict:
    now = datetime.now(timezone.utc)
    await settings_collection().update_one(
        {"owner_id": int(owner_id)},
        {"$set": {"enabled": bool(enabled), "updated_at": now}, "$setOnInsert": {"created_at": now, "mode": "balanced"}},
        upsert=True,
    )
    return await get_ai_settings(owner_id)


async def record_ai_event(owner_id: int, user_id: int, question: str, topic: str, outcome: str, language: str):
    await events_collection().insert_one({
        "owner_id": int(owner_id), "user_id": int(user_id), "question": question[:2000],
        "topic": topic, "outcome": outcome, "language": language,
        "created_at": datetime.now(timezone.utc),
    })


async def save_unanswered_question(owner_id: int, user_id: int, question: str, normalized_question: str):
    now = datetime.now(timezone.utc)
    await unanswered_collection().update_one(
        {"owner_id": int(owner_id), "normalized_question": normalized_question[:500]},
        {"$set": {"question": question[:2000], "last_user_id": int(user_id), "last_asked_at": now, "status": "open"},
         "$setOnInsert": {"created_at": now}, "$inc": {"times_asked": 1}},
        upsert=True,
    )


async def get_session(owner_id: int, user_id: int) -> dict:
    return await sessions_collection().find_one({"owner_id": int(owner_id), "user_id": int(user_id)}) or {}


async def update_session(owner_id: int, user_id: int, **values):
    values["updated_at"] = datetime.now(timezone.utc)
    await sessions_collection().update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)},
        {"$set": values, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
