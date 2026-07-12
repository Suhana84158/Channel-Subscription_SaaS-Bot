from datetime import datetime, timezone
from database.mongo import get_database
from utils.crypto import decrypt_secret, encrypt_secret

COLLECTION = "seller_bots"


def seller_bots_collection():
    return get_database()[COLLECTION]


async def initialize_seller_bot_indexes():
    await seller_bots_collection().create_index("owner_id", unique=True)
    await seller_bots_collection().create_index("bot_id", unique=True)
    await seller_bots_collection().create_index("bot_username_normalized", unique=True)
    await seller_bots_collection().create_index("active")


async def get_bot(owner_id: int):
    return await seller_bots_collection().find_one({"owner_id": owner_id})


async def get_bot_by_bot_id(bot_id: int):
    return await seller_bots_collection().find_one({"bot_id": bot_id})


async def get_bot_by_username(bot_username: str):
    return await seller_bots_collection().find_one({
        "bot_username_normalized": bot_username.lstrip("@").lower()
    })


async def save_bot(owner_id: int, bot_id: int, bot_name: str, bot_username: str, bot_token: str):
    now = datetime.now(timezone.utc)
    await seller_bots_collection().update_one(
        {"owner_id": owner_id},
        {
            "$set": {
                "bot_id": bot_id,
                "bot_name": bot_name,
                "bot_username": bot_username,
                "bot_username_normalized": bot_username.lstrip("@").lower(),
                "bot_token_encrypted": encrypt_secret(bot_token),
                "active": True,
                "status": "registered",
                "updated_at": now,
            },
            "$unset": {"bot_token": ""},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return await get_bot(owner_id)


async def get_decrypted_bot_token(owner_id: int):
    record = await get_bot(owner_id)
    if not record:
        return None
    encrypted = record.get("bot_token_encrypted")
    return decrypt_secret(encrypted) if encrypted else None


async def get_all_active_bots():
    return await seller_bots_collection().find({
        "active": True,
        "bot_token_encrypted": {"$exists": True, "$ne": None},
    }).to_list(length=None)


async def set_bot_active(owner_id: int, active: bool):
    await seller_bots_collection().update_one(
        {"owner_id": owner_id},
        {"$set": {
            "active": bool(active),
            "status": "active" if active else "paused",
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def set_runtime_status(owner_id: int, status: str, error=None):
    await seller_bots_collection().update_one(
        {"owner_id": owner_id},
        {"$set": {
            "runtime_status": status,
            "runtime_error": error,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def delete_bot(owner_id: int):
    return await seller_bots_collection().delete_one({"owner_id": owner_id})


async def bot_exists(owner_id: int):
    return await seller_bots_collection().count_documents({"owner_id": owner_id}) > 0


async def total_bots():
    return await seller_bots_collection().count_documents({})
