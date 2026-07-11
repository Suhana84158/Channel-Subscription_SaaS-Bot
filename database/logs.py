from datetime import datetime, timezone

from database.mongo import get_database

COLLECTION = "logs"


def logs_collection():
    return get_database()[COLLECTION]


async def create_log(
    log_type: str,
    message: str,
    user_id: int = None,
    admin_id: int = None,
    extra: dict = None,
):
    document = {
        "log_type": log_type,
        "message": message,
        "user_id": user_id,
        "admin_id": admin_id,
        "extra": extra or {},
        "created_at": datetime.now(timezone.utc),
    }

    await logs_collection().insert_one(document)

    return document


async def get_logs(limit: int = 100):
    return await logs_collection().find().sort(
        "created_at",
        -1
    ).limit(limit).to_list(length=limit)


async def get_user_logs(user_id: int):
    return await logs_collection().find(
        {"user_id": user_id}
    ).sort(
        "created_at",
        -1
    ).to_list(length=100)


async def clear_logs():
    await logs_collection().delete_many({})
