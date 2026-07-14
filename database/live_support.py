from datetime import datetime, timezone

from database.mongo import get_database

SETTINGS = "clone_live_support_settings"
TOPICS = "clone_live_support_topics"
MESSAGE_LINKS = "clone_live_support_message_links"
BLOCKS = "clone_live_support_blocks"


def c(name: str):
    return get_database()[name]


async def initialize_live_support_indexes():
    await c(SETTINGS).create_index("owner_id", unique=True)
    await c(TOPICS).create_index([("owner_id", 1), ("user_id", 1)], unique=True)
    await c(TOPICS).create_index(
        [("owner_id", 1), ("support_group_id", 1), ("message_thread_id", 1)],
        unique=True,
    )
    await c(MESSAGE_LINKS).create_index(
        [("owner_id", 1), ("admin_chat_id", 1), ("admin_message_id", 1)],
        unique=True,
    )
    await c(MESSAGE_LINKS).create_index("created_at", expireAfterSeconds=60 * 60 * 24 * 180)
    await c(BLOCKS).create_index([("owner_id", 1), ("user_id", 1)], unique=True)


async def get_live_support_settings(owner_id: int):
    now = datetime.now(timezone.utc)
    defaults = {
        "owner_id": int(owner_id),
        "enabled": False,
        "mode": "topic",
        "support_group_id": None,
        "support_group_title": "",
        "created_at": now,
        "updated_at": now,
    }
    await c(SETTINGS).update_one(
        {"owner_id": int(owner_id)},
        {"$setOnInsert": defaults},
        upsert=True,
    )
    return await c(SETTINGS).find_one({"owner_id": int(owner_id)}) or defaults


async def update_live_support_settings(owner_id: int, **values):
    allowed = {
        "enabled",
        "mode",
        "support_group_id",
        "support_group_title",
    }
    clean = {key: value for key, value in values.items() if key in allowed}
    if "mode" in clean and clean["mode"] not in {"private", "topic"}:
        raise ValueError("Support mode must be private or topic")
    clean["updated_at"] = datetime.now(timezone.utc)
    await c(SETTINGS).update_one(
        {"owner_id": int(owner_id)},
        {
            "$set": clean,
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )
    return await get_live_support_settings(owner_id)


async def save_support_topic(
    owner_id: int,
    user_id: int,
    support_group_id: int,
    message_thread_id: int,
    topic_name: str,
):
    now = datetime.now(timezone.utc)
    await c(TOPICS).update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)},
        {
            "$set": {
                "support_group_id": int(support_group_id),
                "message_thread_id": int(message_thread_id),
                "topic_name": topic_name,
                "updated_at": now,
            },
            "$setOnInsert": {
                "owner_id": int(owner_id),
                "user_id": int(user_id),
                "created_at": now,
            },
        },
        upsert=True,
    )
    return await get_support_topic(owner_id, user_id)


async def get_support_topic(owner_id: int, user_id: int):
    return await c(TOPICS).find_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)}
    )


async def get_topic_by_thread(
    owner_id: int,
    support_group_id: int,
    message_thread_id: int,
):
    return await c(TOPICS).find_one(
        {
            "owner_id": int(owner_id),
            "support_group_id": int(support_group_id),
            "message_thread_id": int(message_thread_id),
        }
    )


async def delete_support_topic(owner_id: int, user_id: int):
    await c(TOPICS).delete_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)}
    )


async def save_private_message_link(
    owner_id: int,
    admin_chat_id: int,
    admin_message_id: int,
    user_id: int,
):
    now = datetime.now(timezone.utc)
    await c(MESSAGE_LINKS).update_one(
        {
            "owner_id": int(owner_id),
            "admin_chat_id": int(admin_chat_id),
            "admin_message_id": int(admin_message_id),
        },
        {
            "$set": {
                "user_id": int(user_id),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def get_private_message_link(
    owner_id: int,
    admin_chat_id: int,
    admin_message_id: int,
):
    return await c(MESSAGE_LINKS).find_one(
        {
            "owner_id": int(owner_id),
            "admin_chat_id": int(admin_chat_id),
            "admin_message_id": int(admin_message_id),
        }
    )


async def set_support_block(owner_id: int, user_id: int, blocked: bool):
    now = datetime.now(timezone.utc)
    await c(BLOCKS).update_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)},
        {
            "$set": {"blocked": bool(blocked), "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def is_support_blocked(owner_id: int, user_id: int) -> bool:
    doc = await c(BLOCKS).find_one(
        {"owner_id": int(owner_id), "user_id": int(user_id)}
    )
    return bool(doc and doc.get("blocked"))


async def count_support_blocks(owner_id: int) -> int:
    return await c(BLOCKS).count_documents(
        {"owner_id": int(owner_id), "blocked": True}
    )
